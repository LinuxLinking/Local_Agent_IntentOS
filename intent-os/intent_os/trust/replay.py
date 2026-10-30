"""L1 · 轨迹重放 —— 重放式回归（**不蒸馏**，按 D1）。

设计依据：
- design-foundation/11-trace-replay-rfc.md（本文件的规则出处）
- design-foundation/07-r2-conclusions.md §1 D1（轨迹的新用途：评估 runtime）

── D1 说了什么 ─────────────────────────────────────────────────────────────
「不做模型蒸馏」不等于轨迹没用。D1 给轨迹留了一个出口：

    原用途：喂回模型自蒸馏
    新用途：评估 runtime —— **重放式回归**（「这条轨迹以前失败，现在不失败」）

所以本模块做的是：**把历史轨迹拿当前的判定逻辑重跑一遍，比对结论**。

── 为什么这不违反 D1 ───────────────────────────────────────────────────────
**轨迹绝不喂回模型。** 这不是靠自觉，是靠结构：

  · 本模块**不 import LLM 编译器**，不持有任何模型句柄；
  · 它只做三件事：读日志 → 重建（状态 + 前提）→ 跑**确定性**判定；
  · `tests/test_trace_replay.py` 直接扫源码锁死这一点。

轨迹的用途是**评估 runtime / 审计复盘 / 沉淀护城河数据** —— 不是喂回大脑。

── 口径（RFC 11 §4）────────────────────────────────────────────────────────
拿什么当基准重跑：

  · **状态**：默认用**轨迹里记录的状态**（recorded）——
    回归测试必须确定性，今天重放与昨天重放结果一致。
    可用 `live_state=` 覆盖成「当前活的状态」（问的是另一个问题：
    「世界变了之后，这条老意图还成立吗」）。
  · **策略**：默认用**轨迹里记录的策略** ——
    不然分不清「代码修好了」与「配置变了」。

比对结果四种状态：

  | 状态 | 历史 | 当前 | 含义 |
  |---|---|---|---|
  | `fixed` | 被拦 | 通过 | 「以前失败，现在不失败了」—— **D1 说的那个数** |
  | `regressed` | 通过 | 被拦 | **改坏了** —— 回归测试最该报警的一条 |
  | `unchanged` | 相同 | 相同 | 没变化 |
  | `not_replayable` | 不可知 | — | 轨迹缺基准（没有状态快照 / 没有校验记录） |

── 重放不写审计 ───────────────────────────────────────────────────────────
重放是**评估工具**，读历史日志；它若往被评估的日志里写东西，会污染被评估的对象。
报告由调用方落盘（`reports/`）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..compiler.premise import Premise
from ..memory.state import StateAssertion
from .audit import Event, read_events, segment_runs
from .premise import PremiseChecker, PremiseGate

__all__ = [
    "TraceReplayer",
    "TraceOutcome",
    "ReplayReport",
    "STATUS_FIXED",
    "STATUS_REGRESSED",
    "STATUS_UNCHANGED",
    "STATUS_NOT_REPLAYABLE",
]

#: 以前被拦、现在通过 —— 「这条轨迹以前失败，现在不失败了」。
STATUS_FIXED = "fixed"
#: 以前通过、现在被拦 —— **改坏了**，回归测试最该报警的一条。
STATUS_REGRESSED = "regressed"
#: 结论没变。
STATUS_UNCHANGED = "unchanged"
#: 轨迹里缺基准，重不了（早于本机制的轨迹，或当初校验空转）。
STATUS_NOT_REPLAYABLE = "not_replayable"

_STATUS_LABELS = {
    STATUS_FIXED: "以前被拦 → 现在通过",
    STATUS_REGRESSED: "以前通过 → 现在被拦（改坏了）",
    STATUS_UNCHANGED: "结论没变",
    STATUS_NOT_REPLAYABLE: "重不了（轨迹缺基准）",
}

@dataclass(frozen=True)
class TraceOutcome:
    """一条轨迹的重放结论。"""

    run_index: int
    status: str
    #: 当时这次 run 是否通过了前提门（None = 不可知）
    historical_ok: bool | None
    #: 用当前逻辑重跑，是否通过
    current_ok: bool | None
    #: 轨迹里声明了几条前提
    declared: int
    historical_correction: str = ""
    current_correction: str = ""
    #: 纠正文本变了（判定没变但措辞变了，也得看得见）
    correction_changed: bool = False
    note: str = ""

    @property
    def label(self) -> str:
        return _STATUS_LABELS.get(self.status, self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_index": self.run_index,
            "status": self.status,
            "label": self.label,
            "historical_ok": self.historical_ok,
            "current_ok": self.current_ok,
            "declared": self.declared,
            "historical_correction": self.historical_correction,
            "current_correction": self.current_correction,
            "correction_changed": self.correction_changed,
            "note": self.note,
        }

@dataclass(frozen=True)
class ReplayReport:
    """一次重放的完整报告。"""

    source: str
    traces: tuple[TraceOutcome, ...] = field(default_factory=tuple)

    def by_status(self, status: str) -> list[TraceOutcome]:
        return [t for t in self.traces if t.status == status]

    @property
    def fixed(self) -> list[TraceOutcome]:
        return self.by_status(STATUS_FIXED)

    @property
    def regressed(self) -> list[TraceOutcome]:
        return self.by_status(STATUS_REGRESSED)

    @property
    def unchanged(self) -> list[TraceOutcome]:
        return self.by_status(STATUS_UNCHANGED)

    @property
    def not_replayable(self) -> list[TraceOutcome]:
        return self.by_status(STATUS_NOT_REPLAYABLE)

    def summary(self) -> dict[str, int]:
        counts = {
            STATUS_FIXED: 0,
            STATUS_REGRESSED: 0,
            STATUS_UNCHANGED: 0,
            STATUS_NOT_REPLAYABLE: 0,
        }
        for t in self.traces:
            counts[t.status] = counts.get(t.status, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "summary": self.summary(),
            "traces": [t.to_dict() for t in self.traces],
        }

class TraceReplayer:
    """把历史轨迹拿当前的判定逻辑重跑一遍（D1 的「评估 runtime」）。

    用法：
        report = TraceReplayer().replay(Path("audit.jsonl"))
        print(report.summary())     # {'fixed': 1, 'regressed': 0, ...}

    **不写审计** —— 它是评估工具，不是 run。
    """

    def __init__(
        self,
        *,
        live_state: Mapping[str, StateAssertion] | None = None,
        policy_override: Mapping[str, bool] | None = None,
    ) -> None:
        """Args:
            live_state: 用**活的状态**重放（默认不用 —— 默认用轨迹里记录的，
                回归测试必须确定性）。
            policy_override: 覆盖轨迹里记录的策略（`unknown_is_blocking` /
                `require_premises`）。
        """
        self.live_state = dict(live_state) if live_state is not None else None
        self.policy_override = dict(policy_override) if policy_override else None

    def replay(self, audit_path: Path) -> ReplayReport:
        """重放一个审计文件里的全部轨迹。"""
        events = read_events(Path(audit_path))
        runs = segment_runs(events)
        traces = tuple(self._replay_run(i, run) for i, run in enumerate(runs))
        return ReplayReport(source=str(audit_path), traces=traces)

    # ── 单条轨迹 ────────────────────────────────────────────────────────────

    def _replay_run(
        self, index: int, run_events: list[dict[str, Any]]
    ) -> TraceOutcome:
        intent_data: dict[str, Any] = {}
        pushed: dict[str, Any] | None = None
        checked: dict[str, Any] | None = None
        blocked: dict[str, Any] | None = None

        for rec in run_events:
            event = rec.get("event", "")
            data = rec.get("data", {}) or {}
            if event == Event.INTENT_RECEIVED:
                intent_data = data
            elif event == Event.STATE_VIEW_PUSHED:
                pushed = data
            elif event == Event.PREMISE_CHECKED:
                checked = data
            elif event == Event.PREMISE_BLOCKED:
                blocked = data

        declared = [
            Premise.from_dict(p) for p in (intent_data.get("premises") or [])
        ]

        # ── 历史结论 ──
        historical_ok: bool | None
        historical_correction = ""
        note = ""
        if blocked is not None:
            historical_ok = False
            historical_correction = blocked.get("correction_text", "")
        elif checked is not None:
            historical_correction = checked.get("correction_text", "")
            if checked.get("vacuous"):
                # 当初一条前提都没声明 → 没有任何结论可比。
                # 但「没被拦」这件事本身成立，所以历史判定是 True。
                historical_ok = True
                note = "当初校验为空转（没声明前提）"
            else:
                historical_ok = bool((checked.get("result") or {}).get("ok"))
        else:
            historical_ok = None
            note = "轨迹里没有前提校验记录（早于本机制的轨迹）"

        # ── 当前结论 ──
        state_map, state_note = self._rebuild_state(pushed)
        if state_note:
            return TraceOutcome(
                run_index=index,
                status=STATUS_NOT_REPLAYABLE,
                historical_ok=historical_ok,
                current_ok=None,
                declared=len(declared),
                historical_correction=historical_correction,
                note=state_note,
            )

        policy = dict((checked or {}).get("policy") or {})
        if self.policy_override:
            policy.update(self.policy_override)

        checker = PremiseChecker(
            state=state_map,
            unknown_is_blocking=bool(policy.get("unknown_is_blocking", False)),
        )
        gate = PremiseGate(
            checker, require_premises=bool(policy.get("require_premises", False))
        )
        outcome = gate.evaluate(declared)

        if historical_ok is None:
            status = STATUS_NOT_REPLAYABLE
        elif historical_ok == outcome.ok:
            status = STATUS_UNCHANGED
        elif outcome.ok:
            status = STATUS_FIXED
        else:
            status = STATUS_REGRESSED

        return TraceOutcome(
            run_index=index,
            status=status,
            historical_ok=historical_ok,
            current_ok=outcome.ok,
            declared=len(declared),
            historical_correction=historical_correction,
            current_correction=outcome.correction_text,
            correction_changed=historical_correction != outcome.correction_text,
            note=note,
        )

    def _rebuild_state(
        self, pushed: dict[str, Any] | None
    ) -> tuple[dict[str, StateAssertion], str]:
        """重建校验基准 —— 优先用活的，其次用轨迹里记录的快照。

        返回 (状态表, 备注)。备注非空 = 重建不出来。
        """
        if self.live_state is not None:
            return dict(self.live_state), ""

        assertions = (pushed or {}).get("assertions")
        if assertions is None:
            return {}, "轨迹里没有状态快照（早于 3.5.2 的轨迹不能重放）"

        rebuilt: dict[str, StateAssertion] = {}
        for raw in assertions:
            assertion = StateAssertion.from_dict(raw)
            rebuilt[assertion.key] = assertion
        return rebuilt, ""
