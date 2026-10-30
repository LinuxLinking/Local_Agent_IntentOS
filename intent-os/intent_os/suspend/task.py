"""挂起态（RFC 15 §3.5）—— **可序列化**的任务快照。

白皮书的第三种结局：不是「卡死」，也不是「断崖式拒绝」，而是
**平滑降级 / 挂起等人**。这一模块管的是「挂起态长什么样、怎么落盘、怎么读回」。

──────── 挂起态必须装下什么（RFC 15 §3.5 + 任务书）──────────────────────

| 字段 | 为什么必须有 |
|---|---|
| `task_id` / `suspend_seq` | 这是哪个任务的第几次挂起（文件不撞名、链路可追） |
| `intent` / `plan` | 恢复时要重建计划 —— **不重编**（重编 = 从头再跑，违反「原地恢复」） |
| `completed` | **已完成节点清单**（含结果）—— 恢复时**不重跑**，但验收还要看得见它们的结果 |
| `suspend_node` / `suspend_index` | **恢复点精确到节点**（节点 id + 拓扑序位置）—— 不能靠「再跑一遍」 |
| `reason` / `reason_detail` | 为什么挂起（重路由耗尽 / 无备选 / 高危）—— 人要看得懂 |
| `environment` | 挂起当时的环境快照（复用 `EnvironmentSnapshot.to_json()`） |
| `banned` / `reroute_used` | 本次任务内禁用的边与已用预算 —— 恢复后仍然有效（RFC Q21） |
| `reroutes` | 挂起前的换路留痕 —— 恢复后 KPI 还能算（分母不能丢） |
| `audit_path` | 审计在哪 —— 恢复要接着写同一个审计 |
| `created_at` | 什么时候挂的 |

──────── 三条不破的红线 ────────────────────────────────────────────────────

1. **零模型调用**：本包只 import 既有类型 + 标准库；不 import 任何模型客户端
   （`tests/test_suspend_resume.py` 的源码扫描断言守着）。
2. **D2 边界**：挂起态**不写进 L3 长期记忆**（那是状态断言的地盘）——
   本包**不 import** `memory/state.py`，也不 import `memory/context.py`。
3. **不静默降级**：文件损坏 / 版本不符 → **如实报错**（`SuspendError`），
   绝不「读不出来就当没挂起过」。

──────── 版本号 ────────────────────────────────────────────────────────────

`RESUME_VERSION` —— 格式变了就升版本；读到的版本对不上 → **如实报错**，
不猜、不兼容着读（沿用 `memory/context.py` 的 `CHECKPOINT_VERSION` 惯例）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from ..compiler.intent import (
    Constraint,
    Intent,
    SideEffect,
    SideEffectKind,
    Verification,
    VerificationKind,
)
from ..compiler.plan import Plan, PlanNode
from ..probe.environment import EnvironmentSnapshot, utc_now_iso

#: resume 文件的格式版本（升级时改这里；读到的版本对不上 → 如实报错）
RESUME_VERSION: int = 1

#: 文件里的一行「这是什么东西」—— 防止把别的 JSON 误当成 resume 文件
RESUME_KIND: str = "intent_os.suspended_task"

class SuspendReason(str, Enum):
    """为什么挂起（**穷举**，不静默兜底）。

    - `REROUTE_EXHAUSTED`：重路由预算用尽（RFC 15 §9 Q20：「超了就挂起，别硬撞」）
    - `NO_ALTERNATIVE`：**没有备选**（路由器算不出路）—— 需要人/环境介入
    - `HIGH_RISK`：**高危操作** —— 挂起前**不放行**，人工确认后才继续
    """

    REROUTE_EXHAUSTED = "reroute_exhausted"
    NO_ALTERNATIVE = "no_alternative"
    HIGH_RISK = "high_risk"

class SuspendError(RuntimeError):
    """挂起/恢复的错误（文件损坏、版本不符、字段缺失……）。

    **一律如实报错** —— 绝不「读不出来就当没挂起过」（红线：不静默降级）。
    """

@dataclass(frozen=True)
class SuspendedTask:
    """一个挂起中的任务（**可序列化**）。

    冻结（frozen）：挂起态是**快照**，落盘之后不许就地改 ——
    要改就重新挂起（新的 `suspend_seq`）。
    """

    task_id: str
    suspend_seq: int
    intent: Intent
    plan: Plan
    #: 已完成节点清单：`{"name", "tool", "ok", "output", "error"}`
    completed: tuple[dict[str, Any], ...] = ()
    #: 挂起点：节点 id（`PlanNode.name`）
    suspend_node: str = ""
    #: 挂起点：拓扑序位置（从 0 起）
    suspend_index: int = 0
    reason: SuspendReason = SuspendReason.REROUTE_EXHAUSTED
    reason_detail: str = ""
    #: 挂起当时的环境快照（`EnvironmentSnapshot.to_json()` 的形状）
    environment: dict[str, Any] = field(default_factory=dict)
    #: 本次任务内禁用的边（失败边）—— 恢复后仍然有效（RFC Q21）
    banned: tuple[str, ...] = ()
    #: 挂起前已用掉的重路由次数
    reroute_used: int = 0
    #: 挂起前的换路留痕（恢复后 KPI 还能算 —— 分母不能丢）
    reroutes: tuple[dict[str, Any], ...] = ()
    #: 审计日志路径（恢复要接着写同一个审计）
    audit_path: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    # ── 序列化 ────────────────────────────────────────────────────────────

    def to_json(self) -> dict[str, Any]:
        """resume 文件的形状（**一个概念一处定义**：只有这里定义）。"""
        return {
            "version": RESUME_VERSION,
            "kind": RESUME_KIND,
            "task_id": self.task_id,
            "suspend_seq": self.suspend_seq,
            "created_at": self.created_at,
            "reason": self.reason.value,
            "reason_detail": self.reason_detail,
            "suspend_node": self.suspend_node,
            "suspend_index": self.suspend_index,
            "intent": self.intent.to_dict(),
            "plan": self.plan.to_dict(),
            "completed": [dict(c) for c in self.completed],
            "environment": dict(self.environment),
            "banned": list(self.banned),
            "reroute_used": self.reroute_used,
            "reroutes": [dict(r) for r in self.reroutes],
            "audit_path": self.audit_path,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "SuspendedTask":
        """从 resume 文件的形状还原（**校验从严**：缺字段/坏版本 → 如实报错）。"""
        if not isinstance(data, Mapping):
            raise SuspendError("resume 文件内容不是对象（损坏）")
        version = data.get("version")
        if version != RESUME_VERSION:
            raise SuspendError(
                f"resume 文件版本不兼容：期望 {RESUME_VERSION}，实际 {version!r}"
            )
        kind = data.get("kind")
        if kind != RESUME_KIND:
            raise SuspendError(f"这不是 resume 文件（kind={kind!r}）")
        for key in ("task_id", "intent", "plan"):
            if key not in data:
                raise SuspendError(f"resume 文件损坏：缺少字段 {key!r}")
        try:
            reason = SuspendReason(str(data.get("reason")))
        except ValueError as exc:
            raise SuspendError(
                f"resume 文件损坏：未知的挂起原因 {data.get('reason')!r}"
            ) from exc
        try:
            intent = _intent_from_dict(data["intent"])
            plan = _plan_from_dict(data["plan"], intent)
        except (KeyError, TypeError, ValueError) as exc:
            raise SuspendError(f"resume 文件损坏：意图/计划读不出来（{exc}）") from exc

        completed = []
        for item in data.get("completed") or ():
            if not isinstance(item, Mapping) or "name" not in item:
                raise SuspendError("resume 文件损坏：已完成节点清单里有坏条目")
            completed.append(dict(item))

        return cls(
            task_id=str(data.get("task_id", "")),
            suspend_seq=int(data.get("suspend_seq", 1)),
            intent=intent,
            plan=plan,
            completed=tuple(completed),
            suspend_node=str(data.get("suspend_node", "")),
            suspend_index=int(data.get("suspend_index", 0)),
            reason=reason,
            reason_detail=str(data.get("reason_detail", "")),
            environment=dict(data.get("environment") or {}),
            banned=tuple(str(b) for b in (data.get("banned") or ())),
            reroute_used=int(data.get("reroute_used", 0)),
            reroutes=tuple(dict(r) for r in (data.get("reroutes") or ())),
            audit_path=str(data.get("audit_path", "")),
            created_at=str(data.get("created_at", "")),
        )

    # ── 落盘 / 读回 ───────────────────────────────────────────────────────

    def save(self, path: str | Path) -> Path:
        """写 resume 文件（**UTF-8**，人类可读）。返回写入的路径。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_json(), ensure_ascii=False, indent=2) + "\n"
        target.write_text(text, encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "SuspendedTask":
        """读回 resume 文件（**跨进程**）。

        读不出来 → **如实报错**（`SuspendError`）：文件不存在 / 不是 JSON /
        版本不符 / 字段缺失 —— 一律不静默兜底。
        """
        source = Path(path)
        if not source.exists():
            raise SuspendError(f"resume 文件不存在：{source}")
        try:
            raw = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise SuspendError(f"resume 文件读不出来：{source}（{exc}）") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SuspendError(f"resume 文件不是合法 JSON：{source}（{exc}）") from exc
        return cls.from_json(data)

    # ── 便捷视图 ──────────────────────────────────────────────────────────

    @property
    def completed_names(self) -> frozenset[str]:
        """已完成节点的 name 集合（恢复时跳过它们 —— 不重跑）。"""
        return frozenset(str(c.get("name", "")) for c in self.completed)

    def environment_snapshot(self) -> EnvironmentSnapshot:
        """挂起时的环境快照（还原成 5.1 的形状；空 → 空快照）。"""
        if not self.environment:
            return EnvironmentSnapshot()
        try:
            return EnvironmentSnapshot.from_json(self.environment)
        except (KeyError, TypeError, ValueError):
            return EnvironmentSnapshot()

    def to_summary(self) -> dict[str, Any]:
        """人看的摘要（CLI / 审计用）。"""
        return {
            "task_id": self.task_id,
            "suspend_seq": self.suspend_seq,
            "reason": self.reason.value,
            "reason_detail": self.reason_detail,
            "suspend_node": self.suspend_node,
            "suspend_index": self.suspend_index,
            "completed": len(self.completed),
            "banned": list(self.banned),
            "audit_path": self.audit_path,
            "created_at": self.created_at,
        }

# ── Intent / Plan 的反序列化（只认本仓写出的形状）──────────────────────────

def _intent_from_dict(data: Mapping[str, Any]) -> Intent:
    """从 `Intent.to_dict()` 的形状还原。

    **前提声明（premises）不还原** —— 挂起前已经校验过；恢复路径**不再重跑前提门**
    （那会把「当时的判断」重做成「现在的判断」；真要重判，是 5.5 的事）。
    """
    constraints = tuple(
        Constraint(kind=str(c.get("kind", "")), value=str(c.get("value", "")))
        for c in (data.get("constraints") or ())
    )
    verification = tuple(
        Verification(
            kind=VerificationKind(str(v.get("kind"))),
            tool=str(v.get("tool", "")),
            target=v.get("target"),
            expected=v.get("expected"),
        )
        for v in (data.get("verification") or ())
    )
    side_effects = tuple(
        SideEffect(
            tool=str(s.get("tool", "")),
            scope=str(s.get("scope", "")),
            kind=SideEffectKind(str(s.get("kind", SideEffectKind.NONE.value))),
            reversibility=str(s.get("reversibility", "easy")),
        )
        for s in (data.get("side_effects") or ())
    )
    return Intent(
        raw=str(data.get("raw", "")),
        goal=str(data.get("goal", "")),
        constraints=constraints,
        verification=verification,
        side_effects=side_effects,
        premises=(),
        compiler_notes=tuple(str(n) for n in (data.get("compiler_notes") or ())),
    )

def _plan_from_dict(data: Mapping[str, Any], intent: Intent) -> Plan:
    """从 `Plan.to_dict()` 的形状还原（节点依赖原样带回）。"""
    nodes = tuple(
        PlanNode(
            name=str(n.get("name", "")),
            tool=str(n.get("tool", "")),
            args=dict(n.get("args") or {}),
            description=str(n.get("description", "")),
            depends_on=tuple(str(d) for d in (n.get("depends_on") or ())),
        )
        for n in (data.get("nodes") or ())
    )
    return Plan(id=str(data.get("id", "")), intent=intent, nodes=nodes)

__all__ = [
    "RESUME_KIND",
    "RESUME_VERSION",
    "SuspendError",
    "SuspendReason",
    "SuspendedTask",
]
