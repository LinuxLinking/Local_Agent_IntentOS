"""L2 快照存储 —— 补偿动作（Phase 2.1 · Step 2.1）的基础。

补偿的前提是「能回到执行前的样子」。所以每个有副作用的节点在执行前，
先把可能被改动的文件存一份快照；后续节点失败时，按快照逆向还原。

设计取舍：
- 快照落在**沙箱内**的 `.snapshot/` 目录
  → 路径解析与沙箱边界完全一致，不会越界
  → 跟着沙箱一起被 `.gitignore` 忽略（`sandbox/*`）
- 账本只活在**内存**里，随一次 run 失效
  → 补偿是「一次 run 内的事」；跨 run 还原没有意义，反而危险
  → 重放旧审计记录时不会误触发还原
- 文件执行前**不存在**也要记录
  → 还原语义是「回到执行前」，不是「回到有值」；
    执行前不存在 → 还原 = 删掉它
"""
from __future__ import annotations

import shutil
from pathlib import Path

class SnapshotStore:
    """节点级文件快照账本。

    账本结构：{node_name: {相对路径: 备份文件路径 | None}}
    备份文件为 None 表示「执行前该文件不存在」。
    """

    def __init__(self, sandbox_dir: Path, root: Path | None = None) -> None:
        self.sandbox_dir = Path(sandbox_dir).resolve()
        self.root = Path(root).resolve() if root else self.sandbox_dir / ".snapshot"
        self._ledger: dict[str, dict[str, Path | None]] = {}

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """清空账本与快照目录（一次 run 开始时调用）。"""
        self._ledger.clear()
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

    # ── 捕获 ────────────────────────────────────────────────────────────────

    def capture(self, node_name: str, rel_path: str) -> None:
        """在执行前捕获文件当前状态。

        同一节点同一路径只捕获第一次 —— 补偿要回到**最早**的状态，
        不能被后续的重复捕获覆盖。
        """
        target = self._safe_path(rel_path)
        entries = self._ledger.setdefault(node_name, {})
        if rel_path in entries:
            return
        if not target.exists():
            entries[rel_path] = None
            return
        backup = self.root / node_name / _flat_name(rel_path)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        entries[rel_path] = backup

    # ── 还原 ────────────────────────────────────────────────────────────────

    def restore(self, node_name: str, rel_path: str) -> tuple[bool, str]:
        """还原单个文件。返回 (成功?, 消息)。"""
        entries = self._ledger.get(node_name)
        if entries is None or rel_path not in entries:
            return False, f"没有 {node_name!r} 对 {rel_path!r} 的快照"
        backup = entries[rel_path]
        target = self._safe_path(rel_path)
        try:
            if backup is None:
                # 执行前不存在 → 还原 = 删掉它
                if target.exists():
                    target.unlink()
                return True, f"{rel_path} 已删除（还原到执行前的「不存在」）"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
            return True, f"{rel_path} 已还原"
        except Exception as e:
            return False, f"{rel_path} 还原失败：{e}"

    def restore_node(self, node_name: str) -> tuple[bool, str]:
        """还原某节点的全部快照。返回 (成功?, 汇总消息)。"""
        entries = self._ledger.get(node_name)
        if not entries:
            return False, f"节点 {node_name!r} 没有可还原的快照"
        ok = True
        messages: list[str] = []
        for rel_path in sorted(entries):
            r_ok, msg = self.restore(node_name, rel_path)
            ok = ok and r_ok
            messages.append(msg)
        return ok, "; ".join(messages)

    # ── 查询 ────────────────────────────────────────────────────────────────

    def snapshot_paths(self, node_name: str) -> list[str]:
        """某节点已捕获的路径（相对沙箱）。"""
        return sorted(self._ledger.get(node_name, {}))

    def has_snapshots(self, node_name: str) -> bool:
        return bool(self._ledger.get(node_name))

    # ── 沙箱边界 ────────────────────────────────────────────────────────────

    def _safe_path(self, rel: str) -> Path:
        """沙箱边界校验 —— 与 InlineExecutor 的规则保持一致。"""
        s = rel.strip().strip("\"'`").lstrip("/")
        if s.startswith("./"):
            s = s[2:]
        path = (self.sandbox_dir / s).resolve()
        try:
            path.relative_to(self.sandbox_dir)
        except ValueError as e:
            raise ValueError(f"path escapes sandbox: {rel!r}") from e
        return path

def _flat_name(rel_path: str) -> str:
    """把相对路径压成单个文件名（避免在快照目录里造出多层目录）。"""
    return rel_path.replace("\\", "__").replace("/", "__").replace(":", "_")
