"""Intent OS 命令行入口（Phase 2.3 · Step 2.3）。

用法：
    python -m intent_os audit show    <audit.jsonl> [--limit N] [--event TYPE]
    python -m intent_os audit replay  <audit.jsonl>
    python -m intent_os audit regression <audit.jsonl>   # 重放式回归（Phase 3.5.4）
    python -m intent_os audit verify  <audit.jsonl>
    python -m intent_os audit seal    <audit.jsonl>

设计取舍：
- `show`    —— 原样列出事件流水（调试用）
- `replay`  —— 从事件**重建**一次 run 的完整叙事（审计用）
- `regression` —— 把历史轨迹拿**当前**的判定逻辑重跑一遍（D1 的「评估 runtime」）
- `verify`  —— 校验哈希链，检测篡改（Phase 2.4）
- `seal`    —— 封存日志（只读）

`replay` 与 `show` 的区别：show 是流水，replay 是结论。
审计场景要问的不是「日志里有什么」，而是「那次执行到底做了什么、
谁批的、验过没有、补偿过没有」。

`regression` 与 `replay` 的区别：replay 重建**过去**的叙事，
regression 用**现在**的代码重跑过去的轨迹 —— 回答「这条轨迹以前失败，
现在还失败吗」。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .trust.audit import read_events, seal_log, segment_runs, verify_chain

# ── show ────────────────────────────────────────────────────────────────────

def _cmd_show(args: argparse.Namespace) -> int:
    events = read_events(args.path)
    if not events:
        print(f"（空日志或文件不存在：{args.path}）")
        return 1

    selected = events
    if args.event:
        selected = [e for e in events if e.get("event") == args.event]
    if args.limit:
        selected = selected[-args.limit :]

    for rec in selected:
        ts = rec.get("ts", "?")
        seq = rec.get("seq", "-")
        event = rec.get("event", "?")
        data = rec.get("data", {})
        summary = _short(data)
        print(f"[{ts}] #{seq} {event:<32} {summary}")
    print(f"\n共 {len(events)} 条事件，显示 {len(selected)} 条。")
    return 0

# ── replay ──────────────────────────────────────────────────────────────────

def _node_name(data: dict[str, Any]) -> str:
    """取节点名。

    注意审计里两个事件的键名不一样：
    - NODE_STARTED 记的是 node.to_dict() → 键名 "name"
    - NODE_COMPLETED / NODE_FAILED 记的是 {"node_name": ...}
    回放必须都认，否则同一个节点会被拆成两个。
    """
    return str(data.get("node_name") or data.get("name") or "?")

def _segment_runs(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """按 intent_received 切分出多次执行。

    **实现在 `trust/audit.py`** —— 审计 CLI 与轨迹重放器共用同一份切分
    （RFC 11 §1）。这里只是留一个本模块内的名字，逻辑不在这里写第二遍。
    """
    return segment_runs(events)

def _analyze(run_events: list[dict[str, Any]]) -> dict[str, Any]:
    """从一次 run 的事件里重建结构化叙事。"""
    intent: dict[str, Any] = {}
    plan: dict[str, Any] = {}
    nodes: dict[str, dict[str, Any]] = {}
    verifications: list[dict[str, Any]] = []
    approvals: list[str] = []
    compensations: list[str] = []
    failures: list[str] = []
    # Phase 3.5.2 / 3.5.3：状态推拉 + 前提声明与校验
    state_views: list[dict[str, Any]] = []
    premise_checks: list[dict[str, Any]] = []
    premise_blocks: list[dict[str, Any]] = []

    for rec in run_events:
        event = rec.get("event", "")
        data = rec.get("data", {}) or {}

        if event == "intent_received":
            intent = data
        elif event == "plan_generated":
            plan = data
        elif event == "state_view_pushed":
            state_views.append({**data, "trigger": data.get("trigger", "run")})
        elif event == "state_view_pulled":
            state_views.append({**data, "trigger": "pull"})
        elif event == "premise_checked":
            premise_checks.append(data)
        elif event == "premise_blocked":
            premise_blocks.append(data)
        elif event == "approval_granted":
            approvals.append("Plan 级：已批准")
        elif event == "approval_denied":
            approvals.append("Plan 级：被拒绝")
        elif event == "side_effect_approval_granted":
            approvals.append(f"副作用级：{_node_name(data)} 已批准")
        elif event == "side_effect_approval_denied":
            approvals.append(f"副作用级：{_node_name(data)} 被拒绝")
        elif event == "node_started":
            nodes.setdefault(_node_name(data), {})["status"] = "running"
        elif event == "node_completed":
            entry = nodes.setdefault(_node_name(data), {})
            if "ok" in data:
                entry["status"] = "ok" if data["ok"] else "failed"
            elif "note" in data:
                # noop 节点：runtime 只记 note，没有 ok 字段
                entry["status"] = "noop"
            entry["output"] = data.get("output_preview", "")
        elif event == "node_failed":
            entry = nodes.setdefault(_node_name(data), {})
            entry["status"] = "failed"
            entry["error"] = data.get("error", "")
            failures.append(f"{_node_name(data)}: {data.get('error')}")
        elif event == "compensation_started":
            compensations.append(f"开始补偿：{', '.join(data.get('nodes', []))}")
        elif event == "node_compensated":
            compensations.append(f"已还原：{_node_name(data)}")
        elif event == "compensation_failed":
            compensations.append(f"补偿失败：{_node_name(data)}")
        elif event in ("verify_passed", "verify_failed"):
            verifications.append(
                {
                    "passed": event == "verify_passed",
                    "spec": data.get("spec", {}),
                    "evidence": data.get("evidence", ""),
                }
            )

    return {
        "intent": intent,
        "plan": plan,
        "nodes": nodes,
        "verifications": verifications,
        "approvals": approvals,
        "compensations": compensations,
        "failures": failures,
        "state_views": state_views,
        "premise_checks": premise_checks,
        "premise_blocks": premise_blocks,
        "verdict": _verdict(nodes, verifications, failures, premise_blocks),
    }

def _verdict(
    nodes: dict[str, dict[str, Any]],
    verifications: list[dict[str, Any]],
    failures: list[str],
    premise_blocks: list[dict[str, Any]] | None = None,
) -> str:
    """从节点/验收/失败/拦截记录推导终态。"""
    if premise_blocks:
        # 前提与权威状态矛盾 → run 在动手前就被拦下了（后面什么都没发生）
        return "被拦下（前提与权威状态矛盾）"
    if failures or any(n.get("status") == "failed" for n in nodes.values()):
        return "失败（有节点未通过）"
    if verifications and all(v["passed"] for v in verifications):
        return "已验证（全部验收通过）"
    if not nodes:
        return "未执行（noop 或被拒）"
    if any(n.get("status") == "noop" for n in nodes.values()):
        return "未识别（noop）"
    return "已执行（验收未全部通过）"

def _cmd_replay(args: argparse.Namespace) -> int:
    events = read_events(args.path)
    if not events:
        print(f"（空日志或文件不存在：{args.path}）")
        return 1

    runs = _segment_runs(events)
    print("=" * 66)
    print("  Intent OS · 审计回放")
    print("=" * 66)
    print(f"  日志    : {args.path}")
    print(f"  事件数  : {len(events)}")
    print(f"  执行次数: {len(runs)}")
    print()

    if len(runs) > 1:
        print("  ── 执行概览 ──")
        for i, run_events in enumerate(runs):
            a = _analyze(run_events)
            ts = run_events[0].get("ts", "?")
            goal = a["intent"].get("goal", "?")
            print(f"    [{i}] {ts}  {len(run_events):>3} 事件  → {a['verdict']}")
            print(f"         goal: {goal}")
        print()

    if args.run is not None:
        if not (0 <= args.run < len(runs)):
            print(f"没有第 {args.run} 次执行（共 {len(runs)} 次）")
            return 1
        chosen = [runs[args.run]]
    else:
        chosen = [runs[-1]]

    for run_events in chosen:
        a = _analyze(run_events)
        intent = a["intent"]
        plan = a["plan"]
        nodes = a["nodes"]

        if intent:
            print("  ── 意图 ──")
            print(f"    goal        : {intent.get('goal', '?')}")
            vs = intent.get("verification", [])
            print(f"    验收规格    : {len(vs)} 条")
            for v in vs:
                print(f"      - {v.get('kind')}: {v.get('tool')} '{v.get('target')}' "
                      f"expected={v.get('expected')!r}")
            ses = intent.get("side_effects", [])
            print(f"    副作用声明  : {len(ses)} 条")
            for s in ses:
                print(f"      - {s.get('tool')} [{s.get('kind')}] scope={s.get('scope')} "
                      f"rev={s.get('reversibility')}")
            print()

        if plan:
            plan_nodes = plan.get("nodes", [])
            print("  ── 计划 ──")
            print(f"    plan id     : {plan.get('id', '?')}")
            print(f"    节点数      : {len(plan_nodes)}")
            for n in plan_nodes:
                deps = n.get("depends_on") or []
                dep_str = f"  (依赖 {deps})" if deps else ""
                print(f"      - {n.get('name')}: {n.get('tool')}{dep_str}")
            print()

        if a["state_views"]:
            print("  ── 状态（D2 的推 / 拉）──")
            for sv in a["state_views"]:
                trigger = sv.get("trigger", "?")
                keys = sv.get("keys", [])
                version = sv.get("version", "?")
                print(f"    [{trigger}] v{version}  {len(keys)} 条键")
                if keys:
                    print(f"        keys: {', '.join(keys)}")
                text = sv.get("text", "")
                if text:
                    for line in text.splitlines():
                        print(f"        {line}")
            print()

        if a["premise_checks"]:
            print("  ── 前提（声明 → 校验）──")
            for chk in a["premise_checks"]:
                declared = chk.get("declared", [])
                if not declared:
                    vacuous = chk.get("vacuous")
                    reason = "本次未声明前提，校验为空转" if vacuous else "本次未声明前提"
                    print(f"    （{reason}）")
                    continue
                print(f"    声明了 {len(declared)} 条前提：")
                for p in declared:
                    print(
                        f"      - {p.get('target')}（{p.get('target_kind')}）"
                        f" 断言 {p.get('state_key')} = {p.get('state_value')}"
                    )
                result = chk.get("result", {})
                confirmed = result.get("confirmed", [])
                conflicts = result.get("conflicts", [])
                unknowns = result.get("unknowns", [])
                unchecked = result.get("unchecked", [])
                if confirmed:
                    print(f"    结论：confirmed {', '.join(confirmed)}")
                for c in conflicts:
                    print(
                        f"    结论：conflict —— {c.get('state_key')} "
                        f"断言 {c.get('asserted')}，实际 {c.get('actual')}"
                        f"（v{c.get('state_version')}）"
                    )
                if unknowns:
                    print(f"    结论：unknown（不拦但告知）{', '.join(unknowns)}")
                if unchecked:
                    print(f"    结论：unchecked（永远不算通过）{', '.join(unchecked)}")
            print()

        if a["premise_blocks"]:
            print("  ── 拦截 ──")
            for blk in a["premise_blocks"]:
                print(f"    ✗ {blk.get('reason', '?')}")
                if blk.get("correction_text"):
                    print(f"    纠正文本：{blk['correction_text']}")
            print()

        if a["approvals"]:
            print("  ── 审批 ──")
            for x in a["approvals"]:
                print(f"    {x}")
            print()

        if nodes:
            print("  ── 节点执行 ──")
            for name, info in nodes.items():
                mark = {"ok": "✓", "failed": "✗", "running": "…", "noop": "○"}.get(
                    info.get("status"), "?"
                )
                print(f"    {mark} {name:<16} {info.get('status', '?')}")
                if info.get("error"):
                    print(f"        error : {info['error']}")
                if info.get("output"):
                    print(f"        output: {info['output'][:70]}")
            print()

        if a["verifications"]:
            print("  ── 验收 ──")
            for v in a["verifications"]:
                mark = "✓" if v["passed"] else "✗"
                spec = v["spec"]
                print(f"    {mark} {spec.get('kind')}({spec.get('target')}) → {v['evidence']}")
            print()

        if a["compensations"]:
            print("  ── 补偿 ──")
            for c in a["compensations"]:
                print(f"    {c}")
            print()

        if a["failures"]:
            print("  ── 失败 ──")
            for f in a["failures"]:
                print(f"    ✗ {f}")
            print()

    print("=" * 66)
    print(f"  终态: {_analyze(chosen[-1])['verdict']}")
    print("=" * 66)
    return 0

# ── regression（重放式回归 · Phase 3.5.4）────────────────────────────────────

def _cmd_regression(args: argparse.Namespace) -> int:
    """把历史轨迹拿当前的判定逻辑重跑一遍（D1 的「评估 runtime」）。

    **不写审计** —— 它是评估工具，不是 run；报告由调用方落盘。
    """
    from .trust.replay import TraceReplayer

    replayer = TraceReplayer()
    report = replayer.replay(args.path)

    print("=" * 66)
    print("  Intent OS · 重放式回归（轨迹 × 当前判定逻辑）")
    print("=" * 66)
    print(f"  日志   : {args.path}")
    print(f"  轨迹数 : {len(report.traces)}")
    print()

    if not report.traces:
        print("  （日志里没有可重放的轨迹）")
        print("=" * 66)
        return 0

    for t in report.traces:
        mark = {"fixed": "✓", "regressed": "✗", "unchanged": "·"}.get(t.status, "?")
        print(f"  [{t.run_index}] {mark} {t.status:<14} {t.label}")
        print(f"        声明 {t.declared} 条前提 · 历史 {t.historical_ok} → 当前 {t.current_ok}")
        if t.note:
            print(f"        备注：{t.note}")
        if t.historical_correction:
            print(f"        历史纠正：{t.historical_correction}")
        if t.current_correction:
            print(f"        当前纠正：{t.current_correction}")
        elif t.status in ("fixed", "regressed"):
            print("        当前纠正：（无）")
        if t.correction_changed:
            print("        ⚠ 纠正文本变了（判定没变，措辞变了）")
        print()

    counts = report.summary()
    print("─" * 66)
    print(
        f"  汇总：fixed {counts['fixed']} · regressed {counts['regressed']}"
        f" · unchanged {counts['unchanged']} · not_replayable {counts['not_replayable']}"
    )
    if counts["regressed"]:
        print("  ⚠ 有轨迹以前通过、现在被拦 —— 改坏了，去查是什么改动导致的")
    print("=" * 66)
    return 1 if counts["regressed"] else 0

# ── verify / seal ───────────────────────────────────────────────────────────

def _cmd_verify(args: argparse.Namespace) -> int:
    ok, issues = verify_chain(args.path)
    if ok:
        print(f"✓ 哈希链完整：{args.path}")
        for s in issues:
            print(f"    · {s}")
        return 0
    print(f"✗ 哈希链校验失败：{args.path}")
    for s in issues:
        print(f"    · {s}")
    return 2

def _cmd_seal(args: argparse.Namespace) -> int:
    ok, msg = seal_log(args.path)
    print(("✓ " if ok else "✗ ") + msg)
    return 0 if ok else 1

# ── 入口 ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m intent_os",
        description="Intent OS 命令行工具",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    audit = sub.add_parser("audit", help="审计日志工具")
    audit_sub = audit.add_subparsers(dest="action", required=True)

    p_show = audit_sub.add_parser("show", help="列出事件流水")
    p_show.add_argument("path", type=Path, help="审计日志路径")
    p_show.add_argument("--limit", type=int, default=0, help="只显示最后 N 条")
    p_show.add_argument("--event", type=str, default="", help="只显示某类事件")
    p_show.set_defaults(func=_cmd_show)

    p_replay = audit_sub.add_parser("replay", help="重建一次 run 的完整叙事")
    p_replay.add_argument("path", type=Path, help="审计日志路径")
    p_replay.add_argument(
        "--run",
        type=int,
        default=None,
        help="回放第几次执行（默认最后一次）",
    )
    p_replay.set_defaults(func=_cmd_replay)

    p_verify = audit_sub.add_parser("verify", help="校验哈希链（检测篡改）")
    p_verify.add_argument("path", type=Path, help="审计日志路径")
    p_verify.set_defaults(func=_cmd_verify)

    p_reg = audit_sub.add_parser(
        "regression", help="重放式回归：历史轨迹 × 当前判定逻辑（D1）"
    )
    p_reg.add_argument("path", type=Path, help="审计日志路径")
    p_reg.set_defaults(func=_cmd_regression)

    p_seal = audit_sub.add_parser("seal", help="封存日志（设为只读）")
    p_seal.add_argument("path", type=Path, help="审计日志路径")
    p_seal.set_defaults(func=_cmd_seal)

    return parser

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)

def _short(data: Any, n: int = 80) -> str:
    if not isinstance(data, dict):
        return str(data)[:n]
    s = json.dumps(data, ensure_ascii=False)
    return s[:n] + ("..." if len(s) > n else "")

if __name__ == "__main__":
    sys.exit(main())
