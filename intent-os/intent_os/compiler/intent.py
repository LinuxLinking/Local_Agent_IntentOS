"""L4 Intent — Intent 数据结构 + 模板化解析。

设计基线：design-foundation/02-terminology.md §1「Intent / Verification Spec / Side Effect」。
开放性结论：06 §1 C4（syscall 列表）+ §2 C10（Workflow 两层划分）。

Phase 0：模板化解析（regex 模式）。
Phase 3：替换为 qwen3.5:0.8b 原生 FC 解析（Step 3.1）。
Phase 3.5：Intent 长出 `premises` —— 动手前声明的前提（见 `compiler/premise.py`）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .premise import Premise

class SideEffectKind(str, Enum):
    """工具副作用粗分类。

    Phase 0 用 4 级（none / read / write / external）。
    颗粒度扩展见 design-foundation/04-open-questions.md Q8。
    """

    NONE = "none"
    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"

class VerificationKind(str, Enum):
    """验收 Spec 类型。Phase 0 支持三种；扩展见 06 §3 Q12。"""

    EXIT_CODE = "exit_code"
    FILE_EXISTS = "file_exists"
    FILE_CONTAINS = "file_contains"
    # LLM_JUDGE = "llm_judge"   # Phase 3+ 启用

@dataclass(frozen=True)
class Constraint:
    """意图的硬约束。"""

    kind: str  # "scope" | "non_destructive" | "single_file" | ...
    value: str

@dataclass(frozen=True)
class Verification:
    """验收规格 = 条件 + 证据 + 失败回退。

    Phase 0 简化：tool 字段指定哪个 tool 的输出作为证据。
    """

    kind: VerificationKind
    tool: str  # 哪个 tool 的输出作为证据
    target: str  # path / pattern
    expected: Any  # EXIT_CODE: int(0); FILE_EXISTS: True; FILE_CONTAINS: str

@dataclass(frozen=True)
class SideEffect:
    """副作用声明 = 工具 + 范围 + 类型 + 可逆性。"""

    tool: str
    scope: str  # "sandbox/*" | "none"
    kind: SideEffectKind = SideEffectKind.NONE
    reversibility: str = "easy"  # "easy" | "medium" | "hard"

@dataclass(frozen=True)
class Intent:
    """Intent = 目标 + 约束 + 验收 + 副作用声明（开放性结论 C1/C4）。

    不要求三者都显式，但 parse_intent 必须能补全；不可补全时 fallback
    为 goal-only Intent（要求用户在 Approval Gate 阶段补全）。
    """

    raw: str
    goal: str
    constraints: tuple[Constraint, ...] = field(default_factory=tuple)
    verification: tuple[Verification, ...] = field(default_factory=tuple)
    side_effects: tuple[SideEffect, ...] = field(default_factory=tuple)
    # 动手前声明的前提（Phase 3.5 · 暴论 #17）。
    # 与 verification 平行：verification 验收「做对了没」，premise 校验「开始前的前提对不对」。
    premises: tuple[Premise, ...] = field(default_factory=tuple)
    # 编译器对模型输出的修正痕迹（Phase 3.2 · V1/V2 后置校验）。
    # 形如 "target_drift:model=...;corrected=...;rule=..."，
    # 随 to_dict() 进入 AuditTrail —— 模型被纠正过这件事必须留痕。
    compiler_notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "goal": self.goal,
            "constraints": [{"kind": c.kind, "value": c.value} for c in self.constraints],
            "verification": [
                {
                    "kind": v.kind.value,
                    "tool": v.tool,
                    "target": v.target,
                    "expected": v.expected,
                }
                for v in self.verification
            ],
            "side_effects": [
                {
                    "tool": s.tool,
                    "scope": s.scope,
                    "kind": s.kind.value,
                    "reversibility": s.reversibility,
                }
                for s in self.side_effects
            ],
            "compiler_notes": list(self.compiler_notes),
            "premises": [p.to_dict() for p in self.premises],
        }

# ── 模板化解析器（Phase 0）────────────────────────────────────────────────────
# Phase 3 替换为 LLM 解析（Step 3.1）。模板覆盖最常见的 3 个意图：
#   1. "写 X 存 path" → write + run + verify
#   2. "跑 path"      → run + verify
#   3. "修 path"      → read + write + run + verify

@dataclass(frozen=True)
class _Template:
    pattern: re.Pattern[str]
    build: Callable[[re.Match[str]], Intent]

_TEMPLATES: list[_Template] = [
    # "写 X 存 path (然后跑一下|并验证)?" —— 主模板
    #
    # 「存到 / 存进 / 存入 / 保存到 / 写入到」都要能匹配。
    # 之前只认「存」紧跟路径，README 里写的「写个 fizzbuzz 存到 fizz.py 然后跑一下」
    # 会整句落空 → fallback 成 goal-only Intent（verification / side_effects 全空，
    # 违反「Plan 必带验收」的设计基线）。
    _Template(
        pattern=re.compile(
            r"写(?:个|一个)?\s*(?P<goal>.+?)\s*"
            r"(?:存|保存|写入)(?:到|进|入)?\s*(?P<path>\S+\.py)"
            r"(?:\s*(?:然后|并)?\s*(?:跑(?:一下)?|运行|执行|验证))?",
            re.UNICODE,
        ),
        build=lambda m: Intent(
            raw=m.group(0),
            goal=f"生成 {m['goal'].strip()} 并验证可运行",
            constraints=(Constraint("scope", "sandbox/*"),),
            verification=(
                Verification(VerificationKind.FILE_EXISTS, "write_file", m["path"], True),
                Verification(VerificationKind.EXIT_CODE, "run_python", m["path"], 0),
            ),
            side_effects=(
                SideEffect("write_file", "sandbox/*", SideEffectKind.WRITE, "easy"),
                SideEffect("run_python", "sandbox/*", SideEffectKind.EXTERNAL, "easy"),
            ),
        ),
    ),
    # "跑 path"
    _Template(
        pattern=re.compile(r"(?:跑|运行|执行)\s+(?P<path>\S+\.py)", re.UNICODE),
        build=lambda m: Intent(
            raw=m.group(0),
            goal=f"运行 {m['path']}",
            verification=(
                Verification(VerificationKind.EXIT_CODE, "run_python", m["path"], 0),
            ),
            side_effects=(
                SideEffect("run_python", "sandbox/*", SideEffectKind.EXTERNAL, "easy"),
            ),
        ),
    ),
    # "修 path" / "修复 path"
    _Template(
        pattern=re.compile(r"(?:修|修复|改)\s+(?P<path>\S+\.py)", re.UNICODE),
        build=lambda m: Intent(
            raw=m.group(0),
            goal=f"修复 {m['path']} 并验证",
            verification=(
                Verification(VerificationKind.EXIT_CODE, "run_python", m["path"], 0),
            ),
            side_effects=(
                SideEffect("read_file", "sandbox/*", SideEffectKind.READ, "easy"),
                SideEffect("write_file", "sandbox/*", SideEffectKind.WRITE, "easy"),
                SideEffect("run_python", "sandbox/*", SideEffectKind.EXTERNAL, "easy"),
            ),
        ),
    ),
]

def parse_intent(raw: str) -> Intent:
    """模板化解析。匹配不到模板 → fallback 为 goal-only Intent。

    Fallback Intent 没有 verification（要求 Approval Gate 阶段补全）。
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("Intent must be non-empty")

    for tmpl in _TEMPLATES:
        m = tmpl.pattern.search(raw)
        if m:
            return tmpl.build(m)

    # Fallback：整个 raw 当 goal
    return Intent(raw=raw, goal=raw)
