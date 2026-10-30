"""PyPilotExecutor — 调用 pypilot.tools.builtin.call_tool 执行节点。

Phase 1 集成（2026-10-19）：
- 直接用 PyPilot 已验证的工具层（call_tool / TOOL_REGISTRY / safe_path）
- PyPilot 已经处理 4 个工具 + 沙箱边界 + 错误兜底
- 我们的工作：把 PlanNode 翻译成 (tool_name, args)，解析 PyPilot 返回的字符串

PyPilot 输出格式（与 Phase 0 不同）：
  write_file: "已写入 {name}（{n} 字符，路径: {rel}）"
  run_python: "退出码: {n}\\n--- stdout ---\\n{out}\\n--- stderr ---\\n{err}"
  read_file: 返回文件内容（或错误字符串）
  list_dir:  "类型\\t名字\\t大小" 列表

PyPilot 的 call_tool 出错时返回错误字符串（不抛异常），所以我们用前缀匹配检测。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..compiler.plan import PlanNode
from ..types import NodeResult
from .snapshot import SnapshotStore

class PyPilotExecutor:
    """Phase 1 默认 executor：调用 pypilot.tools.builtin.call_tool。

    用法：
        from intent_os.executor import PyPilotExecutor
        runtime = IntentOSRuntime(..., executor=PyPilotExecutor())

    PyPilot 工具的沙箱由 pypilot.config.SANDBOX_DIR 控制；IntentOSRuntime 的
    sandbox_dir 在 PyPilot 路径下被忽略（PyPilot 强绑定自己的沙箱）。
    """

    # PyPilot call_tool 错误前缀（按 builtin.py:124-141）
    _ERROR_PREFIXES = (
        "未知工具",
        "参数错误",
        "工具执行出错",
        "路径越界",
        "执行超时",
        "文件不存在",
        "只支持执行",
        "这是目录不是文件",
        "这是文件不是目录",
        "[二进制文件",
        "空目录",  # 这其实是 ok 的，但格式特殊
        "路径不存在",
    )

    # 退出码正则（PyPilot 格式 "退出码: N"）
    _EXIT_CODE_RE = re.compile(r"退出码:\s*(-?\d+)")

    def __init__(self) -> None:
        # 延迟导入 PyPilot，避免 Phase 0 路径被污染
        from pypilot.tools.builtin import TOOL_REGISTRY, call_tool
        from pypilot.config import SANDBOX_DIR

        self._call_tool = call_tool
        self._registry = TOOL_REGISTRY
        # Phase 2.1：PyPilot 的工具层没有 undo 语义，补偿靠我们自己的快照
        self.snapshots = SnapshotStore(Path(SANDBOX_DIR))

    # ── 补偿（Phase 2.1）────────────────────────────────────────────────────

    def reset_snapshots(self) -> None:
        """清空快照账本（runtime 在每次 run 开始时调用）。"""
        self.snapshots.reset()

    def compensate(self, node: PlanNode) -> NodeResult:
        """按执行前的快照还原该节点造成的改动。

        ⚠ PyPilot 的工具层本身没有事务语义 —— 这里能还原的只有
        **沙箱内的文件改动**。真发到外部世界的副作用（发邮件、发货、
        调第三方 API）**无法还原**，那种情况必须靠上层审批拦住，
        不能指望补偿兜底。
        """
        if not self.snapshots.has_snapshots(node.name):
            return NodeResult(ok=True, output="无副作用需补偿", error=None)
        ok, msg = self.snapshots.restore_node(node.name)
        return NodeResult(ok=ok, output=msg, error=None if ok else msg)

    def execute(self, node: PlanNode) -> NodeResult:
        if node.tool not in self._registry:
            return NodeResult(
                ok=False,
                output="",
                error=(
                    f"unknown tool: {node.tool!r} "
                    f"(PyPilot has: {sorted(self._registry)})"
                ),
            )
        # 补偿点：写文件 / 跑脚本前先存快照
        if node.tool in {"write_file", "run_python"}:
            rel = node.args.get("path")
            if isinstance(rel, str):
                try:
                    self.snapshots.capture(node.name, rel)
                except ValueError:
                    # 越界路径让 call_tool 自己报错，这里不抢它的台词
                    pass
        # PyPilot 的 call_tool 接受 dict 或 str
        raw = self._call_tool(node.tool, node.args)
        ok, error = self._parse_result(node.tool, raw)
        return NodeResult(ok=ok, output=raw, error=error)

    def _parse_result(self, tool: str, raw: str) -> tuple[bool, str | None]:
        # 错误前缀检测
        for prefix in self._ERROR_PREFIXES:
            if raw.startswith(prefix):
                # 特殊："空目录" 实际是 ok（PyPilot 返回空目录列表）
                if prefix == "空目录":
                    continue
                return False, raw
        # run_python 的退出码检测
        if tool == "run_python":
            m = self._EXIT_CODE_RE.search(raw)
            if m and int(m.group(1)) != 0:
                return False, f"exit {m.group(1)}"
        return True, None
