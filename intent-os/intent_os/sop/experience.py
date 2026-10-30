"""经验错题本（RFC 15 §3.4-D · §9 Q21）。

白皮书的「大学生工程师」：**智力有限，但靠经验与图纸培养能力**。
这个模块管「经验」—— 每条经验都**带环境指纹**：同一个工具在这台机器上走过不通，
换台机器不一定不通（RFC 15 §9 Q21：「跨任务进经验错题本，**带环境指纹**才复用」）。

──────── 三个用途 ──────────────────────────────────────────────────────────

1. **规划前检索**：`lookup(capability, env)` —— 这个能力在这个环境里，历史上哪条路走不通。
2. **重路由排除**：`banned_tools(capability, env)` —— 5.3 重算路径时把「历史失败边」一起 ban 掉，
   别把时间浪费在明知不通的路上（**只在本环境指纹相同时才复用**）。
3. **留痕**：`record_run(...)` 从一次 `RunResult` 里把失败/被拒的节点记成经验。

──────── 三条红线 ──────────────────────────────────────────────────────────

1. **零模型调用**：记录与检索都是纯确定性代码（不训练、不蒸馏，D1）。
2. **D2 边界**：经验是**台账**，不是状态断言 —— 不 import `memory/state.py`。
3. **不静默降级**：读不出来的经验文件 → 如实报错（`ExperienceError`）。
"""
from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..compiler.plan import Plan
from ..probe.environment import EnvironmentSnapshot
from ..router.metadata import normalize_capability
from ..types import RunResult

class ExperienceError(ValueError):
    """经验文件读不出来 / 格式不对 —— **如实报错**。"""

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

#: 进指纹的环境键（**刻意只取「会影响路由判断」的那些**）
_FINGERPRINT_KEYS: tuple[str, ...] = (
    "network.http_reachable",
    "tools.gh.installed",
    "tools.git.installed",
    "tools.ssh.installed",
    "credentials.gh_token_present",
    "credentials.ssh_key_present",
)

@dataclass(frozen=True)
class EnvFingerprint:
    """**环境指纹** —— 经验的复用边界（换了环境，旧经验不算数）。"""

    parts: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_snapshot(cls, env: EnvironmentSnapshot | None) -> "EnvFingerprint":
        if env is None:
            return cls(parts=(("os", platform.system()),))
        parts: list[tuple[str, str]] = [("os", platform.system())]
        for key in _FINGERPRINT_KEYS:
            parts.append((key, env.tri_state(key)))
        return cls(parts=tuple(parts))

    def key(self) -> str:
        """稳定短指纹（sha256 前 12 位）—— 落盘/比对都只认它。"""
        raw = json.dumps(dict(self.parts), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict[str, str]:
        return dict(self.parts)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EnvFingerprint":
        return cls(parts=tuple(sorted((str(k), str(v)) for k, v in data.items())))

@dataclass(frozen=True)
class Experience:
    """一条经验（错题）。"""

    capability: str
    tool: str
    #: "failed"（跑了但不通）/ "denied"（被审批拦下）
    outcome: str
    reason: str = ""
    env_key: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    task_id: str = ""
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "tool": self.tool,
            "outcome": self.outcome,
            "reason": self.reason,
            "env_key": self.env_key,
            "env": dict(self.env),
            "task_id": self.task_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Experience":
        for key in ("capability", "tool", "outcome"):
            if key not in data:
                raise ExperienceError(f"经验条目损坏：缺少字段 {key!r}")
        return cls(
            capability=str(data["capability"]),
            tool=str(data["tool"]),
            outcome=str(data["outcome"]),
            reason=str(data.get("reason", "")),
            env_key=str(data.get("env_key", "")),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            task_id=str(data.get("task_id", "")),
            created_at=str(data.get("created_at", "")),
        )

class ExperienceBook:
    """经验错题本（内存 + 可选 JSONL 落盘）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._entries: list[Experience] = []
        if self.path is not None and self.path.exists():
            self._entries.extend(self._load_jsonl(self.path))

    # ── 记 ────────────────────────────────────────────────────────────────

    def record(self, experience: Experience) -> None:
        # 能力名**归一**（`capability:git_clone` 与 `git_clone` 是同一个东西 ——
        # 归一规则只有 `router/metadata.py` 一处定义，这里复用，不另立一套）。
        if experience.capability:
            experience = Experience(
                capability=normalize_capability(experience.capability),
                tool=experience.tool,
                outcome=experience.outcome,
                reason=experience.reason,
                env_key=experience.env_key,
                env=experience.env,
                task_id=experience.task_id,
                created_at=experience.created_at,
            )
        self._entries.append(experience)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(experience.to_dict(), ensure_ascii=False) + "\n")

    def record_run(
        self,
        result: RunResult,
        plan: Plan,
        *,
        capability: str,
        env: EnvironmentSnapshot | None = None,
        task_id: str = "",
    ) -> int:
        """从一次 run 里把**失败/被拒的节点**记成经验（返回记了几条）。"""
        fingerprint = EnvFingerprint.from_snapshot(env)
        by_name = {n.name: n for n in plan.nodes}
        outcome = "denied" if result.status == "denied" else "failed"
        recorded = 0
        for node_name, node_result in result.node_results.items():
            if node_result.ok:
                continue
            node = by_name.get(node_name)
            if node is None:
                continue
            self.record(
                Experience(
                    capability=capability,
                    tool=node.tool,
                    outcome=outcome,
                    reason=node_result.error or "",
                    env_key=fingerprint.key(),
                    env=fingerprint.to_dict(),
                    task_id=task_id,
                )
            )
            recorded += 1
        return recorded

    # ── 查 ────────────────────────────────────────────────────────────────

    def entries(self) -> tuple[Experience, ...]:
        return tuple(self._entries)

    def lookup(
        self,
        capability: str,
        env: EnvironmentSnapshot | None = None,
        *,
        outcome: str | None = None,
    ) -> tuple[Experience, ...]:
        """**规划前检索**：这个能力在这个环境里的历史教训。

        给了 `env` → **只返回同指纹的**（Q21：换环境就不算数）；
        不给 `env` → 返回全部（调用方自己判断，但这是「宽口径」，别拿去做排除）。
        """
        env_key = EnvFingerprint.from_snapshot(env).key() if env is not None else None
        wanted = normalize_capability(capability) if capability else ""
        out: list[Experience] = []
        for e in self._entries:
            if e.capability and e.capability != wanted:
                continue
            if env_key is not None and e.env_key != env_key:
                continue
            if outcome is not None and e.outcome != outcome:
                continue
            out.append(e)
        return tuple(out)

    def banned_tools(
        self, capability: str, env: EnvironmentSnapshot | None = None
    ) -> frozenset[str]:
        """历史失败过的工具（**同指纹才算**）—— 给 5.3 重路由当 banned 边用。"""
        return frozenset(
            e.tool for e in self.lookup(capability, env, outcome="failed")
        )

    # ── 落盘 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _load_jsonl(path: Path) -> list[Experience]:
        entries: list[Experience] = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ExperienceError(f"经验文件读不出来：{path}（{exc}）") from exc
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(Experience.from_dict(json.loads(line)))
            except json.JSONDecodeError as exc:
                raise ExperienceError(
                    f"经验文件第 {lineno} 行不是合法 JSON：{path}（{exc}）"
                ) from exc
        return entries

__all__ = [
    "EnvFingerprint",
    "Experience",
    "ExperienceBook",
    "ExperienceError",
]
