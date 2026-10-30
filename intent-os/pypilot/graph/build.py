"""节点注册与路由。"""
from langgraph.graph import END, START, StateGraph

from pypilot.graph.nodes import (
    loopback_node,
    make_act_node,
    make_guard_node,
    make_max_iter_node,
    make_model_node,
    make_summarize_node,
)
from pypilot.graph.state import AgentState
from pypilot.guards.policy import FINAL, HARNESS_VERIFY, PARSE_ERROR, STOP, TOOL
from pypilot.config import MAX_ITERATIONS

def route_after_model(state) -> str:
    """LLM 调用失败时 model 节点会直接写 final，此时短路到 END。"""
    return END if state.get("final") else "guard"

def route_after_guard(state) -> str:
    decision = state.get("decision")
    if decision in (FINAL, STOP):
        return END
    if decision in (TOOL, PARSE_ERROR, HARNESS_VERIFY):
        return "act"
    # observe：干预文案已回喂，回 loopback 再来一轮
    return "loopback"

def continue_or_max(state) -> str:
    """迭代上限判定。"""
    return "model" if state.get("step", 0) < MAX_ITERATIONS else "max_iter"

def build_graph(llm, checkpointer, ui, summarize: bool = False):
    """构造并编译执行图。"""
    g = StateGraph(AgentState)
    g.add_node("model", make_model_node(llm, ui))
    g.add_node("guard", make_guard_node(ui))
    g.add_node("act", make_act_node(ui))
    g.add_node("loopback", loopback_node)
    g.add_node("max_iter", make_max_iter_node())

    g.add_edge(START, "model")
    g.add_conditional_edges("model", route_after_model, {"guard": "guard", END: END})
    g.add_conditional_edges(
        "guard", route_after_guard, {"act": "act", "loopback": "loopback", END: END}
    )
    g.add_edge("act", "loopback")

    if summarize:
        # 可选摘要节点：挂在 loopback → model 之间（默认不启用，见 config.ENABLE_SUMMARY）
        g.add_node("summarize", make_summarize_node(llm, ui))
        g.add_conditional_edges(
            "loopback", continue_or_max, {"model": "summarize", "max_iter": "max_iter"}
        )
        g.add_edge("summarize", "model")
    else:
        g.add_conditional_edges(
            "loopback", continue_or_max, {"model": "model", "max_iter": "max_iter"}
        )

    g.add_edge("max_iter", END)
    return g.compile(checkpointer=checkpointer)
