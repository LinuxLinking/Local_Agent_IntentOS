"""约束记忆（RFC 15 §3.4-D · 白皮书 §2.4「约束记忆」）。

**安全红线固化在代码层**，不是提示词 —— 白皮书原话：
「绝对的安全红线（如转账限额、危险命令拦截），固化在代码层，**严禁 LLM 越权**」。

提示词能被模型忽略，代码不能。所以本模块是**执行前的最后一道门**：
`ConstraintMemory.check_node()` 一旦命中，runtime 直接 `status="blocked"`，
**不执行、不商量**。

──────── 三种规则 ──────────────────────────────────────────────────────────

| kind | 查什么 | 例子 |
|---|---|---|
| `forbidden_tool` | 工具名 | 禁用 `tool:shell:rm_rf` |
| `arg_pattern` | 参数值的正则 | 任何参数里出现 `rm -rf /` |
| `max_number` | 某个数值参数的上限 | `amount > 5000` 就拦 |

三个都是**确定性判定**（零模型调用）。规则在接入时声明，不随任务变化 ——
「不让被约束方定义约束」（RFC 14 D-d）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..compiler.plan import Plan, PlanNode

class ConstraintError(ValueError):
    """规则本身写错了（**如实报错**，别让坏规则静默失效）。"""

@dataclass(frozen=True)
class ConstraintRule:
    """一条红线。"""

    id: str
    kind: str  # "forbidden_tool" | "arg_pattern" | "max_number"
    target: str = ""  # 工具名（forbidden_tool）/ 参数名（arg_pattern、max_number；"*" = 所有参数）
    spec: str = ""  # 正则（arg_pattern）/ 上限（max_number）
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("forbidden_tool", "arg_pattern", "max_number"):
            raise ConstraintError(f"未知的规则类型：{self.kind!r}")
        if self.kind == "arg_pattern" and not self.spec:
            raise ConstraintError(f"规则 {self.id!r}：arg_pattern 必须给 spec")
        if self.kind == "max_number":
            try:
                float(self.spec)
            except ValueError as exc:
                raise ConstraintError(f"规则 {self.id!r}：max_number 的 spec 不是数") from exc

@dataclass(frozen=True)
class Violation:
    """命中一条红线。"""

    rule_id: str
    node_name: str
    tool: str
    detail: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "node_name": self.node_name,
            "tool": self.tool,
            "detail": self.detail,
            "reason": self.reason,
        }

class ConstraintMemory:
    """约束记忆（规则集 + 判定）。"""

    def __init__(self, rules: Iterable[ConstraintRule] = ()) -> None:
        self._rules: list[ConstraintRule] = list(rules)

    # ── 规则管理 ──────────────────────────────────────────────────────────

    def add(self, rule: ConstraintRule) -> None:
        if any(r.id == rule.id for r in self._rules):
            raise ConstraintError(f"规则 {rule.id!r} 已存在（不静默覆盖）")
        self._rules.append(rule)

    def rules(self) -> tuple[ConstraintRule, ...]:
        return tuple(self._rules)

    # ── 判定 ──────────────────────────────────────────────────────────────

    def check_node(self, node: PlanNode) -> Violation | None:
        """**执行前**查一个节点；命中 → 返回 `Violation`，否则 `None`。"""
        for rule in self._rules:
            violation = self._check_one(rule, node)
            if violation is not None:
                return violation
        return None

    def check_plan(self, plan: Plan) -> tuple[Violation, ...]:
        """**执行前**查整张 Plan（一次把所有问题都找出来）。"""
        out: list[Violation] = []
        for node in plan.nodes:
            v = self.check_node(node)
            if v is not None:
                out.append(v)
        return tuple(out)

    def _check_one(self, rule: ConstraintRule, node: PlanNode) -> Violation | None:
        if rule.kind == "forbidden_tool":
            if node.tool == rule.target:
                return Violation(
                    rule_id=rule.id,
                    node_name=node.name,
                    tool=node.tool,
                    detail=f"工具 {node.tool!r} 在红线名单里",
                    reason=rule.reason,
                )
            return None

        if rule.kind == "arg_pattern":
            pattern = re.compile(rule.spec)
            for key, value in node.args.items():
                if rule.target not in ("", "*") and key != rule.target:
                    continue
                text = str(value)
                match = pattern.search(text)
                if match:
                    return Violation(
                        rule_id=rule.id,
                        node_name=node.name,
                        tool=node.tool,
                        detail=f"参数 {key!r} 命中禁区 {rule.spec!r}：{match.group(0)!r}",
                        reason=rule.reason,
                    )
            return None

        # max_number
        for key, value in node.args.items():
            if rule.target not in ("", "*") and key != rule.target:
                continue
            number = _as_number(value)
            if number is None:
                continue
            limit = float(rule.spec)
            if number > limit:
                return Violation(
                    rule_id=rule.id,
                    node_name=node.name,
                    tool=node.tool,
                    detail=f"参数 {key!r}={number} 超过上限 {limit}",
                    reason=rule.reason,
                )
        return None

    # ── 常用红线 ──────────────────────────────────────────────────────────

    @classmethod
    def default_redlines(cls) -> "ConstraintMemory":
        """白皮书点名的两条示例红线（真实项目按接入方声明，这里给的是**默认最小集**）。"""
        return cls(
            rules=[
                ConstraintRule(
                    id="no-recursive-delete-root",
                    kind="arg_pattern",
                    target="*",
                    spec=r"rm\s+-rf\s+/(\s|$)",
                    reason="禁止递归删除根目录（危险命令拦截）",
                ),
                ConstraintRule(
                    id="transfer-limit",
                    kind="max_number",
                    target="amount",
                    spec="5000",
                    reason="单笔金额上限 5000（转账限额）",
                ),
            ]
        )

def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None

__all__ = [
    "ConstraintError",
    "ConstraintRule",
    "ConstraintMemory",
    "Violation",
]
