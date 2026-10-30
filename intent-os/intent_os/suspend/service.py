"""挂起/恢复的**服务层** —— 落盘 + 审计（RFC 15 §3.5）。

**做什么**：把「挂起态」落到磁盘（resume 文件），把「谁挂的 / 谁恢复了哪一段」
写进**既有审计**（不新建审计系统）。

──────── 三条不破的红线 ────────────────────────────────────────────────────

1. **复用既有审计**：挂起/恢复都写既有 `AuditTrail`（同一个实例、同一份哈希链）。
2. **D2 边界**：挂起态**不写进 L3 长期记忆** —— 本包不 import `memory/state.py`，
   也不 import `memory/context.py`（挂起态是**执行位置**，不是世界状态）。
3. **不静默降级**：resume 文件写不出来 → 记 `SUSPEND_FAILED` 并**如实抛错**，
   绝不「假装挂起了」。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..probe.environment import EnvironmentSnapshot
from ..trust.audit import AuditTrail, Event
from .policy import SuspendPolicy
from .task import SuspendError, SuspendedTask

#: 默认 resume 目录名（放在沙盒里 —— 沙盒是 gitignore 的，resume 文件也是过程产物）
DEFAULT_RESUME_DIRNAME: str = ".suspend"

def default_resume_dir(sandbox_dir: str | Path) -> Path:
    """默认 resume 目录：`<sandbox_dir>/.suspend`（与 `SnapshotStore` 的 `.snapshot` 同风格）。"""
    return Path(sandbox_dir) / DEFAULT_RESUME_DIRNAME

class SuspendService:
    """挂起/恢复服务（**落盘 + 审计**，不含执行逻辑）。

    ```python
    service = SuspendService(audit, default_resume_dir(sandbox))
    path = service.save(task)      # 落盘 + TASK_SUSPENDED
    task2 = service.load(path)     # 读回（跨进程）
    service.record_resumed(task2)  # TASK_RESUMED
    ```
    """

    def __init__(
        self,
        audit: AuditTrail,
        resume_dir: str | Path,
        *,
        policy: SuspendPolicy | None = None,
        env: EnvironmentSnapshot | None = None,
    ) -> None:
        self.audit = audit
        self.resume_dir = Path(resume_dir)
        self.policy = policy or SuspendPolicy()
        self.env = env

    # ── 路径 ──────────────────────────────────────────────────────────────

    def path_for(self, task_id: str, seq: int) -> Path:
        """resume 文件的路径（`resume_<task_id>_<序号>.json`）。"""
        safe = "".join(ch if (ch.isalnum() and ch.isascii()) or ch in "-_" else "_" for ch in task_id)
        return self.resume_dir / f"resume_{safe}_{int(seq):02d}.json"

    # ── 环境快照 ──────────────────────────────────────────────────────────

    def environment_json(self) -> dict[str, Any]:
        """挂起当时的环境快照（复用 5.1 的 `EnvironmentSnapshot.to_json()` 形状）。"""
        if self.env is None:
            return {}
        return self.env.to_json()

    # ── 落盘 ──────────────────────────────────────────────────────────────

    def save(self, task: SuspendedTask) -> Path:
        """写 resume 文件 + 记 `TASK_SUSPENDED`。

        写不出来 → 记 `SUSPEND_FAILED` 并**如实抛错**（`SuspendError`）。
        """
        target = task.save(self.path_for(task.task_id, task.suspend_seq))
        self.audit.log(
            Event.TASK_SUSPENDED,
            {
                "task_id": task.task_id,
                "suspend_seq": task.suspend_seq,
                "reason": task.reason.value,
                "reason_detail": task.reason_detail,
                "suspend_node": task.suspend_node,
                "suspend_index": task.suspend_index,
                "completed": len(task.completed),
                "banned": list(task.banned),
                "resume_path": str(target),
                "audit_path": task.audit_path,
                "created_at": task.created_at,
            },
        )
        return target

    def record_resumed(
        self,
        task: SuspendedTask,
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        """记 `TASK_RESUMED`（**谁恢复了哪一段**）。"""
        data: dict[str, Any] = {
            "task_id": task.task_id,
            "suspend_seq": task.suspend_seq,
            "suspend_node": task.suspend_node,
            "suspend_index": task.suspend_index,
            "completed": len(task.completed),
            "banned": list(task.banned),
            "audit_path": task.audit_path,
        }
        if extra:
            data.update(dict(extra))
        self.audit.log(Event.TASK_RESUMED, data)

    # ── 读回 ──────────────────────────────────────────────────────────────

    def load(self, path: str | Path) -> SuspendedTask:
        """读回 resume 文件（损坏 → 如实报错，不静默兜底）。"""
        return SuspendedTask.load(path)

__all__ = [
    "DEFAULT_RESUME_DIRNAME",
    "SuspendService",
    "default_resume_dir",
]
