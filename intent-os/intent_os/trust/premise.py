"""L1 · 前提校验 —— 拿权威状态机械判定模型说出口的前提。

设计依据：
- design-foundation/07-r2-conclusions.md §4 暴论 #17（声明 → 校验 → 纠正）
- design-foundation/04-open-questions.md Q19（前提必须能被机械判定）
- design-foundation/07-r2-conclusions.md §2（状态断言 = 权威源，能当校验基准）

── 这里的职责边界 ─────────────────────────────────────────────────────────
**不做判决，只做一致性断言。**
  · 冲突 → 前提与权威状态矛盾 → 拦截 + 下发纠正文本
  · 未知 → 状态里没有这条 → 不拦，但明确告诉模型「你不知道这个」

措辞很重要（暴论 #17）：
  ✗「你的意图不合法」   —— 这是判决，模型会对抗
  ✓「你的前提与状态 X 矛盾」 —— 这是断言，模型能据此改写前提

── 状态断言住在哪 ───────────────────────────────────────────────────────────
`StateAssertion` 的**定义**现在在 `intent_os/memory/state.py`（L3 权威源，
见 design-foundation/09-state-assertion-rfc.md §1）；本模块只是**再导出**，
保持旧导入路径可用。

理由：状态断言是 L3 的第四形态（`07` §2），是**权威源**；
L1 只**消费**它。定义跟着「用途」走，久了必然出现两份。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..compiler.premise import Premise
from ..memory.state import StateAssertion

__all__ = [
    "PremiseChecker",
    "PremiseCheckResult",
    "PremiseConflict",
    "PremiseGate",
    "PremiseGateResult",
    "StateAssertion",
]

@dataclass(frozen=True)
class PremiseConflict:
    """前提与权威状态矛盾。"""

    state_key: str
    asserted: str
    actual: str
    state_version: int

    def render(self) -> str:
        return (
            f"前提「{self.state_key} = {self.asserted}」与权威状态矛盾："
            f"状态里 {self.state_key} = {self.actual}（v{self.state_version}）"
        )

@dataclass(frozen=True)
class PremiseCheckResult:
    """一条前提的校验结果。"""

    ok: bool
    confirmed: tuple[str, ...] = ()
    conflicts: tuple[PremiseConflict, ...] = ()
    unknowns: tuple[str, ...] = ()
    unchecked: tuple[str, ...] = ()  # 不可判定（没有 state_key）

    def correction_text(self) -> str:
        """给模型的纠正文本 —— 描述矛盾，不做判决。"""
        parts: list[str] = []
        for c in self.conflicts:
            parts.append(c.render())
        for key in self.unknowns:
            parts.append(
                f"权威状态里没有 {key} 的信息 —— 这一条你无法确认，"
                "请声明你的依据或改用可校验的前提"
            )
        for key in self.unchecked:
            parts.append(
                f"前提 {key} 没有可校验的状态键 —— 无法判定，请补充 state_key"
            )
        if not parts:
            return ""
        return "。".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "confirmed": list(self.confirmed),
            "conflicts": [
                {
                    "state_key": c.state_key,
                    "asserted": c.asserted,
                    "actual": c.actual,
                    "state_version": c.state_version,
                }
                for c in self.conflicts
            ],
            "unknowns": list(self.unknowns),
            "unchecked": list(self.unchecked),
        }

class PremiseChecker:
    """把前提拿去跟权威状态比。

    Args:
        state: 权威状态表（key → StateAssertion）。**这是校验基准**，
               与「长期记忆」不同 —— 记忆只是线索，状态才是真相。
        unknown_is_blocking: 状态里查不到的前提是否也拦。
               默认 **False** —— 「不知道」不等于「错」，
               但它必须被记录并告知模型。
               注意：**不可判定**（前提没有 state_key）不受此开关影响 ——
               那条永远不算通过。
    """

    def __init__(
        self,
        state: Mapping[str, StateAssertion] | None = None,
        *,
        unknown_is_blocking: bool = False,
    ) -> None:
        self.state: dict[str, StateAssertion] = dict(state or {})
        self.unknown_is_blocking = unknown_is_blocking

    # ── 状态维护 ────────────────────────────────────────────────────────────

    def set_state(self, key: str, value: str, *, version: int = 1, scope: str = "default",
                  note: str = "") -> StateAssertion:
        """写入 / 覆盖一条权威状态断言。"""
        assertion = StateAssertion(
            key=key, value=value, version=version, scope=scope, note=note
        )
        self.state[key] = assertion
        return assertion

    def set_many(self, items: Iterable[StateAssertion]) -> None:
        for item in items:
            self.state[item.key] = item

    # ── 校验 ────────────────────────────────────────────────────────────────

    def check(self, premise: Premise) -> PremiseCheckResult:
        """校验一条前提。

        三种结局，**刻意分开**：

        | 情形 | 判定 | 为什么 |
        |---|---|---|
        | confirmed | 通过 | 前提与权威状态一致 |
        | conflict | **拦截** | 前提被权威状态明确推翻 —— 前提错了 |
        | unknown | 默认不拦 | 状态里没这条 —— **不知道 ≠ 错** |
        | unchecked | **永远不算通过** | 前提自己就没有 state_key —— **没被校验 ≠ 校验通过** |

        最后一行是护栏：一条不可判定的前提**永远不能被报成「已校验」**。
        它不是「不知道」，是**声明本身有缺陷**（模型没给出状态键），
        所以不受 `unknown_is_blocking` 影响。
        """
        if not premise.is_checkable():
            # 不可判定 ≠ 通过：这条永远不算 ok（与 unknown 不同类）
            return PremiseCheckResult(
                ok=False,
                unchecked=(premise.state_key or premise.target or "<空前提>",),
            )

        assertion = self.state.get(premise.state_key)
        if assertion is None:
            return PremiseCheckResult(
                ok=not self.unknown_is_blocking,
                unknowns=(premise.state_key,),
            )

        if _values_equal(premise.state_value, assertion.value):
            return PremiseCheckResult(ok=True, confirmed=(premise.state_key,))

        return PremiseCheckResult(
            ok=False,
            conflicts=(
                PremiseConflict(
                    state_key=premise.state_key,
                    asserted=premise.state_value,
                    actual=assertion.value,
                    state_version=assertion.version,
                ),
            ),
        )

    def check_all(
        self, premises: Iterable[Premise]
    ) -> tuple[list[PremiseCheckResult], PremiseCheckResult]:
        """校验一批前提。

        Returns:
            (逐条结果, 汇总结果) —— 汇总的 `correction_text()` 是要下发给模型的文本。
        """
        results = [self.check(p) for p in premises]
        confirmed: list[str] = []
        conflicts: list[PremiseConflict] = []
        unknowns: list[str] = []
        unchecked: list[str] = []
        for r in results:
            confirmed.extend(r.confirmed)
            conflicts.extend(r.conflicts)
            unknowns.extend(r.unknowns)
            unchecked.extend(r.unchecked)
        ok = all(r.ok for r in results) if results else True
        summary = PremiseCheckResult(
            ok=ok,
            confirmed=tuple(confirmed),
            conflicts=tuple(conflicts),
            unknowns=tuple(unknowns),
            unchecked=tuple(unchecked),
        )
        return results, summary

def _values_equal(asserted: str, actual: str) -> bool:
    """值比较 —— 大小写与空白不敏感，语义敏感。

    刻意**不做**语义相似比较：校验必须确定性、可复现、可单测。
    「present」和「在」不等价 —— 那是模型该转换的，不是校验器该猜的。
    """
    return asserted.strip().casefold() == actual.strip().casefold()

# ── Phase 3.5.4 · 前提门（判定口径的唯一实现）───────────────────────────────
#
# **为什么要有这一层**：3.5.3 把「放不放行」写在 runtime 的私有方法里。
# 但轨迹重放（RFC 11）要判**同一件事** —— 两份实现必然分叉，
# 分叉了重放结论就不可信（「以前失败现在不失败」这句话就没意义了）。
#
# 所以把**判定**从**编排**里剥出来：runtime 调它 + 写审计，
# 重放器调**同一个**它 + 不写审计。一个概念只有一处定义。

@dataclass(frozen=True)
class PremiseGateResult:
    """前提门的判定结果 —— 「放不放行」的唯一口径。"""

    ok: bool
    summary: PremiseCheckResult | None = None
    correction_text: str = ""
    #: "premise_conflict" | "no_premises_declared" | None（None = 放行）
    block_reason: str | None = None
    declared: int = 0
    #: 一条前提都没声明 → 校验是**空转**的（不等于「校验通过」）
    vacuous: bool = False
    per_premise: tuple[PremiseCheckResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "block_reason": self.block_reason,
            "declared": self.declared,
            "vacuous": self.vacuous,
            "correction_text": self.correction_text,
            "summary": self.summary.to_dict() if self.summary else None,
        }

#: 没声明任何前提时的纠正文本（下发模型用）。
NO_PREMISE_TEXT = (
    "本次动作没有声明任何前提 —— 副作用前必须声明前提，"
    "请补上可校验的 state_key 与 state_value"
)

class PremiseGate:
    """前提门 —— 决定放不放行。

    **判定口径的唯一实现**（RFC 11 §3）：runtime 与轨迹重放器都调它，
    两边不可能分叉 —— 分叉了「以前失败现在不失败」这句话就没意义了。

    Args:
        checker: 前提校验器。**由调用方构造** ——
            runtime 用 `PremiseChecker(state=..., unknown_is_blocking=...)` 现场构造，
            重放器用从轨迹重建出来的状态表构造。
            （构造留在调用方，是为了让「谁在用什么基准校验」一眼可见。）
        require_premises: 是否强制「必须声明前提」。默认否 ——
            模板路径没有前提来源，强开会打断既有流程；
            但校验空转这件事会**如实标记**（`vacuous=True`），不装作校验过了。
    """

    def __init__(
        self,
        checker: PremiseChecker,
        *,
        require_premises: bool = False,
    ) -> None:
        self.checker = checker
        self.require_premises = require_premises

    def evaluate(self, premises: Iterable[Premise]) -> PremiseGateResult:
        """判定一批前提。

        返回的 `ok` 语义 = **「可以动手」**：
          · 一致 → True
          · 矛盾 → False（block_reason="premise_conflict"）
          · 没 state_key → False（unchecked 永远不算通过）
          · 状态里没这条 → 默认 True（但 `correction_text` 里**必须告知**）
          · 一条都没声明 → 默认 True（`vacuous=True`，如实标记空转）
        """
        declared = list(premises)

        # ── 一条前提都没声明 ──
        if not declared:
            if self.require_premises:
                return PremiseGateResult(
                    ok=False,
                    block_reason="no_premises_declared",
                    correction_text=NO_PREMISE_TEXT,
                    declared=0,
                    vacuous=True,
                )
            return PremiseGateResult(ok=True, declared=0, vacuous=True)

        # ── 有声明 → 拿权威状态机械比对 ──
        results, summary = self.checker.check_all(declared)
        correction = summary.correction_text()
        if not summary.ok:
            return PremiseGateResult(
                ok=False,
                summary=summary,
                correction_text=correction,
                block_reason="premise_conflict",
                declared=len(declared),
                per_premise=tuple(results),
            )
        return PremiseGateResult(
            ok=True,
            summary=summary,
            correction_text=correction,
            declared=len(declared),
            per_premise=tuple(results),
        )

    def policy(self) -> dict[str, Any]:
        """当前策略 —— 随审计留痕，重放时按「当时的策略」复现。"""
        return {
            "unknown_is_blocking": self.checker.unknown_is_blocking,
            "require_premises": self.require_premises,
        }
