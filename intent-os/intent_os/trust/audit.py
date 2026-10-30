"""L1 Audit Trail — WORM JSONL append-only 日志。

设计基线：design-foundation/01-concept-model.md §2 L1「Audit Trail = 不可篡改日志」。
开放性结论：06 §1 C8（治理泥潭：Audit 第一天就要做对）。

Phase 0：JSONL + fsync 兜底。
Phase 2.4：哈希链（防篡改可验证）+ chmod a-w 封存 + 可选密钥。

──────── 哈希链设计 ────────

每条记录带 `prev_hash` 与 `hash`：

    hash = SHA256(prev_hash + 规范化 payload)      # 无密钥
    hash = HMAC-SHA256(key, prev_hash + payload)   # 有密钥（root-only）

`prev_hash` 串起前一条，于是**改动任意一条记录都会让后续所有哈希对不上**。
只靠 chmod 是不够的 —— 文件属主随时能改回来；哈希链让篡改**可被检出**。

密钥从环境变量 `INTENT_OS_AUDIT_KEY` 读取（不落在代码/配置里）。
有密钥时只有持有者能产出合法哈希；验签用同一个 key。

──────── 封存与追加的张力 ────────

「WORM（写一次）」与「追加既有日志」天然冲突：封存后文件只读，
再打开追加会失败。这是**期望行为**，不是 bug —— 但它意味着封存必须是
**显式**的动作，不能默认打开就封：

- `AuditTrail(path)` → 不封存（保持 Phase 0 的 append-only 语义）
- `AuditTrail(path, seal=True)` → close() 时封存
- `seal_log(path)` → 随时封存既有日志（CLI 可调）

需要 WORM 保证的地方（一次 run 结束、session 结束）在那一层显式封存。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 哈希链起点（创世哈希）
GENESIS_HASH = "0" * 64

# 密钥来源的环境变量名
AUDIT_KEY_ENV = "INTENT_OS_AUDIT_KEY"

# 事件类型常量（统一在 AuditTrail，避免字符串散落）
class Event:
    INTENT_RECEIVED = "intent_received"
    PLAN_GENERATED = "plan_generated"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    # Phase 2.2：副作用级审批（Q9 两层里的第二层）
    SIDE_EFFECT_APPROVAL_REQUESTED = "side_effect_approval_requested"
    SIDE_EFFECT_APPROVAL_GRANTED = "side_effect_approval_granted"
    SIDE_EFFECT_APPROVAL_DENIED = "side_effect_approval_denied"
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    NODE_FAILED = "node_failed"
    # Phase 2.1：Saga 补偿
    COMPENSATION_STARTED = "compensation_started"
    NODE_COMPENSATED = "node_compensated"
    COMPENSATION_FAILED = "compensation_failed"
    VERIFY_PASSED = "verify_passed"
    VERIFY_FAILED = "verify_failed"
    UNKNOWN = "unknown"
    # Phase 3.5.2: 状态接口（D2 的「推」与「拉」）
    # 推 = Runtime 确定性地把状态视图推给模型；拉 = 模型请求更深视图。
    # 两条通道都必须留痕 —— 见 design-foundation/10-runtime-state-premise-rfc.md §6。
    STATE_VIEW_PUSHED = "state_view_pushed"
    STATE_VIEW_PULLED = "state_view_pulled"
    # Phase 3.5.3: 前提声明 + 确定性校验闭环
    # PREMISE_CHECKED 记录「声明了什么 + 校验发现了什么」；PREMISE_BLOCKED 记录拦截。
    PREMISE_CHECKED = "premise_checked"
    PREMISE_BLOCKED = "premise_blocked"
    # Phase 4.1: MCP 工具调用留痕（谁调了哪个 MCP 工具、什么参数、结果）
    MCP_CALL = "mcp_call"
    # Phase 5.3: 弹性重路由（RFC 15 §3.4）
    # 换路**不是**静默降级 —— 触发了、选了哪条替代、耗尽了，都必须留痕。
    REROUTE_TRIGGERED = "reroute_triggered"
    REROUTE_SELECTED = "reroute_selected"
    REROUTE_EXHAUSTED = "reroute_exhausted"
    # Phase 5.4: 挂起与恢复（RFC 15 §3.5）
    # 谁挂的、谁恢复了哪一段 —— 挂起/恢复**必须复用既有审计**。
    TASK_SUSPENDED = "task_suspended"
    TASK_RESUMED = "task_resumed"
    SUSPEND_FAILED = "suspend_failed"
    # Phase 5.5：「大学生工程师」记忆（RFC 15 §3.4-D）
    CONSTRAINT_BLOCKED = "constraint_blocked"
    EXPERIENCE_APPLIED = "experience_applied"
    # 欠账 #18 / RFC 15 §6：run 结束（**每条出口路径都记一条**）
    # 没有它，审计流里**推不出**「最终成功」——
    # 「弹性恢复率」就只能从 `RunResult` 算（RFC 15 §3.4 的已知缺口③）。
    # 口径：data 里的 status / rerouted / reroutes **全部取自 `RunResult`**，
    # 审计侧不另算一套（否则审计与结果各说各话，数字没法互相印证）。
    RUN_FINISHED = "run_finished"

def _resolve_key(key: str | bytes | None) -> bytes | None:
    """密钥优先级：显式传入 > 环境变量 > 无密钥。"""
    if key is None:
        env = os.environ.get(AUDIT_KEY_ENV)
        if not env:
            return None
        key = env
    if isinstance(key, str):
        return key.encode("utf-8")
    return key

def compute_hash(prev_hash: str, payload: dict[str, Any], key: bytes | None) -> str:
    """算一条记录的哈希。"""
    blob = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    data = prev_hash.encode("ascii") + blob
    if key:
        return hmac.new(key, data, hashlib.sha256).hexdigest()
    return hashlib.sha256(data).hexdigest()

def seal_log(path: Path) -> tuple[bool, str]:
    """把日志文件设为只读（WORM）。返回 (成功?, 消息)。

    Windows 上 chmod 只映射到「只读属性」，强度弱于 POSIX；
    真正的防篡改保证来自哈希链，这里是叠加一层。
    """
    p = Path(path)
    if not p.exists():
        return False, f"文件不存在：{p}"
    try:
        p.chmod(stat.S_IREAD)
        return True, f"已封存（只读）：{p.name}"
    except OSError as e:
        return False, f"封存失败：{e}"

@dataclass
class AuditTrail:
    """WORM JSONL append-only 日志（带哈希链）。

    用法：
        trail = AuditTrail(Path("audit.jsonl"))
        trail.log(Event.PLAN_GENERATED, {"nodes": [...]})
        trail.close()

    或 with 块：
        with AuditTrail(Path("audit.jsonl")) as trail:
            trail.log(...)

    写入保证：
    - 每次 log 后 flush + fsync（崩溃不丢）
    - 文件以 append 模式打开（不会截断既有记录）
    - 每条记录带 prev_hash / hash，构成可验证的哈希链
    """

    path: Path
    key: str | bytes | None = None
    seal: bool = False
    _fp: Any = field(init=False, repr=False)
    _prev_hash: str = field(init=False, repr=False, default=GENESIS_HASH)
    _seq: int = field(init=False, repr=False, default=0)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._key = _resolve_key(self.key)
        # 续写既有日志：从最后一条的哈希接上链
        self._prev_hash, self._seq = _tail_chain(self.path)
        # 'a' 模式打开，永不截断既有记录
        self._fp = open(self.path, "a", encoding="utf-8")

    def log(self, event: str, data: dict[str, Any] | None = None) -> None:
        """记录一条事件。失败抛异常（不静默吞）。"""
        self._seq += 1
        payload = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "data": data or {},
        }
        record = dict(payload)
        record["prev_hash"] = self._prev_hash
        record["hash"] = compute_hash(self._prev_hash, payload, self._key)
        line = json.dumps(record, ensure_ascii=False)
        self._fp.write(line + "\n")
        self._fp.flush()
        os.fsync(self._fp.fileno())
        self._prev_hash = record["hash"]

    def close(self) -> None:
        if not self._fp.closed:
            self._fp.close()
        if self.seal:
            seal_log(self.path)

    def seal_now(self) -> tuple[bool, str]:
        """立即封存（不关闭文件句柄 —— Windows 下只读文件仍可写已打开的句柄）。"""
        return seal_log(self.path)

    def __enter__(self) -> "AuditTrail":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        # 兜底关闭（不抛异常）
        try:
            self.close()
        except Exception:
            pass

def _tail_chain(path: Path) -> tuple[str, int]:
    """读既有日志的最后一条哈希与序号，用于续写时接链。"""
    if not path.exists():
        return GENESIS_HASH, 0
    last_hash = GENESIS_HASH
    last_seq = 0
    try:
        with open(path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    if isinstance(rec.get("hash"), str):
                        last_hash = rec["hash"]
                    seq = rec.get("seq")
                    if isinstance(seq, int):
                        last_seq = seq
    except OSError:
        return GENESIS_HASH, 0
    return last_hash, last_seq

def read_events(path: Path) -> list[dict[str, Any]]:
    """读出全部事件记录（跳过坏行）。"""
    p = Path(path)
    if not p.exists():
        return []
    events: list[dict[str, Any]] = []
    with open(p, "r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                events.append(rec)
    return events

def segment_runs(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """按 `intent_received` 切分出多次执行。

    一个审计文件里可能有多次 run（demo 就是把几次写进同一个文件），
    不切分的话回放会把几次执行揉成一段假叙事。

    **切分逻辑只有这一处** —— 审计 CLI 的叙事回放与轨迹重放器共用它
    （RFC 11 §1）。两处各切一遍，早晚切歪。
    """
    runs: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for e in events:
        if e.get("event") == Event.INTENT_RECEIVED and cur:
            runs.append(cur)
            cur = []
        cur.append(e)
    if cur:
        runs.append(cur)
    return runs

@dataclass
class AuditRunOutcome:
    """从审计流里还原出的「一次 run 的终局」—— **与 `RunResult` 同形**。

    **同形是关键**：`reroute/metrics.py` 的 `elastic_recovery_rate` 只读
    `status` 与 `reroutes` 两个字段；审计流还原出来的样本能**直接喂给它** ——
    KPI 就只有**一处定义**（不造第二套口径）。

    字段名沿用 `RunResult` 的：同样是 `status` / `reroutes`。
    """

    status: str
    reroutes: list[dict[str, Any]] = field(default_factory=list)

def run_finished_outcomes(events: list[dict[str, Any]]) -> list[AuditRunOutcome]:
    """从审计流里挑出 `RUN_FINISHED` 事件 → 与 `RunResult` 同形的样本列表。

    **一条 `RUN_FINISHED` = 一次 run 的终局**（成功/失败/拒绝/挂起/阻塞都算）。

    「弹性恢复率」（RFC 15 §6）因此能从审计流算出来：
    把返回值喂给 `reroute.metrics.elastic_recovery_rate` 即可 ——
    **分母口径与从 `RunResult` 算时完全一致**（同一个函数、同一份数据：
    「`reroutes` 非空 = 触发过重路由」）。

    兼容两种写法的事件 data：
    - `reroute_records`：明细列表（含 `fallback_group` —— RFC §6 要按组看）
    - `reroutes`：直接给列表
    """
    outcomes: list[AuditRunOutcome] = []
    for rec in events:
        if rec.get("event") != Event.RUN_FINISHED:
            continue
        data = rec.get("data") or {}
        raw = data.get("reroute_records")
        if not isinstance(raw, list):
            raw = data.get("reroutes")
        records = (
            [dict(r) for r in raw if isinstance(r, dict)]
            if isinstance(raw, list)
            else []
        )
        outcomes.append(
            AuditRunOutcome(
                status=str(data.get("status") or ""),
                reroutes=records,
            )
        )
    return outcomes

def verify_chain(path: Path, key: str | bytes | None = None) -> tuple[bool, list[str]]:
    """校验哈希链。返回 (是否完整?, 问题列表)。

    规则：
    - 带 hash 的记录：重算哈希并比对；prev_hash 必须指向前一条的 hash
    - 不带 hash 的记录（Phase 2.4 之前写的旧记录）：标记为「无哈希旧记录」，
      并在其后**重新起链**（不能把旧记录硬接进新链 —— 那会伪造历史）
    """
    k = _resolve_key(key)
    events = read_events(path)
    issues: list[str] = []
    prev_hash: str | None = GENESIS_HASH
    checked = 0

    for i, rec in enumerate(events, 1):
        if "hash" not in rec or "prev_hash" not in rec:
            issues.append(f"第 {i} 条是无哈希旧记录（WORM 之前写入，未纳入链）")
            prev_hash = GENESIS_HASH
            continue
        if prev_hash is not None and rec["prev_hash"] != prev_hash:
            issues.append(
                f"第 {i} 条 prev_hash 不匹配：期望 {prev_hash[:12]}…，"
                f"实际 {str(rec['prev_hash'])[:12]}…（记录被改动或删除）"
            )
            return False, issues
        payload = {
            "seq": rec.get("seq"),
            "ts": rec.get("ts"),
            "event": rec.get("event"),
            "data": rec.get("data", {}),
        }
        expect = compute_hash(rec["prev_hash"], payload, k)
        if expect != rec["hash"]:
            issues.append(
                f"第 {i} 条哈希不匹配：期望 {expect[:12]}…，实际 {str(rec['hash'])[:12]}…"
                "（内容被改动）"
            )
            return False, issues
        prev_hash = rec["hash"]
        checked += 1

    if checked == 0:
        issues.append("没有可校验的哈希链记录")
    return (not issues) or all("无哈希旧记录" in s for s in issues), issues
