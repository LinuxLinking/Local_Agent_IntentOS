"""上下文组装。"""
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately

from pypilot.config import (
    CJK_CHARS_PER_TOKEN,
    INCLUDE_AI_CONTEXT,
    NUM_CTX,
    RESERVED_COMPLETION_TOKENS,
)
from pypilot.prompts.system import SYSTEM_PROMPT

SUMMARY_PREFIX = "【历史摘要】"

def approx_tokens(messages) -> int:
    """按中文比例估算 token 数（trim_messages 的 token_counter 就用它）。"""
    return count_tokens_approximately(messages, chars_per_token=CJK_CHARS_PER_TOKEN)

def system_tokens() -> int:
    return approx_tokens([SystemMessage(SYSTEM_PROMPT)])

def context_budget() -> int:
    """留给历史消息的 token 预算 = num_ctx − 单步生成预留 − system prompt。"""
    return max(0, NUM_CTX - RESERVED_COMPLETION_TOKENS - system_tokens())

def build_context(messages, *, summary: str = "") -> list[BaseMessage]:
    """把消息史裁成最终要发给模型的消息列表。"""
    if INCLUDE_AI_CONTEXT:
        usable = [m for m in messages if not isinstance(m, SystemMessage)]
    else:
        usable = [m for m in messages if isinstance(m, HumanMessage)]

    budget = context_budget()
    if budget <= 0:
        kept: list[BaseMessage] = []
    else:
        kept = trim_messages(
            usable,
            max_tokens=budget,
            token_counter=approx_tokens,
            strategy="last",
            start_on="human",
            include_system=False,
        )

    ctx: list[BaseMessage] = [SystemMessage(SYSTEM_PROMPT)]
    if summary:
        ctx.append(HumanMessage(f"{SUMMARY_PREFIX}{summary}"))
    return ctx + kept
