"""动态路由（RFC 15 §3.3）—— 能力图 + 加权最短路径（Dijkstra）。

Step 5.2 的核心：**「走哪条路」由系统算，不由模型试错**（RFC 15 §3.3 硬约束）。

    意图 → 需要的 capability
         → 用 5.1 的 EnvironmentSnapshot 过滤掉 requires 不满足的工具
         → 在能力图上求加权最短路径（Dijkstra）
         → 产出既有 Plan / PlanNode（不另起执行链路）

──────── 三条不破的红线 ──────────────────────────────────────────────────

1. **零模型调用**：本包只 import 标准库 + 既有 `EnvironmentSnapshot` / `Plan` /
   `Intent`；不 import 任何模型客户端（`tests/test_router.py` 的源码扫描断言守着）。
2. **D2 边界**：不 import 外置状态断言模块（StateStore 所在模块）。
   路由只看**环境能力**（探针结果），不看**世界状态**（StateStore）。
3. **不静默兜底**：算不出路 → `route()` 返回 `None`（**不是**随便挑一个），
   理由用 `Router.explain()` 查；理由写进 `RouteDecision.reason`（进审计）。

──────── 搜索状态 = 能力节点（不是「路径 × 能力」）──────────────────────

上一版把状态键写成 `(路径, 能力)` —— **那是指数爆炸的来源**：
路径进了状态，状态数随路径长度指数增长（图稍大就跑不动）。

现在的状态键是**能力节点**本身：

| 变量          | 含义 |
|---------------|------|
| `dist[cap]`   | 到达能力 `cap` 的**累计代价**（= 路径上各工具权重之和） |
| `path_of[cap]` | 到达 `cap` 的**工具链**（**只用于重建结果**，不进状态键） |
| `settled`     | 已定案的能力（每个能力只结算一次） |

状态数 = 能力数（与路径长度无关）→ 搜索是**多项式**的。
`path_of` 只在三处用：(a) 累计代价 · (b) 重建工具链 · (c) 同代价时按字典序裁决。

──────── 前置条件的三态处理（RFC 15 §5 · 本次落地的关键口径）──────────────

| 前置状态 | 路由器的动作 | 为什么 |
|---|---|---|
| `true` | 正常候选 | 环境确认支持 |
| `false` | **排除**，理由写清是哪条前置 | 环境确认不支持 |
| `unknown` | **不排除**（当「可能可用」），理由标注「前置未知」，权重加不确定性惩罚 | 探测不到 ≠ 没有；误杀可用路径比多试一次更贵 |

**unknown 的权重口径（本次拍板，RFC 待回填）**：
每个未知前置 +`UNKNOWN_PREREQUISITE_PENALTY_TOKENS`（500 token）。
理由：声明里的 cost/latency/failure 先验默认「前置成立」，
无法确认时把这笔不确定性折成代价计入 —— **不排除、只提价**，
既不误杀（不静默降级），也不会无脑选一条没验证过的路。

──────── 提价 ≠ 排除（RFC 17 · 清欠账 #19）──────────────────────────────

路由里有两种「让边变得不划算」的手段，**不是一回事**：

| 手段 | 边在图里吗 | 权重 |
|---|---|---|
| **禁用**（`banned=`） | **不在**（不参与选路） | —— |
| **提价**（`penalized=`） | **在**（参与选路） | 基础权重 **+`PENALIZED_EDGE_PENALTY_TOKENS`** |

**什么时候用提价**：重路由里硬排除算不出路时，把刚失败的那条边**放回图里但加价**
再算一次 —— 工具可能只是「暂时不通」，**提价比排除更合适**（RFC 17 §2）。

**口径**：`penalized` 优先于 `banned`（同一条工具在两个集合里 → 它在图里，只是很贵）；
前置**明确 false** 的工具两支都排除（环境确认的「没有」是事实，不是「暂时不通」）。

──────── 多前置工具（capability 前置）—— 诚实的近似 ────────────────────

工具的 `requires` 里如果有多个 `capability:` 前置，**全部满足**才能走。
这在一般情况下是 **AND/OR 图的最优解问题（NP 难）**：要同时决定
「每个前置能力走哪条路」，且它们组合起来代价最小。

**本轮取可解释的近似**（**不假装算的是全局最优**）：

> 每个前置能力**各自取「到达它的最低代价路径」（先到先得）**，
> 多条前置路径按结算顺序（代价从低到高）合并成一条执行链，
> 累计代价 = 各前置分支代价之和 + 本工具权重。

- **单前置**（绝大多数情况）→ **精确**：等价于普通加权最短路。
- **多前置** → **近似**：等价于「加性代价的 AND/OR 推导」；
  前置分支**共享工具时会重复计费**（保守估计），
  也可能错过「换一个更贵的单工具同时满足两个前置」这类更优组合。
- 路径上每个工具的 `cap_req=[...]` 写进 `reason`（审计里能看出这条路径
  是在哪个近似下选出来的；多前置额外标 `(multi:min-cost-path-each)`）。

**要更精确就得付 NP 难的代价** —— 那是 5.3 之后按需加的档次，不是 5.2 的地基。

──────── 确定性 ──────────────────────────────────────────────────────────

同一份环境快照 + 同一张图 → 同一条路径 + 同一份理由：

- 所有遍历按**字典序**（`sorted(...)`），不靠 set 的遍历顺序；
- 松弛时按 `(代价, 路径)` **字典序**比较（代价相同 → 路径字典序小的赢）；
- 堆里放 `(代价, 路径, 能力名)` —— 三元组全序可比，弹出顺序唯一确定。
"""
from __future__ import annotations

import heapq
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..compiler.intent import Intent
from ..compiler.plan import Plan, PlanNode, new_plan_id
from ..probe.environment import EnvironmentSnapshot
from .metadata import (
    DEFAULT_TOOL_REGISTRY,
    ToolMetadata,
    normalize_capabilities,
    normalize_capability,
)

# ── 权重口径（可解释 · 常量写死 · 审计里能复算）──────────────────────────

#: token 成本是**主轴**：系数 1.0，单位就是 token。
COST_WEIGHT: float = 1.0
#: 延迟折算：1 秒 ≈ 20 token（延迟不花 token，但占住一次调用的时间窗）。
LATENCY_TOKENS_PER_SECOND: float = 20.0
#: 失败率折算：失败概率 1.0 ≈ 1000 token（失败一次要重来：重路由 + 重审批 + 重留痕）。
FAILURE_TOKENS_PER_PROBABILITY: float = 1000.0
#: 前置未知的惩罚：每条未知前置 +500 token（**不排除**，只是提价 —— 见模块文档）。
UNKNOWN_PREREQUISITE_PENALTY_TOKENS: float = 500.0
#: 失败边的「大幅提价」惩罚（**不排除**，只加代价）—— RFC 17 §2.2。
#:
#: 取值 **10000 token** 的理由（可解释 · 可复算 · 审计里能重算）：
#:
#: - 权重主轴是 token（cost_tokens 为主 + 延迟/失败率折算）。沙盒里单条工具的
#:   实测权重是 **HTTP 260 / SSH 910 / ADMIN 1330 token**（见 `tool_weight`）。
#: - 10000 ≈ 最贵单工具（ADMIN 1330）的 **7.5 倍** —— 足以**压住任何一条现实替代
#:   路径**（单跳 ≤1330；多跳 6 跳以内 ≤8000），让提价支真的只在**没有替代**时
#:   才用这条边。
#: - **不能取无穷大**：提价支的意义就是「最后手段」；连最后手段都没有 = 退化成
#:   硬排除 = 这一支白做。所以是**有限但很大**的常量。
#: - **诚实的边界**：提价是**量级压制**，不是**数学保证** —— 路径长到 8 跳以上
#:   （8×1330 > 10000）时可能压不住。这是有意的取舍：宁可多试一次失败的工具，
#:   也不引入「无界代价」（白皮书批评的「无限重试直到超时」）。
#: - **为什么是加法不是乘法**：`weight × 100` 对便宜边不公平（便宜边 ×100 还是
#:   便宜，可能仍然赢过正常替代）；加固定常量才能保证量级压制。
PENALIZED_EDGE_PENALTY_TOKENS: float = 10_000.0

#: 搜索状态上限（能力数；防止病态图吃穿内存；超出 = 明确失败，不静默）。
MAX_EXPANSIONS: int = 4096

#: 搜索的虚拟源点（空能力）——「还没跑任何工具」的状态。
SOURCE: str = ""

def tool_weight(
    meta: ToolMetadata,
    *,
    unknown_prerequisites: int = 0,
    penalized: bool = False,
) -> float:
    """合成权重（RFC 15 §3.3：cost_tokens 为主，延迟/失败率为辅）。

        weight = cost_tokens
               + LATENCY_TOKENS_PER_SECOND    × (latency_ms / 1000)
               + FAILURE_TOKENS_PER_PROBABILITY × failure_probability
               + UNKNOWN_PREREQUISITE_PENALTY_TOKENS × (前置未知的条数)
               + PENALIZED_EDGE_PENALTY_TOKENS  × (是否提价边)

    为什么这么折算（可解释、可复算、可在审计里重算）：

    - **token 是主轴**：成本系数 1.0，单位就是 token；后三项都是「把非 token
      维度折成 token」的修正项，所以真正的 token 差异依然决定路径。
    - **延迟**：1 秒 ≈ 20 token。延迟本身不花 token，但会占住一次调用的时间窗，
      在批量任务里是真实成本。
    - **失败率**：失败一次不只是白跑一遍，还要重来（重路由 + 重新审批 + 重新留痕），
      所以按「一次失败的期望代价」折算：失败概率 1.0 ≈ 1000 token。
    - **未知前置**：见模块文档 —— 前置无法确认时，把不确定性折成代价计入，
      **但不排除**该工具（红线：探测不到 ≠ 没有）。
    - **提价边**（RFC 17 §2.2）：刚失败、但可能只是「暂时不通」的边 —— 加一个
      很大的常量，**不排除**；让「有替代就走替代、没替代才回头试它」。
    """
    return (
        COST_WEIGHT * float(meta.cost_tokens)
        + LATENCY_TOKENS_PER_SECOND * (float(meta.latency_ms) / 1000.0)
        + FAILURE_TOKENS_PER_PROBABILITY * float(meta.failure_probability)
        + UNKNOWN_PREREQUISITE_PENALTY_TOKENS * float(unknown_prerequisites)
        + (PENALIZED_EDGE_PENALTY_TOKENS if penalized else 0.0)
    )

def precondition_state(value: Any) -> str:
    """把 5.1 的取值翻成三态：``"true"`` / ``"false"`` / ``"unknown"``。

    口径与 5.1 一致（RFC 15 §5 + 5.1 落地表）：

    - `None`（或查不到这个 key）→ **unknown**（探测不到）—— **不是 false**
    - `False` / `0` / `""` → **false**（**已确认没有**）
    - 其余（`True` / 非零数 / 非空串）→ **true**（有）

    注意：这里**不用** `EnvironmentSnapshot.tri_state()` —— 它把非 bool 值
    （例如 `limits.api_rate_remaining` 这种整数）一律翻成 unknown，
    路由器就永远读不到限额。口径在这里自己实现一份，但**取值只来自快照**。
    """
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return "true" if value > 0 else "false"
    if isinstance(value, str):
        return "true" if value != "" else "false"
    return "unknown"

def _num(value: float) -> str:
    """数字的稳定字符串（去掉多余的零）：910.0 → "910"，123.456 → "123.456"。"""
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text if text else "0"

def _slug(text: str) -> str:
    """工具名 → 节点名片段（稳定 · 只留 ASCII 字母数字下划线）。"""
    return re.sub(r"[^0-9A-Za-z_]+", "_", text)

def _merge_chain(paths: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
    """把多条前置路径合成**一条**执行链（去重 · 保序 · 确定）。

    「先到先得」：调用方按前置能力被**结算**的顺序（= 代价从低到高）传入，
    同名工具只保留**第一次**出现的位置 —— 后面分支不再重复执行它。

    这是多前置的**近似**之一（见模块文档）：
    代价按分支**各计一次**（共享工具时保守地重复计费），执行链按去重合成。
    """
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        for name in path:
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
    return tuple(out)

@dataclass(frozen=True)
class _Candidate:
    """评估后的候选工具（内部用）。"""

    name: str
    meta: ToolMetadata
    weight: float
    unknown_keys: tuple[str, ...]
    failed_keys: tuple[str, ...]
    cap_requirements: tuple[str, ...]  # 归一后的能力前置（字典序）
    banned: bool
    penalized: bool  # 提价边（**不排除**，只加代价 —— RFC 17 §2）

@dataclass(frozen=True)
class RouteDecision:
    """一次路由的结果（RFC 15 §5，字段照抄不增不减）。

    - `capability`：要的能力（**调用方原样回显**）
    - `chosen`：选中的工具序列（**有序** = 执行顺序；多跳路径长度 > 1）
    - `alternatives`：备选工具（与所选工具同 `fallback_group`、能提供同一目标能力、
      且当前可用）—— 5.3 重路由时的备胎
    - `reason`：为什么选它（**进审计**；一行 · 段间 ";" 分隔 · 字典序稳定）
    - `excluded`：因**前置条件不满足**被排除的工具（RFC §5 的定义；
      同时被 `banned` 的工具也会出现在 `reason` 的 `banned=` 段里，
      理由段里标 `+banned` —— 两个原因都看得见）
    """

    capability: str
    chosen: tuple[str, ...]
    alternatives: tuple[str, ...]
    reason: str
    excluded: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "chosen": list(self.chosen),
            "alternatives": list(self.alternatives),
            "reason": self.reason,
            "excluded": list(self.excluded),
        }

class Router:
    """确定性路由器（RFC 15 §3.3）：能力图 + 加权最短路径。

    **零模型调用** · **同环境同图 → 同一路径 + 同一份理由**。

    ```python
    router = Router()                                  # 用默认注册表
    decision = router.route("capability:git_clone", env)
    if decision is None:
        print(router.explain("capability:git_clone", env))   # 算不出路也要有话说
    ```

    注册表是**普通内存数据**（工具名 → 元数据），不是外置状态断言 —— 不走 StateStore。
    """

    def __init__(self, registry: Mapping[str, ToolMetadata] | None = None) -> None:
        self._registry: dict[str, ToolMetadata] = dict(
            registry if registry is not None else DEFAULT_TOOL_REGISTRY
        )

    @property
    def registry(self) -> dict[str, ToolMetadata]:
        """注册表副本（外部改不到内部状态）。"""
        return dict(self._registry)

    # ── 对外接口 ──────────────────────────────────────────────────────────

    def route(
        self,
        capability: str,
        env: EnvironmentSnapshot,
        *,
        banned: frozenset[str] = frozenset(),
        penalized: frozenset[str] = frozenset(),
    ) -> RouteDecision | None:
        """给一个 capability 选路。

        - `banned`：**禁用**的边（不参与选路；理由里单独点名）
        - `penalized`：**提价**的边（**参与**选路，权重 +`PENALIZED_EDGE_PENALTY_TOKENS`）
          —— RFC 17 §2：`penalized` 优先于 `banned`（同一工具在两个集合里 →
          它在图里，只是很贵）

        **找不到路 → 返回 `None`**（不静默兜底、不随便挑一个）。
        理由用 `explain()` 查（`RouteDecision` 里没有对象可查时尤其重要）。
        """
        decision, _reason = self._route(
            capability, env, banned=banned, penalized=penalized
        )
        return decision

    def explain(
        self,
        capability: str,
        env: EnvironmentSnapshot,
        *,
        banned: frozenset[str] = frozenset(),
        penalized: frozenset[str] = frozenset(),
    ) -> str:
        """**总是**给出理由（成功/失败都给）—— 失败时从这里查「为什么算不出来」。"""
        _decision, reason = self._route(
            capability, env, banned=banned, penalized=penalized
        )
        return reason

    def plan_for(
        self,
        capability: str,
        env: EnvironmentSnapshot,
        intent: Intent,
        *,
        banned: frozenset[str] = frozenset(),
        penalized: frozenset[str] = frozenset(),
        tool_args: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> Plan:
        """选路并落成既有 `Plan`（算不出路 → 抛错，把理由带上去）。"""
        decision, reason = self._route(
            capability, env, banned=banned, penalized=penalized
        )
        if decision is None:
            raise ValueError(f"路由失败：{reason}")
        return route_to_plan(decision, intent, tool_args=tool_args)

    # ── 内部：工具能力 ────────────────────────────────────────────────────

    def _provided_by(self, path: tuple[str, ...]) -> frozenset[str]:
        """路径上已经提供的能力集合（由路径唯一决定）。"""
        caps: set[str] = set()
        for name in path:
            meta = self._registry.get(name)
            if meta is None:
                continue
            caps.update(normalize_capabilities(meta.offers))
        return frozenset(caps)

    # ── 内部：选路 ────────────────────────────────────────────────────────

    def _route(
        self,
        capability: str,
        env: EnvironmentSnapshot,
        *,
        banned: frozenset[str],
        penalized: frozenset[str] = frozenset(),
    ) -> tuple[RouteDecision | None, str]:
        """选路 + 出理由（成功/失败都出）。"""
        requested = str(capability)
        target = normalize_capability(requested)
        banned_set = frozenset(banned or frozenset())
        penalized_set = frozenset(penalized or frozenset())

        # ── 1. 评估每个工具的前置条件（三态；取值只来自 5.1 的快照）──────
        eligible: list[_Candidate] = []
        excluded: list[tuple[str, tuple[str, ...], bool, bool]] = []
        unknown_detail: dict[str, tuple[str, ...]] = {}

        for name in sorted(self._registry):
            meta = self._registry[name]
            failed: list[str] = []
            unknown: list[str] = []
            for key in meta.env_requirements:
                state = precondition_state(env.get(key))
                if state == "false":
                    failed.append(key)
                elif state == "unknown":
                    unknown.append(key)
            is_banned = name in banned_set
            is_penalized = name in penalized_set
            if failed:
                # 前置**明确**不满足 → 排除（理由写清是哪条前置）
                # （**提价救不了前置 false**：环境确认的「没有」是事实，不是「暂时不通」）
                excluded.append((name, tuple(failed), is_banned, is_penalized))
                continue
            if is_banned and not is_penalized:
                # 已失败的边（5.3 会用）→ 不参与选路，理由里单独记
                # （提价边例外：RFC 17 §2 —— 放回图里，但加价）
                continue
            if unknown:
                unknown_detail[name] = tuple(unknown)
            eligible.append(
                _Candidate(
                    name=name,
                    meta=meta,
                    weight=tool_weight(
                        meta,
                        unknown_prerequisites=len(unknown),
                        penalized=is_penalized,
                    ),
                    unknown_keys=tuple(unknown),
                    failed_keys=(),
                    cap_requirements=normalize_capabilities(meta.capability_requirements),
                    banned=False,
                    penalized=is_penalized,
                )
            )

        by_name = {c.name: c for c in eligible}

        # ── 2. Dijkstra（状态 = 能力节点；路径只用于重建）────────────────
        #    dist[cap]   = 到达 cap 的累计代价（路径上各工具权重之和）
        #    path_of[cap]= 到达 cap 的工具链（重建结果用，不进状态键）
        #    settled     = 已定案的能力（每个能力只结算一次 → 状态数 = 能力数）
        chosen: tuple[str, ...] = ()
        path_weight = 0.0
        no_path_why = ""
        search_states = 0

        if not target:
            no_path_why = "capability 为空"
        else:
            dist: dict[str, float] = {}
            path_of: dict[str, tuple[str, ...]] = {}
            settled: set[str] = set()
            settle_order: dict[str, int] = {}
            relaxed: set[str] = set()
            # 堆元素三元组全序可比：(代价, 路径, 能力名) —— 弹出顺序唯一确定
            heap: list[tuple[float, tuple[str, ...], str]] = [(0.0, (), SOURCE)]

            while heap:
                cost, path, node = heapq.heappop(heap)
                if node in settled:
                    continue  # 已定案
                if cost > dist.get(node, float("inf")):
                    continue  # 过期条目
                search_states += 1
                if search_states > MAX_EXPANSIONS:
                    no_path_why = f"搜索状态超限（>{MAX_EXPANSIONS}）"
                    break
                settled.add(node)
                settle_order[node] = len(settle_order)
                dist[node] = cost
                path_of[node] = path
                if node == target:
                    chosen = path
                    path_weight = cost
                    break

                # 松弛：能力前置**全部就位**的工具（先到先得 —— 便宜的前置先结算）
                for cand in eligible:
                    if cand.name in relaxed:
                        continue
                    need = cand.cap_requirements
                    if need and not all(n in settled for n in need):
                        continue  # 前置能力还没到达 → 这条边现在走不了
                    relaxed.add(cand.name)
                    # 多前置：各自取「到达它的最低代价路径」（近似 · 见模块文档）
                    base_cost = sum(dist[n] for n in need)
                    base_paths = [
                        path_of[n] for n in sorted(need, key=lambda n: (settle_order[n], n))
                    ]
                    new_cost = cand.weight + base_cost
                    new_path = _merge_chain(base_paths) + (cand.name,)
                    for off in normalize_capabilities(cand.meta.offers):
                        if off in settled:
                            continue  # 已定案（代价不可能更好）
                        prev_cost = dist.get(off)
                        prev_path = path_of.get(off)
                        # 同代价时按路径字典序裁决（确定性，不靠遍历顺序）
                        if prev_cost is None or (new_cost, new_path) < (prev_cost, prev_path):
                            dist[off] = new_cost
                            path_of[off] = new_path
                            heapq.heappush(heap, (new_cost, new_path, off))

            if not chosen and not no_path_why:
                no_path_why = "没有工具链能提供该能力"

        # ── 3. 备选（同 fallback_group · 提供同一目标能力 · 当前可用）──────
        alternatives: tuple[str, ...] = ()
        alt_weights: list[tuple[str, float]] = []
        if chosen:
            last = self._registry[chosen[-1]]
            group = last.fallback_group
            if group:
                provided = self._provided_by(chosen)
                alts = [
                    c
                    for c in eligible
                    if c.name not in chosen
                    and not c.penalized  # 提价边不是「正常备选」（RFC 17 §2）
                    and c.meta.fallback_group == group
                    and target in normalize_capabilities(c.meta.offers)
                    and (not c.cap_requirements or set(c.cap_requirements) <= provided)
                ]
                alts.sort(key=lambda c: (c.weight, c.name))
                alternatives = tuple(c.name for c in alts)
                alt_weights = [(c.name, c.weight) for c in alts]

        # ── 4. 理由（进审计；一行 · 段间 ";" 分隔 · 字典序稳定）────────────
        parts: list[str] = [f"capability={requested}"]
        parts.append("chosen=" + (",".join(chosen) if chosen else "-"))
        parts.append("path_weight=" + (_num(path_weight) if chosen else "-"))
        for index, name in enumerate(chosen):
            cand = by_name.get(name)
            if cand is None:
                continue
            segment = (
                f"step[{index}]={name} cost={cand.meta.cost_tokens} "
                f"latency_ms={cand.meta.latency_ms} "
                f"failure={cand.meta.failure_probability} "
                f"unknown_prereq={len(cand.unknown_keys)} "
                f"weight={_num(cand.weight)}"
            )
            if cand.penalized:
                # 提价边：审计里看得见「这条边被加了多少钱」（RFC 17 §2.2）
                segment += f" penalty={_num(PENALIZED_EDGE_PENALTY_TOKENS)}"
            if cand.cap_requirements:
                segment += f" cap_req=[{','.join(cand.cap_requirements)}]"
                if len(cand.cap_requirements) > 1:
                    # 多前置 = 近似：每个前置各取最低代价路径（先到先得）
                    segment += "(multi:min-cost-path-each)"
            parts.append(segment)
        parts.append(
            "alternatives="
            + (",".join(f"{n}({_num(w)})" for n, w in alt_weights) if alt_weights else "-")
        )
        excluded_entries = []
        for name, keys, was_banned, was_penalized in sorted(
            excluded, key=lambda item: item[0]
        ):
            entry = f"{name}←requires[{','.join(keys)}]=false"
            if was_banned:
                entry += "+banned"
            if was_penalized:
                entry += "+penalized"
            excluded_entries.append(entry)
        parts.append("excluded=" + (",".join(excluded_entries) if excluded_entries else "-"))
        parts.append("banned=" + (",".join(sorted(banned_set)) if banned_set else "-"))
        if penalized_set:
            # 提价边段（**只在 penalized 非空时出现** —— 空集合时理由串一个字不变，
            # 既有审计可比对）
            parts.append(
                "penalized="
                + ",".join(
                    f"{n}(+{_num(PENALIZED_EDGE_PENALTY_TOKENS)})"
                    for n in sorted(penalized_set)
                )
            )
        unknown_entries = []
        for name in sorted(unknown_detail):
            cand = by_name.get(name)
            if cand is None:
                continue
            keys = cand.unknown_keys
            entry = f"{name}←requires[{','.join(keys)}]=unknown"
            details = []
            for key in keys:
                detail = env.get_result(key).detail
                if detail:
                    details.append(f"{key}({detail[:60]})")
            if details:
                entry += " " + " ".join(details)
            unknown_entries.append(entry)
        parts.append(
            "unknown_prereq=" + (",".join(unknown_entries) if unknown_entries else "-")
        )
        parts.append(f"search_states={search_states}")
        if no_path_why:
            parts.append("no_path=true")
            parts.append(f"no_path_why={no_path_why}")
            parts.append(
                "eligible="
                + (
                    ",".join(
                        f"{c.name}({_num(c.weight)})"
                        for c in sorted(eligible, key=lambda c: c.name)
                    )
                    or "-"
                )
            )
        reason = "; ".join(parts)

        if not chosen:
            return None, reason

        decision = RouteDecision(
            capability=requested,
            chosen=chosen,
            alternatives=alternatives,
            reason=reason,
            excluded=tuple(
                name for name, _keys, _b, _p in sorted(excluded, key=lambda i: i[0])
            ),
        )
        return decision, reason

def route_to_plan(
    decision: RouteDecision,
    intent: Intent,
    *,
    tool_args: Mapping[str, Mapping[str, Any]] | None = None,
    plan_id: str | None = None,
) -> Plan:
    """把路由结果落成既有 `Plan` / `PlanNode`（**不另起执行链路**）。

    - **节点名稳定**：`route_<序号>_<工具名 slug>` —— 同一份决策 → 同一批名字
      （审计里可比对；不掺随机数）。
    - **依赖**：线性链（第 i 步依赖第 i-1 步）→ `topological_order()` 能算。
    - **工具名**：直接用元数据的 `tool` 字段（接入方按自己的命名注册）。
    - **参数**：`tool_args` 给参数；不给就是空参数（路由器不猜参数 ——
      参数是调用方的事，路由器只管「走哪条路」）。

    算不出路的决策（`chosen` 为空）→ 抛 `ValueError`（不静默兜底）。
    """
    if not decision.chosen:
        raise ValueError("路由结果没有选中任何工具，落不成 Plan（不静默兜底）")

    nodes: list[PlanNode] = []
    previous = ""
    for index, tool in enumerate(decision.chosen):
        name = f"route_{index}_{_slug(tool)}"
        args = dict((tool_args or {}).get(tool, {}))
        nodes.append(
            PlanNode(
                name=name,
                tool=tool,
                args=args,
                description=f"路由 {decision.capability} ← {tool}",
                depends_on=(previous,) if previous else (),
            )
        )
        previous = name

    return Plan(id=plan_id or new_plan_id(), intent=intent, nodes=tuple(nodes))

__all__ = [
    "COST_WEIGHT",
    "FAILURE_TOKENS_PER_PROBABILITY",
    "LATENCY_TOKENS_PER_SECOND",
    "MAX_EXPANSIONS",
    "PENALIZED_EDGE_PENALTY_TOKENS",
    "RouteDecision",
    "Router",
    "SOURCE",
    "UNKNOWN_PREREQUISITE_PENALTY_TOKENS",
    "precondition_state",
    "route_to_plan",
    "tool_weight",
]
