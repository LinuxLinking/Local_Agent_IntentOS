"""SOP 图纸记忆（RFC 15 §3.4-D · 白皮书 §2.4「大学生工程师」）。

**大脑有限，但靠图纸培养能力**：高频任务固化成 DAG，模型只做**填空**
（提取变量），不做自由发挥 —— 这一步是「不训练模型」前提下让系统越跑越熟的
唯一办法（D1：不对大脑动手术）。

──────── 三条硬约束 ────────────────────────────────────────────────────────

1. **骨架不许变**：`fill()` 只替换 `args` 里的占位符 `{slot}`；
   节点名 / 工具 / 依赖关系**原样保留**。模型填错一个变量 → 报错，
   **不许**它顺手改步骤（改了就不叫 SOP 了）。
2. **零模型调用**：渲染 = 纯字符串替换 + 校验，不碰模型。
3. **不静默降级**：缺必填槽 / 多给 key / 值不匹配正则 / 占位符没有对应槽
   → 一律 `SopError`（不猜、不兜底）。

──────── 与其他概念的分工 ──────────────────────────────────────────────────

| 概念 | 是什么 | 不是什么 |
|---|---|---|
| **SOP 图纸**（本模块） | **步骤骨架**：先做什么后做什么，以及哪些是变量 | 不是记忆检索（那是 `ExperienceBook`） |
| 经验错题本 | **历史教训**：哪条路在这个环境里走过不通 | 不是步骤模板 |
| 约束记忆 | **红线**：代码层拦得住的事 | 不是建议（提示词管不了红线） |
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..compiler.intent import Intent
from ..compiler.plan import Plan, PlanNode, topological_order, validate_plan

class SopError(ValueError):
    """SOP 模板/填空的错误（**一律如实报错** —— 不静默兜底）。"""

#: 占位符语法：`{slot_name}`
_SLOT_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

@dataclass(frozen=True)
class SlotSpec:
    """一个「空」——模型只许填这里。"""

    name: str
    #: 值的校验正则（空 = 不校验）
    pattern: str = ""
    required: bool = True
    description: str = ""

    def validate_value(self, value: Any) -> str:
        text = str(value)
        if not text.strip():
            raise SopError(f"槽 {self.name!r} 的值是空的（必填）")
        if self.pattern:
            try:
                ok = re.fullmatch(self.pattern, text) is not None
            except re.error as exc:  # 模板自己写错了正则
                raise SopError(f"槽 {self.name!r} 的校验正则不合法：{exc}") from exc
            if not ok:
                raise SopError(
                    f"槽 {self.name!r} 的值 {text!r} 不匹配 {self.pattern!r}"
                )
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pattern": self.pattern,
            "required": self.required,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SlotSpec":
        return cls(
            name=str(data["name"]),
            pattern=str(data.get("pattern", "")),
            required=bool(data.get("required", True)),
            description=str(data.get("description", "")),
        )

@dataclass(frozen=True)
class SopNode:
    """图纸上的一个步骤（工具 + 参数模板）。"""

    name: str
    tool: str
    #: 参数模板：值里可以写 `{slot}`（渲染时替换）
    args: Mapping[str, str] = field(default_factory=dict)
    description: str = ""
    depends_on: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tool": self.tool,
            "args": dict(self.args),
            "description": self.description,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SopNode":
        return cls(
            name=str(data["name"]),
            tool=str(data["tool"]),
            args={str(k): str(v) for k, v in (data.get("args") or {}).items()},
            description=str(data.get("description", "")),
            depends_on=tuple(str(d) for d in (data.get("depends_on") or ())),
        )

@dataclass(frozen=True)
class SopTemplate:
    """一张图纸：**固定骨架 + 若干空**。"""

    id: str
    name: str = ""
    slots: tuple[SlotSpec, ...] = ()
    nodes: tuple[SopNode, ...] = ()
    version: int = 1

    # ── 自检 ──────────────────────────────────────────────────────────────

    def slot_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.slots)

    def placeholders(self) -> frozenset[str]:
        """骨架里用到的全部占位符（用来查「模板自己漏了槽」）。"""
        used: set[str] = set()
        for node in self.nodes:
            for value in node.args.values():
                used.update(_SLOT_RE.findall(str(value)))
        return frozenset(used)

    def audit(self) -> list[str]:
        """模板自检：返回问题清单（空 = 没问题）。

        查三件事：依赖指向的节点存在 / 图无环 / 占位符都有对应槽。
        """
        problems: list[str] = []
        names = [n.name for n in self.nodes]
        if len(set(names)) != len(names):
            problems.append("节点名重复")
        declared = set(self.slot_names())
        for node in self.nodes:
            for dep in node.depends_on:
                if dep not in names:
                    problems.append(f"节点 {node.name!r} 依赖了不存在的 {dep!r}")
        undeclared = self.placeholders() - declared
        if undeclared:
            problems.append(f"骨架用了没声明的槽：{sorted(undeclared)}")
        if self.nodes:
            try:
                validate_plan(
                    Plan(
                        id=f"sop:{self.id}",
                        intent=Intent(raw="", goal=""),
                        nodes=tuple(
                            PlanNode(
                                name=n.name,
                                tool=n.tool,
                                args=dict(n.args),
                                description=n.description,
                                depends_on=n.depends_on,
                            )
                            for n in self.nodes
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001 —— 自检要报出来，不抛给调用方
                problems.append(f"图不合法：{exc}")
        return problems

    # ── 填空 ──────────────────────────────────────────────────────────────

    def fill(
        self,
        values: Mapping[str, Any],
        *,
        intent: Intent | None = None,
    ) -> Plan:
        """**只填变量**，渲染成可执行的 `Plan`。

        - 少填必填槽 → `SopError`
        - 多给 key（模板里没有这个槽）→ `SopError`（不许瞎塞）
        - 值不匹配槽的正则 → `SopError`
        - 骨架里的占位符没有对应槽 → `SopError`（模板自己坏了）
        """
        problems = self.audit()
        if problems:
            raise SopError(f"SOP {self.id!r} 模板不合法：{problems}")

        declared = {s.name: s for s in self.slots}
        unknown = set(values) - set(declared)
        if unknown:
            raise SopError(f"SOP {self.id!r} 没有这些槽：{sorted(unknown)}")

        rendered: dict[str, str] = {}
        missing: list[str] = []
        for slot in self.slots:
            if slot.name in values:
                rendered[slot.name] = slot.validate_value(values[slot.name])
            elif slot.required:
                missing.append(slot.name)
        if missing:
            raise SopError(f"SOP {self.id!r} 缺必填槽：{missing}")

        nodes: list[PlanNode] = []
        for node in self.nodes:
            args: dict[str, Any] = {}
            for key, template in node.args.items():
                args[key] = _render(template, rendered, sop_id=self.id, node=node.name)
            nodes.append(
                PlanNode(
                    name=node.name,
                    tool=node.tool,
                    args=args,
                    description=node.description,
                    depends_on=node.depends_on,
                )
            )

        plan_intent = intent if intent is not None else Intent(
            raw=f"sop:{self.id}", goal=self.name or self.id
        )
        plan = Plan(id=f"sop:{self.id}", intent=plan_intent, nodes=tuple(nodes))
        # 骨架必须能算拓扑序（图纸不合法就别拿去跑）
        topological_order(plan)
        return plan

    # ── 序列化 ────────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "id": self.id,
            "name": self.name,
            "slots": [s.to_dict() for s in self.slots],
            "nodes": [n.to_dict() for n in self.nodes],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SopTemplate":
        try:
            return cls(
                id=str(data["id"]),
                name=str(data.get("name", "")),
                slots=tuple(SlotSpec.from_dict(s) for s in (data.get("slots") or ())),
                nodes=tuple(SopNode.from_dict(n) for n in (data.get("nodes") or ())),
                version=int(data.get("version", 1)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SopError(f"SOP 模板读不出来：{exc}") from exc

def _render(template: Any, values: Mapping[str, str], *, sop_id: str, node: str) -> str:
    """把 `{slot}` 换掉；**没填的槽直接报错**（绝不把 `{x}` 原样带进执行）。"""
    text = str(template)

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise SopError(f"SOP {sop_id!r} 的节点 {node!r} 用了没填的槽 {name!r}")
        return values[name]

    return _SLOT_RE.sub(_sub, text)

class SopRegistry:
    """图纸册：注册 / 查找 / 列出（**一个概念一处定义**：只在册子里）。"""

    def __init__(self, templates: Iterable[SopTemplate] = ()) -> None:
        self._by_id: dict[str, SopTemplate] = {}
        for t in templates:
            self.register(t)

    def register(self, template: SopTemplate) -> None:
        problems = template.audit()
        if problems:
            raise SopError(f"SOP {template.id!r} 不合法，不许注册：{problems}")
        if template.id in self._by_id:
            raise SopError(f"SOP {template.id!r} 已经注册过（不静默覆盖）")
        self._by_id[template.id] = template

    def get(self, sop_id: str) -> SopTemplate | None:
        return self._by_id.get(sop_id)

    def require(self, sop_id: str) -> SopTemplate:
        template = self.get(sop_id)
        if template is None:
            raise SopError(f"没有这张图纸：{sop_id!r}（已知：{sorted(self._by_id)}）")
        return template

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_id))

    def fill(self, sop_id: str, values: Mapping[str, Any], *, intent: Intent | None = None) -> Plan:
        return self.require(sop_id).fill(values, intent=intent)

#: 演示用图纸：写一个文件然后跑它（骨架固定，只有路径/内容/命令是变量）
def demo_write_and_run_sop() -> SopTemplate:
    return SopTemplate(
        id="write-and-run",
        name="写文件然后跑一遍",
        slots=(
            SlotSpec(
                name="path",
                pattern=r"sandbox/[A-Za-z0-9_.-]+\.py",
                description="沙盒内的目标文件（只许 sandbox/ 下）",
            ),
            SlotSpec(name="content", description="文件内容（整段代码）"),
        ),
        nodes=(
            SopNode(
                name="write",
                tool="write_file",
                args={"path": "{path}", "content": "{content}"},
                description="写目标文件",
            ),
            SopNode(
                name="run",
                tool="run_python",
                args={"path": "{path}"},
                description="跑一遍验证",
                depends_on=("write",),
            ),
        ),
    )

__all__ = [
    "SopError",
    "SlotSpec",
    "SopNode",
    "SopTemplate",
    "SopRegistry",
    "demo_write_and_run_sop",
]
