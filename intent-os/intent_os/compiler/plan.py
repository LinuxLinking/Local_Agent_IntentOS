"""L4 Plan — Plan DAG + 拓扑序 + 校验 + 模板化生成器。

设计基线：design-foundation/01-concept-model.md §2 L4「Plan = 可执行 DAG」+ §3「Plan 必带验收 Spec」。
开放性结论：06 §1 C4（syscall 列表含 plan）+ §2 C10（Workflow 两层）。

Phase 0 polish（2026-10-19）：
- 节点加 `name`（语义）+ `depends_on`（依赖的 name 列表）→ 真 DAG
- `topological_order()` Kahn 算法
- `validate_plan()` 检测重名 / 缺失依赖 / 环
- runtime 改为按拓扑序执行

Phase 1+：子图 / 并行 / 条件分支（plan 节点可嵌套 Plan）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from .intent import Intent, VerificationKind

@dataclass(frozen=True)
class PlanNode:
    """Plan 的单个节点。

    字段：
    - name：语义标识（plan 内唯一；runtime 用作 key）
    - tool：要调的工具名
    - args：工具参数
    - description：给人看的简短描述
    - depends_on：此节点依赖的其他节点 name 列表
    """

    name: str
    tool: str
    args: dict[str, Any]
    description: str = ""
    depends_on: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tool": self.tool,
            "args": self.args,
            "description": self.description,
            "depends_on": list(self.depends_on),
        }

@dataclass(frozen=True)
class Plan:
    """可执行 Plan = 节点集合（任意 DAG）+ 关联 Intent。"""

    id: str
    intent: Intent
    nodes: tuple[PlanNode, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "intent": self.intent.to_dict(),
            "nodes": [n.to_dict() for n in self.nodes],
        }

# ── 拓扑序 / 校验 ─────────────────────────────────────────────────────────────

def topological_order(plan: Plan) -> list[PlanNode]:
    """Kahn 算法：返回拓扑序。检测到环或缺失依赖抛 ValueError。

    用法：
        try:
            ordered = topological_order(plan)
        except ValueError as e:
            print(f"plan 不可执行: {e}")
    """
    by_name = {n.name: n for n in plan.nodes}

    # 校验依赖指向存在的节点
    for node in plan.nodes:
        for dep in node.depends_on:
            if dep not in by_name:
                raise ValueError(
                    f"node {node.name!r} depends on missing {dep!r}"
                )

    # 入度 + 后继
    in_degree: dict[str, int] = {n.name: len(n.depends_on) for n in plan.nodes}
    children: dict[str, list[str]] = {n.name: [] for n in plan.nodes}
    for node in plan.nodes:
        for dep in node.depends_on:
            children[dep].append(node.name)

    # BFS
    queue: list[str] = [n.name for n in plan.nodes if in_degree[n.name] == 0]
    order: list[PlanNode] = []
    while queue:
        name = queue.pop(0)
        order.append(by_name[name])
        for child in children[name]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(order) != len(plan.nodes):
        pending = [n.name for n in plan.nodes if in_degree[n.name] > 0]
        raise ValueError(f"cycle detected; nodes still pending: {pending}")

    return order

def validate_plan(plan: Plan) -> list[str]:
    """返回错误列表（空 = valid）。包含三类问题：

    - 节点 name 重名
    - 依赖指向不存在的节点
    - 环（通过 topological_order 检测）
    """
    errors: list[str] = []

    # 重名
    seen: set[str] = set()
    for n in plan.nodes:
        if n.name in seen:
            errors.append(f"duplicate node name: {n.name!r}")
        seen.add(n.name)

    # 环 + 缺失依赖
    try:
        topological_order(plan)
    except ValueError as e:
        errors.append(str(e))

    return errors

# ── 模板化 Plan 生成器 ────────────────────────────────────────────────────────

def new_plan_id() -> str:
    """生成一个 Plan id（8 位 hex）。

    **这是 Plan id 的唯一定义** —— 别处要生成 Plan id 就 import 这个，
    不要各自造一份（红线：一个概念只有一处定义）。
    """
    return uuid.uuid4().hex[:8]

def _new_id() -> str:
    """向后兼容的旧名（保留给既有调用方）。新代码请用 `new_plan_id`。"""
    return new_plan_id()

def _build_write_content(goal: str) -> str:
    """Phase 0 的最小代码生成：基于 goal 关键字猜代码。

    Phase 1+ 替换为 LLM 生成 / 模板库 / Skill 调用。
    """
    g = goal.lower()
    if "fizzbuzz" in g or "fizz" in g:
        return (
            '"""FizzBuzz: 3 的倍数 Fizz, 5 的倍数 Buzz, 公倍数 FizzBuzz."""\n'
            "\n"
            "for i in range(1, 16):\n"
            "    if i % 15 == 0:\n"
            '        print("FizzBuzz")\n'
            "    elif i % 3 == 0:\n"
            '        print("Fizz")\n'
            "    elif i % 5 == 0:\n"
            '        print("Buzz")\n'
            "    else:\n"
            "        print(i)\n"
        )
    if "hello" in g:
        return 'print("Hello, World!")\n'
    if "sum" in g or "求和" in goal:
        return (
            "total = sum(range(1, 101))\n"
            'print(f"sum 1..100 = {total}")\n'
        )
    return 'print("Hello, World!")\n'

def _has_verification(intent: Intent, kind: VerificationKind) -> bool:
    return any(v.kind == kind for v in intent.verification)

def _find_write_path(intent: Intent) -> str | None:
    """从 verification 推断要写入 / 运行的目标路径。"""
    for v in intent.verification:
        if v.kind in (VerificationKind.FILE_EXISTS, VerificationKind.FILE_CONTAINS):
            return v.target
        if v.kind == VerificationKind.EXIT_CODE:
            return v.target
    return None

def generate_plan(intent: Intent) -> Plan:
    """根据 Intent 的 verification / side_effects 生成 Plan（含依赖关系）。

    节点命名约定：
    - read_target  ←→  read_file
    - write_target ←→  write_file
    - run_target   ←→  run_python
    - noop         ←→  fallback

    依赖约定（线性）：
        write_target depends_on [read_target]（如 read 存在）
        run_target   depends_on [write_target]（如 write 存在）
    """
    nodes: list[PlanNode] = []
    tools = {s.tool for s in intent.side_effects}
    write_path = _find_write_path(intent)

    # read_file
    read_name: str | None = None
    if "read_file" in tools and write_path:
        read_name = "read_target"
        nodes.append(
            PlanNode(
                name=read_name,
                tool="read_file",
                args={"path": write_path},
                description=f"读取 {write_path}",
                depends_on=(),
            )
        )

    # write_file
    write_name: str | None = None
    if "write_file" in tools and write_path:
        write_name = "write_target"
        deps = (read_name,) if read_name else ()
        nodes.append(
            PlanNode(
                name=write_name,
                tool="write_file",
                args={
                    "path": write_path,
                    "content": _build_write_content(intent.goal),
                },
                description=f"写入 {write_path}",
                depends_on=deps,
            )
        )

    # run_python
    if "run_python" in tools or _has_verification(intent, VerificationKind.EXIT_CODE):
        if write_path:
            run_name = "run_target"
            deps = (write_name,) if write_name else ()
            nodes.append(
                PlanNode(
                    name=run_name,
                    tool="run_python",
                    args={"path": write_path},
                    description=f"执行 {write_path}",
                    depends_on=deps,
                )
            )

    # Fallback
    if not nodes:
        nodes.append(
            PlanNode(
                name="noop",
                tool="noop",
                args={"note": "未识别意图，请在 Approval Gate 阶段补全"},
                description="noop（待补全）",
                depends_on=(),
            )
        )

    return Plan(id=new_plan_id(), intent=intent, nodes=tuple(nodes))
