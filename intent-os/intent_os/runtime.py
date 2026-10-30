"""L4 + L1 + L2 编排器 — IntentOSRuntime。

设计基线：design-foundation/01-concept-model.md 5 层结构。
开放性结论：06 §1 C4（syscall 列表）+ §2 C15（软事务：Saga 模式留 Phase 2）。

Phase 0：直接 InlineExecutor（不调 PyPilot，内嵌 write_file / read_file / list_dir / run_python）。
Phase 1（2026-10-19）：引入 executor 抽象；默认 InlineExecutor；PyPilotExecutor 作为 opt-in。
Phase 2：加 Tool compensate()，软事务（Saga）。
Phase 3.5.2 + 3.5.3（2026-10-24）：接进 L3 状态断言与前提声明 ——
    D2 的「推」状态视图 + 前提声明的确定性校验闭环（冲突即拦截 + 下发纠正文本）。
    规则出处：design-foundation/10-runtime-state-premise-rfc.md。
Phase 5.3（2026-10-29）：接进**弹性重路由**（可选注入 `reroute=`）——
    节点失败时**先判能不能换路**，能换就换路前进（RFC 15 §3.4）；
    不能换才走既有补偿路径。不注入时行为与 5.3 之前完全一致。
Phase 5.6（2026-10-30）：**局部补偿**（欠账 #20 · RFC 18）——
    换路成功时，**先**把**失败节点那一个**的快照还原掉（只补那一个，不回原点），
    **再**让替代链接着跑；局部补偿失败 → **不换路**（宁可整条链失败，
    也不让替代链跑在一个状态未知的世界上）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .compiler.intent import Intent, SideEffect, SideEffectKind, parse_intent
from .compiler.plan import (
    Plan,
    PlanNode,
    generate_plan,
    topological_order,
    validate_plan,
)
from .compiler.verify import VerificationEvaluator
from .executor import InlineExecutor
from .memory.state import StateAssertion, StateStore, StateView
from .reroute.engine import side_effect_severity
from .sop.constraints import ConstraintMemory
from .sop.experience import ExperienceBook
from .suspend.policy import HighRiskMode, suspend_reason_for
from .suspend.service import SuspendService
from .suspend.task import SuspendError, SuspendReason, SuspendedTask
from .state_interface import StateInterface
from .trust.approval import ApprovalGate
from .trust.audit import AuditTrail, Event
from .trust.premise import PremiseChecker, PremiseCheckResult, PremiseGate

if TYPE_CHECKING:
    from .executor.base import ToolExecutor
    from .reroute.engine import RerouteEngine, RerouteOutcome
    from .types import NodeResult, RunResult

# Re-export types for backwards compat
from .types import NodeResult, RunResult  # noqa: E402

@dataclass
class _Rescue:
    """一次重路由尝试的结果（runtime 内部用 · 不出这个包）。

    - `nodes`：替代节点（非空 = 换路成功，调用方把「失败节点及其后续」换掉继续跑）
    - `approval_denied`：备选路径的审批被拒（**不绕过审批换路**）
    - `outcome`：引擎给出的完整结论（进 `RunResult.reroutes`）
    """

    nodes: tuple[PlanNode, ...] = ()
    approval_denied: bool = False
    outcome: "RerouteOutcome | None" = None

@dataclass
class _ExecState:
    """执行阶段的可变状态（**run 与 resume 共用** —— 不另起执行链路）。

    - `pending`：待执行队列（重路由会把「失败节点及其后续」整段替换掉）
    - `node_results` / `executed` / `ran` / `done_tools`：既有语义的搬家
    - `reroute_records`：换路留痕（进 `RunResult.reroutes`）
    - `local_compensations`：局部补偿留痕（欠账 #20 · RFC 18）
    - `index`：当前在队列里的位置（**挂起点就是它**）
    - `suspend_seq`：这个任务已经挂起过几次（恢复后再挂 → +1）
    """

    pending: list[PlanNode]
    node_results: dict[str, NodeResult] = field(default_factory=dict)
    executed: list[PlanNode] = field(default_factory=list)
    ran: list[PlanNode] = field(default_factory=list)
    done_tools: list[str] = field(default_factory=list)
    reroute_records: list[dict[str, Any]] = field(default_factory=list)
    # 欠账 #20（RFC 18）：局部补偿的留痕（每次一条 · 如实）
    local_compensations: list[dict[str, Any]] = field(default_factory=list)
    index: int = 0
    suspend_seq: int = 0

class IntentOSRuntime:
    """Intent OS Runtime — 把 Intent 跑完 L4 + L1 + L2 完整链路。

    用法：
        with AuditTrail(Path("audit.jsonl")) as audit:
            # Phase 0 默认（零依赖，InlineExecutor）
            runtime = IntentOSRuntime(
                sandbox_dir=Path("./sandbox"),
                audit=audit,
                approval=ApprovalGate(),
            )
            # Phase 1（推荐，PyPilotExecutor 接 PyPilot 已验证工具层）
            runtime = IntentOSRuntime(
                sandbox_dir=Path("./sandbox"),
                audit=audit,
                approval=ApprovalGate(),
                executor=PyPilotExecutor(),
            )
            # Phase 3.5.2：状态是外置的权威源，模型拿不到「调工具拿状态」的口子
            runtime.set_state("sandbox.clean", "true", source="sensor")
            intent = parse_intent("写个 fizzbuzz 存 fizz.py 然后跑一下")
            result = runtime.run(intent)

    **Phase 5.3（可选注入 `reroute=`）**：节点失败时**先判能不能换路**，
    能换就换路前进（RFC 15 §3.4）；不能换才走既有补偿路径。
    **不注入 `reroute=` 时行为与 5.3 之前完全一致**（失败即补偿回原点）。

    **Phase 5.6（局部补偿 · 欠账 #20 · RFC 18）**：换路成功时，**先**把
    **失败节点那一个**的快照还原掉（只补那一个，**不回原点**），**再**让替代链
    接着跑 —— **顺序是硬要求**：补偿必须在替代节点执行**之前**，
    否则会把替代节点写下的东西覆盖掉（= 回滚掉后续节点已经消费掉的东西）。
    局部补偿失败 → **不换路**，走既有失败路径并如实报告。

    **Phase 3.5.2 + 3.5.3**：L3 状态断言与前提声明接进主链路。
    一次 run 能看到「推了什么状态、声明了什么前提、校验发现了什么」，
    前提与权威状态矛盾时 run 被拦下并下发纠正文本 —— 见
    design-foundation/10-runtime-state-premise-rfc.md。
    """

    def __init__(
        self,
        sandbox_dir: Path,
        audit: AuditTrail,
        approval: ApprovalGate,
        executor: "ToolExecutor | None" = None,
        verifier: VerificationEvaluator | None = None,
        run_timeout: int = 30,
        *,
        state: StateStore | None = None,
        state_interface: StateInterface | None = None,
        state_scope: str | None = None,
        require_premises: bool = False,
        unknown_is_blocking: bool = False,
        side_effect_kinds: "Iterable[SideEffectKind] | None" = None,
        reroute: "RerouteEngine | None" = None,
        suspend: "SuspendService | None" = None,
        constraints: "ConstraintMemory | None" = None,
        experience: "ExperienceBook | None" = None,
    ):
        self.sandbox_dir = Path(sandbox_dir).resolve()
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        self.audit = audit
        self.approval = approval
        self.verifier = verifier or VerificationEvaluator(self.sandbox_dir)
        # 默认 InlineExecutor；传 PyPilotExecutor() 接 PyPilot
        if executor is None:
            self.executor: "ToolExecutor" = InlineExecutor(
                self.sandbox_dir, run_timeout=run_timeout
            )
        else:
            self.executor = executor

        # ── Phase 5.3：弹性重路由（RFC 15 §3.4）───────────────────────
        # **可选**注入：不注入 = 行为与 5.3 之前完全一致（失败即补偿回原点）。
        # 注入时：节点失败 → **先判能不能换路** → 能换就换路前进，不能换才补偿。
        self.reroute = reroute

        # ── Phase 5.4：挂起与恢复（RFC 15 §3.5）──────────────────────
        # **可选**注入：不注入 = 没有挂起这条路（失败即补偿回原点）。
        # 注入时：重路由耗尽（且该耗尽值得等人）→ **挂起等人**而不是 failed。
        if suspend is not None and suspend.audit is not audit:
            raise ValueError(
                "suspend 与 runtime 不是同一个 AuditTrail —— "
                "挂起/恢复必须写进同一份审计（谁挂的、谁恢复了哪一段）"
            )
        self.suspend = suspend

        # ── Phase 5.5：「大学生工程师」记忆（RFC 15 §3.4-D）─────────────
        # **可选注入**：约束记忆是代码层红线（执行前拦）；经验错题本给重路由提供「历史失败边」。
        self.constraints = constraints
        self.experience = experience

        # ── Phase 3.5.2：状态通道（独立于 Tool / MCP，D2）─────────────────
        if state_interface is not None:
            if state is not None and state is not state_interface.store:
                raise ValueError(
                    "state 与 state_interface.store 不是同一个 StateStore —— "
                    "runtime 只有一个权威源，别造第二个"
                )
            self.state_interface = state_interface
            self.state = state_interface.store
        else:
            self.state = state if state is not None else StateStore()
            self.state_interface = StateInterface(self.state, audit=audit)

        # 视图与校验必须同 scope —— 否则「模型没看见却被告知断言错了」。
        self.state_scope = state_scope
        # Phase 3.5.3：前提门策略
        self.require_premises = require_premises
        self.unknown_is_blocking = unknown_is_blocking

        # ── 第二层审批（副作用级）覆盖哪些副作用类型 ──────────────────────────
        # 默认只覆盖 EXTERNAL —— 与 Phase 2.2 的既有口径一致：
        # 「读文件」「写沙箱内文件」不二次确认，「跑脚本 / 动外部系统」要再问一次。
        #
        # **为什么它是可配的**：副作用等级是**接入时声明**的（RFC 14 D-d），
        # 不同接入方对「写」的风险判断不一样。MCP 接入方可以传 {EXTERNAL, WRITE}
        # 让 MCP 的写操作也进第二层审批（RFC 14 §1「MCP 工具是副作用，必须过 L1」）。
        # 默认值保持不变 → 既有行为零影响。
        self.side_effect_kinds: frozenset[SideEffectKind] = (
            frozenset(side_effect_kinds)
            if side_effect_kinds is not None
            else frozenset({SideEffectKind.EXTERNAL})
        )

    # ── 状态接口（D2）────────────────────────────────────────────────────────

    def set_state(self, key: str, value: str, **kwargs) -> StateAssertion:
        """写入权威状态。

        **状态接口不是 Tool** —— 模型调不到它，只能由调用方写。
        `kwargs` 透传给 `StateStore.set`（`source` / `frame` / `reference` / `scope` / `note`）。
        """
        return self.state_interface.set(key, value, **kwargs)

    def state_view(self, *, title: str = "当前状态") -> StateView:
        """当前状态视图（**不留痕**）—— 入口层用它把状态拼进提示词。

        真正的「推」是 run 内部的那一次（`trigger="run"`），
        它是**校验基准**的快照。
        """
        return self.state_interface.view(scope=self.state_scope, title=title)

    def pull_state_view(self, keys, *, title: str = "状态明细") -> StateView:
        """拉：模型请求更深的状态视图（D2 的辅通道，**必留痕**）。

        与推的区别：推是 Runtime 决定推什么（一句话摘要），
        拉是模型挑键（明细：版本 / 来源 / 参照物 / 备注）。
        """
        return self.state_interface.pull_view(keys, scope=self.state_scope, title=title)

    # ── 主入口 ────────────────────────────────────────────────────────────────

    def run(self, intent: Intent, plan: Plan | None = None) -> "RunResult":
        """跑一次 run：Intent → Plan → L1 审批 → L2 执行 → 验收。

        `plan is None`（默认）→ Plan 由 runtime 自己编译（`generate_plan`）；
        `plan is not None` → **直接用接入方传进来的 Plan**，跳过编译。

        **为什么允许外部传入 Plan**：接入方可能比 runtime 更清楚要做什么，
        自己就把 Plan 编好了（典型场景：接入方要调外部工具链，例如 MCP 工具节点 ——
        由接入方负责发现工具、组装节点与依赖关系）。runtime 若再编一遍，
        会把接入方的编排信息覆盖掉。

        **runtime 只认 Intent + Plan，不关心 Plan 里是什么工具**：
        节点里的 `tool` 就是一个字符串，交给 executor 去解析；
        runtime 里没有任何按工具名分支的特例逻辑
        （外部工具走**已有的** L2 executor 协议，见 RFC 14 D-e）。

        无论 Plan 从哪来，审计流水都一样：都会记一条 `PLAN_GENERATED`。

        **欠账 #18**：run 结束（成功 / 失败 / 拒绝 / 挂起 / 阻塞都算）
        一定留一条 `RUN_FINISHED` ——「弹性恢复率」（RFC 15 §6）
        因此能从审计流算出来，不必只依赖 `RunResult`。
        """
        result = self._run(intent, plan)
        return self._log_run_finished(result)

    def _run(self, intent: Intent, plan: Plan | None = None) -> "RunResult":
        """`run()` 的真正实现 —— 打点（`RUN_FINISHED`）只在 `run()` 一处。"""
        # 0. 清掉上一轮 run 的快照 —— 补偿是「一次 run 内」的事，
        #    跨 run 还原没有意义，反而危险。
        reset = getattr(self.executor, "reset_snapshots", None)
        if callable(reset):
            reset()

        # Phase 5.3：重路由的任务级状态（禁用边 / 已用次数）**每次 run 清空**
        # —— 「失败边记多久」= 本次任务内立即生效（RFC 15 §9 Q21）。
        if self.reroute is not None:
            self.reroute.reset_task()

        # 1. 记录 intent（含模型声明的前提 —— INTENT_RECEIVED 带 premises）
        self.audit.log(Event.INTENT_RECEIVED, intent.to_dict())

        # 2. 推状态视图（D2 的「推」）—— 本次 run 的状态上下文。
        #    与校验用的 as_state_map() 同 scope、同时刻（RFC 10 §6）。
        state_view = self.state_interface.push_view(
            scope=self.state_scope, trigger="run"
        )

        # 3. Plan —— 自家编译 or 接入方传入（外部传入时跳过编译）
        if plan is None:
            plan = generate_plan(intent)
        # 留痕不分来源 —— Plan 是外面来的，审计流水也不能少这一环。
        self.audit.log(Event.PLAN_GENERATED, plan.to_dict())

        # 4. Plan 是 noop（fallback Intent）？
        if len(plan.nodes) == 1 and plan.nodes[0].name == "noop":
            self.audit.log(
                Event.NODE_COMPLETED,
                {"node_name": plan.nodes[0].name, "note": "noop"},
            )
            return RunResult(
                status="noop",
                audit_path=str(self.audit.path),
                error=plan.nodes[0].args.get("note"),
                state_view=state_view,
            )

        # 5. Plan 校验（环 / 重名 / 缺失依赖）
        errors = validate_plan(plan)
        if errors:
            err_msg = "; ".join(errors)
            self.audit.log(
                Event.NODE_FAILED,
                {"phase": "plan_validation", "errors": errors},
            )
            return RunResult(
                status="failed",
                audit_path=str(self.audit.path),
                error=err_msg,
                state_view=state_view,
            )

        # 6. 前提门（Phase 3.5.3）—— Plan 级，执行前，且在 Approval 之前。
        #    前提冲突 = Plan 建立在错误信念上 → 先纠正模型，再让人拍板。
        premise_result, correction_text, block_reason = self._premise_gate(intent)
        if block_reason is not None:
            self.audit.log(
                Event.PREMISE_BLOCKED,
                {
                    "reason": block_reason,
                    "declared_count": len(intent.premises),
                    "correction_text": correction_text,
                },
            )
            return RunResult(
                status="blocked",
                audit_path=str(self.audit.path),
                error=_BLOCK_MESSAGES.get(block_reason, block_reason),
                state_view=state_view,
                premise_result=premise_result,
                correction_text=correction_text,
            )

        # 7. Approval Gate
        self.audit.log(Event.APPROVAL_REQUESTED, {"plan_id": plan.id})
        if not self.approval.request(plan):
            self.audit.log(Event.APPROVAL_DENIED, {"plan_id": plan.id})
            return RunResult(
                status="denied",
                audit_path=str(self.audit.path),
                state_view=state_view,
                premise_result=premise_result,
                correction_text=correction_text,
            )
        self.audit.log(Event.APPROVAL_GRANTED, {"plan_id": plan.id})

        ordered = topological_order(plan)
        state = _ExecState(pending=list(ordered))
        return self._execute(
            state=state,
            plan=plan,
            intent=intent,
            state_view=state_view,
            premise_result=premise_result,
            correction_text=correction_text,
        )

    # ── Phase 5.3 / 5.4 · 执行阶段（run 与 resume 共用这一段）────────────

    def _execute(
        self,
        *,
        state: _ExecState,
        plan: Plan,
        intent: Intent,
        state_view: "StateView | None",
        premise_result: "PremiseCheckResult | None",
        correction_text: str,
        suspend_high_risk: bool = True,
    ) -> "RunResult":
        """执行阶段 —— **run 与 resume 共用这一段**（不另起执行链路）。

        `suspend_high_risk=False`（恢复路径）：高危操作**同步问门**、不挂起 ——
        否则会「挂起 → 恢复 → 又挂起」原地打转（恢复时必须有人能拍板）。
        """
        while state.index < len(state.pending):
            node = state.pending[state.index]

            # Phase 5.5：**约束记忆**（代码层红线）—— 先查红线，再看要不要问人。
            # 红线是「不许做的事」，问人也没用；拦住就是拦住（RFC 15 §3.4-D）。
            if self.constraints is not None:
                violation = self.constraints.check_node(node)
                if violation is not None:
                    self.audit.log(Event.CONSTRAINT_BLOCKED, violation.to_dict())
                    return RunResult(
                        status="blocked",
                        node_results=state.node_results,
                        audit_path=str(self.audit.path),
                        error=f"红线拦截：{violation.detail}",
                        state_view=state_view,
                        premise_result=premise_result,
                        correction_text=correction_text,
                        reroutes=list(state.reroute_records),
                    )

            # Phase 5.4：高危操作 → **挂起等人**（挂起前**不放行**，RFC 15 §3.5）
            if suspend_high_risk:
                suspended = self._maybe_suspend_high_risk(
                    node,
                    intent,
                    state=state,
                    plan=plan,
                    state_view=state_view,
                    premise_result=premise_result,
                    correction_text=correction_text,
                )
                if suspended is not None:
                    return suspended

            # 8a. 副作用级审批（Q9 两层里的第二层）
            if not self._approve_side_effect(node, intent):
                compensated, comp_err = self._compensate(state.executed)
                return RunResult(
                    status="denied",
                    node_results=state.node_results,
                    audit_path=str(self.audit.path),
                    compensated=compensated,
                    compensation_error=comp_err,
                    state_view=state_view,
                    premise_result=premise_result,
                    correction_text=correction_text,
                    reroutes=state.reroute_records,
                )

            self.audit.log(Event.NODE_STARTED, node.to_dict())
            state.executed.append(node)
            state.ran.append(node)
            try:
                nr = self.executor.execute(node)
            except Exception as e:
                self.audit.log(
                    Event.NODE_FAILED,
                    {"node_name": node.name, "error": str(e)},
                )
                # Phase 5.3：**先判能不能换路**，再决定补不补偿（RFC 15 §3.4）
                rescue = self._try_reroute(
                    node, state.pending[state.index + 1 :], str(e), intent, state.done_tools
                )
                if rescue.nodes:
                    # 欠账 #20（RFC 18）：换路成功 → **先**把失败节点那一个
                    # 还原掉（只补那一个，不回原点），**再**让替代链接着跑。
                    # **顺序是硬要求**（RFC 18 §4.1）：补偿必须在替代节点执行
                    # **之前** —— 否则会把替代节点写下的东西覆盖掉。
                    local = self._compensate_failed_node(node)
                    state.local_compensations.append(local)
                    if rescue.outcome is not None:
                        record = self._reroute_record(rescue.outcome)
                        record["local_compensation"] = local
                        state.reroute_records.append(record)
                    if local["status"] == "failed":
                        # 局部补偿失败 → **不换路**（RFC 18 §4.2）：无法保证
                        # 替代链跑在一个干净的世界上 → 走既有失败路径，如实报告。
                        return self._fail_run(
                            node=node,
                            error=(
                                f"{e}；局部补偿失败（{local['error']}）—— "
                                "不换路：无法保证替代链跑在一个干净的世界上"
                            ),
                            rescue=rescue,
                            state=state,
                            plan=plan,
                            intent=intent,
                            state_view=state_view,
                            premise_result=premise_result,
                            correction_text=correction_text,
                        )
                    state.pending = state.pending[: state.index] + list(rescue.nodes)
                    continue
                # Phase 5.4：重路由耗尽 → **挂起等人**（不是直接 failed）
                return self._fail_run(
                    node=node,
                    error=str(e),
                    rescue=rescue,
                    state=state,
                    plan=plan,
                    intent=intent,
                    state_view=state_view,
                    premise_result=premise_result,
                    correction_text=correction_text,
                )
            state.node_results[node.name] = nr
            self.audit.log(
                Event.NODE_COMPLETED,
                {
                    "node_name": node.name,
                    "ok": nr.ok,
                    "output_preview": _preview(nr.output),
                },
            )
            if not nr.ok:
                # Phase 5.3：**先判能不能换路**，再决定补不补偿（RFC 15 §3.4）
                rescue = self._try_reroute(
                    node,
                    state.pending[state.index + 1 :],
                    nr.error or "",
                    intent,
                    state.done_tools,
                )
                if rescue.nodes:
                    # 欠账 #20（RFC 18）：同上 —— **先**局部补偿，**再**让替代链跑。
                    local = self._compensate_failed_node(node)
                    state.local_compensations.append(local)
                    if rescue.outcome is not None:
                        record = self._reroute_record(rescue.outcome)
                        record["local_compensation"] = local
                        state.reroute_records.append(record)
                    if local["status"] == "failed":
                        # 局部补偿失败 → **不换路**（RFC 18 §4.2）
                        return self._fail_run(
                            node=node,
                            error=(
                                f"{nr.error or ''}；局部补偿失败（{local['error']}）—— "
                                "不换路：无法保证替代链跑在一个干净的世界上"
                            ),
                            rescue=rescue,
                            state=state,
                            plan=plan,
                            intent=intent,
                            state_view=state_view,
                            premise_result=premise_result,
                            correction_text=correction_text,
                        )
                    state.pending = state.pending[: state.index] + list(rescue.nodes)
                    continue
                # Phase 5.4：重路由耗尽 → **挂起等人**（不是直接 failed）
                return self._fail_run(
                    node=node,
                    error=nr.error or "",
                    rescue=rescue,
                    state=state,
                    plan=plan,
                    intent=intent,
                    state_view=state_view,
                    premise_result=premise_result,
                    correction_text=correction_text,
                )
            state.done_tools.append(node.tool)
            state.index += 1

        # 9. 验收 — verifier 按 tool name 查
        # Phase 5.3：按**跑过的**节点取结果（重路由换掉的节点不在结果里；
        # 同名工具取最后一次执行的结果 —— 换路成功的那次才算数）。
        tool_results: dict[str, NodeResult] = {}
        for n in state.ran:
            if n.name in state.node_results:
                tool_results[n.tool] = state.node_results[n.name]
        verify_results = []
        for spec in intent.verification:
            vr = self.verifier.evaluate(spec, tool_results)
            verify_results.append(vr)
            self.audit.log(
                Event.VERIFY_PASSED if vr.passed else Event.VERIFY_FAILED,
                {"spec": vr.spec_dump(), "evidence": vr.evidence},
            )

        all_passed = all(vr.passed for vr in verify_results)
        # Phase 5.3：走过重路由且验收全过 → 出口状态是 **"rerouted"**
        # （不是 "verified" —— 「成功」与「换过路」是两件事，必须分得清）。
        if state.reroute_records and all_passed:
            status = "rerouted"
        else:
            status = "verified" if all_passed else "verify_failed"
        # 欠账 #20（RFC 18）：局部补偿的留痕也进 run 级结果（**如实合并**）
        compensated, comp_err = self._merge_local_compensation(state)
        return RunResult(
            status=status,
            node_results=state.node_results,
            verify_results=verify_results,
            audit_path=str(self.audit.path),
            compensated=compensated,
            compensation_error=comp_err,
            state_view=state_view,
            premise_result=premise_result,
            correction_text=correction_text,
            reroutes=state.reroute_records,
        )

    # ── 欠账 #18 · RUN_FINISHED（RFC 15 §6）───────────────────────────────

    def _log_run_finished(
        self, result: "RunResult", *, resumed: bool = False
    ) -> "RunResult":
        """留一条「run 结束」的痕 —— **每条出口路径都留**。

        成功 / 失败 / 拒绝 / 挂起 / 阻塞 / noop —— 一条都不能漏：
        只有都记了，审计流里才**推得出**「最终成功」，
        「弹性恢复率」（RFC 15 §6）才算得出
        （否则它只能从 `RunResult` 算 —— 那正是欠账 #18）。

        **口径只此一处**：data 里的字段值**全部取自 `RunResult`**，
        审计侧不另算一套（否则审计与结果各说各话，数字没法互相印证）：

        - `status`          = `RunResult.status`
        - `rerouted`        = 这次 run 触发过重路由吗（`reroutes` 非空）
        - `reroutes`        = 换了几次路（条数）
        - `reroute_records` = 换路明细（与 `RunResult.reroutes` 同形，
          含 `fallback_group` —— RFC §6 要按组看「HTTP→SSH 这组恢复得好不好」，
          光有条数算不出来）
        """
        self.audit.log(
            Event.RUN_FINISHED,
            {
                "status": result.status,
                "rerouted": bool(result.reroutes),
                "reroutes": len(result.reroutes),
                "reroute_records": [dict(r) for r in result.reroutes],
                "resumed": resumed,
                "error": result.error,
                "resume_path": result.resume_path,
                "compensated": list(result.compensated),
            },
        )
        return result

    # ── Phase 3.5.3 · 前提门 ────────────────────────────────────────────────

    def _premise_gate(
        self, intent: Intent
    ) -> tuple[PremiseCheckResult | None, str, str | None]:
        """前提门：声明 → 确定性校验 → 决定放不放行。

        **查状态表打在当下**（不在 run 开始时快照）——
        「状态变了 → 旧前提失效」是整个 Phase 3.5 的存在理由。

        **判定口径本身不在这里** —— 在 `trust/premise.py` 的 `PremiseGate`，
        轨迹重放器调的是**同一个**门（RFC 11 §3：一个概念只有一处定义）。
        这里只负责：构造校验器 → 调门 → **写审计**。

        Returns:
            (结论, 下发给模型的文本, 拦截原因)
            拦截原因为 `None` = 放行。
        """
        declared = list(intent.premises)
        state_map = self.state.as_state_map(scope=self.state_scope)

        # 实际构造校验器（校验基准 = 权威状态表，RFC 09 规则 4 的唯一桥）
        checker = PremiseChecker(
            state=state_map, unknown_is_blocking=self.unknown_is_blocking
        )
        gate = PremiseGate(checker, require_premises=self.require_premises)
        outcome = gate.evaluate(declared)

        if outcome.vacuous:
            # 校验空转 —— **必须留痕**，不能装作校验过了（红线 #4：不静默降级）。
            self.audit.log(
                Event.PREMISE_CHECKED,
                {
                    "declared": [],
                    "vacuous": True,
                    "reason": "no_premises_declared",
                    "blocked": outcome.block_reason is not None,
                    "correction_text": outcome.correction_text,
                    "policy": gate.policy(),
                },
            )
        else:
            self.audit.log(
                Event.PREMISE_CHECKED,
                {
                    "declared": [p.to_dict() for p in declared],
                    "result": outcome.summary.to_dict(),
                    "per_premise": [r.to_dict() for r in outcome.per_premise],
                    "state_keys": sorted(state_map.keys()),
                    "state_scope": self.state_scope,
                    # 纠正文本**每次都留** —— 不拦的时候（unknown 告知）也下发过，
                    # 只在拦截时留痕等于「告知」这一类下发是隐形的（RFC 11 §2.2）。
                    "correction_text": outcome.correction_text,
                    # 策略也留 —— 不记下来，重放时分不清「代码修好了」与「配置变了」。
                    "policy": gate.policy(),
                },
            )

        return outcome.summary, outcome.correction_text, outcome.block_reason

# ── Phase 2 · 两层审批与 Saga 补偿 ───────────────────────────────────────

    def _approve_side_effect(self, node: PlanNode, intent: Intent) -> bool:
        """副作用级审批（Q9 两层里的第二层）。

        只对 Intent 里**声明的副作用等级落在 `self.side_effect_kinds` 里**的节点生效 ——
        默认集合是 `{EXTERNAL}`：「读文件」「写沙箱内文件」不需要二次确认，「跑脚本」要。
        没有声明成副作用的节点直接放行（Plan 级已经批过了）。

        **为什么集合可配**：副作用等级由**接入方**声明（RFC 14 D-d），
        「写」要不要二次确认，取决于接入方怎么判风险 —— MCP 接入可传 {EXTERNAL, WRITE}。
        """
        se = next(
            (
                s
                for s in intent.side_effects
                if s.tool == node.tool and s.kind in self.side_effect_kinds
            ),
            None,
        )
        if se is None:
            return True

        return self._request_side_effect_approval(node, se)

    def _needs_side_effect_approval(self, node: PlanNode, intent: Intent) -> bool:
        """这个节点要不要过**副作用级审批**（挂起判定与审批判定**同一套口径**）。"""
        return any(
            s.tool == node.tool and s.kind in self.side_effect_kinds
            for s in intent.side_effects
        )

    def _request_side_effect_approval(
        self, node: PlanNode, side_effect: SideEffect
    ) -> bool:
        """副作用级审批的**唯一**实现（留痕 + 问既有 ApprovalGate）。

        Phase 5.3 的重路由也走这一层 —— 降级**不是**绕过审批的后门（Q22）。
        """
        self.audit.log(
            Event.SIDE_EFFECT_APPROVAL_REQUESTED,
            {
                "node_name": node.name,
                "tool": node.tool,
                "scope": side_effect.scope,
                "reversibility": side_effect.reversibility,
            },
        )
        granted = self.approval.request_side_effect(node, side_effect)
        self.audit.log(
            Event.SIDE_EFFECT_APPROVAL_GRANTED
            if granted
            else Event.SIDE_EFFECT_APPROVAL_DENIED,
            {"node_name": node.name, "tool": node.tool},
        )
        return granted

    # ── Phase 5.3 · 弹性重路由（RFC 15 §3.4）───────────────────────────────

    def _try_reroute(
        self,
        node: PlanNode,
        remaining: list[PlanNode],
        error: str,
        intent: Intent,
        done_tools: list[str],
    ) -> "_Rescue":
        """节点失败 → **先判能不能换路**，再决定补不补偿。

        换路流程（RFC 15 §3.4）：

            失败边进 banned → 用 5.2 的路由器重算同一条 capability 需求
            → 用备选**替换剩余节点** → 继续执行

        返回 `_Rescue`：

        - `nodes` 非空 → 换路成功（调用方把「失败节点及其后续」换成这些节点）
        - `approval_denied` → 备选路径的审批被拒（**不绕过审批换路**）
        - 其余 → 换不了（没有备选 / 硬上限用尽）→ 调用方走既有补偿路径

        **零模型调用** · **不新建执行链路**：替代节点还是既有 `PlanNode`，
        还是既有 `ToolExecutor` / `ApprovalGate` / `AuditTrail`。
        """
        engine = self.reroute
        if engine is None:
            return _Rescue()

        self.audit.log(
            Event.REROUTE_TRIGGERED,
            {
                "node_name": node.name,
                "tool": node.tool,
                "error": error,
                "capability": engine.capability_of(node.tool),
                "attempt": engine.used + 1,
                "max_reroutes": engine.max_reroutes,
            },
        )

        # Phase 5.5：**经验错题本** —— 同环境指纹下历史失败过的工具，别再撞一遍（Q21）
        if self.experience is not None:
            capability = engine.capability_of(node.tool)
            known_bad = self.experience.banned_tools(capability, engine.env)
            extra = known_bad - engine.banned
            if extra:
                self.audit.log(
                    Event.EXPERIENCE_APPLIED,
                    {
                        "node_name": node.name,
                        "capability": capability,
                        "banned": sorted(extra),
                        "env_key": self.experience.lookup(capability, engine.env)[0].env_key
                        if self.experience.lookup(capability, engine.env)
                        else "",
                    },
                )
                engine.restore_state(banned=engine.banned | extra, used=engine.used)

        outcome = engine.attempt(
            failed_node=node,
            remaining=remaining,
            error=error,
            done_tools=done_tools,
            tool_args=node.args,
        )

        if not outcome.selected:
            self.audit.log(
                Event.REROUTE_EXHAUSTED,
                {
                    "node_name": node.name,
                    "tool": node.tool,
                    "status": outcome.status.value,
                    "reason": outcome.reason,
                    "attempt": outcome.attempt,
                    "max_reroutes": engine.max_reroutes,
                    "banned": list(outcome.banned),
                    "route_reason": outcome.route_reason,
                },
            )
            return _Rescue(outcome=outcome)

        # 降级不是绕过审批的后门（RFC 15 §9 Q22）：副作用更重 → 必须重新过审批
        granted, reapproval = self._approve_reroute(outcome, node, intent)
        if not granted:
            self.audit.log(
                Event.REROUTE_EXHAUSTED,
                {
                    "node_name": node.name,
                    "tool": node.tool,
                    "status": "approval_denied",
                    "reason": "备选路径的审批未通过 —— 降级不是绕过审批的后门",
                    "attempt": outcome.attempt,
                    "capability": outcome.capability,
                    "chosen": list(outcome.chosen),
                    "reapproval_required": reapproval,
                },
            )
            return _Rescue(approval_denied=True, outcome=outcome)

        self.audit.log(
            Event.REROUTE_SELECTED,
            {
                "node_name": node.name,
                "failed_tool": outcome.failed_tool,
                "capability": outcome.capability,
                "chosen": list(outcome.chosen),
                "banned": list(outcome.banned),
                "attempt": outcome.attempt,
                "max_reroutes": engine.max_reroutes,
                "reapproval_required": reapproval,
                "replaced_tools": list(outcome.replaced_tools),
                "route_reason": outcome.route_reason,
            },
        )
        return _Rescue(nodes=outcome.nodes, outcome=outcome)

    def _approve_reroute(
        self,
        outcome: "RerouteOutcome",
        failed_node: PlanNode,
        intent: Intent,
    ) -> tuple[bool, bool]:
        """降级不是绕过审批的后门（RFC 15 §9 Q22）。

        备选路径的副作用**更重**时 → 必须**重新过审批**（走既有的副作用级那一层）；
        同等级别不重来（RFC 的原话是「副作用更重时必须重新过审批」）。

        返回 (是否放行, 是否触发了重新审批)。
        """
        engine = self.reroute
        if engine is None:
            return True, False
        old_kind = engine.kind_of(failed_node.tool)
        reapproval = False
        for node in outcome.nodes:
            new_kind = engine.kind_of(node.tool)
            if new_kind is None:
                continue  # 没有声明副作用等级 → 不构成「更重」
            if old_kind is None:
                need = new_kind in self.side_effect_kinds
            else:
                need = side_effect_severity(new_kind) > side_effect_severity(old_kind)
            if not need:
                continue
            reapproval = True
            side_effect = self._side_effect_for(node, intent)
            if not self._request_side_effect_approval(node, side_effect):
                return False, True
        return True, reapproval

    def _reroute_record(self, outcome: "RerouteOutcome") -> dict[str, Any]:
        """一次换路的留痕（进 `RunResult.reroutes` —— 「换过路」这件事必须看得见）。

        `fallback_group` 也记上：RFC 15 §6 要按组看「HTTP→SSH 这组恢复得好不好」。
        """
        group = ""
        if self.reroute is not None:
            meta = self.reroute.metadata_of(outcome.failed_tool)
            group = meta.fallback_group if meta is not None else ""
        return {
            "attempt": outcome.attempt,
            "failed_tool": outcome.failed_tool,
            "capability": outcome.capability,
            "chosen": list(outcome.chosen),
            "banned": list(outcome.banned),
            "fallback_group": group,
            "reason": outcome.reason,
        }

    # ── Phase 5.4 · 挂起与恢复（RFC 15 §3.5）───────────────────────────────

    def _maybe_suspend_after_failure(
        self,
        outcome: "RerouteOutcome | None",
        node: PlanNode,
        *,
        state: _ExecState,
        plan: Plan,
        intent: Intent,
        error: str,
        state_view: "StateView | None",
        premise_result: "PremiseCheckResult | None",
        correction_text: str,
    ) -> "RunResult | None":
        """重路由耗尽 → **该不该挂起等人**？

        判定在 `suspend/policy.py`（穷举的 `RerouteStatus`，**不猜**）：

        - 预算用尽 / 没有备选 → **挂起**（人改了环境或给了决定之后能继续）
        - 节点没有能力元数据（**配置错误**）→ 不挂起（挂起也没用）
        - 备选审批被拒（人**已经**拒过）→ 不挂起（既有 `denied` 语义保留）

        返回 `None` = 不挂起 → 调用方走既有失败/拒绝路径。
        """
        service = self.suspend
        if service is None or outcome is None:
            return None
        reason = suspend_reason_for(outcome, policy=service.policy)
        if reason is None:
            return None
        detail = f"{outcome.reason}（节点 {node.name} / {node.tool}：{error}）"
        return self._do_suspend(
            reason=reason,
            detail=detail,
            node=node,
            state=state,
            plan=plan,
            intent=intent,
            state_view=state_view,
            premise_result=premise_result,
            correction_text=correction_text,
        )

    def _maybe_suspend_high_risk(
        self,
        node: PlanNode,
        intent: Intent,
        *,
        state: _ExecState,
        plan: Plan,
        state_view: "StateView | None",
        premise_result: "PremiseCheckResult | None",
        correction_text: str,
    ) -> "RunResult | None":
        """高危操作 → **挂起等人**（挂起前**不放行**，RFC 15 §3.5）。

        只在策略是 `HighRiskMode.SUSPEND` 时生效；默认（ASK）照旧**同步问门** ——
        既有 `ApprovalGate` 的同步语义一个字不变。
        """
        service = self.suspend
        if service is None:
            return None
        if service.policy.high_risk is not HighRiskMode.SUSPEND:
            return None
        if not self._needs_side_effect_approval(node, intent):
            return None
        detail = (
            f"高危操作待人工确认：{node.tool}"
            f"（节点 {node.name}；恢复时再走副作用级审批）"
        )
        return self._do_suspend(
            reason=SuspendReason.HIGH_RISK,
            detail=detail,
            node=node,
            state=state,
            plan=plan,
            intent=intent,
            state_view=state_view,
            premise_result=premise_result,
            correction_text=correction_text,
        )

    def _do_suspend(
        self,
        *,
        reason: SuspendReason,
        detail: str,
        node: PlanNode,
        state: _ExecState,
        plan: Plan,
        intent: Intent,
        state_view: "StateView | None",
        premise_result: "PremiseCheckResult | None",
        correction_text: str,
    ) -> "RunResult | None":
        """落成挂起态 → 写 resume 文件 → 返回 `status="suspended"`。

        写不出来 → 记 `SUSPEND_FAILED` → 返回 `None`（调用方走既有失败路径，
        **不假装挂起了** —— 红线：不静默降级）。
        """
        service = self.suspend
        if service is None:
            return None

        completed: list[dict[str, Any]] = []
        for n in state.ran:
            nr = state.node_results.get(n.name)
            if nr is not None and nr.ok:
                completed.append(
                    {
                        "name": n.name,
                        "tool": n.tool,
                        "ok": nr.ok,
                        "output": nr.output,
                        "error": nr.error,
                    }
                )

        environment = service.environment_json()
        if not environment and self.reroute is not None:
            environment = self.reroute.env.to_json()

        task = SuspendedTask(
            task_id=plan.id,
            suspend_seq=state.suspend_seq + 1,
            intent=intent,
            plan=Plan(id=plan.id, intent=intent, nodes=tuple(state.pending)),
            completed=tuple(completed),
            suspend_node=node.name,
            suspend_index=state.index,
            reason=reason,
            reason_detail=detail,
            environment=environment,
            banned=tuple(sorted(self.reroute.banned)) if self.reroute else (),
            reroute_used=self.reroute.used if self.reroute else 0,
            reroutes=tuple(state.reroute_records),
            audit_path=str(self.audit.path),
        )
        try:
            path = service.save(task)
        except Exception as exc:
            self.audit.log(
                Event.SUSPEND_FAILED,
                {
                    "node_name": node.name,
                    "tool": node.tool,
                    "reason": reason.value,
                    "error": str(exc),
                },
            )
            return None
        return RunResult(
            status="suspended",
            node_results=state.node_results,
            audit_path=str(self.audit.path),
            error=detail,
            state_view=state_view,
            premise_result=premise_result,
            correction_text=correction_text,
            reroutes=state.reroute_records,
            resume_path=str(path),
        )

    def resume(self, resume_path: str | Path) -> "RunResult":
        """从 resume 文件**恢复**（**进程重启后也能继续**）。

        读不出来（文件损坏 / 版本不符）→ **如实报错**（`SuspendError`），
        绝不「读不出来就当没挂起过」。
        """
        service = self.suspend
        if service is None:
            raise SuspendError(
                "runtime 没有挂起服务 —— 无法恢复（先把 SuspendService 接进来）"
            )
        task = service.load(resume_path)
        return self.resume_task(task)

    def resume_task(self, task: SuspendedTask) -> "RunResult":
        """恢复一个挂起的任务 —— **从挂起点继续**（已完成节点不重跑）。

        四件事：

        1. **重路由状态**恢复：禁用边**留着**（本次任务内有效，RFC Q21）；
           预算重置（人明确点了「继续」= 一次新的决策点）。
        2. **审计**记 `TASK_RESUMED`（谁恢复了哪一段）。
        3. **已完成节点不重跑**（但它们的结果被恢复 —— 验收还要看得见）。
        4. 恢复点之后的**高危操作重新过审批**（同步问门 —— 人在这一轮拍板）。

        恢复也是「一次 run 的终局」—— 同样留一条 `RUN_FINISHED`（欠账 #18）：
        否则审计流里「挂起 → 恢复」这段是断的。
        """
        result = self._resume_task(task)
        return self._log_run_finished(result, resumed=True)

    def _resume_task(self, task: SuspendedTask) -> "RunResult":
        """`resume_task()` 的真正实现 —— 打点只在 `resume_task()` 一处。"""
        intent = task.intent
        plan = task.plan
        ordered = topological_order(plan)
        done = task.completed_names

        state = _ExecState(
            pending=[n for n in ordered if n.name not in done],
            node_results={
                str(c.get("name")): NodeResult(
                    ok=bool(c.get("ok")),
                    output=str(c.get("output", "")),
                    error=c.get("error"),
                )
                for c in task.completed
            },
            ran=[n for n in ordered if n.name in done],
            done_tools=[str(c.get("tool", "")) for c in task.completed],
            reroute_records=[dict(r) for r in task.reroutes],
            suspend_seq=task.suspend_seq,
        )

        if self.reroute is not None:
            self.reroute.restore_state(banned=task.banned, used=0)

        service = self.suspend
        if service is not None:
            service.record_resumed(
                task, extra={"remaining": len(state.pending)}
            )

        # 执行剩余节点（`suspend_high_risk=False` → 高危操作**同步问门**）
        return self._execute(
            state=state,
            plan=plan,
            intent=intent,
            state_view=None,
            premise_result=None,
            correction_text="",
            suspend_high_risk=False,
        )

    def _side_effect_for(self, node: PlanNode, intent: Intent) -> SideEffect:
        """替代节点的副作用声明。

        接入方在 Intent 里声明过的优先；没声明就按工具元数据**保守**合成
        （范围未知 → `reroute:<tool>`；可逆性未知 → `hard`）。
        """
        for se in intent.side_effects:
            if se.tool == node.tool:
                return se
        kind = SideEffectKind.NONE
        if self.reroute is not None:
            kind = self.reroute.kind_of(node.tool) or SideEffectKind.NONE
        return SideEffect(
            tool=node.tool,
            scope=f"reroute:{node.tool}",
            kind=kind,
            reversibility="hard",
        )

    # ── 欠账 #20 · 局部补偿（RFC 18）────────────────────────────────────────

    def _compensate_failed_node(self, node: PlanNode) -> dict[str, Any]:
        """局部补偿：**只还原失败节点那一个**（不回原点）。

        换路成功时，失败节点可能已经改了半个世界。它留下的**可还原**部分
        （快照账本里有的那些路径）在这里还原掉 —— 用的还是**同一个**
        `SnapshotStore` / 同一个 `executor.compensate()`
        （**不新造第二套补偿机制** —— RFC 15 §3.4 的硬要求）。

        返回一条**如实**的记录（进审计 + `RunResult.reroutes`）：

        - `status="compensated"`：还原了；
        - `status="skipped"`：**没有可还原的快照** → 无事可做（**不是失败**）；
        - `status="failed"`：还原失败 → 后果是「半截副作用留在世界里」。

        **覆盖边界（说清，不静默）**：只还原**账本里有的**路径 —— 脚本对其它
        文件的改动不在账本里 → **不会被还原**（既有 `SnapshotStore` 的边界，
        不是本机制引入的）。
        """
        record: dict[str, Any] = {
            "node": node.name,
            "tool": node.tool,
            "scope": "local",
            "status": "compensated",
            "detail": "",
            "error": None,
        }

        # ① 这个节点**有没有**可还原的快照？
        #    没有 → 它没有可观测的文件副作用 → 无事可做（**不是失败**）。
        #    判据在 `SnapshotStore.has_snapshots`（一个概念一处定义）——
        #    runtime 只是**问同一本账**，不自己造第二本账。
        store = getattr(self.executor, "snapshots", None)
        probe = getattr(store, "has_snapshots", None)
        if callable(probe):
            try:
                if not probe(node.name):
                    record["status"] = "skipped"
                    record["detail"] = (
                        "没有可还原的快照 —— 该节点没有可观测的文件副作用"
                        "（账本里没有的东西不会被还原）"
                    )
                    return record
            except Exception:
                # 判不出来 → 交给 `executor.compensate()` 自己说（它才是权威）
                pass

        # ② 还原 —— **复用既有补偿机制**（同一个 executor / 同一个 SnapshotStore）
        compensated, comp_err = self._compensate([node], scope="local")
        if comp_err:
            # `_compensate` 的错误串自带节点名（"name: 原因"）→ 这里只留原因
            reason = comp_err.split(": ", 1)[1] if ": " in comp_err else comp_err
            record["status"] = "failed"
            record["error"] = reason
            record["detail"] = (
                "还原失败 —— 失败节点的半截副作用**此刻仍留在世界里**（未回滚）"
            )
            return record
        record["detail"] = "已还原失败节点捕获的路径（账本里有的那些）"
        return record

    def _merge_local_compensation(
        self,
        state: "_ExecState",
        compensated: "list[str] | None" = None,
        comp_err: "str | None" = None,
    ) -> tuple[list[str], str | None]:
        """补偿汇总：**链级 + 局部级**如实合并（不互相覆盖、不静默丢）。

        - 局部补偿**成功**的节点 → 进 `compensated`（已还原的节点名）；
        - 局部补偿**失败**的节点 → 进 `compensation_error`，并把**后果**写清楚：
          「半截副作用留在世界里」（如果后续的整条链补偿又把它还原了，
          就如实说「已被整条链补偿还原」—— **不说过头**）。
        """
        names = list(compensated or [])
        errors: list[str] = []
        for item in state.local_compensations:
            if item["status"] == "compensated":
                if item["node"] not in names:
                    names.append(item["node"])
            elif item["status"] == "failed":
                if item["node"] in names:
                    errors.append(
                        f"{item['node']}: 局部补偿失败（{item['error']}）—— "
                        "已被整条链补偿还原"
                    )
                else:
                    errors.append(
                        f"{item['node']}: 局部补偿失败（{item['error']}）—— "
                        "半截副作用留在世界里（未回滚）"
                    )
        if comp_err:
            errors.append(comp_err)
        return names, ("; ".join(errors) or None)

    def _fail_run(
        self,
        *,
        node: PlanNode,
        error: str,
        rescue: "_Rescue",
        state: "_ExecState",
        plan: Plan,
        intent: Intent,
        state_view: "StateView | None",
        premise_result: "PremiseCheckResult | None",
        correction_text: str,
    ) -> "RunResult":
        """失败节点的收尾（**既有路径**：挂起等人 → 否则整条链补偿）。

        抽出来是为了让「局部补偿失败 → 不换路」（RFC 18 §4.2）也能走**同一条**
        收尾路径 —— 不另起一套失败处理（一个概念一处定义）。
        """
        suspended = self._maybe_suspend_after_failure(
            rescue.outcome,
            node,
            state=state,
            plan=plan,
            intent=intent,
            error=error,
            state_view=state_view,
            premise_result=premise_result,
            correction_text=correction_text,
        )
        if suspended is not None:
            return suspended
        compensated, comp_err = self._compensate(state.executed)
        compensated, comp_err = self._merge_local_compensation(
            state, compensated, comp_err
        )
        return RunResult(
            status="denied" if rescue.approval_denied else "failed",
            node_results=state.node_results,
            audit_path=str(self.audit.path),
            error=_failure_error(node, error, rescue),
            compensated=compensated,
            compensation_error=comp_err,
            state_view=state_view,
            premise_result=premise_result,
            correction_text=correction_text,
            reroutes=state.reroute_records,
        )

    def _compensate(
        self, executed: list[PlanNode], *, scope: str = "chain"
    ) -> tuple[list[str], str | None]:
        """Saga 补偿：按逆序还原已经动过手的节点。

        逆序是必须的 —— 后一个节点可能依赖前一个节点的产物，
        还原时必须从依赖链的末端开始往回撤。

        `scope`：**"chain"** = 整条链回滚（既有语义）；
        **"local"** = 局部补偿（只还原**一个**节点 —— 欠账 #20 · RFC 18）。
        同一个函数、同一套机制，只是**范围**不同。

        返回 (已成功还原的节点名列表, 补偿失败说明)。
        """
        if not executed:
            return [], None

        self.audit.log(
            Event.COMPENSATION_STARTED,
            {
                "nodes": [n.name for n in reversed(executed)],
                # 范围（chain / local）+ 覆盖边界（只还原账本里有的路径）
                "scope": scope,
                "coverage": "snapshot_ledger_only",
            },
        )
        compensated: list[str] = []
        errors: list[str] = []
        for node in reversed(executed):
            try:
                nr = self.executor.compensate(node)
            except Exception as e:
                nr = NodeResult(ok=False, output="", error=str(e))
            self.audit.log(
                Event.NODE_COMPENSATED if nr.ok else Event.COMPENSATION_FAILED,
                {
                    "node_name": node.name,
                    "output": nr.output,
                    "error": nr.error,
                },
            )
            if nr.ok:
                compensated.append(node.name)
            else:
                errors.append(f"{node.name}: {nr.error}")
        return compensated, ("; ".join(errors) or None)

def _failure_error(node: PlanNode, error: str, rescue: "_Rescue") -> str:
    """失败信息 —— **如实说清两件事**（不静默、不假装成功）。

    - 节点本身失败：原样带上错误；
    - 备选路径的审批被拒：明说「不绕过审批」，别让人以为只是节点失败。
    """
    if rescue.approval_denied:
        return (
            f"节点 {node.name} 失败：{error}；"
            "重路由的备选路径审批未通过 —— 降级不绕过审批"
        )
    return error

def _preview(s: str, n: int = 80) -> str:
    return s.replace("\n", "\\n")[:n] + ("..." if len(s) > n else "")

# ── Phase 3.5.3 · 前提门的文案 ──────────────────────────────────────────────
#
# 措辞纪律（RFC 08 §3.1）：**陈述，不是判决**。
# ✗「你的意图不合法」—— 模型会对抗
# ✓「你的前提与状态 X 矛盾」—— 模型能据此改写前提
#
# 这里的 `error` 是给**人 / 调用方**看的 run 级摘要；
# 给模型看的文本走 `correction_text`（由 PremiseGate 生成，
# 它的文案定义在 trust/premise.py —— 一个概念只有一处定义）。

_BLOCK_MESSAGES = {
    "premise_conflict": "前提与权威状态矛盾 —— run 被拦下",
    "no_premises_declared": "未声明前提 —— run 被拦下",
}
