"""MCP executor —— 把 MCP 工具接进 L2 的**既有**执行路径（RFC 14 D-e）。

设计依据：`design-foundation/14-mcp-integration-rfc.md` D-d / D-e / D-g

**为什么不是「另开一条路」**：L2 的接口已经是 `ToolExecutor`
（`execute` / `compensate` / `reset_snapshots`）。本类实现它，
`IntentOSRuntime` 就**完全不知道** MCP 的存在 ——
L1 审批链、Audit 留痕、Saga 补偿的逻辑一行都不用改（D-e）。

**执行纪律**：
  · **未声明不执行** —— 工具必须出现在 `McpRegistry` 的接入清单里。
    server 自己临时冒出来的工具、或者模型自己编的名字，一律拒绝（D-d / D-g）。
  · **不撒谎** —— 连不上 / 超时 / 工具报错 → 如实 `ok=False` + 原因。
    **失败绝不变成成功**。
  · **写操作先存快照** —— 补偿的前提是「能回到执行前的样子」。
    快照复用 L2 的 `SnapshotStore`，沙箱边界校验也用它那一份（不另造一套）。

**D2（状态通道不经过 MCP）**：本模块**不 import** `memory/state.py`，
也不提供任何读状态的工具 —— MCP 工具目录里只有 server 自己声明的工具。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..compiler.intent import SideEffectKind
from ..compiler.plan import PlanNode
from ..executor.snapshot import SnapshotStore
from ..types import NodeResult
from ..trust.audit import Event
from .client import McpClient
from .registry import MCP_PREFIX, McpRegistry, McpServerSpec, split_tool_name

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注
    from ..trust.audit import AuditTrail

def _default_client_factory(spec: McpServerSpec) -> McpClient:
    """按 server 声明起一个 MCP 客户端（**懒连接**：真正建会话在第一次调用时）。"""
    return McpClient(
        spec.name,
        spec.command,
        spec.args,
        cwd=spec.cwd,
        env=spec.env,
        connect_timeout=spec.connect_timeout,
        call_timeout=spec.call_timeout,
    )

def _jsonable(value: Any) -> Any:
    """把参数变成能进 JSON 日志的形态。

    不可序列化的对象 → `repr`（**不静默丢字段**）。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return repr(value)

class McpExecutor:
    """实现 ToolExecutor 协议 —— MCP 工具走 L2 的既有执行路径（RFC 14 D-e）。

    用法：
        registry = default_fs_registry(Path("./sandbox"))
        with McpExecutor(registry, sandbox_dir=Path("./sandbox"), audit=audit) as ex:
            node = PlanNode(name="read", tool="mcp:fs:read_file", args={"path": "a.txt"})
            result = ex.execute(node)
    """

    def __init__(
        self,
        registry: McpRegistry,
        *,
        sandbox_dir: Path | None = None,
        audit: "AuditTrail | None" = None,
        client_factory: Callable[[McpServerSpec], McpClient] | None = None,
    ) -> None:
        self.registry = registry
        # 沙箱 = 快照记的地方，**必须与 server 真正写的地方是同一个**：
        #   · 显式给了 sandbox_dir → 用它；与注册表声明的 sandbox_root 不一致 → 直接炸
        #     （记错地方 = 补偿还原不到真正被写的文件，坑 3）
        #   · 没给 → 用注册表声明的 sandbox_root（默认就是 server 的 --root）
        #   · 两者都没有 → 没有沙箱（写操作降级成「不可补偿」，如实留痕）
        declared_root = registry.sandbox_root
        if sandbox_dir is not None:
            self.sandbox_dir = Path(sandbox_dir).resolve()
            if declared_root is not None and self.sandbox_dir != declared_root:
                raise ValueError(
                    f"sandbox_dir ({self.sandbox_dir}) 与注册表声明的 sandbox_root "
                    f"({declared_root}) 不一致 —— 快照会记在别处，"
                    "补偿还原不到真正被写的文件"
                )
        elif declared_root is not None:
            self.sandbox_dir = Path(declared_root).resolve()
        else:
            self.sandbox_dir = None
        self.audit = audit
        self._client_factory = client_factory or _default_client_factory
        self._clients: dict[str, McpClient] = {}
        # close() 的失败留在这里 —— 免得被 __exit__ 吞成「静默」
        self.close_errors: list[str] = []
        # 快照账本 —— 复用 L2 的 SnapshotStore（补偿用）
        self.snapshots = (
            SnapshotStore(self.sandbox_dir) if self.sandbox_dir is not None else None
        )

    # ── 执行 ────────────────────────────────────────────────────────────────

    def execute(self, node: PlanNode) -> NodeResult:
        """执行一个 MCP 节点。**失败绝不变成成功。**"""
        t0 = time.monotonic()
        tool_name = node.tool

        # 1. 不是 MCP 工具 → 直接拒绝（不是「可能是」）
        if not tool_name.startswith(MCP_PREFIX):
            return self._fail(
                node,
                "",
                tool_name,
                t0,
                f"not an MCP tool: {tool_name!r}",
            )

        # 2. 名字格式不对 → 拒绝
        parsed = split_tool_name(tool_name)
        if parsed is None:
            return self._fail(
                node,
                "",
                tool_name,
                t0,
                f"MCP 工具名格式不对（应为 mcp:<server>:<tool>）：{tool_name!r}",
            )
        server_name, raw_tool = parsed

        # 3. **未声明不执行**（D-d）—— 接入声明说了算，不是 server 说了算
        spec = self.registry.tool(tool_name)
        if spec is None:
            return self._fail(
                node,
                server_name,
                raw_tool,
                t0,
                f"工具 {tool_name!r} 未接入声明（不在 McpRegistry 的接入清单里）"
                "—— 未声明的工具一律不执行",
            )
        if self.registry.server(server_name) is None:
            return self._fail(
                node,
                server_name,
                raw_tool,
                t0,
                f"工具 {tool_name!r} 声明的 server {server_name!r} 未接入"
                "（McpRegistry 里没有这个 server）",
            )

        # 4. 写操作：执行前先存快照（补偿的前提）
        compensable = True
        note = ""
        if spec.kind == SideEffectKind.WRITE:
            ok, msg, compensable = self._capture_snapshot(node)
            if not ok:
                return self._fail(node, server_name, raw_tool, t0, msg)
            if not compensable:
                # 降级（**不是失败**）：命令照跑，但降级原因必须留痕 ——
                # 红线 #4：任何 fallback 都要把原因打出来，不能只留一个机器可读的布尔位。
                note = msg

        # 5. 真调
        try:
            client = self.client_for(server_name)
            result = client.call_tool(raw_tool, dict(node.args))
        except Exception as e:
            return self._fail(
                node,
                server_name,
                raw_tool,
                t0,
                f"{type(e).__name__}: {e}",
            )

        if not result.ok:
            return self._fail(
                node,
                server_name,
                raw_tool,
                t0,
                result.error or "(server 未给出错误详情)",
                elapsed=result.elapsed_s,
                compensable=compensable,
                note=note,
            )

        self._audit_call(
            node,
            server_name,
            raw_tool,
            ok=True,
            error="",
            elapsed=result.elapsed_s,
            compensable=compensable,
            note=note,
        )
        return NodeResult(ok=True, output=result.content, error=None)

    # ── 补偿 ────────────────────────────────────────────────────────────────

    def compensate(self, node: PlanNode) -> NodeResult:
        """按执行前的快照还原该节点造成的改动（Saga 补偿）。

        · 有快照 → 还原（失败就如实失败）；
        · 只读工具 → 「无副作用需补偿」（**不是失败**）；
        · 写工具但没有快照 → 如实说「无法补偿」（**不假装无副作用**）。
        """
        if self.snapshots is not None and self.snapshots.has_snapshots(node.name):
            ok, msg = self.snapshots.restore_node(node.name)
            return NodeResult(ok=ok, output=msg, error=None if ok else msg)

        spec = self.registry.tool(node.tool)
        if spec is not None and spec.kind == SideEffectKind.WRITE:
            return NodeResult(
                ok=False,
                output="",
                error=f"写工具 {node.tool!r} 没有快照可还原，无法补偿",
            )
        return NodeResult(ok=True, output="无副作用需补偿", error=None)

    def reset_snapshots(self) -> None:
        """清空上一轮 run 留下的快照账本（runtime 在每次 run 开始时调用）。"""
        if self.snapshots is not None:
            self.snapshots.reset()

    # ── 连接 ────────────────────────────────────────────────────────────────

    def client_for(self, server: str) -> McpClient:
        """取（并缓存）某个 server 的客户端 —— **懒连接 + 会话复用**。

        server 进程只起一次：McpClient 内部持有持久会话，
        多次调用走同一条 stdio 管道（每次重起进程会让启动开销盖过真实成本）。
        """
        client = self._clients.get(server)
        if client is not None:
            return client
        spec = self.registry.server(server)
        if spec is None:
            raise RuntimeError(
                f"MCP server {server!r} 未在接入声明里注册"
                "（McpRegistry 里没有这个 server）"
            )
        client = self._client_factory(spec)
        self._clients[server] = client
        return client

    def close(self) -> None:
        """关掉所有已开的 client。**必须真的关** —— 否则 server 子进程会留着。

        关不掉就如实抛（不静默吞）；逐条原因同时留在 `close_errors` 里。
        """
        errors: list[str] = []
        for name, client in list(self._clients.items()):
            try:
                client.close()
            except Exception as e:
                errors.append(f"{name}: {type(e).__name__}: {e}")
        self._clients.clear()
        if errors:
            self.close_errors.extend(errors)
            raise RuntimeError("关闭 MCP client 失败：" + "; ".join(errors))

    def __enter__(self) -> "McpExecutor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """关连接。**不掩盖 with 块里的原异常**。

        块里已经在抛异常时，close 的失败只记进 `close_errors` 不再抛 ——
        原来的失败原因更要紧（别让「关不掉」盖掉「为什么失败」）；
        块里没异常时，close 失败照抛（不静默）。
        """
        try:
            self.close()
        except Exception:
            # close() 已经把逐条原因写进 close_errors 了，这里不重复记
            if exc_type is None:
                raise

    # ── 内部：快照 / 留痕 ───────────────────────────────────────────────────

    def _capture_snapshot(self, node: PlanNode) -> tuple[bool, str, bool]:
        """写操作执行前存快照。返回 (成功?, 消息, 可补偿?)。

        路径取自 `node.args["path"]`，**边界校验用 SnapshotStore 自己那一份**
        （不另造一套沙箱规则）；越界 → 如实报错，不静默改写路径。
        """
        if self.snapshots is None:
            # 没配置沙箱 → 存不了快照 → 补偿不了。
            # 不静默降级：执行继续（写本身是合法的），但补偿时会如实报「无法补偿」。
            return True, "未配置 sandbox_dir —— 写操作无快照，无法补偿", False
        raw_path = node.args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return (
                False,
                f"写操作缺少 path 参数，无法在执行前存快照：{node.args!r}",
                False,
            )
        try:
            self.snapshots.capture(node.name, raw_path)
        except ValueError as e:
            return False, f"路径越界（沙箱限定在 {self.sandbox_dir}）：{raw_path!r}（{e}）", False
        return True, f"已存快照：{raw_path}", True

    def _fail(
        self,
        node: PlanNode,
        server: str,
        tool: str,
        t0: float,
        error: str,
        *,
        elapsed: float | None = None,
        compensable: bool = True,
        note: str = "",
    ) -> NodeResult:
        """统一的失败出口：**ok=False + 如实原因 + 留痕**。"""
        self._audit_call(
            node,
            server,
            tool,
            ok=False,
            error=error,
            elapsed=elapsed if elapsed is not None else time.monotonic() - t0,
            compensable=compensable,
            note=note,
        )
        return NodeResult(ok=False, output="", error=error)

    def _audit_call(
        self,
        node: PlanNode,
        server: str,
        tool: str,
        *,
        ok: bool,
        error: str,
        elapsed: float,
        compensable: bool = True,
        note: str = "",
    ) -> None:
        """记一条 MCP 调用留痕（谁调了哪个 MCP 工具、什么参数、结果如何）。

        `note` 记**降级原因**（如「没配 sandbox_dir → 写操作无快照，无法补偿」）：
        降级不是失败，命令照跑；但原因必须**留痕、看得见** ——
        光留一个 `compensable: false` 的布尔位，机器能读、人读不到为什么。

        `Event.MCP_CALL` 由 L1 审计层定义；若该常量还没落地，
        退回到字符串 "mcp_call"（**不静默丢弃这条留痕**）。
        """
        if self.audit is None:
            return
        event = getattr(Event, "MCP_CALL", "mcp_call")
        self.audit.log(
            event,
            {
                "node_name": node.name,
                "server": server,
                "tool": tool,
                "args": _jsonable(node.args),
                "ok": ok,
                "error": error,
                "elapsed_s": round(elapsed, 3),
                "compensable": compensable,
                "note": note,
            },
        )
