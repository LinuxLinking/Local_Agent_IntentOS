"""L4 Verification Spec 评估器。

设计基线：design-foundation/01-concept-model.md §2「Verification Spec = 条件 + 证据 + 失败回退」。
开放性结论：06 §2 C12（HITL 拆 7 动作；Verify 是其中 Inspect 的延伸）。

Phase 0：支持 EXIT_CODE / FILE_EXISTS / FILE_CONTAINS 三种 Spec。
Phase 3+：扩展 LLM_JUDGE / DB_QUERY 等。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .intent import Verification, VerificationKind

if TYPE_CHECKING:
    from ..runtime import NodeResult

@dataclass(frozen=True)
class VerifyResult:
    """单个 Spec 的评估结果。"""

    passed: bool
    spec: Verification
    evidence: str  # 给 Audit / 用户看的证据摘要
    actual: Any = None  # 实际值（便于调试）

    def spec_dump(self) -> dict[str, Any]:
        return {
            "kind": self.spec.kind.value,
            "tool": self.spec.tool,
            "target": self.spec.target,
            "expected": self.spec.expected,
        }

class VerificationEvaluator:
    """Phase 0 评估器。从 NodeResult 中提取证据，对比 expected。"""

    def __init__(self, sandbox_dir: Path):
        self.sandbox_dir = Path(sandbox_dir).resolve()

    def evaluate(
        self,
        spec: Verification,
        node_results: dict[str, "NodeResult"],
    ) -> VerifyResult:
        if spec.kind == VerificationKind.EXIT_CODE:
            return self._eval_exit_code(spec, node_results)
        if spec.kind == VerificationKind.FILE_EXISTS:
            return self._eval_file_exists(spec)
        if spec.kind == VerificationKind.FILE_CONTAINS:
            return self._eval_file_contains(spec)
        return VerifyResult(
            passed=False,
            spec=spec,
            evidence=f"unsupported verification kind: {spec.kind.value}",
            actual=None,
        )

    # ── 三种 Spec 的具体评估 ──────────────────────────────────────────────────

    def _eval_exit_code(
        self,
        spec: Verification,
        node_results: dict[str, "NodeResult"],
    ) -> VerifyResult:
        result = node_results.get(spec.tool)
        if result is None:
            return VerifyResult(
                passed=False,
                spec=spec,
                evidence=f"tool {spec.tool!r} was not executed",
                actual=None,
            )
        # 提取退出码（兼容 Phase 0 InlineExecutor 的 "exit_code: N"
        # 与 PyPilotExecutor 的 "退出码: N"）
        m = re.search(r"(?:exit_code|退出码):\s*(-?\d+)", result.output)
        if not m:
            return VerifyResult(
                passed=False,
                spec=spec,
                evidence=f"no exit_code in {spec.tool!r} output",
                actual=result.output,
            )
        actual = int(m.group(1))
        return VerifyResult(
            passed=actual == spec.expected,
            spec=spec,
            evidence=f"{spec.tool} exit_code={actual}, expected={spec.expected}",
            actual=actual,
        )

    def _eval_file_exists(self, spec: Verification) -> VerifyResult:
        path = self._safe_path(spec.target)
        exists = path.exists()
        expected = bool(spec.expected)
        return VerifyResult(
            passed=exists == expected,
            spec=spec,
            evidence=f"{spec.target} exists={exists}, expected={expected}",
            actual=exists,
        )

    def _eval_file_contains(self, spec: Verification) -> VerifyResult:
        path = self._safe_path(spec.target)
        if not path.exists():
            return VerifyResult(
                passed=False,
                spec=spec,
                evidence=f"{spec.target} does not exist",
                actual=None,
            )
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return VerifyResult(
                passed=False,
                spec=spec,
                evidence=f"{spec.target} is not utf-8 text",
                actual=None,
            )
        needle = str(spec.expected)
        found = needle in content
        return VerifyResult(
            passed=found,
            spec=spec,
            evidence=f"{spec.target} contains {needle!r}: {found}",
            actual=found,
        )

    def _safe_path(self, rel: str) -> Path:
        """Phase 0 内嵌的 safe_path（不依赖 PyPilot）。

        规则：
        - 去引号、去前导 ./
        - resolve 到 sandbox_dir 下
        - 用 relative_to 校验越界，越界抛 ValueError
        """
        s = rel.strip().strip("\"'`").lstrip("/")
        if s.startswith("./"):
            s = s[2:]
        path = (self.sandbox_dir / s).resolve()
        try:
            path.relative_to(self.sandbox_dir)
        except ValueError as e:
            raise ValueError(f"path escapes sandbox: {rel!r}") from e
        return path
