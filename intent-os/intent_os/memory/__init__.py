"""L3 · Working Memory —— 长期记忆（语义文件系统）。

设计依据：
- design-foundation/01-concept-model.md §2 L3
- design-foundation/06-open-conclusions-r1.md C13（长期记忆 4 层结构 + 遗忘）
- design-foundation/04-open-questions.md Q7（事件流为底，向量为索引）

三个模块：
- `embedder.py` —— 语义索引层的向量来源（真模型 / 确定性假实现）
- `policy.py`   —— 遗忘策略（TTL / 遗忘曲线）
- `store.py`    —— 物理层 + 版本层 + 权限分区 + 召回
- `context.py`  —— 上下文虚拟内存（常驻集 / 换入 / 换出 / 缺页 / Checkpoint）
- `state.py`    —— 状态断言（权威状态源 + 版本 + 冲突消解 + Q17 双表示渲染）
"""
from .context import (
    ORIGIN_RESIDENT,
    ORIGIN_SWAP_IN,
    ContextItem,
    ContextManager,
    estimate_tokens,
)
from .embedder import Embedder, HashEmbedder, OllamaEmbedder
from .policy import ForgettingPolicy
from .state import (
    AGENT_FRAME,
    EV_CONFLICT,
    EV_SET,
    FRAME_AGENT,
    FRAME_DOMAIN,
    FRAME_WORLD,
    WORLD_FRAME,
    Frame,
    RelationResolver,
    StateAssertion,
    StateConflict,
    StateEvent,
    StateRenderer,
    StateStore,
    StateView,
)
from .store import (
    EV_EXPIRE,
    EV_FORGET,
    EV_REMEMBER,
    EV_SUPERSEDE,
    MemoryEvent,
    MemoryRecord,
    MemoryStore,
    ScoredMemory,
)

__all__ = [
    "Embedder",
    "HashEmbedder",
    "OllamaEmbedder",
    "ForgettingPolicy",
    "MemoryStore",
    "MemoryRecord",
    "ScoredMemory",
    "MemoryEvent",
    "EV_REMEMBER",
    "EV_SUPERSEDE",
    "EV_FORGET",
    "EV_EXPIRE",
    # 上下文虚拟内存
    "ContextManager",
    "ContextItem",
    "estimate_tokens",
    "ORIGIN_RESIDENT",
    "ORIGIN_SWAP_IN",
    # Phase 3.5.1: 状态断言（权威状态源 + Q17 双表示）
    "StateStore",
    "StateAssertion",
    "StateConflict",
    "StateEvent",
    "StateRenderer",
    "StateView",
    "RelationResolver",
    "Frame",
    "WORLD_FRAME",
    "AGENT_FRAME",
    "FRAME_WORLD",
    "FRAME_DOMAIN",
    "FRAME_AGENT",
    "EV_SET",
    "EV_CONFLICT",
]
