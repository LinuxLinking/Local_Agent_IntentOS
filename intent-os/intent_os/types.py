"""Intent OS 共享数据类型（避免循环导入）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .compiler.verify import VerifyResult

if TYPE_CHECKING:
    from .memory.state import StateView
    from .trust.premise import PremiseCheckResult

@dataclass
class NodeResult:
    """单个 Plan 节点的执行结果。

    由 ToolExecutor 返回（executor/base.py 的 Protocol 使用）。
    """

    ok: bool
    output: str
    error: str | None = None

@dataclass
class RunResult:
    """整次 Intent 执行的结果。"""

    # 出口状态（**封闭集合**，Phase 5.3 新增 "rerouted"）：
    #   "verified"      —— 验收全过（没走过重路由）
    #   "rerouted"      —— 验收全过，但**中途换过路**（Phase 5.3 新增）
    #   "verify_failed" —— 验收没过
    #   "denied"        —— 审批被拒（含重路由备选路径的审批被拒）
    #   "failed"        —— 执行失败（含重路由耗尽后的如实失败）
    #   "noop"          —— fallback 意图
    #   "blocked"       —— 前提门拦下
    #   "suspended"     —— **挂起等人**（Phase 5.4 新增；resume 文件路径见 `resume_path`）
    status: str
    node_results: dict[str, NodeResult] = field(default_factory=dict)
    verify_results: list[VerifyResult] = field(default_factory=list)
    audit_path: str | None = None
    error: str | None = None
    # Phase 2.1：Saga 补偿结果
    compensated: list[str] = field(default_factory=list)  # 已成功还原的节点名
    compensation_error: str | None = None  # 补偿失败的说明
    # ── Phase 3.5.2 / 3.5.3：状态与前提 ─────────────────────────────────
    # state_view  —— 本次 run 推给模型的状态视图（D2 的「推」的载荷）
    # premise_result —— 前提校验结论（confirmed / conflicts / unknowns / unchecked）
    # correction_text —— 下发给模型的文本（矛盾纠正 + 未知告知；空串 = 无需下发）
    state_view: "StateView | None" = None
    premise_result: "PremiseCheckResult | None" = None
    correction_text: str = ""
    # Phase 5.3：弹性重路由留痕（每次换路一条：失败的工具 / 选中的替代 / 禁用边）
    reroutes: list[dict[str, Any]] = field(default_factory=list)
    # Phase 5.4：挂起时的 resume 文件路径（status="suspended" 时有值）
    resume_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "node_results": {
                nid: {"ok": nr.ok, "output": nr.output, "error": nr.error}
                for nid, nr in self.node_results.items()
            },
            "verify_results": [
                {
                    "passed": vr.passed,
                    "spec": vr.spec_dump(),
                    "evidence": vr.evidence,
                }
                for vr in self.verify_results
            ],
            "audit_path": self.audit_path,
            "error": self.error,
            "compensated": list(self.compensated),
            "compensation_error": self.compensation_error,
            "state_view": self.state_view.to_dict() if self.state_view else None,
            "premise_result": (
                self.premise_result.to_dict() if self.premise_result else None
            ),
            "correction_text": self.correction_text,
            "reroutes": [dict(r) for r in self.reroutes],
            "resume_path": self.resume_path,
        }
