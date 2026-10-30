"""L2 Tool Executor 接口。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..compiler.plan import PlanNode
from ..types import NodeResult

@runtime_checkable
class ToolExecutor(Protocol):
    """L2 节点执行器接口。

    任何实现此接口的类都能被 IntentOSRuntime 当作 L2 executor：
        runtime = IntentOSRuntime(..., executor=SomeExecutor())
        runtime.run(intent)  # 节点通过 executor.execute(node) 执行

    实现：
    - InlineExecutor（默认）：内嵌工具实现，零依赖，用于测试
    - PyPilotExecutor（推荐）：调用 pypilot.tools.builtin.call_tool

    Phase 2.1 扩展了补偿语义 —— Saga 模式要求「失败后能回到执行前」：
    - execute() 前必须自行捕获可回滚的状态（实现里用 SnapshotStore）
    - compensate() 按捕获的状态逆向还原
    - reset_snapshots() 在每次 run 开始时清空上一轮的快照
    """

    def execute(self, node: PlanNode) -> NodeResult:
        """执行单个 PlanNode，返回 NodeResult。"""
        ...

    def compensate(self, node: PlanNode) -> NodeResult:
        """撤销该节点造成的副作用（Saga 补偿）。

        返回 NodeResult：ok=True 表示已还原，ok=False 表示无法还原
        （error 里说明原因）。无法补偿的实现（如调用了外部系统的
        executor）必须**如实返回失败**，不能假装成功。
        """
        ...

    def reset_snapshots(self) -> None:
        """清空上一轮 run 留下的快照/账本。"""
        ...
