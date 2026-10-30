"""InlineExecutor — Phase 0 内嵌工具实现（测试用，零依赖）。

工具集：write_file / read_file / list_dir / run_python
沙箱：通过构造时的 sandbox_dir 强制
输出格式：English `exit_code:` 系列（与 Phase 0 verifier 一致）

PyPilotExecutor 是生产实现；本类用于单元测试与离线环境。

Phase 2.1：写文件与跑脚本前自动捕获快照，支持 Saga 补偿。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ..compiler.plan import PlanNode
from ..types import NodeResult
from .snapshot import SnapshotStore

class InlineExecutor:
    """Phase 0 内嵌 executor（不依赖 PyPilot）。"""

    def __init__(self, sandbox_dir: Path, run_timeout: int = 30) -> None:
        self.sandbox_dir = Path(sandbox_dir).resolve()
        self.run_timeout = run_timeout
        self.snapshots = SnapshotStore(self.sandbox_dir)

    def execute(self, node: PlanNode) -> NodeResult:
        if node.tool == "write_file":
            return self._write_file(node)
        if node.tool == "read_file":
            return self._read_file(node.args)
        if node.tool == "list_dir":
            return self._list_dir(node.args)
        if node.tool == "run_python":
            return self._run_python(node)
        return NodeResult(ok=False, output="", error=f"unknown tool: {node.tool!r}")

    # ── 补偿（Phase 2.1）────────────────────────────────────────────────────

    def reset_snapshots(self) -> None:
        """清空快照账本（runtime 在每次 run 开始时调用）。"""
        self.snapshots.reset()

    def compensate(self, node: PlanNode) -> NodeResult:
        """按执行前的快照还原该节点造成的改动。"""
        if not self.snapshots.has_snapshots(node.name):
            # 这个节点没碰过文件 —— 无需补偿（不是失败）
            return NodeResult(ok=True, output="无副作用需补偿", error=None)
        ok, msg = self.snapshots.restore_node(node.name)
        return NodeResult(ok=ok, output=msg, error=None if ok else msg)

    # ── 沙箱路径 ───────────────────────────────────────────────────────────────

    def _safe_path(self, rel: str) -> Path:
        s = rel.strip().strip("\"'`").lstrip("/")
        if s.startswith("./"):
            s = s[2:]
        path = (self.sandbox_dir / s).resolve()
        try:
            path.relative_to(self.sandbox_dir)
        except ValueError as e:
            raise ValueError(f"path escapes sandbox: {rel!r}") from e
        return path

    # ── 4 个工具 ───────────────────────────────────────────────────────────────

    def _write_file(self, node: PlanNode) -> NodeResult:
        args = node.args
        path = self._safe_path(args["path"])
        # 补偿点：写之前先存快照（文件原本不存在也会记下来）
        self.snapshots.capture(node.name, args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return NodeResult(
            ok=True,
            output=f"wrote {len(args['content'])} chars to {args['path']}",
        )

    def _read_file(self, args: dict) -> NodeResult:
        path = self._safe_path(args["path"])
        if not path.exists():
            return NodeResult(ok=False, output="", error=f"file not found: {args['path']}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return NodeResult(ok=False, output="", error=f"not utf-8: {args['path']}")
        return NodeResult(ok=True, output=content)

    def _list_dir(self, args: dict) -> NodeResult:
        path = self._safe_path(args.get("path", "."))
        if not path.exists():
            return NodeResult(ok=False, output="", error=f"path not found: {path}")
        if path.is_file():
            return NodeResult(ok=False, output="", error=f"is file not dir: {path.name}")
        entries = []
        for c in sorted(path.iterdir()):
            kind = "dir" if c.is_dir() else "file"
            size = f"{c.stat().st_size}B" if c.is_file() else ""
            entries.append(f"{kind}\t{c.name}\t{size}")
        if not entries:
            return NodeResult(ok=True, output=f"empty dir: {path.name}")
        return NodeResult(ok=True, output="\n".join(entries))

    def _run_python(self, node: PlanNode) -> NodeResult:
        args = node.args
        path = self._safe_path(args["path"])
        if not path.exists():
            return NodeResult(ok=False, output="", error=f"file not found: {args['path']}")
        # 补偿点：脚本运行可能改写自身或同目录文件，先存快照
        self.snapshots.capture(node.name, args["path"])
        try:
            cp = subprocess.run(
                [sys.executable, "-X", "utf8", str(path)],
                cwd=str(self.sandbox_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.run_timeout,
            )
            output = f"exit_code: {cp.returncode}\nstdout: {cp.stdout}\nstderr: {cp.stderr}"
            ok = cp.returncode == 0
            return NodeResult(
                ok=ok,
                output=output,
                error=None if ok else f"exit {cp.returncode}",
            )
        except subprocess.TimeoutExpired:
            return NodeResult(ok=False, output="", error=f"timeout {self.run_timeout}s")
