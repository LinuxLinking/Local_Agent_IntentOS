"""确定性沙盒（RFC 15 §6 · Q24）——「HTTP 失败 → 自动走 SSH → 成功」可复现。

Step 5.3 的验收底座：**不依赖真实网络、不依赖真实模型**，
同一场景跑两次 → 同一串结果（RFC 15 §6：「需要确定性沙盒才能稳定测」）。

──────── 沙盒里有什么 ──────────────────────────────────────────────────────

| 工具 | 能力 | 副作用 | 行为 | 用途 |
|---|---|---|---|---|
| `tool:git:clone_http` | `git_clone` | READ | **必失败**（HTTP 429 限流） | 被排除的失败边 |
| `tool:git:clone_ssh` | `git_clone` | READ | **必成功**（SSH） | 主线替代路径（RFC 的例子） |
| `tool:git:clone_admin` | `git_clone` | EXTERNAL | 必成功 | **副作用更重**的备选（Q22：必须重新过审批） |
| `tool:git:push_http` | `git_push` | WRITE | **必失败**（HTTP 限流） | 「替换剩余节点」的第二段 |
| `tool:git:push_ssh` | `git_push` | WRITE | 必成功 | 换路后的第二段 |

同 `fallback_group` = 可互相替代（RFC 15 §3.2 的 `fallback_group`）。

──────── 为什么 HTTP 那条会「失败」而环境探针说它「可用」───────────────

环境快照里 `network.http_reachable=True` —— **探针查得到「网络通不通」，
查不到「对方限不限流」**。这正是重路由存在的理由：
静态环境判断不够，**运行时失败要能换路**（而不是断崖式失败）。

──────── 零模型调用 · 零网络 ────────────────────────────────────────────────

本模块只 import 既有类型 + 标准库；不 import 任何模型客户端、不碰网络。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..compiler.intent import SideEffectKind
from ..compiler.plan import PlanNode
from ..executor.inline import InlineExecutor
from ..probe.environment import EnvironmentSnapshot, ProbeResult
from ..router.metadata import ToolMetadata, build_registry
from ..types import NodeResult

#: 工具名（沙盒里的假工具）
HTTP_CLONE = "tool:git:clone_http"
SSH_CLONE = "tool:git:clone_ssh"
ADMIN_CLONE = "tool:git:clone_admin"
HTTP_PUSH = "tool:git:push_http"
SSH_PUSH = "tool:git:push_ssh"

#: 硬上限用例用的三个「依次失败」的工具（同一 fallback_group）
TRY_A = "tool:probe:try_a"
TRY_B = "tool:probe:try_b"
TRY_C = "tool:probe:try_c"

#: 沙盒里的全部工具
SANDBOX_TOOLS: frozenset[str] = frozenset(
    {HTTP_CLONE, SSH_CLONE, ADMIN_CLONE, HTTP_PUSH, SSH_PUSH, TRY_A, TRY_B, TRY_C}
)

#: 默认失败的工具 + 失败原因（如实记，不静默）
DEFAULT_FAILURES: dict[str, str] = {
    HTTP_CLONE: "HTTP 429 rate limited（沙盒固定失败）",
    HTTP_PUSH: "HTTP 429 rate limited（沙盒固定失败）",
}

#: 沙盒环境快照的固定时间戳（确定性）
PROBED_AT = "2026-10-29T00:00:00+00:00"

def _tool(
    name: str,
    *,
    kind: SideEffectKind,
    requires: tuple[str, ...],
    offers: tuple[str, ...],
    cost_tokens: int,
    latency_ms: int,
    failure_probability: float,
    fallback_group: str,
) -> ToolMetadata:
    """建一条工具声明（接入时声明 —— RFC 15 §3.2）。"""
    return ToolMetadata(
        tool=name,
        kind=kind,
        requires=requires,
        offers=offers,
        cost_tokens=cost_tokens,
        latency_ms=latency_ms,
        failure_probability=failure_probability,
        fallback_group=fallback_group,
    )

def sandbox_registry(tools: Iterable[str] | None = None) -> dict[str, ToolMetadata]:
    """沙盒的能力图（工具名 → `ToolMetadata`）。

    `tools` 给子集 → 只返回那几条（测试用来造「没有 SSH 兜底」的场景）。
    不给 → 全套。

    **成本是刻意排的**：HTTP 便宜（100）、SSH 贵（800）→
    路由器**一开始**就会选 HTTP → 失败后才有得换（这才是重路由的用武之地）。
    """
    all_tools = {
        HTTP_CLONE: _tool(
            HTTP_CLONE,
            kind=SideEffectKind.READ,
            requires=("tools.git.installed", "network.http_reachable"),
            offers=("capability:git_clone",),
            cost_tokens=100,
            latency_ms=500,
            failure_probability=0.15,
            fallback_group="git_clone",
        ),
        SSH_CLONE: _tool(
            SSH_CLONE,
            kind=SideEffectKind.READ,
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
        ADMIN_CLONE: _tool(
            ADMIN_CLONE,
            kind=SideEffectKind.EXTERNAL,
            requires=("tools.git.installed", "credentials.admin_token_present"),
            offers=("capability:git_clone",),
            cost_tokens=1200,
            latency_ms=4000,
            failure_probability=0.05,
            fallback_group="git_clone",
        ),
        HTTP_PUSH: _tool(
            HTTP_PUSH,
            kind=SideEffectKind.WRITE,
            requires=("capability:git_clone",),
            offers=("capability:git_push",),
            cost_tokens=100,
            latency_ms=800,
            failure_probability=0.15,
            fallback_group="git_push",
        ),
        SSH_PUSH: _tool(
            SSH_PUSH,
            kind=SideEffectKind.WRITE,
            requires=("capability:git_clone",),
            offers=("capability:git_push",),
            cost_tokens=800,
            latency_ms=2500,
            failure_probability=0.05,
            fallback_group="git_push",
        ),
        TRY_A: _tool(
            TRY_A,
            kind=SideEffectKind.READ,
            requires=(),
            offers=("capability:probe",),
            cost_tokens=100,
            latency_ms=100,
            failure_probability=1.0,
            fallback_group="probe",
        ),
        TRY_B: _tool(
            TRY_B,
            kind=SideEffectKind.READ,
            requires=(),
            offers=("capability:probe",),
            cost_tokens=200,
            latency_ms=100,
            failure_probability=1.0,
            fallback_group="probe",
        ),
        TRY_C: _tool(
            TRY_C,
            kind=SideEffectKind.READ,
            requires=(),
            offers=("capability:probe",),
            cost_tokens=300,
            latency_ms=100,
            failure_probability=1.0,
            fallback_group="probe",
        ),
    }
    if tools is None:
        return build_registry(all_tools.values())
    wanted = set(tools)
    return build_registry(all_tools[name] for name in sorted(wanted) if name in all_tools)

def sandbox_env() -> EnvironmentSnapshot:
    """沙盒的环境快照（**全部为真**，含 `network.http_reachable`）。

    为什么 HTTP 明明「可达」却会失败：探针查得到「网络通不通」，
    查不到「对方限不限流」—— 静态环境判断不够，**运行时失败要能换路**。
    """
    values = {
        "tools.git.installed": True,
        "tools.ssh.installed": True,
        "credentials.ssh_key_present": True,
        "credentials.gh_token_present": True,
        "credentials.admin_token_present": True,
        "network.http_reachable": True,
    }
    return EnvironmentSnapshot(
        results=tuple(
            ProbeResult(
                key=key,
                value=value,
                detail="sandbox",
                probed_at=PROBED_AT,
            )
            for key, value in sorted(values.items())
        )
    )

class SandboxExecutor(InlineExecutor):
    """确定性假执行器（**继承既有 `InlineExecutor`** —— 复用快照与补偿链路）。

    - 固定失败的工具 → 返回 `ok=False` + 如实的错误说明
    - 其余沙盒工具 → 返回 `ok=True`
    - `calls` 记录执行过的工具（测试断言「走了哪条路」）

    **不碰网络、不调模型**（RFC 15 §6 的诚实前提）。
    """

    def __init__(
        self,
        sandbox_dir: Path,
        *,
        failing: Iterable[str] | None = None,
    ) -> None:
        super().__init__(sandbox_dir)
        self.failing: frozenset[str] = frozenset(
            DEFAULT_FAILURES if failing is None else failing
        )
        #: 执行过的工具名（顺序 = 执行顺序）
        self.calls: list[str] = []
        #: 被补偿过的节点名（证明补偿走的还是**同一个**执行器）
        self.compensations: list[str] = []

    def execute(self, node: PlanNode) -> NodeResult:
        """执行一个节点（沙盒内是**固定**结果）。"""
        self.calls.append(node.tool)
        if node.tool in self.failing:
            reason = DEFAULT_FAILURES.get(node.tool, "沙盒固定失败（模拟限流）")
            return NodeResult(ok=False, output="", error=reason)
        if node.tool in SANDBOX_TOOLS:
            return NodeResult(
                ok=True,
                output=f"{node.tool} 成功（沙盒固定成功）",
                error=None,
            )
        return super().execute(node)

    def compensate(self, node: PlanNode) -> NodeResult:
        """补偿（**继承既有 `InlineExecutor` 的实现** —— 不另起一套）。"""
        self.compensations.append(node.name)
        return super().compensate(node)

__all__ = [
    "ADMIN_CLONE",
    "DEFAULT_FAILURES",
    "HTTP_CLONE",
    "HTTP_PUSH",
    "PROBED_AT",
    "SANDBOX_TOOLS",
    "SSH_CLONE",
    "SSH_PUSH",
    "TRY_A",
    "TRY_B",
    "TRY_C",
    "SandboxExecutor",
    "sandbox_env",
    "sandbox_registry",
]
