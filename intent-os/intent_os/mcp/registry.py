"""MCP 接入声明（注册表）—— 谁接进来了、每个工具是什么副作用等级。

设计依据：`design-foundation/14-mcp-integration-rfc.md` D-d / D-g / D-h

**这里是什么**：Intent OS 对「外部世界」的**接入清单**（真相源）。
接进来哪些 server、允许调哪些工具、每个工具的副作用等级与影响范围，
**全部由我们在接入时写死**。

**这里不是什么**：
  · **不是 server 的自述** —— MCP 的 `tools/list` 只给名字/描述/inputSchema，
    没有副作用语义。采信 server 自述 = 让**被约束方**定义约束（D-d）。
  · **不是状态出口** —— 外置状态（`StateStore`）**绝不**做成 MCP 工具（D2 / D-h）。
    本模块不 import `memory/state.py`，也不提供任何「读状态」的工具声明。

**未声明 = 不存在**：executor 只执行注册表里声明过的工具；
server 临时冒出来的新工具一律不认（宁可拒绝，不可纵容）。

命名约定（D-g）：内部工具名固定 `mcp:<server>:<tool>` —— 防止与内置工具
（write_file / read_file / …）撞名，审计里也一眼看得出是 MCP 来的。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..compiler.intent import SideEffect, SideEffectKind
from .client import McpToolSpec

#: MCP 工具名的前缀（RFC 14 D-g）
MCP_PREFIX = "mcp:"

@dataclass(frozen=True)
class McpServerSpec:
    """一个被接进来的 MCP server 的**启动声明**。

    `command` + `args` 就是 stdio transport 的启动方式 —— server 跑在子进程里
    （D-c），跟 OS 比喻里的「用户态设备驱动」完全一致。
    """

    name: str  # server id，如 "fs"
    command: str  # 可执行文件
    args: tuple[str, ...] = ()  # argv
    cwd: str | None = None
    env: dict[str, str] | None = None
    connect_timeout: float = 20.0
    call_timeout: float = 30.0

def split_tool_name(name: str) -> tuple[str, str] | None:
    """把内部工具名拆成 (server, 原始工具名)。

    "mcp:fs:read_file" → ("fs", "read_file")。
    不是 `mcp:` 前缀 / 段数不足 / 有空段 → None（**不猜、不猜错**）。
    """
    if not name.startswith(MCP_PREFIX):
        return None
    parts = name.split(":", 2)
    if len(parts) < 3:
        return None
    server, tool = parts[1], parts[2]
    if not server or not tool:
        return None
    return server, tool

class McpRegistry:
    """接入声明的真相源：有哪些 server、每个 server 暴露哪些工具、各自什么副作用等级。

    用法：
        registry = default_fs_registry(Path("./sandbox"))
        registry.side_effects()          # → 接入时声明的 SideEffect 列表
        registry.parse_tool_name("mcp:fs:read_file")  # → ("fs", "read_file")

    **只声明，不连接** —— 注册表不碰任何进程；连接由 `executor.McpExecutor` 负责。
    """

    def __init__(
        self,
        servers: Sequence[McpServerSpec] = (),
        tools: Sequence[McpToolSpec] = (),
        *,
        sandbox_root: Path | None = None,
    ) -> None:
        self._servers: dict[str, McpServerSpec] = {}
        self._tools: dict[str, McpToolSpec] = {}
        # 沙箱根 = 快照记的地方 = server 真正写的地方（**同一个地方**，不靠人记）
        self.sandbox_root = (
            Path(sandbox_root).resolve() if sandbox_root is not None else None
        )
        for spec in servers:
            self.add_server(spec)
        for spec in tools:
            self.add_tool(spec)

    # ── 接入 ────────────────────────────────────────────────────────────────

    def add_server(self, spec: McpServerSpec) -> None:
        """接入一个 server。重复接入直接报错（不静默覆盖）。"""
        if not spec.name:
            raise ValueError("MCP server 必须有名字（接入声明不能是空名字）")
        if spec.name in self._servers:
            raise ValueError(
                f"MCP server {spec.name!r} 重复接入（注册表里已有同名 server）"
            )
        self._servers[spec.name] = spec

    def add_tool(self, spec: McpToolSpec) -> None:
        """接入一个工具。名字必须符合 `mcp:<server>:<tool>`（D-g）。

        校验两件事：
          1. 名字带 `mcp:` 前缀（防止与内置工具撞名）；
          2. 名字里的 server/tool 与 spec 字段一致（防止声明自相矛盾）。
        """
        if not spec.name.startswith(MCP_PREFIX):
            raise ValueError(
                f"MCP 工具名必须以 {MCP_PREFIX!r} 开头（RFC 14 D-g）：{spec.name!r}"
            )
        parsed = split_tool_name(spec.name)
        if parsed is None or parsed != (spec.server, spec.tool):
            raise ValueError(
                f"MCP 工具名 {spec.name!r} 与声明不一致："
                f"server={spec.server!r} tool={spec.tool!r}"
            )
        if spec.name in self._tools:
            raise ValueError(f"MCP 工具 {spec.name!r} 重复接入（注册表里已有同名工具）")
        self._tools[spec.name] = spec

    # ── 查询 ────────────────────────────────────────────────────────────────

    def server(self, name: str) -> McpServerSpec | None:
        """按名字取 server 声明；没接入 → None。"""
        return self._servers.get(name)

    def server_names(self) -> list[str]:
        """已接入的 server 名字。"""
        return list(self._servers)

    def tool(self, name: str) -> McpToolSpec | None:
        """按内部名取工具声明；**没声明 → None**（executor 据此拒绝执行）。"""
        return self._tools.get(name)

    def tool_names(self) -> list[str]:
        """已声明的工具内部名（`mcp:<server>:<tool>`）。"""
        return list(self._tools)

    def tools_of(self, server: str) -> list[McpToolSpec]:
        """某个 server 名下的全部工具声明。"""
        return [spec for spec in self._tools.values() if spec.server == server]

    def side_effects(self) -> tuple[SideEffect, ...]:
        """从工具声明派生的副作用列表（**接入时声明的等级，不是 server 自述**）。"""
        return tuple(
            SideEffect(
                tool=spec.name,
                scope=spec.scope,
                kind=spec.kind,
                reversibility=spec.risk,
            )
            for spec in self._tools.values()
        )

    def parse_tool_name(self, name: str) -> tuple[str, str] | None:
        """"mcp:fs:read_file" → ("fs", "read_file")；格式不对 → None。"""
        return split_tool_name(name)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"McpRegistry(servers={self.server_names()!r}, "
            f"tools={self.tool_names()!r})"
        )

# ── 接入样板 ──────────────────────────────────────────────────────────────────

def default_fs_registry(root: Path) -> McpRegistry:
    """把自带的 filesystem MCP server 接进来 —— **「接入时声明」的样板**。

    三件事都写死在这里：
      · server 怎么起（command / args / cwd / env）；
      · 允许哪三个工具（别的工具一律不认）；
      · 每个工具**我们**声明的副作用等级与影响范围。

    `root` 既是 filesystem server 的沙箱根（`--root`），
    也被登记成 `registry.sandbox_root` —— executor 默认就用它当 `sandbox_dir`，
    这样「快照记在哪」和「真写在哪」**默认就是同一个地方**（不靠人记）。
    """
    root = Path(root).resolve()
    project_root = Path(__file__).resolve().parents[2]

    server = McpServerSpec(
        name="fs",
        command=sys.executable,
        args=("-m", "intent_os.mcp.filesystem_server", "--root", str(root)),
        cwd=str(project_root),
        env={"PYTHONPATH": _python_path(project_root)},
    )

    scope = f"{root}/*"
    tools = (
        McpToolSpec(
            name="mcp:fs:read_file",
            server="fs",
            tool="read_file",
            description="读取沙盒内的一个文本文件",
            kind=SideEffectKind.READ,
            scope=scope,
            risk="easy",
        ),
        McpToolSpec(
            name="mcp:fs:list_dir",
            server="fs",
            tool="list_dir",
            description="列出沙盒内一个目录下的条目",
            kind=SideEffectKind.READ,
            scope=scope,
            risk="easy",
        ),
        McpToolSpec(
            name="mcp:fs:write_file",
            server="fs",
            tool="write_file",
            description="把文本写入沙盒内的一个文件（覆盖写）",
            kind=SideEffectKind.WRITE,
            scope=scope,
            risk="easy",
        ),
    )
    return McpRegistry(servers=(server,), tools=tools, sandbox_root=root)

def _python_path(project_root: Path) -> str:
    """把项目根拼到 PYTHONPATH 最前面 —— 让 server 子进程 import 得到 `intent_os`。"""
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(project_root)] + ([existing] if existing else [])
    return os.pathsep.join(parts)
