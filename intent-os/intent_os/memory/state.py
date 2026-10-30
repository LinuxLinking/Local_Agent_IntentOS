"""L3 · 状态断言（State Assertion）—— 权威状态源 + Q17 双表示渲染。

设计依据：
- design-foundation/09-state-assertion-rfc.md（本文件的规则出处）
- design-foundation/07-r2-conclusions.md §2（状态断言 = L3 第四形态）
- design-foundation/04-open-questions.md Q17（双表示：权威存世界坐标，喂模型渲染成相对 + 自然语言）
- design-foundation/08-premise-schema-rfc.md §5（状态表从哪来 —— 本文件回答）

── 为什么状态断言住在 L3 ─────────────────────────────────────────────────────
`07` §2 把状态断言定为 L3 的第四形态（常驻集 / 换入集 / 状态断言 / 长期记忆）。
它**是权威源** —— 「现在是什么样」的可校验真相。
L1 的 `PremiseChecker` 只是**消费者**：拿状态表比值，不生产状态。

把 `StateAssertion` 放在 `trust/` 旁边（3.5.0 的权宜之计）会让「定义」跟着「用途」走，
久了必然出现两份 —— 本文件是**唯一定义处**，`trust/premise.py` 只做再导出。

── 四条规则（RFC 09 §3）─────────────────────────────────────────────────────
1. **世界坐标表示**：断言挂在稳定参照系（Frame）上；自我中心断言不作校验基准
2. **冲突消解**：版本号大者胜出（store 自增 ⇒ 最后写胜出）；**冲突必须留痕**
3. **Q17 双表示渲染**：权威层存稳定表述；喂模型渲染成相对 + 自然语言
4. **state_key 契约**：状态键空间 == 前提键空间，`as_state_map()` 是唯一的桥

── 与长期记忆的区别（别搞混）────────────────────────────────────────────────
状态断言 = **现在**是什么样（权威、可校验、能当基准）
长期记忆 = **过去**发生了什么（线索、不能当基准）

── 措辞纪律（继承 RFC 08 §3.1）──────────────────────────────────────────────
冲突文本是**陈述**，不是判决：
  ✗「你的状态错了」      ← 判决 → 模型会对抗
  ✓「状态 X 有两个来源说法不一」  ← 陈述 → 模型能据此改写前提
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

# 事件类型
EV_SET = "set"
EV_CONFLICT = "conflict"
EV_REMOVE = "remove"

# 坐标系种类
FRAME_WORLD = "world"
FRAME_DOMAIN = "domain"
FRAME_AGENT = "agent"

@dataclass(frozen=True)
class Frame:
    """坐标系 —— 状态断言的参照基准。

    **为什么是抽象而不是坐标**：Q17 要的是「权威层存世界坐标」，
    但 `07` §9 明确警告「空间专用架构」是隐喻驱动架构风险 ——
    产品战场是文件 / 代码符号 / API schema / 历史决策，不是坐标纸。
    所以这里只表达「这条断言挂在哪个参照系上」，**不表达 x/y/heading**。

    `kind` 决定它能不能当校验基准：
      world / domain —— 稳定参照系 → ✅ 可作基准
      agent          —— 自我中心 → ❌ 随观察者姿态变化 → 默认排除
    """

    id: str
    kind: str = FRAME_WORLD
    description: str = ""

    @property
    def is_stable(self) -> bool:
        """稳定参照系 = 不随观察者姿态变化 → 可作校验基准。"""
        return self.kind != FRAME_AGENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "description": self.description,
            "is_stable": self.is_stable,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Frame":
        """从 `to_dict()` 的产物还原。

        `is_stable` 是**派生属性**（由 kind 决定），不参与还原 ——
        否则「自我中心断言能否当基准」这件事就有两个真相来源了。
        """
        return cls(
            id=str(d.get("id", "world")),
            kind=str(d.get("kind", FRAME_WORLD)),
            description=str(d.get("description", "")),
        )

#: 默认世界坐标系 —— 稳定，可作校验基准。
WORLD_FRAME = Frame(id="world", kind=FRAME_WORLD, description="世界坐标系（稳定参照系）")

#: 自我坐标系 —— 随观察者姿态变化，不可作校验基准。
AGENT_FRAME = Frame(id="agent", kind=FRAME_AGENT, description="自我坐标系（随姿态变化）")

@dataclass(frozen=True)
class StateAssertion:
    """权威状态里的一条断言。

    `key` 就是 premise 的 `state_key` —— 两边靠 key 对接（RFC 09 规则 4）。
    `version` 参与冲突报告：告诉模型「我依据的是第几版状态」。

    字段顺序**刻意保持** `key, value, version, scope, note` 在前 ——
    RFC 08 之前 `StateAssertion` 住在 `trust/premise.py`，已有调用方按位置传参。
    """

    key: str
    value: str
    version: int = 1
    scope: str = "default"
    note: str = ""
    frame: Frame = WORLD_FRAME
    reference: str = ""  # 参照物 —— 相对表述的锚（如 "仓库东墙"）
    source: str = "runtime"  # 写入来源 —— 冲突消解的依据

    @property
    def frame_dependent(self) -> bool:
        """自我中心断言：随姿态变化 → **不能当校验基准**。

        这条是 Q17 的核心论证在代码里的落点：
        「自我中心会随转身失效，校验会自相矛盾」——
        拿一条会漂的断言当基准，两边一起漂，校验就没有意义了。
        """
        return not self.frame.is_stable

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "version": self.version,
            "scope": self.scope,
            "note": self.note,
            "frame": self.frame.to_dict(),
            "reference": self.reference,
            "source": self.source,
            "frame_dependent": self.frame_dependent,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StateAssertion":
        """从 `to_dict()` 的产物还原 —— 轨迹重放要靠它重建校验基准。

        `frame_dependent` 是**派生属性**（由 frame.kind 决定），不参与还原 ——
        还原出来的断言必须自己算得出「是不是自我中心」，不能信序列化字段。
        """
        frame_data = d.get("frame") or {}
        return cls(
            key=str(d.get("key", "")),
            value=str(d.get("value", "")),
            version=int(d.get("version", 1)),
            scope=str(d.get("scope", "default")),
            note=str(d.get("note", "")),
            frame=Frame.from_dict(frame_data) if frame_data else WORLD_FRAME,
            reference=str(d.get("reference", "")),
            source=str(d.get("source", "runtime")),
        )

@dataclass(frozen=True)
class StateConflict:
    """两个来源对同一 key 说法不一。

    **冲突 ≠ 更新**：同一来源连续写是更新；不同来源值不同才是冲突。
    后者正是暴论 #17 要抓的东西（「模型以为 X，状态说不是 X」）。
    """

    key: str
    previous_value: str
    new_value: str
    previous_source: str
    new_source: str
    previous_version: int
    version: int

    def render(self) -> str:
        """冲突文本 —— 陈述，不是判决。"""
        return (
            f"状态「{self.key}」有两个来源说法不一："
            f"{self.previous_source}（v{self.previous_version}）说 {self.previous_value}，"
            f"{self.new_source}（v{self.version}）说 {self.new_value}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "previous_value": self.previous_value,
            "new_value": self.new_value,
            "previous_source": self.previous_source,
            "new_source": self.new_source,
            "previous_version": self.previous_version,
            "version": self.version,
        }

@dataclass(frozen=True)
class StateEvent:
    """状态事件流里的一条事件（append-only）。

    与 `MemoryStore` 的事件流同一传统（Q7「事件流为底」）——
    但状态断言的事件流是**同步内存**的，不是 SQLite：状态是「现在」，不是历史。
    """

    ts: float
    event: str
    key: str
    detail: str = ""
    version: int = 0

@dataclass(frozen=True)
class StateView:
    """渲染出的状态视图 —— 文本 + 键清单 + 状态纪元（D2「推」的载荷）。

    `version` 是**状态纪元**：整批状态是第几版。
    模型据此可以说「我依据的是 v3 的状态」——
    与 RFC 08 §3.2 呼应：模型要能判断「是状态变了，还是我一直就错」。
    """

    text: str
    keys: tuple[str, ...]
    version: int
    omitted: tuple[str, ...] = ()  # 因 frame_dependent 被排除的键

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "keys": list(self.keys),
            "version": self.version,
            "omitted": list(self.omitted),
        }

class StateStore:
    """权威状态源：写入 / 版本 / 冲突消解 / 对接校验器。

    用法：
        store = StateStore()
        store.set("wall.at.3m", "仓库东侧 3 米有墙",
                  reference="仓库东墙", source="sensor")
        store.as_state_map()          # → 喂 PremiseChecker(state=...)
        store.view()                  # → 喂模型的视图

    **版本号由 store 自增**，不由调用方传（RFC 09 D-e）：
    两个写入方都传 `v1` 会自相矛盾。
    """

    def __init__(self, *, scope: str = "default") -> None:
        self.scope = scope
        self._frames: dict[str, Frame] = {WORLD_FRAME.id: WORLD_FRAME}
        self._current: dict[str, StateAssertion] = {}
        self._history: dict[str, list[StateAssertion]] = {}
        self._conflicts: list[StateConflict] = []
        self._events: list[StateEvent] = []
        self._epoch = 0  # 状态纪元 —— 每次写入 +1

    # ── 坐标系注册 ────────────────────────────────────────────────────────────

    def register_frame(self, frame: Frame) -> Frame:
        """注册一个坐标系。

        同一个 id **不允许**换 kind —— 坐标系一旦声明就不能悄悄改性质
        （否则「这条断言还算不算稳定」会在运行中漂移）。
        """
        existing = self._frames.get(frame.id)
        if existing is not None and existing.kind != frame.kind:
            raise ValueError(
                f"frame {frame.id!r} 已注册为 {existing.kind!r}，不能改成 {frame.kind!r}"
            )
        self._frames[frame.id] = frame
        return frame

    def frame(self, frame_id: str) -> Frame | None:
        return self._frames.get(frame_id)

    def _resolve_frame(self, frame: Frame | str | None) -> Frame:
        if frame is None:
            return self._frames.get(self.scope) or WORLD_FRAME
        if isinstance(frame, Frame):
            return self.register_frame(frame)
        found = self._frames.get(frame)
        if found is None:
            raise KeyError(f"unknown frame: {frame!r}")
        return found

    # ── 写入 ──────────────────────────────────────────────────────────────────

    def set(
        self,
        key: str,
        value: str,
        *,
        reference: str = "",
        frame: Frame | str | None = None,
        source: str = "runtime",
        scope: str | None = None,
        note: str = "",
    ) -> StateAssertion:
        """写入 / 覆盖一条权威状态断言。

        同一 key 再次写入 = 同一件事的新版本：**版本 +1，旧版进版本链**。
        若**来源不同且值不同** → 记一条 `StateConflict`（RFC 09 规则 2）。
        """
        key = key.strip()
        if not key:
            raise ValueError("state key must not be empty")

        frame_obj = self._resolve_frame(frame)
        scope_val = scope or self.scope
        previous = self._current.get(key)
        version = (previous.version + 1) if previous is not None else 1

        assertion = StateAssertion(
            key=key,
            value=value,
            version=version,
            scope=scope_val,
            note=note,
            frame=frame_obj,
            reference=reference,
            source=source,
        )

        # 冲突检测：不同来源 + 不同值。
        # 同来源改值 = 更新；不同来源同值 = 一致；都不是冲突。
        if (
            previous is not None
            and previous.source != source
            and previous.value != value
        ):
            conflict = StateConflict(
                key=key,
                previous_value=previous.value,
                new_value=value,
                previous_source=previous.source,
                new_source=source,
                previous_version=previous.version,
                version=version,
            )
            self._conflicts.append(conflict)
            self._log(EV_CONFLICT, key, conflict.render(), version)

        if previous is not None:
            self._history.setdefault(key, []).append(previous)

        self._current[key] = assertion
        self._epoch += 1
        self._log(EV_SET, key, f"{source}: {value}", version)
        return assertion

    def set_many(self, items: Iterable[StateAssertion]) -> None:
        """批量写入 —— 注意：这里**按传入顺序重建版本链**，调用方需自己保证版本递增。"""
        for item in items:
            self.set(
                item.key,
                item.value,
                reference=item.reference,
                frame=item.frame,
                source=item.source,
                scope=item.scope,
                note=item.note,
            )

    # ── 退役（remove）────────────────────────────────────────────────────────

    def remove(self, key: str) -> bool:
        """退役一条断言 —— **世界变了，那条事实不再成立**。

        场景：观察者换了个位置，「当前所在房间」这类事实就换了主人；
        旧的那条若继续留在状态表里，会让视图掺进已经不相关的事实
        （推给模型的东西必须只包含当下有效的断言）。

        语义：
          · 从**当前**断言里移除（不再参与 `as_state_map()`，即不再作校验基准）
          · **版本链保留**（`history()` 仍查得到它）—— 历史不因为「退役」而消失
          · 记一条 `remove` 事件 —— 退役这件事本身要留痕

        返回是否真的移除了（本来就没有 → False）。
        """
        key = key.strip()
        if key not in self._current:
            return False
        retired = self._current.pop(key)
        self._history.setdefault(key, []).append(retired)
        self._epoch += 1
        self._log(EV_REMOVE, key, f"{retired.source}: {retired.value}（已退役）", retired.version)
        return True

    # ── 读取 ──────────────────────────────────────────────────────────────────

    def get(self, key: str) -> StateAssertion | None:
        return self._current.get(key)

    def history(self, key: str) -> list[StateAssertion]:
        """版本链（旧 → 新，不含当前版本）。"""
        return list(self._history.get(key, ()))

    def assertions(self, scope: str | None = None) -> list[StateAssertion]:
        """当前所有断言（按写入顺序）。scope 可切片。"""
        items = list(self._current.values())
        if scope is not None:
            items = [a for a in items if a.scope == scope]
        return items

    def version_of(self, key: str) -> int:
        assertion = self._current.get(key)
        return assertion.version if assertion is not None else 0

    @property
    def epoch(self) -> int:
        """状态纪元 —— 每次写入 +1。视图用它标「这批状态是第几版」。"""
        return self._epoch

    def conflicts(self) -> list[StateConflict]:
        return list(self._conflicts)

    def events(self) -> list[StateEvent]:
        return list(self._events)

    # ── 事件流 ────────────────────────────────────────────────────────────────

    def _log(self, event: str, key: str, detail: str = "", version: int = 0) -> None:
        """记一条事件（append-only）。

        状态事件流与 `MemoryStore` 同一传统（Q7「事件流为底」），
        但状态断言的事件流是**同步内存**的 —— 状态是「现在」，不是历史。
        """
        self._events.append(
            StateEvent(
                ts=time.time(),
                event=event,
                key=key,
                detail=detail,
                version=version,
            )
        )

    # ── 对接 L1（RFC 09 规则 4：唯一的桥）─────────────────────────────────────

    def as_state_map(
        self,
        *,
        include_frame_dependent: bool = False,
        scope: str | None = None,
    ) -> dict[str, StateAssertion]:
        """权威状态表 —— **喂 `PremiseChecker(state=...)` 的唯一对接口**。

        默认**排除 frame_dependent 断言**：它们随观察者姿态变化，
        拿它们当基准会让「前提 vs 状态」两边一起漂移（Q17 的原话）。

        `scope` 可切片（RFC 09 §6 风险表承诺的能力）：
        传 `None` = 全域。**视图与校验必须用同一个 scope** ——
        否则会出现「模型没看见这个键，却被告知断言错了」的荒谬
        （见 design-foundation/10-runtime-state-premise-rfc.md §3）。
        """
        return {
            key: assertion
            for key, assertion in self._current.items()
            if (include_frame_dependent or not assertion.frame_dependent)
            and (scope is None or assertion.scope == scope)
        }

    def view(
        self,
        renderer: "StateRenderer | None" = None,
        *,
        title: str = "当前状态",
        include_frame_dependent: bool = False,
        scope: str | None = None,
    ) -> StateView:
        """渲染出喂给模型的状态视图（D2 的「推」的载荷）。

        `scope` 与 `as_state_map(scope=...)` 同义 —— 视图与校验基准必须同 scope。
        """
        renderer = renderer or StateRenderer()
        keep = [
            a
            for a in self._current.values()
            if (include_frame_dependent or not a.frame_dependent)
            and (scope is None or a.scope == scope)
        ]
        omitted = tuple(
            a.key
            for a in self._current.values()
            if a.frame_dependent and not include_frame_dependent
        )
        return renderer.render_view(keep, title=title, version=self._epoch, omitted=omitted)

class RelationResolver(Protocol):
    """把一条结构化断言渲染成相对自然语言的扩展点。

    **核心层不含空间计算**（RFC 09 D-g / `07` §9）：空间只是载体，机制要能换域。
    3.5.5 的迷宫 store 提供空间实现；本步只保证「换域不换机制」。
    """

    def describe(self, assertion: StateAssertion) -> str | None:
        """返回自然语言描述；返回 None 表示渲染不了，走默认模板。"""
        ...

class StateRenderer:
    """Q17 双表示的渲染器（RFC 09 规则 3）。

    **权威层存稳定表述 → 喂模型时渲染成相对 + 自然语言。**

    渲染优先级（确定性，无歧义）：
      1. `RelationResolver` 能渲染 → 用它的结果
      2. 有 `reference` → 「`{key} = {value}`（相对 `{reference}`）」
      3. 无 `reference` → 「`{key} = {value}`」

    **输出里不出现坐标数值** —— 3.3 实测：模型靠语义匹配，不靠字面
    （字面基线 hit@1 20%，真嵌入 100%）。「仓库东侧 3 米有墙」模型匹配得上，
    `(12, 7, 朝向90°)` 匹配不上。
    """

    def __init__(self, resolver: RelationResolver | None = None) -> None:
        self.resolver = resolver

    def render_one(self, assertion: StateAssertion) -> str:
        """渲染一条断言。"""
        if self.resolver is not None:
            described = self.resolver.describe(assertion)
            if described:
                return described
        if assertion.reference:
            return f"{assertion.key} = {assertion.value}（相对 {assertion.reference}）"
        return f"{assertion.key} = {assertion.value}"

    def render_detail(self, assertion: StateAssertion) -> str:
        """渲染一条断言的**明细** —— 「拉」通道用。

        推的渲染是「key = value」一句话；拉的是**更深**的视图：
        额外给出**版本 / 来源 / 参照物 / 备注** ——
        模型要判断「是状态变了，还是我一直就错」时，需要这几项（RFC 08 §3.2）。

        措辞仍是**陈述**，不是判决（继承 RFC 08 §3.1）。
        """
        head = self.render_one(assertion)
        extras: list[str] = [f"v{assertion.version}", f"来源 {assertion.source}"]
        if assertion.reference:
            extras.append(f"参照 {assertion.reference}")
        if assertion.note:
            extras.append(f"备注 {assertion.note}")
        return f"{head}（{' · '.join(extras)}）"

    def render_view(
        self,
        assertions: Iterable[StateAssertion],
        *,
        title: str = "当前状态",
        version: int = 0,
        omitted: tuple[str, ...] = (),
    ) -> StateView:
        """渲染一批断言成一段可注入上下文的文本。"""
        items = list(assertions)
        if items:
            body = "\n".join(f"- {self.render_one(a)}" for a in items)
            text = f"【{title}】\n{body}"
        else:
            text = f"【{title}】（空）"
        if omitted:
            # 诚实：告诉模型哪些状态没进视图，以及为什么。
            text += f"\n（{len(omitted)} 条自我中心状态未纳入：{', '.join(omitted)}）"
        return StateView(
            text=text,
            keys=tuple(a.key for a in items),
            version=version,
            omitted=omitted,
        )
