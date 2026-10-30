"""L2 Tool Executor — 节点执行器接口与实现。

设计基线：design-foundation/01-concept-model.md §2 L2「Runtime」。
开放性结论：06 §2 C10（Workflow 函数级 vs 事务级）。

Phase 1（2026-10-19）：
- ToolExecutor Protocol：节点执行的统一接口
- PyPilotExecutor：通过 pypilot.tools.builtin.call_tool 执行
- InlineExecutor：Phase 0 内嵌实现（测试用，零依赖）
"""
from .base import ToolExecutor
from .inline import InlineExecutor
from .pypilot import PyPilotExecutor

__all__ = [
    "ToolExecutor",
    "InlineExecutor",
    "PyPilotExecutor",
]
