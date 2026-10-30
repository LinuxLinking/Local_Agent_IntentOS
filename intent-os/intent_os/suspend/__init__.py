"""挂起与恢复（RFC 15 §3.5 · Phase 5.4）。

白皮书的第三种结局：**平滑降级 / 挂起等人**。5.3 做到了「降级」，
本包补上「等人」——「重路由耗尽 / 没有备选 / 高危操作」时，
把任务**挂起**（可序列化），人确认 / 环境修好后**原地恢复**（恢复点精确到节点）。

──────── 三个子模块 ────────────────────────────────────────────────────────

| 模块 | 管什么 |
|---|---|
| `task.py` | 挂起态 `SuspendedTask`：形状 / 序列化 / resume 文件读写（**跨进程**） |
| `policy.py` | 挂起判定：**哪些困境该挂起**（穷举的 `RerouteStatus`，不猜）+ 高危模式 |
| `service.py` | 服务层：落盘 + 写**既有审计**（`TASK_SUSPENDED` / `TASK_RESUMED`） |

──────── 三条不破的红线 ────────────────────────────────────────────────────

1. **零模型调用**：只 import 既有类型 + 标准库。
2. **D2 边界**：挂起态**不写进 L3 长期记忆**（那是状态断言的地盘）——
   本包不 import `memory/state.py`，也不 import `memory/context.py`。
3. **不静默降级**：resume 文件损坏 / 版本不符 → **如实报错**（`SuspendError`），
   绝不「读不出来就当没挂起过」。

用法：

```python
service = SuspendService(audit, default_resume_dir(sandbox_dir))
runtime = IntentOSRuntime(..., reroute=engine, suspend=service)
result = runtime.run(intent, plan)
if result.status == "suspended":
    ...
    result2 = runtime.resume(result.resume_path)   # 也可以换个进程
```
"""
from __future__ import annotations

from .policy import (
    DEFAULT_SUSPENDABLE,
    HighRiskMode,
    SuspendPolicy,
    suspend_reason_for,
)
from .service import (
    DEFAULT_RESUME_DIRNAME,
    SuspendService,
    default_resume_dir,
)
from .task import (
    RESUME_KIND,
    RESUME_VERSION,
    SuspendError,
    SuspendReason,
    SuspendedTask,
)

__all__ = [
    # 挂起态
    "SuspendedTask",
    "SuspendReason",
    "SuspendError",
    "RESUME_KIND",
    "RESUME_VERSION",
    # 判定
    "SuspendPolicy",
    "HighRiskMode",
    "suspend_reason_for",
    "DEFAULT_SUSPENDABLE",
    # 服务
    "SuspendService",
    "default_resume_dir",
    "DEFAULT_RESUME_DIRNAME",
]
