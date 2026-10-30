"""L3 域适配器 —— **域**放这里，**机制**不绑域。

设计依据：design-foundation/07-r2-conclusions.md §9（隐喻驱动架构风险）。

> **「空间状态」只是结构化外部状态的一个特例**；机制里长出 `wall` / `coordinate`
> 就绑死了 —— 产品战场是文件 / 代码符号 / API schema / 历史决策。

所以核心层（`memory/state.py` · `trust/premise.py`）**只认「key → value」**，
不认识任何具体域。各域的适配器放在这里：

| 域 | 模块 | 产出什么状态键 |
|---|---|---|
| 空间（3.5.5） | `maze.py` | `maze.corridor.<房间>.<方向>` · `maze.goal.room` |
| 文件 / 代码符号 | （3.5.2–3.5.4 的用例已覆盖） | `sandbox.clean` · `repo.branch` |

**换域不换机制** —— 每个域只需要能回答两个问题：

  1. 「现在是什么样」→ 产出状态断言（key + value + 世界坐标系）
  2. 「动一下会怎样」→ 确定性转移

其余（前提声明 / 确定性校验 / 拦截 / 纠正 / 留痕 / 重放）全部由核心层负责。
"""
from .maze import (
    BLOCKED,
    DIRECTIONS,
    OPEN,
    Corridor,
    Maze,
    MoveResult,
    Room,
)

__all__ = [
    "Maze",
    "Room",
    "Corridor",
    "MoveResult",
    "DIRECTIONS",
    "OPEN",
    "BLOCKED",
]
