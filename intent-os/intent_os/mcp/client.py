"""MCP 客户端（stdio transport）—— 让 Intent OS 把一个 MCP server 当成 I/O 总线用。

设计依据：`design-foundation/14-mcp-integration-rfc.md`

三条红线（都在这里守住）：
  1. **MCP 工具是副作用** —— 副作用等级在**接入时声明**（`McpToolSpec`），
     不采信 server 自述（被约束方不能定义约束）。
  2. **状态通道不经过 MCP** —— 本模块只认 server 声明的工具；
     Intent OS 的外置状态（`StateStore`）**绝不**以工具形式暴露（D2）。
  3. **不许撒谎** —— 连不上 / 超时 / 工具报错 → 如实返回错误，**不假装成功**。

实现要点：
  · 用官方 SDK（`mcp==2.3.0`）的 `stdio_client` + `ClientSession` —— 证明协议兼容，不是自证。
  · **持久会话**：server 进程只起一次，会话跨多次调用复用
    （每次调用都重起进程会让启动开销盖过真实成本）。
  · **超时**：调用级超时用 SDK 的 `read_timeout_seconds`；连接级超时自己兜。
  · **清理**：SDK 在会话关闭时「关 stdin → 宽限 → **杀整个进程树**」——
    这正是 2026-10-28 那个 1.05 亿 token 事故要求的纪律（不留残余进程）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Sequence

from ..compiler.intent import SideEffectKind

@dataclass(frozen=True)
class McpToolSpec:
    """一个 MCP 工具的**接入声明**。

    `kind` 是**我们**声明的副作用等级，不是 server 自述的 ——
    server 的 `tools/list` 只有名字/描述/inputSchema，没有副作用语义。
    理由：不能让**被约束方**定义约束（和「模型自己声明前提」一样不可信）。
    """

    name: str  # 内部名：mcp:<server>:<tool>
    server: str
    tool: str  # server 侧原名
    description: str = ""
    kind: SideEffectKind = SideEffectKind.READ
    scope: str = ""  # 影响范围（给审批人看，如 "sandbox/*"）
    risk: str = "easy"  # easy / medium / hard（审批粒度）

@dataclass
class McpCallResult:
    """一次 MCP 调用的结果。**失败必须如实返回 ok=False。**"""

    ok: bool
    content: str = ""
    error: str = ""
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "content": self.content,
            "error": self.error,
            "elapsed_s": round(self.elapsed_s, 3),
        }

class McpClient:
    """一个 MCP server 的连接（子进程 + stdio + 持久会话）。

    用法：
        with McpClient("fs", sys.executable, ["-m", "intent_os.mcp.filesystem_server",
                                             "--root", sandbox]) as c:
            tools = c.list_tools()
            r = c.call_tool("read_file", {"path": "a.txt"})
    """

    def __init__(
        self,
        server_name: str,
        command: str,
        args: Sequence[str] = (),
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        connect_timeout: float = 20.0,
        call_timeout: float = 30.0,
    ) -> None:
        self.server_name = server_name
        self.command = command
        self.args = list(args)
        self.cwd = cwd
        self.env = env
        self.connect_timeout = connect_timeout
        self.call_timeout = call_timeout

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self._open_error: str = ""

    # ── 会话生命周期 ────────────────────────────────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, name=f"mcp-{self.server_name}", daemon=True
            )
            self._thread.start()
        return self._loop

    def _submit(self, coro, timeout: float):
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)

    async def _open_session(self) -> Any:
        """在循环线程里开一个**持久**会话（server 进程只起一次）。"""
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            cwd=self.cwd,
            env={**os.environ, **(self.env or {})},
        )
        # errlog 走 stderr —— **不能污染 stdout**（stdout 是协议流）
        read, write = await self._stack.enter_async_context(
            stdio_client(params, errlog=sys.stderr)
        )
        self._session = await self._stack.enter_async_context(
            ClientSession(read, write)
        )
        await self._session.initialize()
        return self._session

    async def _session_or_open(self) -> Any:
        if self._session is not None:
            return self._session
        if self._open_error:
            raise RuntimeError(self._open_error)
        try:
            return await self._open_session()
        except Exception as e:
            self._open_error = f"{type(e).__name__}: {e}"
            raise RuntimeError(self._open_error) from e

    # ── 能力 ────────────────────────────────────────────────────────────────

    def list_tools(self) -> list[dict[str, Any]]:
        """列出 server 提供的工具（原始描述；副作用等级另由接入方声明）。"""
        try:
            return self._submit(self._list_tools_async(), self.connect_timeout)
        except Exception as e:
            raise RuntimeError(f"MCP tools/list 失败：{e}") from e

    async def _list_tools_async(self) -> list[dict[str, Any]]:
        session = await self._session_or_open()
        result = await session.list_tools()
        out: list[dict[str, Any]] = []
        for t in result.tools:
            out.append(
                {
                    "name": t.name,
                    "description": (t.description or "").strip(),
                    "input_schema": getattr(t, "input_schema", None) or {},
                }
            )
        return out

    def call_tool(self, tool: str, arguments: dict[str, Any]) -> McpCallResult:
        """调用一个工具。**失败如实返回 ok=False。**"""
        t0 = time.monotonic()
        try:
            content = self._submit(
                self._call_tool_async(tool, arguments), self.call_timeout + 5
            )
        except Exception as e:
            return McpCallResult(
                ok=False, error=f"{type(e).__name__}: {e}", elapsed_s=time.monotonic() - t0
            )
        return McpCallResult(ok=True, content=content, elapsed_s=time.monotonic() - t0)

    async def _call_tool_async(self, tool: str, arguments: dict[str, Any]) -> str:
        session = await self._session_or_open()
        result = await session.call_tool(
            tool, arguments, read_timeout_seconds=self.call_timeout
        )
        # mcp 2.x：字段是 is_error（不是 isError）
        if getattr(result, "is_error", False):
            detail = "；".join(
                str(getattr(c, "text", c)) for c in (result.content or [])
            )
            raise RuntimeError(f"MCP 工具报错：{detail or '(无详情)'}")
        parts = []
        for c in result.content or []:
            text = getattr(c, "text", None)
            parts.append(text if text is not None else str(c))
        return "\n".join(parts)

    # ── 清理 ────────────────────────────────────────────────────────────────

    async def _aclose(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()  # → SDK 关 stdin → 宽限 → 杀整个进程树
            except Exception:
                pass
            self._stack = None
        self._session = None

    def close(self) -> None:
        """关会话 + 停循环。**必须调用**（否则 server 进程会留着）。"""
        loop = self._loop
        if loop is None:
            return
        try:
            self._submit(self._aclose(), timeout=15)
        except Exception:
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)
        except Exception:
            pass
        self._loop = None
        self._thread = None

    def __enter__(self) -> "McpClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
