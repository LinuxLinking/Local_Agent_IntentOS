"""环境探针 · 数据形状（RFC 15 §3.1 / §5）。

Step 5.1 的地基：**探针结果是数据，不是提示词**。

──────── 三态是刻意的（红线：不静默降级）────────────────────────────

`ProbeResult.value` 是三态的载体：

| 值       | 含义                                |
|----------|-------------------------------------|
| `True`   | 探测到**有**                        |
| `False`  | 探测到**没有**                      |
| `None`   | **unknown**（探测不到）—— 不是 False |

把 unknown 当 false，会让路由器**误杀**可用路径（RFC 15 §5）。

字符串字段另有一个约定：RFC §3.1 用 `str|null` 表达「没有」，
但那与「null = unknown」冲突（同一个 null 表达两件事，路由器没法判）。
所以本实现取：**空字符串 `""` = 已确认没有；`None` = 探测不到（unknown）。**

──────── D2 边界（RFC 15 §3.1 的警告 · Q23）────────────────────────────

- **探针**问的是「环境有什么能力」（有没有 gh / 能不能上网 / 有没有 SSH key）。
  它是**环境能力**，白皮书明确要求它**可以被做成工具**（`check_network()`）。
- **状态断言**（`intent_os.memory.state` 里的 StateStore）问的是「世界 / 任务现在是什么状态」。
  它是**外置状态**，按 D2 **绝不走工具**。

所以本包**不 import** `intent_os.memory.state`（状态断言模块），也不 import 任何模型客户端 ——
这条边界由 `tests/test_probe_environment.py` 的**源码扫描断言**守住。

探针实现见 `probes.py`，探针工具见 `tools.py`。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

#: 落盘文件名（RFC 15 §3.1：`Environment.json`）
ENVIRONMENT_FILENAME = "Environment.json"

#: 探针覆盖的外部工具（RFC §3.1 点了 gh / ssh / git）
TOOL_NAMES: tuple[str, ...] = ("gh", "ssh", "git")

#: 快照的四个段（RFC §3.1）
SECTIONS: tuple[str, ...] = ("network", "tools", "credentials", "limits")

def utc_now_iso() -> str:
    """当前时间（ISO8601 / UTC）—— 探针结果的统一时间戳来源。"""
    return datetime.now(timezone.utc).isoformat()

@dataclass(frozen=True)
class ProbeResult:
    """单个探针的结果（RFC 15 §5）。

    `value` 是三态：`True` / `False` / `None`(unknown)。
    """

    key: str
    value: str | bool | int | None
    detail: str = ""
    probed_at: str = ""

    @property
    def is_unknown(self) -> bool:
        """value 是 None → **探测不到**（unknown），**不是 false**。"""
        return self.value is None

    @property
    def is_known(self) -> bool:
        """value 不是 None → 探测到了（true 或 false）。"""
        return self.value is not None

    def as_bool(self) -> bool | None:
        """bool 视角的值；非 bool 值（含 unknown）→ None。"""
        if isinstance(self.value, bool):
            return self.value
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "detail": self.detail,
            "probed_at": self.probed_at,
        }

@dataclass(frozen=True)
class EnvironmentSnapshot:
    """一次探测的结果集合（RFC 15 §5 的形状）。"""

    results: tuple[ProbeResult, ...] = ()

    # ── 取值 ──────────────────────────────────────────────────────────────

    def get(self, key: str) -> str | bool | int | None:
        """按 key 取值。**缺失 → None（unknown）**，绝不是 False。

        三态：True / False / None(unknown)。
        """
        for r in self.results:
            if r.key == key:
                return r.value
        return None

    def get_result(self, key: str) -> ProbeResult:
        """按 key 取完整结果（含 detail）。缺失 → 造一条 unknown 结果。"""
        for r in self.results:
            if r.key == key:
                return r
        return ProbeResult(key=key, value=None, detail="not probed")

    def tri_state(self, key: str) -> str:
        """把值翻成三个词之一：``"true"`` / ``"false"`` / ``"unknown"``。"""
        r = self.get_result(key)
        if r.is_unknown:
            return "unknown"
        b = r.as_bool()
        if b is None:
            return "unknown"
        return "true" if b else "false"

    def keys(self) -> tuple[str, ...]:
        """所有被探过的 key（按探测顺序）。"""
        return tuple(r.key for r in self.results)

    def has(self, key: str) -> bool:
        """这个 key 被探过没有（探过 ≠ 探到了值）。"""
        return any(r.key == key for r in self.results)

    @property
    def probed_at(self) -> str:
        """快照时间戳（取第一条结果的时间戳；没有就是空串）。"""
        for r in self.results:
            if r.probed_at:
                return r.probed_at
        return ""

    # ── 落盘形状（RFC 15 §3.1）─────────────────────────────────────────────

    def to_json(self) -> dict[str, Any]:
        """落盘 / 传输形状 —— **严格按 RFC 15 §3.1**。

        顶层键固定五个：network / tools / credentials / limits / probed_at。
        unknown（None）落盘成 ``null``；「已确认没有」的空字符串原样落盘。
        """
        return {
            "network": {
                "http_reachable": self.get("network.http_reachable"),
                "proxy": self.get("network.proxy"),
                "dns_ok": self.get("network.dns_ok"),
            },
            "tools": {
                name: {
                    "installed": self.get(f"tools.{name}.installed"),
                    "version": self.get(f"tools.{name}.version"),
                }
                for name in TOOL_NAMES
            },
            "credentials": {
                "ssh_key_present": self.get("credentials.ssh_key_present"),
                "ssh_key_path": self.get("credentials.ssh_key_path"),
                "gh_token_present": self.get("credentials.gh_token_present"),
            },
            "limits": {
                "api_rate_remaining": self.get("limits.api_rate_remaining"),
                "sandbox_writable": self.get("limits.sandbox_writable"),
            },
            "probed_at": self.probed_at,
        }

    # ── 落盘 / 读回 ───────────────────────────────────────────────────────

    def save(self, path: str | Path) -> Path:
        """落盘成 `Environment.json`（UTF-8 / indent=2 / ensure_ascii=False）。

        `path` 可以是**目录**（写成 `<目录>/Environment.json`），
        也可以是**文件路径**（直接写那个文件）。返回实际写入的路径。
        """
        p = Path(path)
        if p.is_dir() or (not p.exists() and p.suffix == ""):
            p = p / ENVIRONMENT_FILENAME
        p.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_json(), ensure_ascii=False, indent=2) + "\n"
        p.write_text(text, encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "EnvironmentSnapshot":
        """读回落盘文件（`path` 可以是目录，也可以是文件路径）。"""
        p = Path(path)
        if p.is_dir():
            p = p / ENVIRONMENT_FILENAME
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_json(data)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "EnvironmentSnapshot":
        """从 RFC §3.1 的形状还原。

        **每条结果的 `detail` 不落盘**（RFC 的形状里没有它）——
        落盘的是**能力快照**，不是探针日志；要原因得看内存里的结果。
        """
        ts = str(data.get("probed_at") or "")
        results: list[ProbeResult] = []

        net = dict(data.get("network") or {})
        for name in ("http_reachable", "proxy", "dns_ok"):
            results.append(
                ProbeResult(key=f"network.{name}", value=net.get(name), probed_at=ts)
            )

        tools = dict(data.get("tools") or {})
        for tool in TOOL_NAMES:
            entry = dict(tools.get(tool) or {})
            for name in ("installed", "version"):
                results.append(
                    ProbeResult(key=f"tools.{tool}.{name}", value=entry.get(name), probed_at=ts)
                )

        cred = dict(data.get("credentials") or {})
        for name in ("ssh_key_present", "ssh_key_path", "gh_token_present"):
            results.append(
                ProbeResult(key=f"credentials.{name}", value=cred.get(name), probed_at=ts)
            )

        lim = dict(data.get("limits") or {})
        for name in ("api_rate_remaining", "sandbox_writable"):
            results.append(
                ProbeResult(key=f"limits.{name}", value=lim.get(name), probed_at=ts)
            )

        return cls(results=tuple(results))
