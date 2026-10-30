"""运行状态定义。"""
from typing import Annotated, Any

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]

    # ── 本轮任务 ─────────────────────────────────────────────────────────
    user_input: str

    # ── 模型节点产出 ─────────────────────────────────────────────────────
    step: int                 # 已完成的 LLM 调用次数，本轮从 0 起算
    raw_text: str             # 模型原文
    action: str | None        # None = 模型给的是 Final Answer
    action_input: Any         # dict | ("__PARSE_FAILED__", raw) | str(Final 文本)

    # ── 上一步书签（防循环 / 收尾检测用） ─────────────────────────────────
    last_step: tuple | None   # (action, repr(action_input))
    last_action: str | None
    last_obs: str | None

    repeat_count: int
    force_verify_count: int
    empty_count: int
    finalize_count: int
    parse_fail_count: int

    # ── 工作流状态位 ─────────────────────────────────────────────────────
    need_verify: bool         # 刚 write_file 还没 run_python 验证
    pending_path: str | None

    # ── 本步裁决（guard 写，act 消费） ────────────────────────────────────
    decision: str             # final|stop|observe|tool|parse_error|harness_verify
    observation: str          # 待回喂的 Observation 正文（不含 "Observation: " 前缀）
    forced_action: str | None # harness 代跑的工具名
    forced_input: Any         # harness 代跑的参数

    # ── 其它 ─────────────────────────────────────────────────────────────
    stop_reason: str          # 强停原因，仅供日志/调试
    summary: str              # 被截断历史的滚动摘要（默认关闭）
    final: str                # 终态答案

def new_turn(user_input: str) -> dict:
    """构造一次任务调用的初始状态。"""
    return {
        "messages": [HumanMessage(user_input)],
        "user_input": user_input,
        "step": 0,
        "raw_text": "",
        "action": None,
        "action_input": None,
        "last_step": None,
        "last_action": None,
        "last_obs": None,
        "repeat_count": 0,
        "force_verify_count": 0,
        "empty_count": 0,
        "finalize_count": 0,
        "parse_fail_count": 0,
        "need_verify": False,
        "pending_path": None,
        "decision": "",
        "observation": "",
        "forced_action": None,
        "forced_input": None,
        "stop_reason": "",
        "summary": "",
        "final": "",
    }
