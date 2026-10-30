"""Intent OS REPL — 入口。

与 main.py（PyPilot 原 REPL）平级，互不干扰。

用法：
    uv run python main_intent.py            # 模板模式（默认，确定性、零模型开销）
    uv run python main_intent.py --llm      # LLM 优先（失败自动回退模板）

进去后输入自然语言意图，Agent 走完整 L4 + L1 + L2 链路，
Audit 留痕到 ./audit/intent_audit.jsonl。

REPL 命令：
    /exit、/quit    退出
    /audit          查看当前审计日志
    /approve-on     自动批准（测试 / 受信任场景）
    /approve-off    回到逐次确认
    /llm-on         切到 LLM 优先编译
    /llm-off        切回模板编译（默认）
    /mode           看当前编译模式与审批模式
    /state          看当前权威状态（D2 的「推」，会留痕）
    /set-state <k> <v>   写入一条权威状态
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from pypilot.config import SANDBOX_DIR as PYPILOT_SANDBOX

from intent_os import (
    LAST_CALL_STATS,
    LAST_LLM_ERROR,
    ApprovalGate,
    AuditTrail,
    Intent,
    IntentOSRuntime,
    parse_intent,
    parse_intent_auto,
)
from intent_os.trust.audit import Event

# 让 stdio 在 Windows GBK 终端下不崩
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass

BANNER = r"""
 ___      _   _   ___  ___  __  __
|_ _| ___| |_| | / __|/ _ \(  )  /
 | | / _ \ __| || (__| (_) )    (
|___|\___/\__|__|\___|\___/ \_/\_)

  Intent OS · L4 Intent Compiler + L1 Trust Layer
  Sandbox : {sandbox}
  Audit   : {audit}
  编译模式: {mode}
  输入意图试试："写个 fizzbuzz 存 fizz.py 然后跑一下"
"""

# 零宽字符：U+FEFF(BOM) / U+200B(零宽空格) / U+200C / U+200D / U+2060
# Windows 终端粘贴、PowerShell 管道喂入都常在行首带 BOM，
# 不清掉的话 "/approve-on" 会被当成一条普通意图送进编译器。
_INVISIBLE_RE = re.compile("[﻿‌‍⁠]")

def _clean(raw: str) -> str:
    """去掉 BOM 与零宽字符后去空白。"""
    return _INVISIBLE_RE.sub("", raw).strip()

def compile_intent(raw: str, use_llm: bool, context: str = "") -> tuple[Intent, str]:
    """按当前模式编译 Intent。

    use_llm=False → 纯模板（确定性、零模型开销，默认）
    use_llm=True  → LLM 优先，失败自动回退模板（parse_intent_auto）

    `context` 是注入给模型看的前置上下文（状态视图文本）——
    **D2 的「推」在这一层落地**：runtime 决定推什么，入口层负责把文本递到模型眼前。

    分层说明：**谁决定用哪种编译器属于入口层的策略，不属于 runtime。**
    IntentOSRuntime 只负责「拿到 Intent 之后怎么执行」，保持确定性 ——
    这样 runtime 的行为可测、可审计，与 LLM 是否在线无关。

    Returns:
        (Intent, source) — source ∈ {"llm", "template"}
    """
    if not use_llm:
        return parse_intent(raw), "template"
    return parse_intent_auto(raw, context=context)

def main() -> None:
    # 用 PyPilot 的沙箱（保持路径一致）
    sandbox = PYPILOT_SANDBOX
    audit_dir = Path(__file__).resolve().parent / "audit"
    audit_dir.mkdir(exist_ok=True)
    audit_path = audit_dir / f"intent_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.jsonl"

    # 编译模式：默认模板（确定性、零模型开销）；--llm 切到 LLM 优先
    use_llm = "--llm" in sys.argv[1:]
    mode = "LLM 优先（失败自动回退模板）" if use_llm else "模板（确定性，零模型开销）"

    print(BANNER.format(sandbox=sandbox, audit=audit_path, mode=mode))

    with AuditTrail(audit_path) as audit:
        # 默认 auto_approve=False，CLI 确认
        runtime = IntentOSRuntime(
            sandbox_dir=sandbox,
            audit=audit,
            approval=ApprovalGate(),
        )

        while True:
            try:
                raw = _clean(input(">>> "))
            except (EOFError, KeyboardInterrupt):
                print("\n再见～")
                break
            if not raw:
                continue
            cmd = raw.lower()
            if cmd in ("/exit", "/quit"):
                print("再见～")
                break
            if cmd == "/audit":
                # 打印当前 audit 内容（最近 20 行）
                lines = audit_path.read_text(encoding="utf-8").splitlines()[-20:]
                for line in lines:
                    rec = json.loads(line)
                    print(f"  [{rec['ts']}] {rec['event']}: {_short(rec['data'])}")
                print()
                continue
            if cmd == "/approve-on":
                # 测试 / 受信任场景：自动批准
                runtime.approval.auto_approve = True
                print("[approval auto-approve on]\n")
                continue
            if cmd == "/approve-off":
                runtime.approval.auto_approve = False
                print("[approval auto-approve off]\n")
                continue
            if cmd == "/llm-on":
                use_llm = True
                mode = "LLM 优先（失败自动回退模板）"
                print("[intent compiler: llm-first]\n")
                continue
            if cmd == "/llm-off":
                use_llm = False
                mode = "模板（确定性，零模型开销）"
                print("[intent compiler: template]\n")
                continue
            if cmd == "/mode":
                print(f"  编译模式 : {mode}")
                print(f"  审批模式 : {'自动批准' if runtime.approval.auto_approve else '逐次确认'}")
                print(f"  沙箱     : {sandbox}")
                print()
                continue
            if cmd == "/state":
                # D2 的「推」—— 手动看一眼当前权威状态（会留痕）。
                view = runtime.state_interface.push_view(trigger="manual")
                print(view.text)
                print()
                continue
            if cmd.startswith("/set-state"):
                parts = raw.split(None, 2)
                if len(parts) < 3:
                    print("用法：/set-state <key> <value>\n")
                    continue
                assertion = runtime.set_state(parts[1], parts[2], source="manual")
                print(f"  已写入 {assertion.key} = {assertion.value}（v{assertion.version}）\n")
                continue

            t0 = time.time()
            # ── D2 的「推」：把权威状态推给模型 ──────────────────────────────
            # LLM 路径下模型要**先看到状态**才能声明前提；
            # 模板路径没有模型可推，run 内部那一次推才是留痕的那次。
            context = ""
            if use_llm:
                context = runtime.state_interface.push_view(trigger="compile").text
            try:
                intent, source = compile_intent(raw, use_llm, context)
            except ValueError as e:
                print(f"[parse error] {e}\n")
                continue

            print(f"\n[Intent]  (来源: {source})")
            print(f"  goal        : {intent.goal}")
            print(f"  constraints : {[c.kind for c in intent.constraints]}")
            print(f"  verification: {[v.kind.value for v in intent.verification]}")
            print(f"  side effects: {[s.tool + ':' + s.kind.value for s in intent.side_effects]}")
            if source == "llm":
                stats = LAST_CALL_STATS
                print(
                    f"  llm         : {stats.get('model')} / {stats.get('method')}"
                    f" / {stats.get('latency_s')}s"
                )
            elif use_llm and LAST_LLM_ERROR:
                # 静默回退是信任杀手 —— 必须让用户看见「为什么没用上模型」
                print(
                    f"  ⚠ LLM 失败，已回退模板："
                    f"{LAST_LLM_ERROR.get('type')}: {LAST_LLM_ERROR.get('message')}"
                )
            print()

            result = runtime.run(intent)
            elapsed = time.time() - t0

            print(f"\n=== RunResult ({elapsed:.2f}s) ===")
            print(f"  status     : {result.status}")
            print(f"  audit_path : {result.audit_path}")
            if result.error:
                print(f"  error      : {result.error}")
            if result.state_view:
                sv = result.state_view
                print(
                    f"  state      : v{sv.version} · {len(sv.keys)} 条 · "
                    f"keys={list(sv.keys)}"
                )
            if result.premise_result:
                pr = result.premise_result
                print(
                    f"  premises   : confirmed={list(pr.confirmed)}"
                    f" conflicts={[c.state_key for c in pr.conflicts]}"
                    f" unknowns={list(pr.unknowns)}"
                    f" unchecked={list(pr.unchecked)}"
                )
            if result.correction_text:
                # 纠正文本 —— 下发给模型的通道（这里只是把它显示出来；
                # 真正的「下发」是入口层把它拼进下一轮提示词）。
                print(f"  ⚠ 纠正文本 : {result.correction_text}")
            if result.node_results:
                print(f"  nodes      :")
                for nid, nr in result.node_results.items():
                    print(f"    - {nid[:6]}: ok={nr.ok}  {_preview(nr.output)}")
            if result.verify_results:
                print(f"  verifications:")
                for vr in result.verify_results:
                    mark = "✓" if vr.passed else "✗"
                    print(f"    {mark} {vr.spec.kind.value}({vr.spec.target}): {vr.evidence}")
            print()

def _short(data: dict, n: int = 60) -> str:
    s = repr(data)
    return s[:n] + ("..." if len(s) > n else "")

def _preview(s: str, n: int = 60) -> str:
    s = s.replace("\n", "\\n")
    return s[:n] + ("..." if len(s) > n else "")

if __name__ == "__main__":
    main()
