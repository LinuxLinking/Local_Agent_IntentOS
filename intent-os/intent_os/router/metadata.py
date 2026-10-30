"""工具元数据（RFC 15 §3.2）—— 路由器的「声明」层。

Step 5.2 的地基：**工具的代价与前置条件是声明，不是模型猜的**。

──────── 为什么要「接入时声明」（RFC 15 §3.2 / RFC 14 D-d）───────────────

D-d 的教训：不能让**被约束方**定义约束。
所以每个工具在**接入时**声明它的前置条件 / 能力 / 成本 / 延迟 / 失败率；
路由器只读「声明」+ 5.1 的 `EnvironmentSnapshot`，
**不调模型、不探环境、不猜**（RFC 15 §3.3：路由是确定性代码）。

──────── `requires` 里两种条目（5.2 落地时定的口径）────────────────────────

| 前缀 | 含义 | 谁来判 |
|---|---|---|
| 无（环境 key，如 `tools.ssh.installed`） | **环境前置**：这条能力要求环境里有什么 | 5.1 的 `EnvironmentSnapshot`（三态：true / false / unknown） |
| `capability:`（如 `capability:git_clone`） | **能力前置**：这条能力要求先跑出某个能力 | 路由图本身（路径上是否已经提供） |

这正是能力图能算**多跳路径**的原因：
`tool:git:push_ssh` 依赖 `capability:git_clone`，
所以「push」的最短路径是 clone → push，而不是单点挑一个 push 工具。

环境前置的键名沿用 5.1 的探针 key（`tools.*.installed` / `credentials.*` /
`network.*` / `limits.*`），**不另起一套命名**（一个概念一处定义）。

──────── 三态口径（RFC 15 §5 · 5.1 已定）────────────────────────────────

`EnvironmentSnapshot.get(key)` 的取值按 5.1 的口径翻成三态：

- `True` / 非零数 / 非空串 → **true**（有）
- `False` / `0` / `""` → **false**（**已确认没有**）
- `None` / 查不到 → **unknown**（**探测不到**）

**unknown 不等于 false**（红线：不静默降级 / 不误杀可用路径）——
路由器遇到 unknown 前置时**不排除**该工具，只在理由里标注「前置未知」。

──────── D2 / 零模型边界 ────────────────────────────────────────────────

本包**不 import** 任何模型客户端（LLM 编译器 / HTTP 客户端 / 本地模型运行时），
也**不 import** 外置状态断言模块（StateStore 所在模块）：
路由只看**环境能力**（探针结果），不看**世界状态**（StateStore）。
这条边界由 `tests/test_router.py` 的**源码扫描断言**守住。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..compiler.intent import SideEffectKind

#: 能力前缀（RFC 15 §3.2 的 `offers` 例子：`"capability:git_clone"`）
CAPABILITY_PREFIX = "capability:"

def normalize_capability(capability: str) -> str:
    """能力名归一 —— 去掉 `capability:` 前缀（**一个概念一处定义**）。

    `capability:git_clone` 与 `git_clone` 是**同一个能力**：
    调用方写哪种都行，匹配不会因为前缀写法不同而落空。
    前缀常量只在 `metadata.py` 定义一次，路由器 import 这个函数（不自己再写一份）。
    """
    text = str(capability)
    if text.startswith(CAPABILITY_PREFIX):
        return text[len(CAPABILITY_PREFIX) :]
    return text

def normalize_capabilities(capabilities: Iterable[str]) -> tuple[str, ...]:
    """一批能力名归一 + 去重 + **字典序**（稳定 —— 理由里出现的顺序不飘）。"""
    return tuple(sorted({normalize_capability(c) for c in capabilities}))

@dataclass(frozen=True)
class ToolMetadata:
    """一个可执行工具/路径的元数据（RFC 15 §3.2，字段照抄不增不减）。

    字段：

    - `tool`：工具标识（如 `"tool:git:clone_ssh"`）
    - `kind`：副作用分级（复用既有的 `SideEffectKind`，不另起一套）
    - `requires`：前置条件（环境 key，或 `capability:` 前缀的能力前置）
    - `offers`：它能达成什么能力（如 `("capability:git_clone",)`）
    - `cost_tokens`：预估 token 成本
    - `latency_ms`：预估延迟
    - `failure_probability`：先验失败概率（`0.0 ~ 1.0`）
    - `fallback_group`：同组 = 可互相替代（HTTP 与 SSH 是同一组）

    **不可变**（frozen）：声明就是声明，运行期不许改；历史修正走别处（5.3+）。
    """

    tool: str
    kind: SideEffectKind
    requires: tuple[str, ...] = ()
    offers: tuple[str, ...] = ()
    cost_tokens: int = 0
    latency_ms: int = 0
    failure_probability: float = 0.0
    fallback_group: str = ""

    def __post_init__(self) -> None:
        # 允许调用方传 list —— 统一成 tuple（frozen 数据要可哈希/可比较）
        object.__setattr__(self, "requires", tuple(str(r) for r in self.requires))
        object.__setattr__(self, "offers", tuple(str(o) for o in self.offers))
        object.__setattr__(self, "kind", SideEffectKind(self.kind))
        if not 0.0 <= float(self.failure_probability) <= 1.0:
            raise ValueError(
                f"failure_probability 必须在 0~1 之间：{self.tool!r} "
                f"= {self.failure_probability!r}"
            )

    # ── requires 的两种含义 ────────────────────────────────────────────────

    @property
    def env_requirements(self) -> tuple[str, ...]:
        """环境前置（由 5.1 的 `EnvironmentSnapshot` 判三态）。"""
        return tuple(r for r in self.requires if not r.startswith(CAPABILITY_PREFIX))

    @property
    def capability_requirements(self) -> tuple[str, ...]:
        """能力前置（由路由图本身判：路径上是否已经提供该能力）。"""
        return tuple(r for r in self.requires if r.startswith(CAPABILITY_PREFIX))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "kind": self.kind.value,
            "requires": list(self.requires),
            "offers": list(self.offers),
            "cost_tokens": self.cost_tokens,
            "latency_ms": self.latency_ms,
            "failure_probability": self.failure_probability,
            "fallback_group": self.fallback_group,
        }

# ── 注册表（工具名 → ToolMetadata）───────────────────────────────────────────

def build_registry(tools: Iterable[ToolMetadata]) -> dict[str, ToolMetadata]:
    """由元数据列表建注册表（同名后写的覆盖先写的 —— 接入方说了算）。"""
    registry: dict[str, ToolMetadata] = {}
    for meta in tools:
        registry[meta.tool] = meta
    return registry

def register_tool(
    metadata: ToolMetadata,
    registry: Mapping[str, ToolMetadata] | None = None,
) -> dict[str, ToolMetadata]:
    """把一个工具登记进注册表，**返回新注册表**（不改原对象）。

    「不 import 外置状态断言模块」之外的另一条边界：
    注册表是**普通内存数据**，不是外置状态断言 —— 不走 StateStore。
    """
    merged = dict(registry if registry is not None else DEFAULT_TOOL_REGISTRY)
    merged[metadata.tool] = metadata
    return merged

def get_tool(
    tool: str,
    registry: Mapping[str, ToolMetadata] | None = None,
) -> ToolMetadata | None:
    """按工具名查元数据；查不到 → `None`（**不是**「装了个空工具」）。"""
    reg = registry if registry is not None else DEFAULT_TOOL_REGISTRY
    return reg.get(tool)

def capabilities_of(
    registry: Mapping[str, ToolMetadata] | None = None,
) -> tuple[str, ...]:
    """注册表里能提供的全部能力（去重 + 字典序，稳定）。"""
    reg = registry if registry is not None else DEFAULT_TOOL_REGISTRY
    caps: set[str] = set()
    for meta in reg.values():
        caps.update(meta.offers)
    return tuple(sorted(caps))

def _default_registry() -> dict[str, ToolMetadata]:
    """内置工具集 —— RFC 15 §3.2 的 GitHub/git 例子。

    - `gh` 没装 + HTTP 不通 + SSH key 在 → 路由器应直接选 SSH 路径（§3.3 的例子）
    - `push` 依赖 `capability:git_clone` → 演示能力图的**多跳**路径
    """
    return build_registry(
        [
            ToolMetadata(
                tool="tool:git:clone_ssh",
                kind=SideEffectKind.EXTERNAL,
                requires=(
                    "tools.git.installed",
                    "tools.ssh.installed",
                    "credentials.ssh_key_present",
                ),
                offers=("capability:git_clone",),
                cost_tokens=800,
                latency_ms=3000,
                failure_probability=0.05,
                fallback_group="git_clone",
            ),
            ToolMetadata(
                tool="tool:git:clone_http",
                kind=SideEffectKind.EXTERNAL,
                requires=("tools.git.installed", "network.http_reachable"),
                offers=("capability:git_clone",),
                cost_tokens=1200,
                latency_ms=5000,
                failure_probability=0.15,
                fallback_group="git_clone",
            ),
            ToolMetadata(
                tool="tool:git:push_ssh",
                kind=SideEffectKind.EXTERNAL,
                requires=(
                    "tools.git.installed",
                    "tools.ssh.installed",
                    "credentials.ssh_key_present",
                    "capability:git_clone",
                ),
                offers=("capability:git_push",),
                cost_tokens=900,
                latency_ms=2500,
                failure_probability=0.08,
                fallback_group="git_push",
            ),
            ToolMetadata(
                tool="tool:git:push_http",
                kind=SideEffectKind.EXTERNAL,
                requires=(
                    "tools.git.installed",
                    "network.http_reachable",
                    "credentials.gh_token_present",
                    "capability:git_clone",
                ),
                offers=("capability:git_push",),
                cost_tokens=1300,
                latency_ms=4000,
                failure_probability=0.18,
                fallback_group="git_push",
            ),
            ToolMetadata(
                tool="tool:gh:pr_create",
                kind=SideEffectKind.EXTERNAL,
                requires=(
                    "tools.gh.installed",
                    "credentials.gh_token_present",
                    "network.http_reachable",
                ),
                offers=("capability:pr_create",),
                cost_tokens=1500,
                latency_ms=4000,
                failure_probability=0.10,
                fallback_group="pr_create",
            ),
        ]
    )

#: 默认注册表（工具名 → ToolMetadata）—— 接入方可以在此基础上 register。
DEFAULT_TOOL_REGISTRY: dict[str, ToolMetadata] = _default_registry()

__all__ = [
    "CAPABILITY_PREFIX",
    "DEFAULT_TOOL_REGISTRY",
    "ToolMetadata",
    "build_registry",
    "capabilities_of",
    "get_tool",
    "normalize_capabilities",
    "normalize_capability",
    "register_tool",
]
