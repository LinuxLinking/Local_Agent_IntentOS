"""Intent OS — L4 Intent Compiler + L1 Trust Layer on top of PyPilot L2.

Phase 0 (2026-10-19): 独立可测的骨架，不依赖 PyPilot。
Phase 1 (2026-10-19): 将 PyPilot 作为 L2 executor 接入（PyPilotExecutor）。
Phase 4.1 (2026-10-28): MCP 总线接入（接入声明 + ToolExecutor 实现 —— 状态通道不走 MCP，D2）。

对应 design-foundation/06-open-conclusions-r1.md 的开放性结论。

──────── 边界（欠账 #16 · D2 风格）───────────────────────────────────────────
包初始化**不再连带 import LLM 编译器**（`intent_os.compiler.intent_llm`）：
探针（`intent_os.probe`）是零模型调用的确定性代码，不该被模型客户端
（langchain-ollama / pydantic schema / httpx）一起拖下水 —— 编译器**谁用谁 import**。
公开 API 一个不少：改用 PEP 562 模块级 `__getattr__` **惰性导入**；
`intent_os.__all__` 里的名字、绑定对象、行为与改之前**完全一致**（惰性 ≠ 换实现）。
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

# 公开名字 → 定义它的子模块（惰性导入路由表；一个概念一处定义）
_LAZY_ATTRS: dict[str, str] = {
    # ── L4 Intent（模板解析 · 纯本地 · 无模型） ──
    "Constraint": "intent_os.compiler.intent",
    "Intent": "intent_os.compiler.intent",
    "SideEffect": "intent_os.compiler.intent",
    "SideEffectKind": "intent_os.compiler.intent",
    "Verification": "intent_os.compiler.intent",
    "VerificationKind": "intent_os.compiler.intent",
    "parse_intent": "intent_os.compiler.intent",
    # ── L4 LLM 编译器（**惰性的重点**：谁用谁 import，探针包不牵连） ──
    "LAST_CALL_STATS": "intent_os.compiler.intent_llm",
    "LAST_COMPILER_DRIFT": "intent_os.compiler.intent_llm",
    "LAST_LLM_ERROR": "intent_os.compiler.intent_llm",
    "parse_intent_auto": "intent_os.compiler.intent_llm",
    "parse_intent_llm": "intent_os.compiler.intent_llm",
    "schema_to_intent": "intent_os.compiler.intent_llm",
    # ── Plan / 验收 ──
    "Plan": "intent_os.compiler.plan",
    "PlanNode": "intent_os.compiler.plan",
    "generate_plan": "intent_os.compiler.plan",
    "topological_order": "intent_os.compiler.plan",
    "validate_plan": "intent_os.compiler.plan",
    "VerificationEvaluator": "intent_os.compiler.verify",
    "VerifyResult": "intent_os.compiler.verify",
    # ── 前提（暴论 #17） ──
    "Premise": "intent_os.compiler.premise",
    "PremiseSchema": "intent_os.compiler.premise",
    "PremiseChecker": "intent_os.trust.premise",
    "PremiseCheckResult": "intent_os.trust.premise",
    "PremiseConflict": "intent_os.trust.premise",
    # ── 域适配器（空间域） ──
    "Corridor": "intent_os.domains.maze",
    "Maze": "intent_os.domains.maze",
    "MoveResult": "intent_os.domains.maze",
    "Room": "intent_os.domains.maze",
    # ── 执行器 ──
    "InlineExecutor": "intent_os.executor",
    "PyPilotExecutor": "intent_os.executor",
    "ToolExecutor": "intent_os.executor",
    "SnapshotStore": "intent_os.executor.snapshot",
    # ── MCP 总线 ──
    "McpCall": "intent_os.mcp",
    "McpCallResult": "intent_os.mcp",
    "McpClient": "intent_os.mcp",
    "McpExecutor": "intent_os.mcp",
    "McpRegistry": "intent_os.mcp",
    "McpServerSpec": "intent_os.mcp",
    "McpToolSpec": "intent_os.mcp",
    "build_mcp_plan": "intent_os.mcp",
    "default_fs_registry": "intent_os.mcp",
    # ── L3 长期记忆 + 上下文虚拟内存 + 权威状态源 ──
    "AGENT_FRAME": "intent_os.memory",
    "ContextItem": "intent_os.memory",
    "ContextManager": "intent_os.memory",
    "Embedder": "intent_os.memory",
    "ForgettingPolicy": "intent_os.memory",
    "Frame": "intent_os.memory",
    "HashEmbedder": "intent_os.memory",
    "MemoryEvent": "intent_os.memory",
    "MemoryRecord": "intent_os.memory",
    "MemoryStore": "intent_os.memory",
    "OllamaEmbedder": "intent_os.memory",
    "RelationResolver": "intent_os.memory",
    "ScoredMemory": "intent_os.memory",
    "StateAssertion": "intent_os.memory",
    "StateConflict": "intent_os.memory",
    "StateEvent": "intent_os.memory",
    "StateRenderer": "intent_os.memory",
    "StateStore": "intent_os.memory",
    "StateView": "intent_os.memory",
    "WORLD_FRAME": "intent_os.memory",
    # ── 运行时 ──
    "IntentOSRuntime": "intent_os.runtime",
    "NodeResult": "intent_os.runtime",
    "RunResult": "intent_os.runtime",
    # ── 状态接口（D2 的推 / 拉） ──
    "AuditSink": "intent_os.state_interface",
    "StateInterface": "intent_os.state_interface",
    # ── 信任层 ──
    "ApprovalGate": "intent_os.trust.approval",
    "AUDIT_KEY_ENV": "intent_os.trust.audit",
    "AuditTrail": "intent_os.trust.audit",
    "Event": "intent_os.trust.audit",
    "GENESIS_HASH": "intent_os.trust.audit",
    "compute_hash": "intent_os.trust.audit",
    "read_events": "intent_os.trust.audit",
    "seal_log": "intent_os.trust.audit",
    "verify_chain": "intent_os.trust.audit",
}

# 改之前由包初始化顺带 import 的子模块 —— 保持 `intent_os.<子模块>` 属性访问可用
_LAZY_SUBMODULES: frozenset[str] = frozenset(
    {
        "compiler",
        "domains",
        "executor",
        "mcp",
        "memory",
        "probe",
        "reroute",
        "router",
        "runtime",
        "sop",
        "state_interface",
        "suspend",
        "trust",
        "types",
    }
)

__all__ = [
    "Intent",
    "Constraint",
    "Verification",
    "VerificationKind",
    "SideEffect",
    "SideEffectKind",
    "parse_intent",
    "Plan",
    "PlanNode",
    "generate_plan",
    "topological_order",
    "validate_plan",
    "VerificationEvaluator",
    "VerifyResult",
    "AuditTrail",
    "ApprovalGate",
    "Event",
    # Phase 2.1: 补偿
    "SnapshotStore",
    # Phase 2.4: WORM
    "read_events",
    "verify_chain",
    "seal_log",
    "compute_hash",
    "GENESIS_HASH",
    "AUDIT_KEY_ENV",
    "IntentOSRuntime",
    "NodeResult",
    "RunResult",
    # Phase 1: executors
    "ToolExecutor",
    "InlineExecutor",
    "PyPilotExecutor",
    # Phase 3: LLM Intent Compiler
    "parse_intent_llm",
    "parse_intent_auto",
    "schema_to_intent",
    "LAST_CALL_STATS",
    "LAST_LLM_ERROR",
    # Phase 3.2: V1/V2 后置校验的修正记录
    "LAST_COMPILER_DRIFT",
    # Phase 3.3: L3 长期记忆（语义文件系统）
    "MemoryStore",
    "MemoryRecord",
    "ScoredMemory",
    "MemoryEvent",
    "ForgettingPolicy",
    "Embedder",
    "HashEmbedder",
    "OllamaEmbedder",
    # Phase 3.4: 上下文虚拟内存
    "ContextManager",
    "ContextItem",
    # Phase 3.5: 前提声明与校验（暴论 #17）
    "Premise",
    "PremiseSchema",
    "PremiseChecker",
    "PremiseCheckResult",
    "PremiseConflict",
    "StateAssertion",
    # Phase 3.5.1: 状态断言（权威状态源 + Q17 双表示渲染）
    "StateStore",
    "StateConflict",
    "StateEvent",
    "StateRenderer",
    "StateView",
    "RelationResolver",
    "Frame",
    "WORLD_FRAME",
    "AGENT_FRAME",
    # Phase 3.5.2: 状态接口（D2 的推 / 拉 —— 独立于 Tool / MCP）
    "StateInterface",
    "AuditSink",
    # Phase 3.5.5: 域适配器（空间域 —— 机制不绑域，换域不换机制）
    "Maze",
    "Room",
    "Corridor",
    "MoveResult",
    # Phase 4.1: MCP 总线（接入声明 + L2 执行器 —— 状态通道不走这里，D2）
    "McpClient",
    "McpCallResult",
    "McpToolSpec",
    "McpExecutor",
    "McpRegistry",
    "McpServerSpec",
    "McpCall",
    "build_mcp_plan",
    "default_fs_registry",
]

def __getattr__(name: str) -> Any:
    """PEP 562 —— 模块级惰性属性：公开 API **谁用谁 import**。

    名字解析失败就抛 AttributeError（不静默降级）；子模块 import 失败时
    ImportError 原样冒泡（不吞异常）。
    """
    mod_path = _LAZY_ATTRS.get(name)
    if mod_path is not None:
        value = getattr(import_module(mod_path), name)
        globals()[name] = value  # 缓存：同一进程里只 import 一次
        return value
    if name in _LAZY_SUBMODULES:
        module = import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

def __dir__() -> list[str]:
    """把惰性名字也列出来（否则 dir(intent_os) 看不到公开 API）。"""
    return sorted(set(globals()) | set(_LAZY_ATTRS) | _LAZY_SUBMODULES)
