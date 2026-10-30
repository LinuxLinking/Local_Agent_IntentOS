"""L3 · 状态接口（D2）—— Runtime 与模型之间的状态通道。

设计依据：
- design-foundation/10-runtime-state-premise-rfc.md（本文件的规则出处）
- design-foundation/07-r2-conclusions.md §1 D2（外置状态不是传统工具调用）
- design-foundation/09-state-assertion-rfc.md §3（Q17 双表示渲染 + state_key 契约）

── 为什么这个类存在 ───────────────────────────────────────────────────────
D2 说「外置状态**不作为 Tool / MCP 暴露给模型**」。这句话必须**在代码里结构性地成立**，
而不是靠约定 —— 所以状态通道是一个**独立于 `ToolExecutor` 协议**的类：

    模型侧
      ├── Tool / MCP 通道 ──→ L2 executor.execute(node)  ← 一次性 I/O，模型发起
      └── 状态通道        ──→ StateInterface              ← 持续维护的状态，Runtime 决定推什么

工具集是 `{write_file, read_file, list_dir, run_python}` —— 状态**不在其中**。
模型拿不到任何「调工具拿状态」的口子，状态只能由 Runtime 推。

── 推为主，拉为辅（D2 原文）────────────────────────────────────────────────
- **推（push）**：Runtime **确定性地**渲染状态视图，注入模型上下文。
  「选什么是内核的决定」—— 不是模型点的菜。**每次 run 必推一次。**
- **拉（pull）**：模型请求**更深**的视图（指定键 + 明细渲染）。
  可选，**但必须留痕**，且**不能替代推**。

两条通道**都必须留痕** —— 所以 `audit` 是构造参数而不是可选项：
没有审计通道就不许推、不许拉（这不是苛刻，是 D2「必须留痕」的字面实现）。

── 为什么接口住在顶层而不是 memory/ 下 ─────────────────────────────────────
它同时需要 `memory.state`（数据面）与 `trust.audit`（留痕面）。
放进 `memory/` 会造成 `memory → trust` 的反向依赖，
而 `trust.premise` 已经依赖 `memory.state` —— 那会变成环。
顶层模块（与 `runtime.py` / `types.py` 同层）是最自然的位置。

── 措辞纪律（继承 RFC 08 §3.1）────────────────────────────────────────────
渲染结果是**陈述**，不是判决：
  ✗「你的状态错了」      ← 判决 → 模型会对抗
  ✓「当前状态是 X」      ← 陈述 → 模型能据此改写前提
"""
from __future__ import annotations

from typing import Any, Iterable, Protocol

from .memory.state import (
    StateAssertion,
    StateRenderer,
    StateStore,
    StateView,
)
from .trust.audit import Event

class AuditSink(Protocol):
    """留痕面 —— 只要有 `.log(event, data)` 就行。

    刻意用 Protocol 而不是绑死 `AuditTrail`：
    测试可以塞一个收集器，生产用 `AuditTrail`，两边同一套事件名。
    """

    def log(self, event: str, data: dict[str, Any] | None = None) -> None: ...

class StateInterface:
    """外置状态的对外接口 —— **推为主，拉为辅**（D2）。

    用法：
        state_if = StateInterface(store, audit=audit)
        state_if.set("sandbox.clean", "true", source="sensor")
        view = state_if.push_view()          # 推（留痕）
        detail = state_if.pull_view(["sandbox.clean"])   # 拉（留痕）

    **状态断言的定义在 `memory/state.py`**（L3 权威源），
    本类只做**通道**，不重新定义任何状态概念。
    """

    def __init__(
        self,
        store: StateStore | None = None,
        *,
        audit: AuditSink,
        renderer: StateRenderer | None = None,
    ) -> None:
        """Args:
            store: 权威状态源。不传就新建一个空的。
            audit: **必填** —— 推与拉都必须留痕（D2）。
            renderer: 渲染器（Q17 双表示）。默认用 `StateRenderer()`。
        """
        self.store = store if store is not None else StateStore()
        self.renderer = renderer or StateRenderer()
        self._audit = audit

    # ── 写 ────────────────────────────────────────────────────────────────────

    def set(self, key: str, value: str, **kwargs: Any) -> StateAssertion:
        """写入权威状态（版本自增 + 冲突消解，见 `StateStore.set`）。

        写入方通过 `source=` 区分自己 —— 冲突消解的依据是来源，不是值。
        """
        return self.store.set(key, value, **kwargs)

    # ── 读（纯渲染，不留痕）───────────────────────────────────────────────────

    def view(self, *, scope: str | None = None, title: str = "当前状态") -> StateView:
        """渲染状态视图 —— **不留痕**。

        给「只是看一眼」的场合用（比如入口层拼提示词时先看一眼）。
        真正的「推」是 `push_view`。
        """
        return self.store.view(self.renderer, title=title, scope=scope)

    # ── 推（push）—— 主通道 ──────────────────────────────────────────────────

    def push_view(
        self,
        *,
        scope: str | None = None,
        title: str = "当前状态",
        trigger: str = "run",
    ) -> StateView:
        """推：Runtime **确定性地**渲染状态视图，注入模型上下文。

        Args:
            scope: 状态切片。**必须与校验用的 scope 一致** ——
                否则会出现「模型没看见这个键，却被告知断言错了」。
            trigger: 这次推的来源标记（"run" = 本次 run 的校验基准；
                "compile" = 编译前推给模型看的那份）。
                审计里靠它区分两次推 —— **两者可能不同**：
                状态在「模型声明前提」与「run 执行」之间变了，
                那正是暴论 #17 要抓的「状态变了」。

        Returns:
            `StateView` —— 文本 + 键清单 + 状态纪元（模型据此说「我依据的是第几版」）。
        """
        view = self.view(scope=scope, title=title)
        # 校验基准的结构化快照 —— **轨迹自足**的关键（RFC 11 §2.1）。
        # 用 `as_state_map()` 而不是视图：视图是渲染后的文本，**有损**，
        # 反推不出结构化字段；这里记的必须是**校验用的那份**。
        # （序列化的是已有形状 `StateAssertion.to_dict()`，不新增定义。）
        state_map = self.store.as_state_map(scope=scope)
        self._audit.log(
            Event.STATE_VIEW_PUSHED,
            {
                "trigger": trigger,
                "scope": scope,
                "keys": list(view.keys),
                "version": view.version,
                "omitted": list(view.omitted),
                "text": view.text,
                "assertions": [a.to_dict() for a in state_map.values()],
            },
        )
        return view

    # ── 拉（pull）—— 辅通道 ──────────────────────────────────────────────────

    def pull_view(
        self,
        keys: Iterable[str],
        *,
        scope: str | None = None,
        title: str = "状态明细",
    ) -> StateView:
        """拉：模型请求**更深**的视图（按键 + 明细渲染）。

        与推的区别：
          · 推 = Runtime 决定推什么，一句话摘要
          · 拉 = 模型挑键，给明细（版本 / 来源 / 参照物 / 备注）

        **必须留痕**（D2：「拉为辅，但必须留痕」）——
        审计里记下请求了什么、命中什么、缺什么。

        拉**不能替代推**：推是每次 run 的强制动作，拉是可选的。
        """
        wanted = [k.strip() for k in keys if k and k.strip()]
        found = []
        missing: list[str] = []
        for key in wanted:
            assertion = self.store.get(key)
            if assertion is None:
                missing.append(key)
                continue
            if scope is not None and assertion.scope != scope:
                missing.append(key)
                continue
            found.append(assertion)

        body = "\n".join(f"- {self.renderer.render_detail(a)}" for a in found)
        text = f"【{title}】\n{body}" if found else f"【{title}】（空）"
        if missing:
            # 诚实：告诉模型要看的键里哪些根本没有。
            text += f"\n（{len(missing)} 个键在权威状态里不存在：{', '.join(missing)}）"

        view = StateView(
            text=text,
            keys=tuple(a.key for a in found),
            version=self.store.epoch,
            omitted=(),
        )
        self._audit.log(
            Event.STATE_VIEW_PULLED,
            {
                "requested": wanted,
                "found": list(view.keys),
                "missing": missing,
                "scope": scope,
                "version": view.version,
                "text": view.text,
            },
        )
        return view

    # ── 冲突与事件（审计用）──────────────────────────────────────────────────

    def conflicts(self) -> list[Any]:
        """状态冲突列表（不同来源对同一 key 说法不一）。"""
        return self.store.conflicts()

    def events(self) -> list[Any]:
        """状态事件流（append-only）。"""
        return self.store.events()
