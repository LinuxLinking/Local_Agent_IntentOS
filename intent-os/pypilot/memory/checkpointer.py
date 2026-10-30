"""会话状态管理。"""
import uuid

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from pypilot.config import INCLUDE_AI_CONTEXT
from pypilot.memory.context import approx_tokens, context_budget
from pypilot.prompts import nudges

def make_checkpointer() -> InMemorySaver:
    """构造进程内 checkpointer。"""
    return InMemorySaver()

def new_thread_id() -> str:
    """生成新的 thread id。"""
    return str(uuid.uuid4())

def needs_summary(messages) -> bool:
    """历史是否超过上下文预算（摘要节点的触发谓词）。"""
    if INCLUDE_AI_CONTEXT:
        usable = list(messages)
    else:
        usable = [m for m in messages if isinstance(m, HumanMessage)]
    return approx_tokens(usable) > context_budget()

def build_summary(llm, messages) -> str:
    """把较早的消息压缩成一段摘要。"""
    transcript = "\n".join(f"{m.type}: {m.content}" for m in messages)
    prompt = nudges.SUMMARY_PROMPT.format(transcript=transcript)
    resp = llm.invoke(prompt)
    return resp.content if isinstance(resp.content, str) else str(resp.content)
