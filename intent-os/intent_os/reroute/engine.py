"""弹性重路由（RFC 15 §3.4）—— 失败之后**换路前进**，不是补偿回原点。

Step 5.3 的核心：IntentOS 的护城河不是「不拒绝」，而是**拒绝之后的容错与重路由**。

──────── 机制（RFC 15 §3.4 的流程）────────────────────────────────────────

    节点执行 → 失败 / 超时 / 被拒
        → 把失败的工具记进 `banned`（**本次任务内**生效，Q21）
        → 用 5.2 的路由器**重算**同一条 capability 需求上的路径（排除失败边）
        → 用备选**替换剩余节点**（已完成的节点不重做）
        → 继续执行（还是走既有的 ToolExecutor / ApprovalGate / AuditTrail）

    没有备选 / 硬上限用尽 → 走**既有补偿路径**，如实报告（不假装成功）

──────── 两支：先硬排除，无路才提价（RFC 17 · 清欠账 #19）─────────────────

RFC 15 §3.4 的流程图里有**两支**，本轮把第二支也做了：

    ① 硬排除：把失败边从图里拿掉 → 重算 → 有替代就换路（**既有行为**）
    ② **提价**：① 算不出路时，把刚失败的那条边**放回图里、但加一个大价钱**
                → 再算一次 —— 工具可能只是「暂时不通」，**提价比排除更合适**

两支是**同一次 `attempt()`** 里的两次路由，**计一次**硬上限（2 次/任务照旧生效，
提价支不是「绕过上限的免费重试」）。提价支产出的还是**既有 `PlanNode`**。

**提价宽限只给「这次才第一次失败」的边**：本次尝试**之前**就已经在禁用集合里的边
（本任务里已经失败过 / 经验错题本点名的）**不复活** —— 已知坏边不给宽限
（RFC 17 §2.4 口径 9）。所以一条边在本任务内**最多被提价重试一次**。

──────── 三条不破的红线 ────────────────────────────────────────────────────

1. **零模型调用**：本包只 import 标准库 + 既有 `Router` / `EnvironmentSnapshot` /
   `PlanNode`；不 import 任何模型客户端（`tests/test_reroute.py` 的源码扫描断言守着）。
2. **不另起执行链路**：重路由只产出**既有 `PlanNode`**，由既有的 `ToolExecutor`
   执行、既有 `AuditTrail` 留痕、既有 `ApprovalGate` 审批、既有 `SnapshotStore`
   补偿 —— 一个都不新建（RFC 15 §3.4 的硬要求）。
3. **不静默降级**：每一次换路都留痕（`REROUTE_TRIGGERED` / `REROUTE_SELECTED` /
   `REROUTE_EXHAUSTED`）；换不了路就**如实说换不了**，不静默重试、不假装成功。

──────── 为什么单独一个包（而不是挂在 `router/` 下）────────────────────────

- `router/` 是**纯选路器**：能力 → 路径，**不带任务状态**（同一份输入永远同一份输出）；
- 重路由带**任务级状态**（`banned` 集合、已用次数），是另一件事；
- 依赖方向单向：`reroute` → `router`（重路由**用**路由器重算路径），不反向 ——
  `router/` 因此不需要知道「重路由」的存在，5.2 的零模型 / D2 边界原样保持。

──────── 硬上限（RFC 15 §9 Q20）──────────────────────────────────────────

默认 **2 次/任务**（`DEFAULT_MAX_REROUTES`，可配）——「否则就是白皮书批评的
『无限重试直到超时』」（RFC §3.4）。超了**不硬撞**：走既有补偿路径并如实报告
（挂起是 5.4 的事，本轮不做）。

──────── 「替换剩余节点」怎么换（口径）────────────────────────────────────

失败节点 + 它后面的节点 = 一串「**能力槽位**」（槽位的 capability 由
`ToolMetadata.offers` 声明 —— 接入时声明，RFC 15 §3.2）。逐槽位用路由器重算：

1. 失败槽位：按它的 capability 重算（失败的那条边已进 `banned`）；
2. 后续槽位：按各自的 capability 重算（路由器会自己带上能力前置）；
3. 各槽位的路径**按序合并去重** → 一条新的执行链；
4. 已经跑完的工具**从新链里剔掉**（「已完成的节点不重做」）。

取不到 capability 的节点（工具不在能力图里）→ **不换路**（如实说明），
不猜、不硬塞。

──────── 确定性 ──────────────────────────────────────────────────────────

路由器本身是确定性的（5.2），本包不引入任何随机性；遍历按注册表顺序。
同一场景两次执行 → 同一批替代节点、同一串审计事件、同一最终状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from ..compiler.intent import SideEffectKind
from ..compiler.plan import PlanNode
from ..probe.environment import EnvironmentSnapshot
from ..router.metadata import ToolMetadata, normalize_capabilities
from ..router.router import PENALIZED_EDGE_PENALTY_TOKENS, Router

#: **硬上限：2 次/任务**（RFC 15 §9 Q20 · TODO 决策索引同款）。
DEFAULT_MAX_REROUTES: int = 2

#: **换路模式**（`RerouteOutcome.mode`）—— 审计里必须看得见「用的是哪支」。
#:
#: - `exclude`：**硬排除**支产出路径（既有行为；绝大多数场景走这一支）
#: - `penalty`：**提价**支产出路径（硬排除无路 → 把刚失败的边放回图里加价再算）
#: - `none`：**没有支**产出路径（撞上限 / 无能力元数据 / 两支都无路）
REROUTE_MODE_EXCLUDE: str = "exclude"
REROUTE_MODE_PENALTY: str = "penalty"
REROUTE_MODE_NONE: str = "none"

#: 副作用等级序（降级判定用）—— 备选**更重**才必须重新过审批（Q22）。
KIND_SEVERITY: dict[SideEffectKind, int] = {
    SideEffectKind.NONE: 0,
    SideEffectKind.READ: 1,
    SideEffectKind.WRITE: 2,
    SideEffectKind.EXTERNAL: 3,
}

def side_effect_severity(kind: SideEffectKind | None) -> int:
    """副作用等级 → 可比的大小（`None` = 未知 → -1）。

    「更重」= 等级序更大：NONE < READ < WRITE < EXTERNAL。
    """
    if kind is None:
        return -1
    return KIND_SEVERITY.get(kind, 0)

class RerouteStatus(str, Enum):
    """一次重路由尝试的结论（**穷举**，不静默兜底）。"""

    #: 选到了替代路径 → 替换剩余节点，继续执行
    SELECTED = "selected"
    #: 硬上限用尽（> max_reroutes 次/任务）—— 不硬撞
    EXHAUSTED_LIMIT = "exhausted_limit"
    #: 没有可用的替代路径（路由器算不出路）—— **可以挂起等人**
    EXHAUSTED_NO_PATH = "exhausted_no_path"
    #: 备选路径的审批没通过（降级不绕审批）—— 人**已经**拒过了
    EXHAUSTED_APPROVAL = "exhausted_approval"
    #: 节点没有能力元数据 —— **配置错误**，挂起也没用（人得先改配置）
    EXHAUSTED_NO_METADATA = "exhausted_no_metadata"

@dataclass(frozen=True)
class RerouteOutcome:
    """一次重路由尝试的结果（**全部如实**，没有静默兜底）。

    - `status`：结论（`RerouteStatus`）
    - `reason`：人可读的理由（进审计）
    - `nodes`：替代节点（`SELECTED` 时非空；**既有 `PlanNode`** —— 不新造节点类型）
    - `capability`：失败节点对应的能力需求（审计里看得见「重算的是哪条需求」）
    - `chosen`：替代路径的工具序列（审计里看得见「选了哪条替代」）
    - `banned`：本次生效的禁用边集合（失败边 + 之前失败过的边）
    - `attempt`：这是本任务的第几次重路由（1 起）
    - `failed_tool`：失败的工具名
    - `replaced_tools`：被替换掉的原节点工具（失败节点 + 其后续）
    - `route_reason`：路由器的理由原文（可复算，审计里能追）
    - `mode`：**用的是哪支**（`REROUTE_MODE_EXCLUDE` / `REROUTE_MODE_PENALTY` /
      `REROUTE_MODE_NONE`）—— RFC 17 §4.2。**不静默降级**：审计里看得见。
    - `penalized`：被**提价**放回图里的边（RFC 17 §2；只有提价支才非空）
    """

    status: RerouteStatus
    reason: str = ""
    nodes: tuple[PlanNode, ...] = ()
    capability: str = ""
    chosen: tuple[str, ...] = ()
    banned: tuple[str, ...] = ()
    attempt: int = 0
    failed_tool: str = ""
    replaced_tools: tuple[str, ...] = ()
    route_reason: str = ""
    mode: str = REROUTE_MODE_NONE
    penalized: tuple[str, ...] = ()

    @property
    def selected(self) -> bool:
        """是否选到了替代路径。"""
        return self.status is RerouteStatus.SELECTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "nodes": [n.to_dict() for n in self.nodes],
            "capability": self.capability,
            "chosen": list(self.chosen),
            "banned": list(self.banned),
            "attempt": self.attempt,
            "failed_tool": self.failed_tool,
            "replaced_tools": list(self.replaced_tools),
            "route_reason": self.route_reason,
            "mode": self.mode,
            "penalized": list(self.penalized),
        }

@dataclass(frozen=True)
class _SlotPlan:
    """逐槽位重算的结果（内部用）—— **要么**有路径，**要么**有失败原因。

    「算不出路」也是一种结论（不静默兜底）→ 用 `error_*` 字段如实记下来。
    """

    chains: tuple[tuple[str, ...], ...] = ()
    caps: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    error_status: RerouteStatus | None = None
    error_reason: str = ""
    error_route_reason: str = ""
    error_capability: str = ""

@dataclass
class RerouteEngine:
    """弹性重路由引擎（RFC 15 §3.4）—— **任务级**状态 + 确定性重算。

    ```python
    engine = RerouteEngine(Router(registry), env, max_reroutes=2)
    outcome = engine.attempt(failed_node=node, remaining=rest)
    if outcome.selected:
        pending = pending[:i] + list(outcome.nodes)      # 换路继续
    else:
        ...                                               # 走既有补偿路径
    ```

    **任务级状态**（每次 run 开始时由 runtime 调 `reset_task()` 清空）：

    - `_banned`：本次任务内**禁用**的工具（失败边）—— 立即生效（RFC Q21）
    - `_used`：已用掉的重路由次数（对照硬上限）

    **零模型调用** · **确定性**：同样的输入 → 同样的替代节点。
    """

    router: Router
    env: EnvironmentSnapshot
    max_reroutes: int = DEFAULT_MAX_REROUTES
    _banned: set[str] = field(default_factory=set, init=False, repr=False)
    _used: int = field(default_factory=int, init=False, repr=False)

    # ── 任务生命周期 ────────────────────────────────────────────────────────

    def restore_state(self, *, banned: Iterable[str] = (), used: int = 0) -> None:
        """恢复任务级状态（挂起 → 恢复时用）。

        - `banned`：本次任务内禁用的边 —— **恢复后仍然有效**（RFC 15 §9 Q21：
          本次任务内确定生效；跨任务才需要环境指纹）。
        - `used`：已用掉的重路由次数 —— **恢复时由调用方决定**（runtime 传 0：
          人明确点了「继续」，这是一次新的决策点；否则恢复后一次失败就又挂起，
          人根本没机会再决定）。
        """
        self._banned = set(banned)
        self._used = int(used)

    def reset_task(self) -> None:
        """清空任务级状态（runtime 在每次 run 开始时调用）。

        「失败边记多久」= **本次任务内**（RFC 15 §9 Q21）：
        本次内必须立刻生效；跨任务要谨慎（环境会变），那是 5.5 经验错题本的事。
        """
        self._banned.clear()
        self._used = 0

    @property
    def used(self) -> int:
        """本任务已用掉的重路由次数。"""
        return self._used

    @property
    def banned(self) -> frozenset[str]:
        """本任务内禁用的工具（失败边）。"""
        return frozenset(self._banned)

    # ── 元数据查询（runtime 也要用：审批判定 / 审计）───────────────────────

    def metadata_of(self, tool: str) -> ToolMetadata | None:
        """工具的声明（不在能力图里 → `None`，不是「装了个空工具」）。"""
        return self.router.registry.get(tool)

    def capability_of(self, tool: str) -> str:
        """工具提供的能力（归一后的**第一个**；没有 → 空串）。

        工具声明多个能力时取字典序第一个 —— 这是**近似**（接入方可用
        `attempt(capability=...)` 显式指定失败节点的能力需求）。
        """
        meta = self.metadata_of(tool)
        if meta is None:
            return ""
        caps = normalize_capabilities(meta.offers)
        return caps[0] if caps else ""

    def kind_of(self, tool: str) -> SideEffectKind | None:
        """工具声明的副作用等级（没有声明 → `None`）。"""
        meta = self.metadata_of(tool)
        return meta.kind if meta is not None else None

    # ── 主入口：换路 ────────────────────────────────────────────────────────

    def attempt(
        self,
        *,
        failed_node: PlanNode,
        remaining: Sequence[PlanNode] = (),
        error: str = "",
        capability: str = "",
        done_tools: Iterable[str] = (),
        tool_args: Mapping[str, Any] | None = None,
    ) -> RerouteOutcome:
        """失败之后试一次换路。

        - `failed_node`：刚失败的节点
        - `remaining`：它**之后**还没跑的节点（会被一起替换）
        - `capability`：失败节点的能力需求（不给就从元数据推导）
        - `done_tools`：已经跑完的工具（新链里要剔掉 —— 已完成的节点不重做）
        - `tool_args`：替代节点的参数（不给就沿用失败节点的参数）

        **两支（RFC 17 §2）**：先试**硬排除**（排除失败边）；算不出路时自动走
        **提价**支（把刚失败的那条边放回图里、但加一个大价钱再算一次 —— 工具
        可能只是「暂时不通」）。两支**计同一次**重路由（硬上限 2 次/任务照旧生效）。

        返回 `RerouteOutcome` —— **永远有结论**，不抛异常、不静默兜底。
        """
        failed_tool = failed_node.tool
        used = self._used

        # ── 0. 硬上限（Q20）：超了不硬撞 ──────────────────────────────────
        if used >= self.max_reroutes:
            return RerouteOutcome(
                status=RerouteStatus.EXHAUSTED_LIMIT,
                reason=(
                    f"重路由已达硬上限（{used}/{self.max_reroutes} 次/任务）"
                    "—— 不硬撞，走既有补偿路径"
                ),
                banned=tuple(sorted(self._banned)),
                attempt=used,
                failed_tool=failed_tool,
                mode=REROUTE_MODE_NONE,
            )

        # ── 1. 失败边进 banned（本次任务内立即生效）─────────────────────────
        #    `already_banned` = 本次尝试**之前**就禁用的边（之前失败过的 /
        #    经验错题本点名的）—— 提价支**不复活**它们（口径 9）。
        already_banned = frozenset(self._banned)
        banned = frozenset(self._banned | {failed_tool})
        slots: list[PlanNode] = [failed_node, *remaining]

        # ── 2. 第一支：硬排除（排除失败边后逐槽位重算）──────────────────────
        first = self._route_slots(slots, capability, banned=banned, penalized=frozenset())

        mode = REROUTE_MODE_EXCLUDE
        penalized_edges: frozenset[str] = frozenset()
        plan_result = first

        if first.error_status is RerouteStatus.EXHAUSTED_NO_PATH:
            if failed_tool in already_banned:
                # 已知坏边 → **不给提价宽限**（口径 9）：它已经失败过 /
                # 被经验错题本点名过，本任务内不再复活（如实报告）。
                return RerouteOutcome(
                    status=RerouteStatus.EXHAUSTED_NO_PATH,
                    reason=(
                        f"能力 {first.error_capability} 算不出替代路径"
                        "（排除失败边无路 → 该边已是已知坏边，提价支不复活它）"
                    ),
                    route_reason=first.error_route_reason,
                    banned=tuple(sorted(banned)),
                    attempt=used + 1,
                    failed_tool=failed_tool,
                    mode=REROUTE_MODE_NONE,
                )
            # ── 3. 第二支：提价（把刚失败的那条边放回图里，但给它一个很大的价）──
            #    RFC 17 §2：没有备选、但工具可能只是「暂时不通」时，
            #    **提价比排除更合适**（`penalized` 优先于 `banned`：它仍在图里，只是很贵）。
            penalized_edges = frozenset({failed_tool})
            second = self._route_slots(
                slots, capability, banned=banned, penalized=penalized_edges
            )
            if second.error_status is not None:
                # 两支都无路 → 如实报告（**status 不变** → 挂起策略一个字不用改）
                return RerouteOutcome(
                    status=RerouteStatus.EXHAUSTED_NO_PATH,
                    reason=(
                        f"能力 {second.error_capability or first.error_capability} "
                        "算不出替代路径（排除失败边无路 → 提价分支也无路）"
                    ),
                    route_reason=" || ".join(
                        part
                        for part in (first.error_route_reason, second.error_route_reason)
                        if part
                    ),
                    banned=tuple(sorted(banned)),
                    attempt=used + 1,
                    failed_tool=failed_tool,
                    penalized=tuple(sorted(penalized_edges)),
                    mode=REROUTE_MODE_NONE,
                )
            plan_result = second
            mode = REROUTE_MODE_PENALTY
        elif first.error_status is not None:
            # 没有能力元数据 → 提价也救不了（配置错误）→ 如实报告
            return RerouteOutcome(
                status=first.error_status,
                reason=first.error_reason,
                banned=tuple(sorted(banned)),
                attempt=used + 1,
                failed_tool=failed_tool,
                mode=REROUTE_MODE_NONE,
            )

        chains = plan_result.chains
        slot_caps = plan_result.caps
        reasons = plan_result.reasons

        # ── 4. 合并各槽位的路径（去重保序）─────────────────────────────────
        merged: list[str] = []
        for chain in chains:
            for tool in chain:
                if tool not in merged:
                    merged.append(tool)

        # ── 5. 已完成的节点不重做（把跑过的工具剔掉）──────────────────────
        done = set(done_tools)
        merged = [tool for tool in merged if tool not in done]
        if not merged:
            return RerouteOutcome(
                status=RerouteStatus.EXHAUSTED_NO_PATH,
                reason="替代路径为空（剩下的都跑过了）—— 无法换路",
                banned=tuple(sorted(banned)),
                attempt=used + 1,
                failed_tool=failed_tool,
                penalized=tuple(sorted(penalized_edges)),
                mode=REROUTE_MODE_NONE,
            )

        # ── 6. 落成既有 PlanNode（不新造节点类型）─────────────────────────
        attempt_no = used + 1
        nodes = _build_nodes(
            merged,
            attempt=attempt_no,
            args=dict(tool_args) if tool_args is not None else dict(failed_node.args),
        )

        # ── 7. 记账（任务级状态）───────────────────────────────────────────
        #    提价 **≠ 解除禁用**（RFC 17 §2 口径 3）：刚失败的那条边**仍然是**
        #    本任务的失败边（Q21：本次任务内确定生效）—— 提价只是**这一次重算**
        #    里的放行。
        self._banned = set(banned)
        self._used = attempt_no

        if mode == REROUTE_MODE_PENALTY:
            reason = (
                f"节点 {failed_node.name}（{failed_tool}）失败 → 排除失败边后**无路** → "
                f"把该边大幅提价（+{PENALIZED_EDGE_PENALTY_TOKENS:g} token）再算 → "
                f"能力 {slot_caps[0]} → 改走 {' → '.join(merged)}"
            )
        else:
            reason = (
                f"节点 {failed_node.name}（{failed_tool}）失败 → 排除失败边后"
                f"重算能力 {slot_caps[0]} → 改走 {' → '.join(merged)}"
            )

        return RerouteOutcome(
            status=RerouteStatus.SELECTED,
            reason=reason,
            nodes=nodes,
            capability=slot_caps[0],
            chosen=tuple(merged),
            banned=tuple(sorted(banned)),
            attempt=attempt_no,
            failed_tool=failed_tool,
            replaced_tools=tuple(node.tool for node in slots),
            route_reason=" || ".join(reasons),
            mode=mode,
            penalized=tuple(sorted(penalized_edges)),
        )

    # ── 内部：逐槽位重算（两支共用）───────────────────────────────────────

    def _route_slots(
        self,
        slots: Sequence[PlanNode],
        capability: str,
        *,
        banned: frozenset[str],
        penalized: frozenset[str],
    ) -> _SlotPlan:
        """逐槽位重算（口径见模块文档「替换剩余节点」怎么换）。

        - `banned`：**硬排除**的边（不在图里）
        - `penalized`：**提价**的边（**在**图里，权重 +`PENALIZED_EDGE_PENALTY_TOKENS`；
          优先于 `banned` —— RFC 17 §2）

        返回 `_SlotPlan`：**要么**给出每槽位的路径，**要么**给出失败原因
        （算不出路就说算不出路 —— 不静默兜底）。
        """
        chains: list[tuple[str, ...]] = []
        slot_caps: list[str] = []
        reasons: list[str] = []
        for index, node in enumerate(slots):
            cap = capability if (index == 0 and capability) else self.capability_of(node.tool)
            if not cap:
                return _SlotPlan(
                    error_status=RerouteStatus.EXHAUSTED_NO_METADATA,
                    error_reason=(
                        f"节点 {node.name}（{node.tool}）没有能力元数据 —— "
                        "无法换路（工具必须在接入时声明，RFC 15 §3.2）"
                    ),
                )
            decision = self.router.route(cap, self.env, banned=banned, penalized=penalized)
            if decision is None:
                return _SlotPlan(
                    error_status=RerouteStatus.EXHAUSTED_NO_PATH,
                    error_reason=f"能力 {cap} 算不出替代路径",
                    error_route_reason=self.router.explain(
                        cap, self.env, banned=banned, penalized=penalized
                    ),
                    error_capability=cap,
                )
            chains.append(decision.chosen)
            slot_caps.append(cap)
            reasons.append(decision.reason)
        return _SlotPlan(
            chains=tuple(chains), caps=tuple(slot_caps), reasons=tuple(reasons)
        )

# ── 工具函数 ────────────────────────────────────────────────────────────────

def _safe_fragment(text: str) -> str:
    """工具名 → 节点名片段（只留 ASCII 字母数字与下划线）。"""
    return "".join(ch if (ch.isalnum() and ch.isascii()) or ch == "_" else "_" for ch in text)

def _build_nodes(
    tools: Sequence[str],
    *,
    attempt: int,
    args: Mapping[str, Any],
) -> tuple[PlanNode, ...]:
    """把替代路径落成既有 `PlanNode`（线性链 · 名字稳定）。

    名字带 `reroute_<次数>_<序号>_` 前缀 —— **不与原计划撞名**
    （原计划里可能有同名工具的节点，撞了会覆盖 `node_results`）。
    """
    nodes: list[PlanNode] = []
    previous = ""
    for index, tool in enumerate(tools):
        name = f"reroute_{attempt}_{index}_{_safe_fragment(tool)}"
        nodes.append(
            PlanNode(
                name=name,
                tool=tool,
                args=dict(args),
                description=f"重路由替代节点（{attempt}）← {tool}",
                depends_on=(previous,) if previous else (),
            )
        )
        previous = name
    return tuple(nodes)

__all__ = [
    "DEFAULT_MAX_REROUTES",
    "KIND_SEVERITY",
    "REROUTE_MODE_EXCLUDE",
    "REROUTE_MODE_NONE",
    "REROUTE_MODE_PENALTY",
    "RerouteEngine",
    "RerouteOutcome",
    "RerouteStatus",
    "side_effect_severity",
]
