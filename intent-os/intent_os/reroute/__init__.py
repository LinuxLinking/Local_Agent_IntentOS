"""intent_os.reroute —— 弹性重路由（RFC 15 · Step 5.3）。

**做什么**：节点失败 / 超时 / 被拒之后，**先判能不能换路**，
再决定补不补偿 —— 能换就换路前进，不能换才回原点。

    失败 → 失败边进 banned → 用 5.2 的路由器重算 → 替换剩余节点 → 继续执行

**为什么是护城河**：IntentOS 的价值不是「不拒绝」，
而是**拒绝之后的容错与重路由**（RFC 15 §3.4）。

三条不破的红线：

1. **零模型调用** —— 换哪条路由**确定性代码**算（路由器），不由模型试错。
2. **不另起执行链路** —— 复用既有的 `ToolExecutor` / `AuditTrail` /
   `ApprovalGate` / `SnapshotStore`；重路由只产出**既有 `PlanNode`**。
3. **不静默降级** —— 每次换路都留痕（`REROUTE_TRIGGERED` / `REROUTE_SELECTED` /
   `REROUTE_EXHAUSTED`）；换不了就如实说换不了。

模块分工：`engine.py` 换路机制（任务级状态 + 确定性重算）·
`sandbox.py` 确定性沙盒（HTTP 失败 → SSH 成功，可复现）。
"""
from .engine import (
    DEFAULT_MAX_REROUTES,
    KIND_SEVERITY,
    RerouteEngine,
    RerouteOutcome,
    RerouteStatus,
    side_effect_severity,
)
from .metrics import SUCCESS_STATUSES, elastic_recovery_rate
from .sandbox import (
    ADMIN_CLONE,
    DEFAULT_FAILURES,
    HTTP_CLONE,
    HTTP_PUSH,
    SSH_CLONE,
    SSH_PUSH,
    TRY_A,
    TRY_B,
    TRY_C,
    SandboxExecutor,
    sandbox_env,
    sandbox_registry,
)

__all__ = [
    # 换路机制
    "RerouteEngine",
    "RerouteOutcome",
    "RerouteStatus",
    "DEFAULT_MAX_REROUTES",
    "KIND_SEVERITY",
    "side_effect_severity",
    # 弹性恢复率（RFC 15 §6）
    "elastic_recovery_rate",
    "SUCCESS_STATUSES",
    # 确定性沙盒
    "SandboxExecutor",
    "sandbox_env",
    "sandbox_registry",
    "HTTP_CLONE",
    "SSH_CLONE",
    "ADMIN_CLONE",
    "HTTP_PUSH",
    "SSH_PUSH",
    "TRY_A",
    "TRY_B",
    "TRY_C",
    "DEFAULT_FAILURES",
]
