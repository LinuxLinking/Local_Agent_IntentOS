"""L3 · 遗忘策略 —— 「什么该记、什么该忘、什么时候该忘」。

设计依据：
- design-foundation/06-open-conclusions-r1.md C13
  「关键能力是遗忘 —— OS 有 swap out，文件系统有 delete，但 LLM 天然"不忘"」
  明确列了四件套：**TTL / Right to be Forgotten / Forgetting Curve / Conflict Resolution**
- 本模块负责前三件中的 **TTL** 与 **Forgetting Curve**；
  Right to be Forgotten（物理删除）在 `store.py` 的 `forget()`；
  冲突消解（最后写胜出 + 版本链）也在 `store.py`。

── 为什么必须有 ───────────────────────────────────────────────────────────
LLM 天然「不忘」：上下文窗口内的东西一走就没了，跨会话更是清零。
要让它像操作系统一样有 swap out 和 delete，就得**显式定义**：
  · TTL         —— 每条记忆有过期时间，过期即失效
  · 遗忘曲线    —— 权重随时间衰减，不重要的先忘
  · 遗忘权      —— 能不能被显式删除（合规要求）
  · 冲突消解    —— 新记忆与旧记忆冲突时谁胜出

「语义文件系统」的核心不是存下来，是**定义什么该忘**。

── 默认值是提案，不是结论 ─────────────────────────────────────────────────
C13 明确写着「遗忘策略的默认值」仍待决。这里的默认值取的是保守起点：
  · TTL 默认不设（不猜用户的记忆周期）
  · 半衰期 30 天（≈ 一个月内逐渐淡出，但不会突然消失）
改动只需改 `ForgettingPolicy` 的字段，不影响存储格式。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

# 30 天 —— 遗忘曲线半衰期
DEFAULT_HALF_LIFE = 30 * 24 * 3600.0

@dataclass
class ForgettingPolicy:
    """遗忘策略。

    Attributes:
        ttl_seconds: 默认存活时长（秒）。None = 永不过期。
        half_life_seconds: 遗忘曲线半衰期 —— 每过这么久，权重减半。
        decay_enabled: 关掉就退化成「只看相似度」的纯向量召回。
        min_score: 召回分数下限，低于它就不返回（等效于「彻底忘了」）。
    """

    ttl_seconds: float | None = None
    half_life_seconds: float = DEFAULT_HALF_LIFE
    decay_enabled: bool = True
    min_score: float = 0.0

    # ── TTL ────────────────────────────────────────────────────────────────

    def expires_at(self, created_at: float) -> float | None:
        """按 TTL 算过期时刻；没设 TTL 则永不过期。"""
        if self.ttl_seconds is None:
            return None
        return created_at + self.ttl_seconds

    def is_expired(self, expires_at: float | None, now: float | None = None) -> bool:
        """是否已过期。"""
        if expires_at is None:
            return False
        return expires_at <= (now if now is not None else time.time())

    # ── 遗忘曲线 ────────────────────────────────────────────────────────────

    def decay_factor(self, age_seconds: float, now: float | None = None) -> float:
        """时间衰减因子：半衰期制，0.5^(age/half_life)。

        age <= 0 → 1.0（刚写入的记忆不打折）。
        """
        if not self.decay_enabled:
            return 1.0
        if age_seconds <= 0:
            return 1.0
        if self.half_life_seconds <= 0:
            return 1.0
        return 0.5 ** (age_seconds / self.half_life_seconds)

    # ── 打分 ────────────────────────────────────────────────────────────────

    def score(
        self,
        *,
        similarity: float,
        importance: float,
        age_seconds: float,
    ) -> float:
        """召回分数 = 相似度 × 重要度 × 衰减。

        重要度默认 1.0（不额外加权）；衰减让「久远且不重要」的记忆自然沉底。
        """
        return similarity * max(0.0, importance) * self.decay_factor(age_seconds)
