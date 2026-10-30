"""图节点实现。"""
from langchain_core.messages import AIMessage, HumanMessage

from pypilot.config import MAX_ITERATIONS
from pypilot.graph.state import AgentState
from pypilot.guards.policy import (
    FINAL,
    HARNESS_VERIFY,
    OBSERVE,
    PARSE_ERROR,
    STOP,
    TOOL,
    GuardView,
    evaluate_guards,
)
from pypilot.memory.checkpointer import build_summary, needs_summary
from pypilot.memory.context import build_context
from pypilot.prompts import nudges
from pypilot.protocol.react import parse_react
from pypilot.tools.builtin import call_tool

class Ui:
    """全部终端输出的唯一出口。verbose 可运行时切换（main 的 /quiet 要改它）。"""

    def __init__(self, verbose: bool = True):
        self.verbose = bool(verbose)

    def step(self, n: int, text: str) -> None:
        """打印当前步骤。"""
        if not self.verbose:
            return
        print(f"\n--- step {n} ---")
        print(text)

    def observation(self, obs: str, *, full: bool = False) -> None:
        """打印 Observation。默认超 1200 字截断；harness 那条走 full=True 不截断。"""
        if not self.verbose:
            return
        if full or len(obs) < nudges.OBS_TRUNCATE_LIMIT:
            shown = obs
        else:
            shown = obs[: nudges.OBS_TRUNCATE_LIMIT] + nudges.OBS_TRUNCATE_MARK
        print(f"Observation: {shown}")

    def harness(self, path: str) -> None:
        """harness 代跑前的那一行提示（run_python 后面是两个空格）。"""
        if not self.verbose:
            return
        print(nudges.HARNESS_VERBOSE.format(path=path))

# ── 节点工厂 ──────────────────────────────────────────────────────────────

def make_model_node(llm, ui: Ui):
    """调一次模型、解析成 (action, action_input)。"""

    def model_node(state: AgentState) -> dict:
        step = state.get("step", 0) + 1
        ctx = build_context(state.get("messages", []), summary=state.get("summary") or "")
        try:
            resp = llm.invoke(ctx)
        except Exception as e:
            return {"step": step, "raw_text": "", "final": nudges.llm_error_final(e)}

        # 鸭子类型：FakeLLM 返回的是 _Resp 而不是 AIMessage，别在这里 isinstance 判类型
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        ui.step(step, text)
        action, action_input = parse_react(text)
        return {
            "step": step,
            "raw_text": text,
            "action": action,
            "action_input": action_input,
            # 模型原文仅作留档；默认不回喂（见 memory/context.py）
            "messages": [AIMessage(text)],
        }

    return model_node

def make_guard_node(ui: Ui):
    """执行步骤裁决，把结果写回状态，并把干预文案回喂进历史。"""

    def guard_node(state: AgentState) -> dict:
        view = GuardView(
            action=state.get("action"),
            action_input=state.get("action_input"),
            last_step=state.get("last_step"),
            last_action=state.get("last_action"),
            last_obs=state.get("last_obs"),
            need_verify=state.get("need_verify", False),
            pending_path=state.get("pending_path"),
            repeat_count=state.get("repeat_count", 0),
            force_verify_count=state.get("force_verify_count", 0),
            empty_count=state.get("empty_count", 0),
            finalize_count=state.get("finalize_count", 0),
            parse_fail_count=state.get("parse_fail_count", 0),
        )
        out = evaluate_guards(view)

        updates = {
            "decision": out.decision,
            "stop_reason": out.stop_reason,
            "observation": out.observation,
            "repeat_count": state.get("repeat_count", 0) + out.repeat_count,
            "force_verify_count": state.get("force_verify_count", 0) + out.force_verify_count,
            "empty_count": state.get("empty_count", 0) + out.empty_count,
            "finalize_count": state.get("finalize_count", 0) + out.finalize_count,
            "parse_fail_count": state.get("parse_fail_count", 0) + out.parse_fail_count,
        }

        if out.decision in (FINAL, STOP):
            updates["final"] = out.final
        elif out.decision == OBSERVE:
            # 干预文案：打印 + 回喂，然后回 loopback 再来一轮
            ui.observation(out.observation)
            updates["messages"] = [HumanMessage(f"Observation: {out.observation}")]
        elif out.decision == HARNESS_VERIFY:
            # 守卫不执行工具，只交代该跑什么；执行由 act 统一做（含打印与落盘）
            updates["forced_action"] = out.action
            updates["forced_input"] = out.action_input
        return updates

    return guard_node

def make_act_node(ui: Ui):
    """按裁决执行工具或回喂提示，并维护对应状态。"""

    def act_node(state: AgentState) -> dict:
        decision = state.get("decision")

        if decision == HARNESS_VERIFY:
            forced = state.get("forced_input") or {}
            path = forced.get("path")
            ui.harness(path)
            obs = call_tool(state.get("forced_action") or "run_python", forced)
            ui.observation(obs, full=True)   # harness 这条路不截断
            suffix = nudges.harness_obs_suffix(path)
            return {
                "messages": [HumanMessage(f"Observation: {obs}{suffix}")],
                "last_action": "run_python",
                "last_obs": obs,
                "need_verify": False,
                "force_verify_count": 0,
                "finalize_count": 0,
            }

        action = state.get("action")
        action_input = state.get("action_input")

        if decision == PARSE_ERROR:
            obs = state.get("observation") or nudges.parse_failed_obs(str(action_input))
            ui.observation(obs)
            return {"messages": [HumanMessage(f"Observation: {obs}")]}

        obs = call_tool(action, action_input)
        ui.observation(obs)

        updates = {
            "messages": [HumanMessage(f"Observation: {obs}")],
            "last_step": (action, repr(action_input)),
            "repeat_count": 0,
            "last_action": action,
            "last_obs": obs,
            "finalize_count": 0,
        }

        if action == "write_file":
            updates["need_verify"] = True
            updates["force_verify_count"] = 0
            updates["pending_path"] = (
                action_input.get("path") if isinstance(action_input, dict) else None
            )
        elif action == "run_python":
            updates["need_verify"] = False
            updates["force_verify_count"] = 0
        return updates

    return act_node

def loopback_node(state: AgentState) -> dict:
    """空节点：承载 loopback → model / max_iter 条件边。"""
    return {}

def make_max_iter_node():
    """构造达到迭代上限时收尾的节点。"""

    def max_iter_node(state: AgentState) -> dict:
        return {"final": nudges.max_iter_final(MAX_ITERATIONS)}

    return max_iter_node

# 摘要时保留最近几条 HumanMessage 原文，其余压成摘要
SUMMARIZE_KEEP_LAST = 4

def make_summarize_node(llm, ui: Ui):
    """压缩较早的历史消息（默认不挂到图上）。"""

    def summarize_node(state: AgentState) -> dict:
        messages = state.get("messages", [])
        if not needs_summary(messages):
            return {}
        humans = [m for m in messages if isinstance(m, HumanMessage)]
        if len(humans) <= SUMMARIZE_KEEP_LAST:
            return {}
        old = humans[:-SUMMARIZE_KEEP_LAST]
        try:
            summary = build_summary(llm, old)
        except Exception as e:
            if ui.verbose:
                print(f"[摘要失败，已跳过] {type(e).__name__}: {e}")
            return {}
        return {"summary": summary}

    return summarize_node
