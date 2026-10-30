"""L3 · 语义文件系统（Semantic FS）—— 长期记忆的物理层 + 索引层 + 版本层。

设计依据：
- design-foundation/01-concept-model.md §2 L3
  「长期记忆（Semantic FS）：带语义索引 / 遗忘策略 / 冲突消解 / 权限分区」
- design-foundation/04-open-questions.md Q7
  主推方向：**结构化事件流为底，向量为索引**
- design-foundation/06-open-conclusions-r1.md C13（4 层结构）

── 物理布局 ───────────────────────────────────────────────────────────────
SQLite 两张表 + 一个向量索引：

  memories        当前状态（由事件流物化出来的物化视图）
  memory_events   append-only 事件流 —— 永久留痕、可回放
  memories.vector 语义索引层的向量（归一化后的 float32）

「事件流为底，向量为索引」在这里落地成：**事件流是真相，memories 是物化**。
向量只是索引 —— 删掉向量列还能从事件流重建；反过来不行。

── 4 层能力的落点（C13）──────────────────────────────────────────────────
  L4 语义索引层 → memories.vector + 余弦检索
  L3 版本控制层 → 同 key 的版本链（supersedes），最后写胜出但保留历史
  L2 权限分区层 → scope 列 + visible_scopes 硬边界
  L1 物理存储层 → SQLite

  遗忘（TTL / 遗忘曲线 / 遗忘权）→ policy.py + 本文件的 expire() / forget()

── 冲突消解策略 ───────────────────────────────────────────────────────────
同一 `key` 再次写入 = 同一件事的新版本：**最后写胜出**，但旧版本不删，
只标记 `superseded` 并记事件 —— 冲突可回看、可仲裁，符合 Q7 的
「冲突消解走事件流的最后写胜出策略」。
"""
from __future__ import annotations

import array
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .embedder import Embedder, HashEmbedder, _normalize
from .policy import ForgettingPolicy

# 事件类型
EV_REMEMBER = "remember"
EV_SUPERSEDE = "supersede"
EV_FORGET = "forget"
EV_EXPIRE = "expire"

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_EXPIRED = "expired"

@dataclass(frozen=True)
class MemoryRecord:
    """一条记忆的当前状态。"""

    id: int
    key: str
    content: str
    scope: str
    importance: float
    created_at: float
    expires_at: float | None
    version: int
    supersedes: int | None
    status: str
    meta: dict[str, Any]

@dataclass(frozen=True)
class ScoredMemory:
    """召回结果：记忆 + 命中得分。"""

    record: MemoryRecord
    similarity: float
    score: float
    age_seconds: float

@dataclass(frozen=True)
class MemoryEvent:
    """事件流里的一条事件（append-only）。"""

    id: int
    ts: float
    event: str
    memory_id: int | None
    key: str
    detail: str

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    scope       TEXT    NOT NULL DEFAULT 'default',
    importance  REAL    NOT NULL DEFAULT 1.0,
    created_at  REAL    NOT NULL,
    expires_at  REAL,
    version     INTEGER NOT NULL DEFAULT 1,
    supersedes  INTEGER,
    status      TEXT    NOT NULL DEFAULT 'active',
    meta        TEXT    NOT NULL DEFAULT '{}',
    vector      BLOB
);
CREATE INDEX IF NOT EXISTS idx_memories_key    ON memories(key);
CREATE INDEX IF NOT EXISTS idx_memories_scope  ON memories(scope);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);

CREATE TABLE IF NOT EXISTS memory_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    event      TEXT NOT NULL,
    memory_id  INTEGER,
    key        TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS store_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

class MemoryStore:
    """语义文件系统：写入 / 召回 / 遗忘 / 版本 / 权限分区。

    用法：
        store = MemoryStore(":memory:", embedder=OllamaEmbedder())
        store.remember("用户偏好用 uv 而不是 pip", key="pref-uv", scope="user")
        hits = store.recall("怎么装依赖", limit=3)
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        embedder: Embedder | None = None,
        policy: ForgettingPolicy | None = None,
        visible_scopes: Sequence[str] | None = None,
    ) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.embedder: Embedder = embedder or HashEmbedder()
        self.policy = policy or ForgettingPolicy()
        # 权限分区的硬边界：设了就只能看见这些 scope（生产环境按租户/agent 隔离）
        self.visible_scopes = set(visible_scopes) if visible_scopes is not None else None
        try:
            self._bind_embedder()
        except Exception:
            # 构造失败必须把连接关掉 —— 否则文件被占住（Windows 上尤其明显）
            self.close()
            raise

    # ── embedder 一致性 ─────────────────────────────────────────────────────

    def _bind_embedder(self) -> None:
        """记录 / 校验向量来自同一个 embedder。

        不同模型的向量混在一起比相似度 = 纯胡说。宁可报错，不给假结果。
        """
        row = self.conn.execute(
            "SELECT v FROM store_meta WHERE k = 'embed_model'"
        ).fetchone()
        tag = getattr(self.embedder, "model", "unknown")
        if row is None:
            self.conn.execute(
                "INSERT INTO store_meta (k, v) VALUES ('embed_model', ?)", (tag,)
            )
            self.conn.commit()
        elif row["v"] != tag:
            raise ValueError(
                f"向量来自不同 embedder（库={row['v']!r}，当前={tag!r}），"
                "换模型必须重建记忆库，不能混检"
            )

    # ── 写入 ────────────────────────────────────────────────────────────────

    def remember(
        self,
        content: str,
        *,
        key: str | None = None,
        scope: str = "default",
        importance: float = 1.0,
        ttl_seconds: float | None = None,
        meta: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> MemoryRecord:
        """写入一条记忆。同 key 再写 = 新版本（旧版本转 superseded，不删）。"""
        text = (content or "").strip()
        if not text:
            raise ValueError("不能记空内容")
        ts = now if now is not None else time.time()
        mem_key = key or _auto_key(text)
        ttl = ttl_seconds if ttl_seconds is not None else self.policy.ttl_seconds
        expires_at = ts + ttl if ttl is not None else None

        previous = self._active_row(mem_key)
        version = 1
        supersedes: int | None = None
        if previous is not None:
            version = int(previous["version"]) + 1
            supersedes = int(previous["id"])
            self.conn.execute(
                "UPDATE memories SET status = ? WHERE id = ?",
                (STATUS_SUPERSEDED, previous["id"]),
            )
            self._log_event(
                EV_SUPERSEDE, previous["id"], mem_key, f"被 v{version} 取代"
            )

        vector = _normalize(list(self.embedder.embed(text)))
        cur = self.conn.execute(
            "INSERT INTO memories (key, content, scope, importance, created_at,"
            " expires_at, version, supersedes, status, meta, vector)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mem_key,
                text,
                scope,
                float(importance),
                ts,
                expires_at,
                version,
                supersedes,
                STATUS_ACTIVE,
                json.dumps(meta or {}, ensure_ascii=False),
                _pack_vector(vector),
            ),
        )
        self._log_event(EV_REMEMBER, int(cur.lastrowid or 0), mem_key, f"v{version}")
        self.conn.commit()
        record = self._row_to_record(self._row(cur.lastrowid))
        assert record is not None
        return record

    # ── 召回 ────────────────────────────────────────────────────────────────

    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        scopes: Iterable[str] | None = None,
        min_similarity: float = 0.0,
        now: float | None = None,
    ) -> list[ScoredMemory]:
        """按语义相似度召回记忆。

        过滤链：scope 权限 → 过期 → 相似度下限 → 遗忘曲线打分 → 排序取前 N。
        """
        ts = now if now is not None else time.time()
        allowed = self._allowed_scopes(scopes)
        qvec = _normalize(list(self.embedder.embed(query)))

        rows = self.conn.execute(
            "SELECT * FROM memories WHERE status = ?", (STATUS_ACTIVE,)
        ).fetchall()

        hits: list[ScoredMemory] = []
        for row in rows:
            if allowed is not None and row["scope"] not in allowed:
                continue
            if self.policy.is_expired(row["expires_at"], ts):
                continue
            vec = _unpack_vector(row["vector"])
            sim = _dot(qvec, vec)
            if sim < min_similarity:
                continue
            age = max(0.0, ts - float(row["created_at"]))
            score = self.policy.score(
                similarity=sim, importance=float(row["importance"]), age_seconds=age
            )
            if score < self.policy.min_score:
                continue
            record = self._row_to_record(row)
            if record is None:
                continue
            hits.append(
                ScoredMemory(record=record, similarity=sim, score=score, age_seconds=age)
            )

        hits.sort(key=lambda h: (-h.score, -h.similarity, h.record.id))
        return hits[:limit]

    # ── 遗忘 ────────────────────────────────────────────────────────────────

    def expire(self, now: float | None = None) -> int:
        """TTL 过期清理：把已过期的 active 记录标记为 expired（物理删除另见 purge）。"""
        ts = now if now is not None else time.time()
        rows = self.conn.execute(
            "SELECT id, key, expires_at FROM memories WHERE status = ? AND expires_at IS NOT NULL",
            (STATUS_ACTIVE,),
        ).fetchall()
        count = 0
        for row in rows:
            if float(row["expires_at"]) <= ts:
                self.conn.execute(
                    "UPDATE memories SET status = ? WHERE id = ?",
                    (STATUS_EXPIRED, int(row["id"])),
                )
                self._log_event(EV_EXPIRE, int(row["id"]), row["key"], "TTL 到期")
                count += 1
        if count:
            self.conn.commit()
        return count

    def forget(
        self,
        *,
        key: str | None = None,
        memory_id: int | None = None,
        cascade: bool = True,
    ) -> int:
        """Right to be Forgotten —— 显式删除（物理删除，不是标记）。

        默认连带删除同 key 的历史版本（cascade=True）——
        「忘掉一件事」不该只忘掉最新版、留着旧版还能翻出来。
        """
        if key is None and memory_id is None:
            raise ValueError("forget 需要 key 或 memory_id")
        if key is not None:
            rows = self.conn.execute(
                "SELECT id, key FROM memories WHERE key = ?", (key,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, key FROM memories WHERE id = ?", (int(memory_id),)
            ).fetchall()
        ids = [int(r["id"]) for r in rows]
        if not ids:
            return 0
        if not cascade and key is not None:
            # 只删当前 active 版本
            active = self.conn.execute(
                "SELECT id FROM memories WHERE key = ? AND status = ?",
                (key, STATUS_ACTIVE),
            ).fetchall()
            ids = [int(r["id"]) for r in active] or ids[:1]
        for mid in ids:
            self.conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
            self._log_event(EV_FORGET, mid, key or "", "显式遗忘")
        self.conn.commit()
        return len(ids)

    def purge_expired(self) -> int:
        """物理清理已过期记录（标记之外还占着地方的时候用）。"""
        cur = self.conn.execute(
            "DELETE FROM memories WHERE status = ?", (STATUS_EXPIRED,)
        )
        self.conn.commit()
        return cur.rowcount or 0

    # ── 查询 ────────────────────────────────────────────────────────────────

    def get(self, key: str) -> MemoryRecord | None:
        """按 key 取当前 active 版本。"""
        row = self._active_row(key)
        return self._row_to_record(row) if row is not None else None

    def history(self, key: str) -> list[MemoryRecord]:
        """同 key 的全部版本（按版本号升序）—— 冲突消解的可回看性。"""
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE key = ? ORDER BY version ASC", (key,)
        ).fetchall()
        return [r for r in (self._row_to_record(row) for row in rows) if r is not None]

    def events(self, limit: int = 0) -> list[MemoryEvent]:
        """事件流（append-only，按时间升序）。"""
        sql = "SELECT * FROM memory_events ORDER BY id ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql).fetchall()
        return [
            MemoryEvent(
                id=int(r["id"]),
                ts=float(r["ts"]),
                event=r["event"],
                memory_id=r["memory_id"],
                key=r["key"],
                detail=r["detail"],
            )
            for r in rows
        ]

    def count(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
        )

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _allowed_scopes(self, scopes: Iterable[str] | None) -> set[str] | None:
        """算最终可见的 scope 集合。

        None = 不限（单租户开发模式）。
        生产环境务必设 visible_scopes —— 那是硬边界。
        """
        if self.visible_scopes is None:
            return set(scopes) if scopes is not None else None
        allowed = set(self.visible_scopes)
        if scopes is not None:
            allowed &= set(scopes)
        return allowed

    def _active_row(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM memories WHERE key = ? AND status = ?"
            " ORDER BY version DESC LIMIT 1",
            (key, STATUS_ACTIVE),
        ).fetchone()

    def _row(self, row_id: int | None) -> sqlite3.Row | None:
        if row_id is None:
            return None
        return self.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (int(row_id),)
        ).fetchone()

    def _row_to_record(self, row: sqlite3.Row | None) -> MemoryRecord | None:
        if row is None:
            return None
        try:
            meta = json.loads(row["meta"]) if row["meta"] else {}
        except json.JSONDecodeError:
            meta = {}
        return MemoryRecord(
            id=int(row["id"]),
            key=row["key"],
            content=row["content"],
            scope=row["scope"],
            importance=float(row["importance"]),
            created_at=float(row["created_at"]),
            expires_at=None if row["expires_at"] is None else float(row["expires_at"]),
            version=int(row["version"]),
            supersedes=None if row["supersedes"] is None else int(row["supersedes"]),
            status=row["status"],
            meta=meta if isinstance(meta, dict) else {},
        )

    def _log_event(
        self, event: str, memory_id: int | None, key: str, detail: str = ""
    ) -> None:
        self.conn.execute(
            "INSERT INTO memory_events (ts, event, memory_id, key, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (time.time(), event, memory_id, key, detail),
        )

def _auto_key(content: str) -> str:
    """没给 key 时按内容摘要生成一个（同名内容 → 同 key → 走版本链）。"""
    import hashlib

    return "auto-" + hashlib.blake2b(
        content.strip().encode("utf-8"), digest_size=8
    ).hexdigest()

def _pack_vector(vec: Sequence[float]) -> bytes:
    return array.array("f", [float(v) for v in vec]).tobytes()

def _unpack_vector(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    arr = array.array("f")
    arr.frombytes(blob)
    return list(arr)

def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))
