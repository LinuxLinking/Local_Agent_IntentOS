"""步骤裁决函数。"""
from dataclasses import dataclass, field
from typing import Any

from pypilot.prompts import nudges

FORCE_VERIFY_LIMIT = 2
REPEAT_STOP_LIMIT = 4
REPEAT_TEMPLATE_AT = 2
EMPTY_STOP_LIMIT = 2
FINALIZE_STOP_LIMIT = 2
PARSE_FAIL_LIMIT = 3

# 决策取值
FINAL = "final"
STOP = "stop"
OBSERVE = "observe"
TOOL = "tool"
PARSE_ERROR = "parse_error"
HARNESS_VERIFY = "harness_verify"

@dataclass(frozen=True)
class GuardView:
    """一步的输入视图：模型这一步干了什么 + 目前的记账状态。"""
    action: str | None
    action_input: Any
    last_step: tuple | None = None
    last_action: str | None = None
    last_obs: str | None = None
    need_verify: bool = False
    pending_path: str | None = None
    repeat_count: int = 0
    force_verify_count: int = 0
    empty_count: int = 0
    finalize_count: int = 0
    parse_fail_count: int = 0

@dataclass(frozen=True)
class GuardOutcome:
    """裁决结果：走哪条路 + 要回喂什么 + 计数器增量。

    计数器是**增量**不是绝对值；节点负责做加法。这样纯函数不必关心状态是怎么存的。
    """
    decision: str
    observation: str = ""
    final: str = ""
    stop_reason: str = ""

    repeat_count: int = 0        # 增量
    force_verify_count: int = 0  # 增量
    empty_count: int = 0         # 增量
    finalize_count: int = 0      # 增量
    parse_fail_count: int = 0    # 增量

    # harness_verify 专用：守卫本身不执行工具，只把"该跑什么"交给 act 节点
    action: str | None = None
    action_input: Any = None
    extra: dict = field(default_factory=dict)

def is_parse_failed(action_input) -> bool:
    """是否是解析失败标记。"""
    return (
        isinstance(action_input, tuple)
        and len(action_input) > 0
        and action_input[0] == "__PARSE_FAILED__"
    )

def evaluate_guards(v: GuardView) -> GuardOutcome:
    """按固定顺序裁决当前步骤。"""
    action = v.action
    action_input = v.action_input

    # 写入后要求先运行验证
    if v.need_verify and action != "run_python":
        new_count = v.force_verify_count + 1
        if new_count >= FORCE_VERIFY_LIMIT:
            if not v.pending_path:
                return GuardOutcome(
                    decision=STOP,
                    final=nudges.NEED_VERIFY_NO_PATH_FINAL,
                    stop_reason="need_verify_but_path_unknown",
                    force_verify_count=1,
                )
            # harness 代跑：把该跑什么交给 act 节点，由它统一执行并落盘 Observation
            return GuardOutcome(
                decision=HARNESS_VERIFY,
                action="run_python",
                action_input={"path": v.pending_path},
                force_verify_count=1,
            )
        return GuardOutcome(
            decision=OBSERVE,
            observation=nudges.need_verify_obs(v.pending_path or "目标文件"),
            force_verify_count=1,
        )

    # 空输出
    if action is None and not str(action_input).strip():
        new_count = v.empty_count + 1
        if new_count >= EMPTY_STOP_LIMIT:
            return GuardOutcome(
                decision=STOP,
                final=nudges.EMPTY_OUTPUT_FINAL,
                stop_reason="empty_output",
                empty_count=1,
            )
        return GuardOutcome(
            decision=OBSERVE,
            observation=nudges.EMPTY_OUTPUT_OBS,
            empty_count=1,
        )

    # 最终回答
    if action is None:
        return GuardOutcome(decision=FINAL, final=str(action_input))

    # 解析失败
    if is_parse_failed(action_input):
        new_count = v.parse_fail_count + 1
        if new_count >= PARSE_FAIL_LIMIT:
            return GuardOutcome(
                decision=STOP,
                final=nudges.PARSE_FAIL_STOP_FINAL,
                stop_reason="parse_fail",
                parse_fail_count=1,
            )
        return GuardOutcome(
            decision=PARSE_ERROR,
            observation=nudges.parse_failed_obs(action_input[1], new_count),
            parse_fail_count=1,
        )

    # 连续重复同一动作
    cur_step = (action, repr(action_input))
    if cur_step == v.last_step:
        new_count = v.repeat_count + 1
        obs = (
            nudges.repeat_nudge(action, action_input)
            if new_count >= REPEAT_TEMPLATE_AT
            else nudges.REPEAT_WARNING_GENERIC
        )
        if new_count >= REPEAT_STOP_LIMIT:
            return GuardOutcome(
                decision=STOP,
                final=nudges.REPEAT_STOP_FINAL,
                stop_reason="repeat_loop",
                repeat_count=1,
            )
        # last_step 保持不变，下次若还是重复就继续干预
        return GuardOutcome(decision=OBSERVE, observation=obs, repeat_count=1)

    # 验证通过后仍继续调用工具
    if (
        v.last_action == "run_python"
        and v.last_obs is not None
        and v.last_obs.startswith("退出码: 0")
        and not isinstance(action_input, tuple)
    ):
        new_count = v.finalize_count + 1
        if new_count >= FINALIZE_STOP_LIMIT:
            return GuardOutcome(
                decision=STOP,
                final=nudges.FINALIZE_STOP_FINAL,
                stop_reason="no_finalize",
                finalize_count=1,
            )
        return GuardOutcome(
            decision=OBSERVE,
            observation=nudges.FINALIZE_OBS,
            finalize_count=1,
        )

    # 正常工具调用
    return GuardOutcome(decision=TOOL, action=action, action_input=action_input)
