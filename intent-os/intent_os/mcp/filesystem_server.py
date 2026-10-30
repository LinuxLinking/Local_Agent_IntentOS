"""最小 filesystem MCP server（官方 SDK · FastMCP）。

设计依据：`design-foundation/14-mcp-integration-rfc.md` D-b
—— 自带一个最小 server，而不是接第三方 npm 包：离线可跑、测试可复现、
工具集一眼看穿。**但必须用官方 SDK 写**（否则又变成自证）。

它跑在**子进程**里（stdio transport）—— 对应 OS 比喻里的「用户态设备驱动」。

⚠ **沙盒边界**：所有路径都被限制在 `--root` 指定的目录内。
   这是 L1 审批之外的**第二道边界**（纵深防御）：
   即使 L1 放行了，模型也不能写到沙盒外面去。
   越界 → 如实报错，**不静默改写路径**。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer  # mcp 2.x：FastMCP 已改名为 MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# **本模块刻意不声明工具的副作用等级。**
#
# RFC 14 D-d：副作用等级**接入时声明**，不采信 server 自述 ——
# 让被约束方（server）自己定义约束，跟「让模型自己声明前提」一样不可信。
# 等级写在 `intent_os/mcp/registry.py` 的接入清单里（那是 Intent OS 侧的真相源）。

def build_server(root: Path) -> MCPServer:
    """建一个只认沙盒内路径的 filesystem MCP server。"""
    server = MCPServer("intent-os-filesystem")
    root = root.resolve()

    def _safe(rel: str) -> Path:
        """把相对路径解析到沙盒内；越界就报错（不静默改写）。

        抛 `ToolError` 而不是 `ValueError` —— SDK 只把 `ToolError` 的 message
        **原样带回协议**（`is_error=True` + content 里就是这句话）；
        其它异常会被当成「崩溃」，客户端只能看到一句
        `Error executing tool <name>`，**真实原因就丢了**（不撒谎）。
        """
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise ToolError(f"路径越界（沙盒限定在 {root}）：{rel}") from None
        return target

    @server.tool()
    def read_file(path: str) -> str:
        """读取沙盒内的一个文本文件。"""
        p = _safe(path)
        if not p.exists():
            raise ToolError(f"文件不存在：{path}")
        return p.read_text(encoding="utf-8", errors="replace")

    @server.tool()
    def list_dir(path: str = ".") -> list[str]:
        """列出沙盒内一个目录下的条目。"""
        p = _safe(path)
        if not p.exists():
            raise ToolError(f"目录不存在：{path}")
        if not p.is_dir():
            raise ToolError(f"不是目录：{path}")
        return sorted(x.name for x in p.iterdir())

    @server.tool()
    def write_file(path: str, content: str) -> str:
        """把文本写入沙盒内的一个文件（覆盖写）。"""
        p = _safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已写入 {p.relative_to(root)}（{len(content)} 字符）"

    return server

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="最小 filesystem MCP server（stdio）")
    ap.add_argument("--root", default=".", help="沙盒根目录（所有路径限定在此内）")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    server = build_server(root)
    # stdio transport：走 stdin/stdout 的 JSON-RPC —— 必须**不能**往 stdout 打别的
    # （任何 print 都会污染协议流）。日志只能走 stderr。
    print(f"[fs-mcp] 沙盒根目录：{root}", file=sys.stderr)
    server.run(transport="stdio")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
