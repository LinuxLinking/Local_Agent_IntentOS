"""L1 Approval Gate — 副作用前的关卡。

设计基线：design-foundation/01-concept-model.md §2 L1「Approval Gate」。
开放性结论：06 §2 C12（HITL 拆 7 动作；Phase 0 实现最低限度 Pause + Abort）。

Phase 0：CLI prompt `[Y/n]` + auto_approve/auto_deny 模式。
Phase 2.2（Step 2.2）：**两层审批** —— Plan 级 + 副作用级。

    Plan 级  ：编译完成后一次性看完整计划再决定要不要做（总览）
    副作用级 ：每个声明为 external 的副作用执行前再问一次（具体动作 + 影响范围）

为什么要两层：Plan 级审批让人看到「这次要做的事」，但计划一旦批准，
里面每个 external 副作用就自动放行了 —— 而「跑一个脚本」和「发一封邮件」
在计划视图里长得一样。副作用级审批把这一层再拆开。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from ..compiler.plan import Plan, PlanNode
from ..compiler.intent import SideEffect

@dataclass
class ApprovalGate:
    """副作用前的审批关卡。

    模式：
    - 默认：CLI prompt，Y 批准 / n 拒绝
    - auto_approve=True：自动批准（用于测试 / 受信任场景）
    - auto_deny=True：自动拒绝（用于验证 Denied 路径）

    Phase 0 只做 CLI；规则引擎延后到 Phase 2。
    """

    auto_approve: bool = False
    auto_deny: bool = False
    stream: Any = None  # 测试可注入

    def __post_init__(self) -> None:
        if self.stream is None:
            self.stream = sys.stdout

    def request(self, plan: Plan) -> bool:
        """请求审批（Plan 级）。返回 True=批准，False=拒绝。"""
        if self.auto_deny:
            return False
        if self.auto_approve:
            return True

        self._print_plan(plan)
        return self._ask()

    # ── 第二层：副作用级 ────────────────────────────────────────────────────

    def request_side_effect(self, node: PlanNode, side_effect: SideEffect) -> bool:
        """请求审批（副作用级）。

        Plan 已经批过了，这里再针对**这一个具体动作**问一次：
        哪个工具、要动什么范围、能不能回滚。

        返回 True=批准，False=拒绝。
        """
        if self.auto_deny:
            return False
        if self.auto_approve:
            return True

        out = self.stream
        print("", file=out)
        print("─" * 60, file=out)
        print(f"[Approval Gate · 副作用级] {node.name}", file=out)
        print(f"  动作    : {node.tool}", file=out)
        print(f"  参数    : {_shorten_args(node.args)}", file=out)
        print(f"  影响范围: {side_effect.scope}", file=out)
        print(f"  副作用型: {side_effect.kind.value}", file=out)
        print(f"  可逆性  : {side_effect.reversibility}", file=out)
        print("─" * 60, file=out)
        return self._ask()

    def _print_plan(self, plan: Plan) -> None:
        out = self.stream
        print("", file=out)
        print("═" * 60, file=out)
        print(f"[Approval Gate] Plan {plan.id}", file=out)
        print(f"  Goal       : {plan.intent.goal}", file=out)
        print(f"  Constraints: {[c.kind + '=' + c.value for c in plan.intent.constraints]}", file=out)
        print(f"  Side effects:", file=out)
        for se in plan.intent.side_effects:
            print(f"    - {se.tool} [{se.kind.value}] scope={se.scope} reversibility={se.reversibility}", file=out)
        print(f"  Plan nodes ({len(plan.nodes)}):", file=out)
        for i, node in enumerate(plan.nodes, 1):
            print(f"    {i}. {node.tool}({_shorten_args(node.args)}) — {node.description}", file=out)
        print(f"  Verifications:", file=out)
        for v in plan.intent.verification:
            print(f"    - {v.kind.value}: {v.tool} '{v.target}' expected={v.expected!r}", file=out)
        print("═" * 60, file=out)

    def _ask(self) -> bool:
        while True:
            ans = input("Proceed? [Y/n]: ").strip().lower()
            if ans in ("", "y", "yes"):
                return True
            if ans in ("n", "no"):
                return False
            print("Please answer Y or n")

def _shorten_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        s = repr(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)
