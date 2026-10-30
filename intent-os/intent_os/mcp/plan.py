"""MCP 计划 —— 把「要调哪些 MCP 工具」表达成 Intent OS 的 `Plan`。

设计依据：`design-foundation/14-mcp-integration-rfc.md` D-e / D-g

L4 编译器产出的是 `Plan`（节点 DAG），L2 执行器吃的是 `PlanNode`。
本模块只做一件事：把「一次 MCP 工具调用」的声明（`McpCall`）
翻译成 `PlanNode`，让 MCP 工具能**复用**既有的 Plan → 审批 → 执行 → 验收链路，
而不是另搞一套。

命名沿用 D-g 的 `mcp:<server>:<tool>` —— 防止与内置工具撞名，
审计里也一眼看得出这节点是 MCP 来的。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..compiler.intent import Intent
from ..compiler.plan import Plan, PlanNode, new_plan_id
from .registry import MCP_PREFIX

@dataclass(frozen=True)
class McpCall:
    """一次 MCP 工具调用的声明（`PlanNode` 的前身）。

    字段与 `PlanNode` 一一对应，只是名字更贴近「MCP 调用」这件事。
    """

    name: str  # 节点名（plan 内唯一）
    tool: str  # "mcp:fs:read_file"
    args: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    depends_on: tuple[str, ...] = ()

def build_mcp_plan(
    intent: Intent,
    calls: Sequence[McpCall],
    *,
    plan_id: str | None = None,
) -> Plan:
    """把 `McpCall` 序列编成一个 `Plan`（节点 DAG + 关联 Intent）。

    `plan_id` 缺省就随机生成 8 位 hex —— 用 `compiler/plan.py` 的 `new_plan_id`
    （Plan id 的**唯一定义**，别处不另造一份）。

    工具名必须是 `mcp:<server>:<tool>`：编计划时就拦下拼错的工具名，
    别等到执行期才发现「这不是个 MCP 工具」。
    """
    nodes: list[PlanNode] = []
    for call in calls:
        if not call.tool.startswith(MCP_PREFIX):
            raise ValueError(
                f"MCP 计划里的工具名必须是 mcp:<server>:<tool>（RFC 14 D-g）："
                f"{call.tool!r}"
            )
        nodes.append(
            PlanNode(
                name=call.name,
                tool=call.tool,
                args=dict(call.args),
                description=call.description,
                depends_on=tuple(call.depends_on),
            )
        )
    return Plan(id=plan_id or new_plan_id(), intent=intent, nodes=tuple(nodes))
