"""intent_os.router —— 工具元数据 + 能力图 + 加权最短路径（RFC 15 · Step 5.2）。

**做什么**：把「要什么能力」变成「走哪条路」——
工具元数据（前置条件 / 能力 / 成本 / 延迟 / 失败率 / 备选组）
+ 能力图 + Dijkstra 加权最短路径，**输入是 5.1 的 `EnvironmentSnapshot`**。

**三条不破的红线**：

1. **零模型调用** —— 路由是确定性代码。模型只负责说「我要什么 capability」，
   「走哪条路」由系统算（RFC 15 §3.3：不让模型自己选路）。
2. **不静默降级** —— 前置**探测不到**（unknown）时**不排除**该工具，
   只在理由里标注「前置未知」并加不确定性惩罚；算不出路就返回 `None`，
   理由用 `Router.explain()` 查。
3. **不另起执行链路** —— 路由结果落成既有 `Plan` / `PlanNode`，
   走既有的审批 / 执行 / 留痕 / 补偿。

**D2 边界**：本包**不 import** 任何模型客户端（LLM 编译器 / HTTP 客户端 /
本地模型运行时），也**不 import** 外置状态断言模块（StateStore 所在模块）——
路由只看**环境能力**（探针结果），不看**世界状态**。
这条边界由 `tests/test_router.py` 的**源码扫描断言**守住。

模块分工：`metadata.py` 声明与注册表 · `router.py` 能力图 + Dijkstra + 落 Plan。

**搜索状态 = 能力节点**（不是「路径 × 能力」—— 路径进状态会指数爆炸）；
路径只作为**重建结果**存在。同代价按**路径字典序**裁决 → 结果确定可复现。
"""
from .metadata import (
    CAPABILITY_PREFIX,
    DEFAULT_TOOL_REGISTRY,
    ToolMetadata,
    build_registry,
    capabilities_of,
    get_tool,
    normalize_capabilities,
    normalize_capability,
    register_tool,
)
from .router import (
    COST_WEIGHT,
    FAILURE_TOKENS_PER_PROBABILITY,
    LATENCY_TOKENS_PER_SECOND,
    MAX_EXPANSIONS,
    UNKNOWN_PREREQUISITE_PENALTY_TOKENS,
    RouteDecision,
    Router,
    SOURCE,
    precondition_state,
    route_to_plan,
    tool_weight,
)

__all__ = [
    # 工具元数据
    "ToolMetadata",
    "CAPABILITY_PREFIX",
    "DEFAULT_TOOL_REGISTRY",
    "build_registry",
    "capabilities_of",
    "get_tool",
    "normalize_capabilities",
    "normalize_capability",
    "register_tool",
    # 路由
    "RouteDecision",
    "Router",
    "SOURCE",
    "route_to_plan",
    "precondition_state",
    "tool_weight",
    # 权重口径（可解释 · 审计里能复算）
    "COST_WEIGHT",
    "LATENCY_TOKENS_PER_SECOND",
    "FAILURE_TOKENS_PER_PROBABILITY",
    "UNKNOWN_PREREQUISITE_PENALTY_TOKENS",
    "MAX_EXPANSIONS",
]
