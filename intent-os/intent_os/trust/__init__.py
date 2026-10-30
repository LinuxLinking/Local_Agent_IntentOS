"""L1 Trust Layer — 审计 / 审批 / 前提校验 / 补偿。

- `audit.py`    —— AuditTrail（append-only + 哈希链 + WORM 封存）
- `approval.py` —— ApprovalGate（Plan 级 + 副作用级两层审批）
- `premise.py` —— PremiseChecker（拿权威状态机械判定模型声明的前提）

`StateAssertion` 的定义在 `intent_os/memory/state.py`（L3 权威源），
本包只做再导出 —— 见 design-foundation/09-state-assertion-rfc.md §1。
"""
from .approval import ApprovalGate
from .audit import AuditTrail, Event
from .premise import (
    PremiseChecker,
    PremiseCheckResult,
    PremiseConflict,
    StateAssertion,
)

__all__ = [
    "AuditTrail",
    "Event",
    "ApprovalGate",
    "PremiseChecker",
    "PremiseCheckResult",
    "PremiseConflict",
    "StateAssertion",
]
