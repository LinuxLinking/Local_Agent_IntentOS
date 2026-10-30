"""回喂话术与终态文案。

本文件的字符串是测试断言的对象，修改字符串需同步测试。
"""

# ── 防循环 ────────────────────────────────────────────────────────────────

REPEAT_WARNING_GENERIC = (
    "⚠ 你刚才已经做过完全一样的操作，不要重复！\n"
    "请推进到下一步：\n"
    "  - 改错任务里：刚 read_file → 下一步 write_file 修复；"
    "刚 write_file → 下一步 run_python 验证；run_python 退出码 0 → 直接给 Final Answer。\n"
    "  - 写代码任务里：write_file 之后必须 run_python 验证。"
)

REPEAT_NUDGE = {
    "read_file": (
        "⚠ 第二次了：{path} 你已经读过，内容就在上面的 Observation 里，再读一遍不会多出任何信息。\n"
        "现在唯一该做的是把修好的完整代码写回去。照抄下面三行，"
        "content 就是你修好的完整代码：\n"
        "Thought: 已读到内容，现在修复\n"
        "Action: write_file\n"
        'Action Input: {{"path": "{path}", "content": "<修好的完整代码>"}}'
    ),
    "write_file": (
        "⚠ 第二次了：{path} 你已经写过，重复写同样的内容不会改变任何东西。\n"
        "下一步必须是 run_python 验证。照抄下面三行：\n"
        "Thought: 跑一下验证\n"
        "Action: run_python\n"
        'Action Input: {{"path": "{path}"}}'
    ),
    "run_python": (
        "⚠ 第二次了：{path} 已经跑过了，结果就是上面那个。不要再跑。\n"
        "现在直接收尾。照抄下面两行：\n"
        "Thought: 验证通过，任务完成\n"
        "Final Answer: <做了什么、验证结果如何>"
    ),
}

def repeat_nudge(action: str, action_input) -> str:
    """按被重复的动作，给一条可直接照抄的干预指令。"""
    tpl = REPEAT_NUDGE.get(action)
    if tpl is None:
        return REPEAT_WARNING_GENERIC
    path = action_input.get("path") if isinstance(action_input, dict) else None
    return tpl.format(path=path or "目标文件")

def need_verify_obs(pending_path: str) -> str:
    return (
        f"⚠ 你刚 write_file 写入 {pending_path}，还没 run_python 验证。\n"
        f"不能凭空说退出码 0 正常——跳过验证的动作一律不接受。下一步必须是：\n"
        f"Thought: 跑一下验证\n"
        f"Action: run_python\n"
        f"Action Input: {{\"path\": \"{pending_path}\"}}"
    )

NEED_VERIFY_NO_PATH_FINAL = (
    "（Agent 写了文件却不肯验证，且系统拿不到文件路径，已停止。"
    "可 /reset 后换个说法重试。）"
)

# harness 代跑后追加在 Observation 后面的说明（注意前导换行）
def harness_obs_suffix(pending_path: str) -> str:
    return (
        f"\n（系统替你跑了 run_python 验证 {pending_path}，上面是真实结果，"
        f"不是凭空写的。请基于它给出 Final Answer。）"
    )

# harness 代跑时 verbose 打印的那一行（注意 run_python 后面是【两个空格】）
HARNESS_VERBOSE = '[harness 代为验证] Action: run_python  Action Input: {{"path": "{path}"}}'

EMPTY_OUTPUT_OBS = (
    "⚠ 你刚才的输出是空的，这不构成 Final Answer。\n"
    "请基于上面的 Observation，用下面的格式收尾：\n"
    "Thought: <一句话总结>\n"
    "Final Answer: <做了什么、验证结果如何>"
)

EMPTY_OUTPUT_FINAL = "（模型连续输出空内容，已停止。上一步 Observation 里的结果仍然有效。）"

REPEAT_STOP_FINAL = "（Agent 反复重复同一操作，已停止。可以 /reset 后换个说法重试。）"

FINALIZE_OBS = (
    "⚠ 上一步 run_python 已退出码 0，验证通过，任务已完成。\n"
    "不要再 read_file / write_file / run_python，立即输出：\n"
    "Thought: <一句话总结结果>\n"
    "Final Answer: <做了什么、验证结果如何>"
)

FINALIZE_STOP_FINAL = (
    "（Agent 在验证通过后仍未给出 Final Answer，已停止。"
    "任务实际已完成并验证通过，可 /reset 重开下个任务。）"
)

# ── 解析失败 / 兜底 ───────────────────────────────────────────────────────

# 非法原文在回喂里只露个头，别把整个代码块灌回上下文（原文可能是一整份文件）
PARSE_FAIL_RAW_LIMIT = 200

def parse_failed_obs(raw: str, attempt: int = 1) -> str:
    """解析失败时回喂的提示。"""
    shown = raw if len(raw) <= PARSE_FAIL_RAW_LIMIT else raw[:PARSE_FAIL_RAW_LIMIT] + "…（已截断）"
    if attempt >= 2:
        return (
            f"⚠ 第 {attempt} 次了：Action Input 还不是合法 JSON。\n"
            f"最常见的错因是 content 用了三引号划定或在字符串里直接换行——两者 JSON 都不允许。\n"
            f"照这个格式重写，把整个文件压进一行 content，换行写成 \\n：\n"
            "Thought: 写文件\n"
            "Action: write_file\n"
            'Action Input: {"path": "multiply.py", "content": "def f():\\n    return 1\\n"}\n'
            "（content 里：\\n 是换行、\\\" 是引号、\\\\ 是反斜杠；除转义外全部写在一行里。）"
        )
    return (
        f"Action Input 不是合法 JSON: {shown}\n"
        f"请重新输出这一步。要求：双引号、换行用 \\n 转义、不能用三引号。"
    )

PARSE_FAIL_STOP_FINAL = "（Agent 连续多次没能输出合法 JSON 的 Action Input，已停止。可以 /reset 后换个说法重试。）"

def max_iter_final(max_iterations: int) -> str:
    return (
        f"（达到最大迭代数 {max_iterations}，Agent 未能给出 Final Answer。"
        f"最近的 Observation 已在上面。）"
    )

def llm_error_final(exc: Exception) -> str:
    return f"[LLM 调用失败] {type(exc).__name__}: {exc}"

EMPTY_FINAL = "(空)"

OBS_TRUNCATE_LIMIT = 1200
OBS_TRUNCATE_MARK = "\n...[已截断]"

SUMMARY_PROMPT = (
    "把下面这段编程助手与用户的对话压缩成一段简短的中文摘要，"
    "保留：用户的任务目标、已经创建或修改了哪些文件、最后一次验证的结果。"
    "只输出摘要本身，不要任何前后缀。\n\n"
    "对话记录：\n{transcript}"
)
