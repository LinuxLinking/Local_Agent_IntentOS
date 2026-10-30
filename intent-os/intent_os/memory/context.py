"""L3 · 上下文虚拟内存 —— 常驻集 / 工作集 / 换入 / 换出 / 缺页 / Checkpoint。

设计依据：
- design-foundation/06-open-conclusions-r1.md **C2**（已收敛）
  「OS 负责『换入 / 换出 / 缺页中断』，模型负责『哪些语义重要』的打分」
  「原型 L3 必须实现『换入 / 换出 / Checkpoint』三件套；
    不实现『重要性打分接口』也算可接受降级」
- design-foundation/01-concept-model.md §2 L3
  常驻集（系统提示 / 核心 Skill / 工具摘要）·
  换入集（当前 Plan 的相关上下文 / 相关 RAG 文档）·
  长期记忆（跨会话语义存储）
- 跨层共识 #4「上下文是有限资源 —— 任何放进上下文的东西都要回答『为什么现在需要』」

── OS 类比 ─────────────────────────────────────────────────────────────────
  常驻集 resident set   内核常驻页 —— pinned，永不换出
  工作集 working set    当前占用的上下文窗口 —— 受预算约束
  换入 swap in         缺页时从长期记忆把内容拉进工作集
  换出 swap out        工作集超预算，把最不重要的挤出去
  缺页 page fault      访问一个不在工作集里的条目 = 缺页中断
  Checkpoint           把当前状态存下来，之后能接着跑（可暂停 / 可恢复）

── 关于「重要性打分接口」的降级 ───────────────────────────────────────────
C2 的理想形态是：模型提供语义重要性打分，OS 只管调度。
本原型**不实现模型打分接口**，走 C2 明确允许的降级路径：

    重要性 = 条目自带的重要度 × 时间衰减

打分是**确定性**的（不额外调模型、不改延迟、可单测）。
代价：打分不反映「当前任务是否真的需要这条」——
那需要模型侧的语义判断，属于 Phase 3.4 之后的深化方向。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .policy import ForgettingPolicy

# 换入来源
ORIGIN_RESIDENT = "resident"
ORIGIN_SWAP_IN = "swap_in"

CHECKPOINT_VERSION = 1

def estimate_tokens(text: str) -> int:
    """token 估算：CJK 约 1 字 1 token，ASCII 约 4 字符 1 token。

    不是精确分词，但足够做**预算约束**——
    上下文管理的目标是「别把窗口撑爆」，不需要精确到 token。

    ASCII 那一段**向上取整**：预算是护栏，宁可略微高估，
    也不能因为四舍五入把窗口撑爆。
    """
    if not text:
        return 0
    cjk = 0
    ascii_chars = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f":
            cjk += 1
        elif ch.isascii() and not ch.isspace():
            ascii_chars += 1
    return max(1, cjk + (ascii_chars + 3) // 4)

@dataclass
class ContextItem:
    """上下文里的一条内容。"""

    key: str
    content: str
    tokens: int
    importance: float
    origin: str
    last_access: float
    hit_count: int = 0
    pinned: bool = False  # 常驻集：永不换出

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "content": self.content,
            "tokens": self.tokens,
            "importance": self.importance,
            "origin": self.origin,
            "last_access": self.last_access,
            "hit_count": self.hit_count,
            "pinned": self.pinned,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ContextItem":
        return cls(
            key=d["key"],
            content=d["content"],
            tokens=int(d["tokens"]),
            importance=float(d["importance"]),
            origin=d.get("origin", ORIGIN_SWAP_IN),
            last_access=float(d.get("last_access", 0.0)),
            hit_count=int(d.get("hit_count", 0)),
            pinned=bool(d.get("pinned", False)),
        )

@dataclass
class ContextManager:
    """上下文虚拟内存管理器。

    用法：
        cm = ContextManager(store, budget_tokens=2048)
        cm.mount_resident("system-prompt", "你是一个 Intent 编译器……")
        item = cm.access("pref-uv")      # 不在工作集 → 缺页 → 从 store 换入
        snap = cm.checkpoint()            # 存个快照
        cm.restore(snap)                  # 恢复继续
    """

    store: Any = None  # MemoryStore（可空：没有长期记忆就换不进来）
    budget_tokens: int = 2048
    min_importance: float = 0.0
    policy: ForgettingPolicy = field(default_factory=ForgettingPolicy)
    _items: dict[str, ContextItem] = field(default_factory=dict, repr=False)
    counters: dict[str, int] = field(
        default_factory=lambda: {
            "access": 0,
            "hit": 0,
            "miss": 0,
            "swap_in": 0,
            "swap_out": 0,
            "evict_failed": 0,
        },
        repr=False,
    )

    # ── 常驻集 ──────────────────────────────────────────────────────────────

    def mount_resident(self, key: str, content: str, importance: float = 1.0) -> ContextItem:
        """挂载常驻条目（系统提示 / 核心 Skill / 工具摘要）。

        常驻条目**永不换出**，也不受预算驱逐 —— 它们是「内核常驻页」。
        """
        item = ContextItem(
            key=key,
            content=content,
            tokens=estimate_tokens(content),
            importance=importance,
            origin=ORIGIN_RESIDENT,
            last_access=time.time(),
            pinned=True,
        )
        self._items[key] = item
        return item

    @property
    def resident_items(self) -> list[ContextItem]:
        return [i for i in self._items.values() if i.pinned]

    # ── 工作集 ──────────────────────────────────────────────────────────────

    @property
    def working_items(self) -> list[ContextItem]:
        return [i for i in self._items.values() if not i.pinned]

    def working_tokens(self) -> int:
        """工作集占用（不含常驻集 —— 常驻不占可分配预算）。"""
        return sum(i.tokens for i in self.working_items)

    def total_tokens(self) -> int:
        return sum(i.tokens for i in self._items.values())

    def is_resident(self, key: str) -> bool:
        item = self._items.get(key)
        return item is not None and item.pinned

    # ── 访问与缺页 ──────────────────────────────────────────────────────────

    def access(self, key: str, now: float | None = None) -> ContextItem | None:
        """访问一条上下文内容。

        在工作集里 → 命中；不在 → **缺页中断** → 从长期记忆换入。
        换入失败（长期记忆里也没有）→ 返回 None。
        """
        ts = now if now is not None else time.time()
        self.counters["access"] += 1
        item = self._items.get(key)
        if item is not None:
            self.counters["hit"] += 1
            item.last_access = ts
            item.hit_count += 1
            return item

        self.counters["miss"] += 1
        swapped = self.swap_in(key, now=ts)
        if swapped is None:
            return None
        self.counters["swap_in"] += 1
        swapped.hit_count += 1
        return swapped

    def swap_in(self, key: str, now: float | None = None) -> ContextItem | None:
        """换入：从长期记忆把一条内容拉进工作集。

        预算不够时先换出几条（腾地方），再放进来。
        """
        ts = now if now is not None else time.time()
        record = None
        if self.store is not None:
            record = self.store.get(key)
        if record is None:
            return None
        if record.importance < self.min_importance:
            # 重要度不够 —— 不占上下文（C2 的 importance_threshold 语义）
            return None

        item = ContextItem(
            key=record.key,
            content=record.content,
            tokens=estimate_tokens(record.content),
            importance=record.importance,
            origin=ORIGIN_SWAP_IN,
            last_access=ts,
            pinned=False,
        )
        self._items[key] = item
        # 放进来之后可能超预算 → 换出别的（但不能换出自己）
        self.evict_if_needed(protect=key, now=ts)
        return item

    # ── 换出 ────────────────────────────────────────────────────────────────

    def evict_if_needed(
        self, protect: str | None = None, now: float | None = None
    ) -> list[str]:
        """工作集超预算就换出最不重要的几条。返回被换出的 key 列表。

        换出顺序：重要性 × 时间衰减 的分数**最低**的先走
        （C2 降级路径：打分是确定性的，不调模型）。
        常驻条目与 `protect` 指定的条目不动。
        """
        ts = now if now is not None else time.time()
        evicted: list[str] = []
        while self.working_tokens() > self.budget_tokens:
            victim = self._pick_victim(protect, ts)
            if victim is None:
                # 只剩常驻条目（或都受保护）—— 换不出去了
                self.counters["evict_failed"] += 1
                break
            self._items.pop(victim.key, None)
            evicted.append(victim.key)
            self.counters["swap_out"] += 1
        return evicted

    def swap_out(self, key: str) -> bool:
        """显式换出某条（常驻条目不许换）。"""
        item = self._items.get(key)
        if item is None or item.pinned:
            return False
        del self._items[key]
        self.counters["swap_out"] += 1
        return True

    def _pick_victim(self, protect: str | None, now: float) -> ContextItem | None:
        """挑最该被换出的那条。"""
        candidates = [
            i
            for i in self.working_items
            if i.key != protect and not i.pinned
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda i: (self._score(i, now), i.key))

    def _score(self, item: ContextItem, now: float) -> float:
        """条目分数 = 重要度 × 时间衰减。

        时间衰减用的是「距上次访问多久」—— 最近用过的更不容易被换出，
        这是 LRU 的近似（不精确，但确定性）。
        """
        age = max(0.0, now - item.last_access)
        return item.importance * self.policy.decay_factor(age)

    # ── Checkpoint ──────────────────────────────────────────────────────────

    def checkpoint(self) -> dict[str, Any]:
        """把当前状态存成可序列化的快照（可暂停 / 可恢复）。"""
        return {
            "version": CHECKPOINT_VERSION,
            "ts": time.time(),
            "budget_tokens": self.budget_tokens,
            "min_importance": self.min_importance,
            "counters": dict(self.counters),
            "items": [i.to_dict() for i in self._items.values()],
        }

    def restore(self, checkpoint: dict[str, Any]) -> None:
        """从快照恢复状态。"""
        version = int(checkpoint.get("version", 0))
        if version != CHECKPOINT_VERSION:
            raise ValueError(
                f"Checkpoint 版本不兼容：期望 {CHECKPOINT_VERSION}，实际 {version}"
            )
        self.budget_tokens = int(
            checkpoint.get("budget_tokens", self.budget_tokens)
        )
        self.min_importance = float(
            checkpoint.get("min_importance", self.min_importance)
        )
        self._items = {
            d["key"]: ContextItem.from_dict(d) for d in checkpoint.get("items", [])
        }
        saved = checkpoint.get("counters") or {}
        for k, v in saved.items():
            self.counters[k] = int(v)

    # ── 查询 ────────────────────────────────────────────────────────────────

    def get(self, key: str) -> ContextItem | None:
        return self._items.get(key)

    def keys(self) -> list[str]:
        return sorted(self._items)

    def stats(self) -> dict[str, Any]:
        access = self.counters["access"]
        return {
            **self.counters,
            "hit_rate": round(self.counters["hit"] / access, 4) if access else 0.0,
            "working_tokens": self.working_tokens(),
            "total_tokens": self.total_tokens(),
            "budget_tokens": self.budget_tokens,
            "resident_count": len(self.resident_items),
            "working_count": len(self.working_items),
        }

    def describe(self) -> str:
        """人读的状态摘要（调试 / 审计用）。"""
        lines = [
            f"上下文预算 {self.budget_tokens} tokens，"
            f"常驻 {len(self.resident_items)} 条，工作集 {len(self.working_items)} 条"
            f"（占用 {self.working_tokens()} tokens）"
        ]
        for item in sorted(self._items.values(), key=lambda i: i.key):
            mark = "[常驻]" if item.pinned else "[换入]"
            lines.append(
                f"  {mark} {item.key:<24} {item.tokens:>5}t  "
                f"重要度={item.importance:.2f} 命中={item.hit_count}"
            )
        s = self.stats()
        lines.append(
            f"  访问 {s['access']} 次：命中 {s['hit']} / 缺页 {s['miss']} "
            f"（命中率 {s['hit_rate']:.0%}），换入 {s['swap_in']} / 换出 {s['swap_out']}"
        )
        return "\n".join(lines)
