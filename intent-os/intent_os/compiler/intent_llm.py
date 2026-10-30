"""L4 Intent — LLM-based Intent Compiler (Phase 3.1)。

用 qwen3.5:0.8b 原生 FC（tool calling）解析自然语言 Intent。

设计基线：
- design-foundation/01-concept-model.md §2 L4 Intent Compiler
- 06 §4 暴论 #12（Context Engineer 替代 Prompt Engineer）

Phase 3.1（2026-10-19）：
- 模型：qwen3.5:0.8b（原生 FC 兼容 LangChain）
- 协议：langchain_ollama.ChatOllama + with_structured_output
- 输入：自然语言
- 输出：结构化 Intent（同 Phase 0 schema）
- 失败兜底：解析异常 → 抛 RuntimeError，调用方决定回退策略

Phase 3.1 硬化（2026-10-23）：
小模型在本地推理时速度不可控，且本地 Ollama 可能半挂不挂。原实现既没有
单请求超时，也没有总预算，慢模型能把调用方永久拖住。这次补三层约束：
1. `timeout`    —— 单次 HTTP 请求的 httpx 超时（经 client_kwargs 注入 ollama.Client）
2. `num_predict` / `num_ctx` —— 限制单次生成的 token 数与上下文长度，直接压住延迟
3. `deadline_seconds` —— 整个 parse 的墙钟总预算；剩余时间不足时按剩余量收缩
   单请求超时，保证"超预算就停"而不是"超预算还在重试"

另：`method` 从隐式默认值改为显式参数。langchain-ollama 的
`with_structured_output` 默认 method="json_schema"（Ollama 受限解码），
不是 tool calling。模型本身带 tools 能力，两条路都可用，调用方按需选。

vs Phase 0 模板：
- 模板：确定性，0 依赖，但只能解析已知 pattern（"写 X 存 path"、"跑 path"、"修 path"）
- LLM：能解析任意自然语言，但依赖模型可用 + 可能不稳定
- 建议组合：LLM-first + 模板 fallback（parse_intent_auto）

Phase 3.2 硬化（2026-10-24）：
19 轮实测（reports/phase3.2-stability.md）暴露两类**确定性**错误：
  · json_schema 19/19 把「fizz.py」写成「fizzbuzz.py」（稳定地答错）
  · function_calling 3/3 把「修 broken.py」降级成 read（动作降级）

结论：**结构约束保格式，不保语义** —— 小模型的瓶颈在「填什么」，不在「能不能填」。
三协议解析成功率都是 100%、每任务输出完全可复现，差别只在「稳定地答什么」。

所以修法不是换模型、不是重试，而是编译器侧加两道**确定性后置校验**：
  V1 · target 一致性 —— 原文里的文件名优先（原文只有一个 .py 时才纠正）
  V2 · 动作完整性 —— 修类意图的动作不能弱于 fix
两道校验都不额外调模型、不改延迟、可单测；纠正痕迹随 Intent.compiler_notes 进审计。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from .intent import (
    Constraint,
    Intent,
    SideEffect,
    SideEffectKind,
    Verification,
    VerificationKind,
)

# ── Pydantic schema（LangChain 自动转 JSON schema 给 FC）───────────────────────
#
# ⚠ 为什么是「扁平三字段」而不是「逐条声明验收与副作用」（2026-10-23 实测结论）
#
# 原设计让模型直接填 verification[{kind,tool,target,expected}] 和
# side_effects[{tool,scope,kind,reversibility}]。在 qwen3.5:0.8b 上实测三条失败：
#
#   1. `expected: Any` → pydantic 渲染出没有 `type` 的 schema 节点，
#      Ollama 直接拒收（invalid JSON schema in format）
#   2. function_calling 路径下模型把 verification 填成
#      ["exit_code","file_exists","file_contains"] —— 照抄 kind 枚举当数组元素，
#      完全没理解每项该是对象
#   3. json_schema 路径下模型陷入复读（同一条 verification 重复 13 次），
#      撞 num_predict 截断，side_effects 直接没输出
#
# 根因：让 0.8b 模型同时做「理解意图」和「填写验收规格」两件事，后者它做不好。
# 而验收规格本来就不该由模型写 —— 退出码是 0、文件存在、副作用范围，
# 这些都能由「动作 + 目标文件」确定性推导出来。
#
# 所以职责重划：
#   LLM 负责：理解自然语言 → 动作 + 目标文件（语义判断，模型擅长）
#   编译器负责：动作 + 目标文件 → 验收规格 + 副作用声明 + Plan DAG（机械推导）
#
# 这与模板解析器（Phase 0）同构，区别只在于「怎么从自然语言拿到动作和目标」。
# 若日后换用更大的模型、需要更细的验收语义，再把富 schema 作为可选路径加回来。

class _IntentSchema(BaseModel):
    goal: str = Field(description="用户想要达成什么（一句中文或英文）")
    action: Literal["write_and_run", "run", "read", "fix"] = Field(
        description=(
            "要执行的动作：write_and_run=写新代码并运行；"
            "run=只运行已有文件；read=只读取文件；fix=修复已有文件并验证"
        )
    )
    target: str = Field(description="目标文件名，如 fizz.py（只要文件名，不要整句）")

# ── Schema → Intent 转换 ─────────────────────────────────────────────────────

# ── 最近一次调用的度量（零成本，不改变返回类型）────────────────────────────
# 供可行性探针 / 后续 L1 KPI 度量（C11）读取：延迟、token 数、生成速率。
# 单进程非线程安全，够用即可；正式度量应改走独立的 metrics sink。
LAST_CALL_STATS: dict[str, Any] = {}

def _record_stats(
    schema_obj: Any,
    started: float,
    method: str,
    model: str,
) -> None:
    """把最近一次调用的度量写进 LAST_CALL_STATS。"""
    raw = getattr(schema_obj, "raw", None)
    meta = getattr(raw, "response_metadata", None) or {}
    stats: dict[str, Any] = {
        "method": method,
        "model": model,
        "latency_s": round(time.monotonic() - started, 3),
    }
    for key in ("prompt_eval_count", "eval_count", "total_duration", "load_duration"):
        if key in meta:
            stats[key] = meta[key]
    eval_count = stats.get("eval_count")
    latency = stats.get("latency_s") or 0.0
    if isinstance(eval_count, int) and eval_count > 0 and latency > 0:
        stats["tokens_per_s"] = round(eval_count / latency, 2)
    LAST_CALL_STATS.clear()
    LAST_CALL_STATS.update(stats)

# ── 动作 → 验收规格 / 副作用声明（确定性推导）────────────────────────────────
#
# 「动作 + 目标文件」必然对应一组验收规格与副作用，这是机械映射，
# 不该让模型写 —— 0.8b 模型写不了（见上方实测记录）。
#
#   (VerificationKind, 证据 tool, expected)
#   (tool, SideEffectKind)

@dataclass(frozen=True)
class _ActionProfile:
    verification: tuple[tuple[VerificationKind, str, Any], ...]
    side_effects: tuple[tuple[str, SideEffectKind], ...]

_ACTION_PROFILES: dict[str, _ActionProfile] = {
    "write_and_run": _ActionProfile(
        verification=(
            (VerificationKind.FILE_EXISTS, "write_file", True),
            (VerificationKind.EXIT_CODE, "run_python", 0),
        ),
        side_effects=(
            ("write_file", SideEffectKind.WRITE),
            ("run_python", SideEffectKind.EXTERNAL),
        ),
    ),
    "run": _ActionProfile(
        verification=((VerificationKind.EXIT_CODE, "run_python", 0),),
        side_effects=(("run_python", SideEffectKind.EXTERNAL),),
    ),
    "read": _ActionProfile(
        verification=((VerificationKind.FILE_EXISTS, "read_file", True),),
        side_effects=(("read_file", SideEffectKind.READ),),
    ),
    "fix": _ActionProfile(
        verification=((VerificationKind.EXIT_CODE, "run_python", 0),),
        side_effects=(
            ("read_file", SideEffectKind.READ),
            ("write_file", SideEffectKind.WRITE),
            ("run_python", SideEffectKind.EXTERNAL),
        ),
    ),
}

_FILENAME_RE = re.compile(r"[\w\-./\\]+\.py")

# 「修/修复/改 + 空白 + .py 文件名」—— 与模板解析器的 fix 正则同源。
# 刻意不写「改」字的宽泛匹配：「把 a.py 改成 b.py」里 改 后面跟的是 成，
# 匹配不上；而「改成加一行注释」同样匹配不上 —— 宁可漏判，不误判。
_FIX_PATTERN = re.compile(r"(?:修|修复|改)\s+\S+\.py|\bfix\s+\S+\.py", re.IGNORECASE)

def _normalize_target(raw: str) -> str:
    """从模型输出里抠出文件名。

    小模型常把整句塞进 target（「把 fizzbuzz 存到 fizz.py 然后跑一下」）。
    这里用正则取第一个 .py 片段；取不到就原样返回，交由沙箱边界去拦。
    """
    text = (raw or "").strip().strip("\"'`")
    m = _FILENAME_RE.search(text)
    return m.group(0) if m else text

def _raw_py_filenames(raw: str) -> list[str]:
    """从**用户原文**里抽出所有 .py 文件名（去重、保序）。"""
    names: list[str] = []
    for m in _FILENAME_RE.finditer(raw or ""):
        name = m.group(0)
        if name not in names:
            names.append(name)
    return names

# ── Phase 3.2 · V1/V2 确定性后置校验 ────────────────────────────────────────
#
# 实测见 reports/phase3.2-stability.md：
#   · json_schema 19/19 把「fizz.py」写成「fizzbuzz.py」（稳定地答错）
#   · function_calling 3/3 把「修 broken.py」降级成 read（动作降级）
#
# 这两类错误都是**确定性**的 —— 同一输入永远同一个错。
# 所以最省事的修法不是换模型、不是重试，而是**编译器侧加确定性校验**：
# 符合「LLM 判语义、编译器做机械推导」的分工，且不额外调模型、不改延迟。
#
# V1 · target 一致性：原文里的文件名优先
#   仅当原文里有**且仅有一个** .py 文件名、且与模型给的 target 不是同一个文件时，
#   才纠正为原文的名字。原文有多个 .py（「把 a.py 改成 b.py」）时**不动手** ——
#   那种情况下模型的判断可能是对的（用户真要改名），原文优先会反而改错。
#
# V2 · 动作完整性：修类意图的动作不能弱于 fix
#   原文命中 fix 正则、且原文只有一个 .py 文件名、且模型给的是 read/run 这类
#   **更弱**的动作时，升级为 fix（fix = read → write → run，编译器知道完整动作链）。
#   write_and_run 不升级 —— 覆盖写一个新文件本身可能是用户要的。

def _apply_v1_target(raw: str, target: str) -> tuple[str, dict[str, Any] | None]:
    """V1：target 与原文文件名的一致性校验。"""
    names = _raw_py_filenames(raw)
    if len(names) != 1:
        return target, None  # 原文没提文件名，或提了多个 —— 不动手
    only = names[0]
    # 只比文件名部分：模型给 "sandbox/fizz.py"、原文写 "fizz.py" 时不必纠正
    if only == target or _basename(only) == _basename(target):
        return target, None
    return only, {
        "model_target": target,
        "corrected_target": only,
        "rule": "raw_text_filename",
    }

def _apply_v2_action(raw: str, action: str) -> tuple[str, dict[str, Any] | None]:
    """V2：修类意图的动作完整性校验。"""
    if action == "fix":
        return action, None
    if action not in ("read", "run"):
        return action, None
    if not _FIX_PATTERN.search(raw or ""):
        return action, None
    if len(_raw_py_filenames(raw)) != 1:
        return action, None
    return "fix", {
        "model_action": action,
        "corrected_action": "fix",
        "rule": "fix_keyword",
    }

def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]

# 最近一次编译的修正记录（零成本，不改变返回类型）。
# 与 LAST_CALL_STATS / LAST_LLM_ERROR 同款约定：单进程非线程安全，够用即可。
LAST_COMPILER_DRIFT: dict[str, Any] = {}

def schema_to_intent(schema: _IntentSchema, raw: str) -> Intent:
    """把 LLM 输出的「动作 + 目标文件」推导成完整 Intent。

    中间夹两道确定性后置校验（Phase 3.2 · V1/V2）：
    模型的 target / action 会被**原文**纠正，纠正痕迹记进
    `Intent.compiler_notes`（随 AuditTrail 留痕）与 `LAST_COMPILER_DRIFT`。
    """
    LAST_COMPILER_DRIFT.clear()

    target, target_drift = _apply_v1_target(raw, _normalize_target(schema.target))
    action, action_drift = _apply_v2_action(raw, schema.action)

    notes: list[str] = []
    if target_drift:
        notes.append(
            "target_drift:model={model_target};corrected={corrected_target};rule={rule}".format(
                **target_drift
            )
        )
        LAST_COMPILER_DRIFT["target_drift"] = target_drift
    if action_drift:
        notes.append(
            "action_upgrade:model={model_action};corrected={corrected_action};rule={rule}".format(
                **action_drift
            )
        )
        LAST_COMPILER_DRIFT["action_upgrade"] = action_drift

    profile = _ACTION_PROFILES.get(action, _ACTION_PROFILES["run"])

    verifications = tuple(
        Verification(kind=kind, tool=tool, target=target, expected=expected)
        for kind, tool, expected in profile.verification
    )
    side_effects = tuple(
        SideEffect(tool=tool, scope="sandbox/*", kind=kind, reversibility="easy")
        for tool, kind in profile.side_effects
    )
    return Intent(
        raw=raw,
        goal=schema.goal,
        constraints=(Constraint(kind="scope", value="sandbox/*"),),
        verification=verifications,
        side_effects=side_effects,
        compiler_notes=tuple(notes),
    )

# ── 主入口：LLM 解析 ────────────────────────────────────────────────────────

def parse_intent_llm(
    raw: str,
    model: str = "qwen3.5:0.8b",
    base_url: str = "http://localhost:11434",
    temperature: float = 0.0,
    max_retries: int = 1,
    *,
    method: str = "json_schema",
    timeout: float = 90.0,
    num_predict: int = 512,
    num_ctx: int = 4096,
    deadline_seconds: float | None = None,
    context: str = "",
    reasoning: bool | None = None,
) -> Intent:
    """用 LLM 结构化输出解析 Intent。

    约束三层（防止慢模型拖死调用方）：
    - `timeout`：单次 HTTP 请求超时（秒），经 client_kwargs 注入 httpx
    - `num_predict` / `num_ctx`：限制生成长度与上下文，直接压延迟
    - `deadline_seconds`：整个 parse 的墙钟总预算；剩余时间不足时按剩余量
      收缩单请求超时，预算耗尽直接抛 TimeoutError，不再重试

    Args:
        method: with_structured_output 的方法。
            "json_schema" = Ollama 受限解码（默认，输出必定是合法 JSON）
            "function_calling" = 原生 tool calling（模型需带 tools 能力）
        context: **注入给模型看的前置上下文**（Phase 3.5.2 · D2 的「推」——
            通常是状态视图文本）。

            ⚠ `context` **刻意不参与 V1/V2 后置校验** ——
            那两道校验是关于**用户原文**的（原文里的文件名、原文是不是「修」类）。
            把状态视图混进去会让「原文只有一个 .py」这类判断失真。
        reasoning: **思考模式开关**（langchain-ollama 的 `reasoning` 参数）。

            ⚠ **qwen3.5:0.8b 是 thinking 模型** —— 默认开着思考，
            token 预算被思考吃光时返回的 content 是**空字符串**
            （`done_reason="length"`），解析必然失败。
            提示词越复杂越容易触发（实测：简单指令能过，多约束提示就空）。

            所以：**提示词复杂时显式传 `reasoning=False`**。
            默认 `None` = 保持模型默认行为（不改变 3.1/3.2 已测过的口径）。

    Raises:
        RuntimeError: 模型调用失败或输出无法解析
        TimeoutError: 超出 deadline_seconds 总预算
    """
    from langchain_ollama import ChatOllama

    deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds

    last_err: Exception | None = None
    for _attempt in range(max_retries + 1):
        budget = timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"LLM Intent parse exceeded deadline of {deadline_seconds}s"
                )
            # 单请求超时不得超过剩余总预算
            budget = min(timeout, remaining)

        # ChatOllama 构造不触网（validate_model_on_init 默认 False），
        # 按剩余预算重建 client，让 httpx 超时精确对齐总闸门。
        llm = ChatOllama(
            model=model,
            base_url=base_url,
            temperature=temperature,
            num_predict=num_predict,
            num_ctx=num_ctx,
            client_kwargs={"timeout": budget},
            reasoning=reasoning,
        )
        structured = llm.with_structured_output(
            _IntentSchema, method=method, include_raw=True
        )

        try:
            started = time.monotonic()
            # 状态视图等上下文注入到模型眼前（D2 的「推」）。
            # 注意：V1/V2 后置校验用的仍是**原始用户文本** `raw` ——
            # 上下文不是原文的一部分，混进去会让文件名判断失真。
            prompt = f"{context}\n\n{raw}" if context else raw
            out = structured.invoke(prompt)
            # include_raw=True → {"raw": AIMessage, "parsed": Schema, "parsing_error": ...}
            parsed = out.get("parsed") if isinstance(out, dict) else out
            if parsed is None:
                # 把模型原始输出带进异常，否则失败就是黑盒
                raw_msg = out.get("raw") if isinstance(out, dict) else None
                parsing_error = out.get("parsing_error") if isinstance(out, dict) else None
                detail = ""
                if raw_msg is not None:
                    content = str(getattr(raw_msg, "content", "") or "")[:200]
                    detail = f" | raw content={content!r}"
                    tool_calls = getattr(raw_msg, "tool_calls", None)
                    if tool_calls:
                        detail += f" | tool_calls={tool_calls!r}"
                raise ValueError(
                    f"structured output parse failed: {parsing_error}{detail}"
                )
            # LangChain 0.3+ 返回 Schema 实例（不是 dict）
            if not isinstance(parsed, _IntentSchema):
                # 兜底：dict → Schema
                parsed = _IntentSchema(**parsed)
            _record_stats(out, started, method, model)
            return schema_to_intent(parsed, raw)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(
        f"LLM Intent parse failed after {max_retries + 1} attempts: {last_err}"
    ) from last_err

# ── 自动策略：LLM-first + 模板 fallback ───────────────────────────────────────

# 最近一次 LLM 失败的原因。回退到模板时必须让人知道「为什么回退」——
# 否则「模型没答上来」和「模型答了但结果非法」在日志里长得一模一样。
LAST_LLM_ERROR: dict[str, Any] = {}

def parse_intent_auto(
    raw: str,
    *,
    prefer_llm: bool = True,
    model: str = "qwen3.5:0.8b",
    context: str = "",
    reasoning: bool | None = None,
    **llm_kwargs: Any,
) -> tuple[Intent, str]:
    """组合策略：先 LLM，失败回退模板。

    `llm_kwargs` 原样透传给 `parse_intent_llm`（timeout / deadline_seconds 等）。
    `context` 是注入给模型看的上下文（状态视图等）—— 模板路径用不上它。

    失败原因记在 `LAST_LLM_ERROR`（{"type": ..., "message": ...}），
    成功或根本没走 LLM 时为空。

    Returns:
        (Intent, source) — source ∈ {"llm", "template"}
    """
    from .intent import parse_intent  # 避免循环

    LAST_LLM_ERROR.clear()
    if prefer_llm:
        try:
            intent = parse_intent_llm(
                raw, model=model, context=context, reasoning=reasoning, **llm_kwargs
            )
            return intent, "llm"
        except Exception as e:
            LAST_LLM_ERROR["type"] = type(e).__name__
            LAST_LLM_ERROR["message"] = str(e)
    intent = parse_intent(raw)
    return intent, "template"
