"""挂起判定 —— **哪些困境该挂起等人**（RFC 15 §3.4 → §3.5 的接口）。

白皮书的第三种结局不是「摆烂」，而是「**挂起等人**」。但**不是所有走投无路
都该挂起** —— 挂起是给「**人改了环境/给了决定之后能继续**」的场合用的。

──────── 判定表（重路由耗尽 → 挂起与否）──────────────────────────────────

| 耗尽形态 | 挂起？ | 挂起原因 | 为什么 |
|---|---|---|---|
| `EXHAUSTED_LIMIT` | ✅ | `REROUTE_EXHAUSTED` | RFC 15 §9 Q20：「超了就挂起，别硬撞」—— 人可以决定换条路 / 继续 |
| `EXHAUSTED_NO_PATH` | ✅ | `NO_ALTERNATIVE` | 「没有备选」—— 需要人/环境介入（装了 ssh、补了 key……） |
| `EXHAUSTED_NO_METADATA` | ❌ | —— | **配置错误**：工具没在接入时声明能力（RFC 15 §3.2）—— 挂起也没用，人得先改配置 |
| `EXHAUSTED_APPROVAL` | ❌ | —— | 人**已经**拒过了（同步 Y/n）—— 既有 `denied` 语义**保留不动** |

**这条边界很重要**：挂起是「等人来解」，不是「把错误换个地方存起来」。
配置错误挂起 = 把 bug 变成了一堆等不到的工单。

──────── 高危操作：挂起 vs 同步问 ──────────────────────────────────────────

既有 `ApprovalGate` 是**同步** Y/n —— 那是「现在就问一句」。
本模块的挂起是**异步**（人可能几小时后才回）—— RFC 15 §3.5：
「现有 ApprovalGate 是同步 Y/n；这里是异步挂起 + 后续恢复，
**是它的超集，不是替代**」。

`HighRiskMode`：

- `ASK`（**默认**）：高危操作照旧同步问门 —— **既有行为一个字不变**。
- `SUSPEND`：高危操作**挂起等人**（挂起前**不放行**）；恢复时再走同步审批。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from ..reroute.engine import RerouteStatus
from .task import SuspendReason

if TYPE_CHECKING:
    from ..reroute.engine import RerouteOutcome

class HighRiskMode(str, Enum):
    """高危操作遇到时怎么办。"""

    #: 同步问既有 ApprovalGate（默认 —— 不改既有行为）
    ASK = "ask"
    #: 挂起等人（恢复时再问）
    SUSPEND = "suspend"

#: 默认「该挂起」的耗尽形态（预算用尽 + 没有备选）
DEFAULT_SUSPENDABLE: frozenset[RerouteStatus] = frozenset(
    {RerouteStatus.EXHAUSTED_LIMIT, RerouteStatus.EXHAUSTED_NO_PATH}
)

@dataclass(frozen=True)
class SuspendPolicy:
    """挂起策略（**可配**；默认口径见上）。

    - `high_risk`：高危操作是同步问门还是挂起等人
    - `suspendable`：哪些重路由耗尽形态该挂起（其余 → 走既有失败/拒绝路径）
    """

    high_risk: HighRiskMode = HighRiskMode.ASK
    suspendable: frozenset[RerouteStatus] = field(
        default_factory=lambda: set(DEFAULT_SUSPENDABLE)
    )

def suspend_reason_for(
    outcome: "RerouteOutcome | None",
    *,
    policy: SuspendPolicy | None = None,
) -> SuspendReason | None:
    """重路由耗尽 → **该不该挂起等人**？

    返回挂起原因；`None` = **不该挂起**（配置错误 / 人已拒过 → 走既有路径）。

    判定依据是 `outcome.status`（穷举的 `RerouteStatus`），**不猜**。
    """
    if outcome is None:
        return None
    active = policy or SuspendPolicy()
    if outcome.status not in active.suspendable:
        return None
    if outcome.status is RerouteStatus.EXHAUSTED_LIMIT:
        return SuspendReason.REROUTE_EXHAUSTED
    if outcome.status is RerouteStatus.EXHAUSTED_NO_PATH:
        return SuspendReason.NO_ALTERNATIVE
    return None  # 其它形态（NO_METADATA / APPROVAL）→ 不挂起

__all__ = [
    "DEFAULT_SUSPENDABLE",
    "HighRiskMode",
    "SuspendPolicy",
    "suspend_reason_for",
]
