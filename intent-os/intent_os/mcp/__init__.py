"""L2 · MCP 总线 —— 把外部 MCP server 接成 Intent OS 的 I/O 通道。

设计依据：`design-foundation/14-mcp-integration-rfc.md`

五件东西：
  · `filesystem_server.py` —— 一个最小的真 MCP server（官方 SDK · FastMCP）
  · `client.py`            —— MCP 客户端（stdio · 持久会话 · SDK 自带超时与杀树）
  · `registry.py`          —— **接入声明**：有哪些 server、哪些工具、各自什么副作用等级
  · `executor.py`          —— 实现 `ToolExecutor` 协议（MCP 工具走 L2 的既有路径）
  · `plan.py`              —— `McpCall` → `Plan` 的小工具（复用既有 Plan 链路）

红线：
  · **D2 / D-h**：外置状态（`StateStore`）**绝不**以工具形式从这里暴露 ——
    MCP 工具目录里只有 server 自己声明的工具。
  · **D-d**：副作用等级**接入时声明**，不采信 server 自述。
  · **不撒谎**：连不上 / 超时 / 工具报错 → 如实 `ok=False` + 原因。
"""
from .client import McpCallResult, McpClient, McpToolSpec
from .executor import McpExecutor
from .plan import McpCall, build_mcp_plan
from .registry import McpRegistry, McpServerSpec, default_fs_registry

__all__ = [
    "McpClient",
    "McpCallResult",
    "McpToolSpec",
    "McpExecutor",
    "McpRegistry",
    "McpServerSpec",
    "McpCall",
    "build_mcp_plan",
    "default_fs_registry",
]
