"""弹性恢复率（RFC 15 §6）—— 白皮书的核心 KPI。

    弹性恢复率 = 触发过重路由且最终成功的任务数 / 触发过重路由的任务数

──────── 两条口径（RFC §6 的原话）──────────────────────────────────────────

1. **分母只算「真的触发过重路由」的** —— 没触发的不算，
   否则会被简单任务稀释（一次成功的任务也进分母，数字就没意义了）。
2. **同时按 `fallback_group` 分组** ——
   「HTTP→SSH 这组恢复得好不好，一眼看得出」。

「最终成功」= 出口状态是 `verified` / `rerouted`；
`failed` / `denied` / `verify_failed` / `blocked` 都算**没恢复**（不粉饰）。

**诚实前提**：这个数要可比，前提是**确定性沙盒**（RFC §6 末段）——
见 `intent_os/reroute/sandbox.py`（HTTP 固定失败 / SSH 固定成功）。

零模型调用 · 纯统计（只读 `RunResult`，不碰环境、不碰网络）。
"""
from __future__ import annotations

from typing import Any, Iterable

#: 「最终成功」的出口状态（Phase 5.3 新增了 "rerouted"）
SUCCESS_STATUSES: tuple[str, ...] = ("verified", "rerouted")

def elastic_recovery_rate(results: Iterable[Any]) -> dict[str, Any]:
    """算弹性恢复率（RFC 15 §6）。

    入参是**一批 run 的结果**（`RunResult` 即可 —— 只读 `status` 与 `reroutes`）。

    返回：

    ```python
    {
        "rate": 0.5,               # 弹性恢复率（0~1；没触发过重路由 → 0.0）
        "triggered": 4,            # 分母：触发过重路由的任务数
        "recovered": 2,            # 分子：其中最终成功的
        "by_fallback_group": {     # 按组看（HTTP→SSH 这组恢复得好不好）
            "git_clone": {"tasks": 3, "recovered": 2, "rate": 0.666...},
        },
    }
    ```

    **没触发过重路由的任务不进分母**（RFC §6 的口径）。
    """
    triggered = 0
    recovered = 0
    groups: dict[str, list[int]] = {}

    for result in results:
        records = list(getattr(result, "reroutes", None) or [])
        if not records:
            continue  # 没触发过重路由 → 不进分母
        triggered += 1
        ok = str(getattr(result, "status", "")) in SUCCESS_STATUSES
        if ok:
            recovered += 1
        for record in records:
            group = str(record.get("fallback_group") or "")
            slot = groups.setdefault(group, [0, 0])
            slot[0] += 1
            if ok:
                slot[1] += 1

    return {
        "rate": (recovered / triggered) if triggered else 0.0,
        "triggered": triggered,
        "recovered": recovered,
        "by_fallback_group": {
            name: {
                "tasks": counts[0],
                "recovered": counts[1],
                "rate": (counts[1] / counts[0]) if counts[0] else 0.0,
            }
            for name, counts in sorted(groups.items())
        },
    }

__all__ = ["SUCCESS_STATUSES", "elastic_recovery_rate"]
