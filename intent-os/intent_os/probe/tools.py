"""探针工具（白皮书明确要求 `check_network()` 这种工具形态）。

**只读**：只读环境（PATH / 环境变量 / 文件系统属性 / TCP 连通性），不写任何东西。
**零模型调用**：工具本身是确定性代码；模型只是「主动来取」的一方（RFC 15 §3.1 约束 2）。
**副作用等级 NONE**：接入时声明（不是被调方自述）—— Step 5.2 的路由器直接复用这张表。

──────── D2 边界（RFC 15 §3.1 的警告 · Q23）────────────────────────────

探针工具**不是**状态出口：探针问「环境有什么能力」，
状态断言问「世界 / 任务现在是什么状态」，后者**绝不走工具**。
本模块不 import `intent_os.memory.state`（状态断言模块），也不 import 任何模型客户端。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ..compiler.intent import SideEffectKind
from .environment import EnvironmentSnapshot
from .probes import default_probes, probe_environment

def _dump(payload: Any) -> str:
    """结构化字符串 —— 一律 JSON 文本（不转义中文，缩进 2）。"""
    return json.dumps(payload, ensure_ascii=False, indent=2)

def _probe_section(section: str, snapshot: EnvironmentSnapshot | None = None) -> EnvironmentSnapshot:
    """只探某一段（工具要的是「一段」，不是整份环境）。"""
    if snapshot is not None:
        return snapshot
    probes = default_probes()
    picked = {k: v for k, v in probes.items() if k.split(".", 1)[0] == section}
    return probe_environment(picked)

def check_network(snapshot: EnvironmentSnapshot | None = None) -> str:
    """探测网络能力（HTTP 可达 / 代理 / DNS）→ JSON 文本。

    只读、无副作用（`SideEffectKind.NONE`）。探不到的项记 ``null``（unknown），
    **不假装成 false**。
    """
    snap = _probe_section("network", snapshot)
    return _dump(snap.to_json()["network"])

def check_tools(snapshot: EnvironmentSnapshot | None = None) -> str:
    """探测外部工具（gh / ssh / git）在不在、什么版本 → JSON 文本。"""
    snap = _probe_section("tools", snapshot)
    return _dump(snap.to_json()["tools"])

def check_credentials(snapshot: EnvironmentSnapshot | None = None) -> str:
    """探测凭据（SSH key / gh token）在不在 → JSON 文本。"""
    snap = _probe_section("credentials", snapshot)
    return _dump(snap.to_json()["credentials"])

@dataclass(frozen=True)
class ProbeTool:
    """探针工具的接入声明（名字 → 可调用 + 副作用等级）。

    Step 5.2 的路由器直接复用这张表：工具的「前置条件」从此有据可依，
    副作用等级也**接入时声明**（不是被调方自述）。
    """

    name: str
    call: Callable[..., str]
    side_effect: SideEffectKind
    description: str

#: 探针工具注册表（名字 → 可调用 + 副作用等级）
PROBE_TOOLS: dict[str, ProbeTool] = {
    "check_network": ProbeTool(
        name="check_network",
        call=check_network,
        side_effect=SideEffectKind.NONE,
        description="探测网络能力：HTTP 可达性 / 代理 / DNS（只读）",
    ),
    "check_tools": ProbeTool(
        name="check_tools",
        call=check_tools,
        side_effect=SideEffectKind.NONE,
        description="探测外部工具 gh / ssh / git 是否安装及版本（只读）",
    ),
    "check_credentials": ProbeTool(
        name="check_credentials",
        call=check_credentials,
        side_effect=SideEffectKind.NONE,
        description="探测凭据：SSH 私钥 / GitHub token 是否存在（只读）",
    ),
}

def probe_tool_names() -> tuple[str, ...]:
    """已注册的探针工具名。"""
    return tuple(PROBE_TOOLS)

def get_probe_tool(name: str) -> ProbeTool | None:
    """按名字取探针工具（没有 → None，不静默兜底成别的工具）。"""
    return PROBE_TOOLS.get(name)

def probe_tool_side_effect(name: str) -> SideEffectKind | None:
    """按名字取副作用等级（没有 → None）。"""
    tool = PROBE_TOOLS.get(name)
    return tool.side_effect if tool else None

def call_probe_tool(name: str, snapshot: EnvironmentSnapshot | None = None) -> str:
    """按名字调一个探针工具（路由器的入口形态）。

    未知名字 → 明确报错（不静默、不假装成功）。
    """
    tool = PROBE_TOOLS.get(name)
    if tool is None:
        raise KeyError(f"没有这个探针工具：{name}（已知：{', '.join(PROBE_TOOLS)}）")
    return tool.call(snapshot=snapshot)
