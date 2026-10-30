"""L3 域适配器 · 字符画迷宫沙盒 —— 外置海马体的**空间域**实现。

设计依据：
- design-foundation/12-maze-sandbox-rfc.md（本文件的规则出处）
- design-foundation/09-state-assertion-rfc.md §3 规则 1/3（世界坐标 · Q17 双表示）
- design-foundation/07-r2-conclusions.md §9（空间只是载体，**机制要能换域**）

── 这个模块是什么 ──────────────────────────────────────────────────────────
一个**确定性的环境**（零模型调用）：房间有名字，走廊有方向，有的通有的被墙堵着。
它是 3.5.5 的**沙盒载体**，负责把「迷宫长什么样」翻译成**权威状态断言**。

`07` §9 说得很直白：**「空间只是载体」**。所以：

  · 机制（状态断言 / 前提校验 / 拦截）**不含任何空间概念** ——
    它们只认「key → value」；本模块只负责**生产**这些 key。
  · 换域不换机制：文件域、代码符号域、API schema 域都能用同一套门。

── 为什么状态键是「房间名 + 方向」而不是坐标 ──────────────────────────────
Q17 的核心论证：**自我中心会随转身失效，校验会自相矛盾**。

所以键必须是**世界坐标（allocentric）** —— 关于某个**有名地点**的事实：

    maze.corridor.start.east = open        # 从 start 往东的走廊通不通
    maze.corridor.start.west = blocked
    maze.goal.room = fruit                 # 目标在哪个房间

注意几点：

  · **房间用名字，不用坐标** —— 键里不含 `3` / `12,7` 这类坐标数值，
    也不含会随位置漂移的「你面前」。渲染出来也不出现坐标（RFC 09 规则 3）。
  · **自我中心的断言不作基准** —— 「你面前有没有墙」会随转身失效，
    所以它**不进** `as_state_map()`（frame_dependent）。本模块产出的是
    **世界坐标**断言（挂在 `world` frame 上），可以当校验基准。
  · 键空间 **== 前提键空间**（RFC 09 规则 4）：模型声明的前提用的就是这些键。

── 两种观察，喂给不同的组 ─────────────────────────────────────────────────
| 组 | 拿到什么 | 用途 |
|---|---|---|
| **A** | `render()` 的**字符画** | 基线：模型自己从原始观察里推理 |
| **B / C** | 状态断言渲染出的**相对自然语言视图** | IntentOS 组：结构化状态 |

两者来自**同一份迷宫状态**，所以差异不是「信息量不同」，
而是「表示方式不同」—— 这正是 `07` §5 要测的东西。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..memory.state import Frame, StateAssertion, WORLD_FRAME

__all__ = [
    "Maze",
    "Room",
    "Corridor",
    "MoveResult",
    "DIRECTIONS",
    "OPEN",
    "BLOCKED",
]

#: 四个方向（固定顺序，保证渲染与断言的顺序稳定）。
DIRECTIONS = ("north", "east", "south", "west")

#: 走廊的两种状态。
OPEN = "open"
BLOCKED = "blocked"

#: 空间域的坐标系 —— **稳定参照系**（相对「世界」固定，不随观察者姿态变化）。
SPACE_FRAME = WORLD_FRAME

@dataclass(frozen=True)
class Room:
    """一个房间 —— 有名字，无坐标。

    **为什么用名字**：坐标数值在渲染里会误导模型（3.3 实测：模型靠语义匹配，
    不靠字面），而房间名既稳定又可读。`07` §9 的隐喻驱动架构风险也在这条线上 ——
    机制里长出坐标就绑死空间域了。
    """

    name: str
    label: str = ""  # 人类可读的名字（渲染时用；默认取 name）

    def display(self) -> str:
        return self.label or self.name

@dataclass(frozen=True)
class Corridor:
    """一条走廊 —— 从某个房间往某个方向出去，通或被堵。"""

    room: str  # 起点房间名
    direction: str  # north / east / south / west
    target: str = ""  # 通向的房间名（被堵时为空）
    blocked: bool = False

    @property
    def key(self) -> str:
        """状态键 —— 前提的 `state_key` 就是它。"""
        return f"maze.corridor.{self.room}.{self.direction}"

    @property
    def value(self) -> str:
        return BLOCKED if self.blocked else OPEN

@dataclass(frozen=True)
class MoveResult:
    """一次移动的结果。"""

    ok: bool
    direction: str
    reason: str = ""
    at_goal: bool = False
    steps: int = 0

class Maze:
    """字符画迷宫沙盒 —— 确定性的环境（**零模型调用**）。

    用法：
        maze = Maze.demo()
        maze.render()                  # 字符画（A 组拿这个）
        maze.state_assertions()        # 权威状态断言（B / C 组推这个）
        maze.move("east")              # 确定性移动
    """

    def __init__(
        self,
        rooms: Mapping[str, Room],
        corridors: Iterable[Corridor],
        *,
        start: str,
        goal: str = "",
        apple: str = "",
    ) -> None:
        self.rooms: dict[str, Room] = dict(rooms)
        self.corridors: dict[tuple[str, str], Corridor] = {
            (c.room, c.direction): c for c in corridors
        }
        self.start = start
        self.goal = goal
        self.apple = apple or goal
        self.position = start
        self.steps = 0
        # 本模块写进 store 的键 —— 下次同步时用来判断哪些要退役。
        self._synced_keys: set[str] = set()

    # ── 构造 ────────────────────────────────────────────────────────────────

    @classmethod
    def demo(cls) -> "Maze":
        """演示用迷宫 —— **固定布局**，可复现。

        ```
        起点 ──south──▶ 中转 ──east──▶ 岔路 ──east──▶ 北廊 ──south──▶ 果房
          └─east→ 墙（此路不通，看起来却像个能走的方向）
        ```

        布局是刻意设计的：
          · **路线要绕**（4 步，不是直走 2 步）—— 步数指标才有区分度；
          · **有一条「看起来像路」的堵路**（起点往东有墙）——
            模型很可能以为「往东能走」，声明 `belief=open` → 与权威状态矛盾
            → 正是暴论 #17 要抓的东西。
        """
        rooms = {
            "start": Room("start", "起点"),
            "mid": Room("mid", "中转"),
            "fork": Room("fork", "岔路"),
            "n2": Room("n2", "北廊"),
            "fruit": Room("fruit", "果房"),
        }
        corridors = [
            # 起点：只有南边能走（东边是墙 —— 模型的幻觉诱饵）
            Corridor("start", "south", target="mid"),
            Corridor("start", "east", blocked=True),
            Corridor("start", "north", blocked=True),
            Corridor("start", "west", blocked=True),
            # 中转
            Corridor("mid", "east", target="fork"),
            Corridor("mid", "north", target="start"),
            Corridor("mid", "south", blocked=True),
            Corridor("mid", "west", blocked=True),
            # 岔路
            Corridor("fork", "east", target="n2"),
            Corridor("fork", "west", target="mid"),
            Corridor("fork", "north", blocked=True),
            Corridor("fork", "south", blocked=True),
            # 北廊
            Corridor("n2", "south", target="fruit"),
            Corridor("n2", "west", target="fork"),
            Corridor("n2", "north", blocked=True),
            Corridor("n2", "east", blocked=True),
            # 果房（终点）
            Corridor("fruit", "north", target="n2"),
            Corridor("fruit", "east", blocked=True),
            Corridor("fruit", "south", blocked=True),
            Corridor("fruit", "west", blocked=True),
        ]
        return cls(rooms, corridors, start="start", goal="fruit", apple="fruit")

    # ── 查询 ────────────────────────────────────────────────────────────────

    def room(self, name: str) -> Room:
        return self.rooms.get(name, Room(name))

    def corridor(self, room: str, direction: str) -> Corridor:
        """某个房间某个方向的走廊 —— 没有就当作被堵（世界坐标下「没有走廊」= 堵）。"""
        return self.corridors.get(
            (room, direction), Corridor(room, direction, blocked=True)
        )

    def exits(self, room: str | None = None) -> list[Corridor]:
        """某个房间的四个方向的走廊（固定顺序）。"""
        r = room or self.position
        return [self.corridor(r, d) for d in DIRECTIONS]

    def at_goal(self) -> bool:
        return bool(self.goal) and self.position == self.goal

    # ── 观察 ────────────────────────────────────────────────────────────────

    def render(self) -> str:
        """**原始观察** —— 字符画（A 组拿这个）。

        刻意保留「要费力解析」的形态：A 组要靠它自己推理，这正是基线的难度所在。
        信息量与 B/C 组的**状态视图对等**（走廊通不通 + 通向哪个房间）——
        否则 A vs B 测的就不是「表示方式」，是「谁看到得多」。
        """
        lines: list[str] = []
        lines.append(f"  你在：{self.room(self.position).display()}")
        lines.append("  ┌" + "─" * 34 + "┐")
        for corridor in self.exits():
            if corridor.blocked or not corridor.target:
                body = f"{corridor.direction:<6} ███  （墙）"
            else:
                body = (
                    f"{corridor.direction:<6} ···  → {self.room(corridor.target).display()}"
                )
            lines.append(f"  │ {body:<30} │")
        lines.append("  └" + "─" * 34 + "┘")
        lines.append(f"  目标：{self.room(self.apple).display()}")
        return "\n".join(lines)

    def state_assertions(self, room: str | None = None) -> list[StateAssertion]:
        """**权威状态断言** —— 喂 `StateStore`（B / C 组推这个）。

        全部挂在 **world 坐标系**（稳定参照系）→ **可以当校验基准**
        （RFC 09 规则 1：自我中心断言不作基准）。

        键里**没有坐标数值**，也没有「你面前」这类会随转身失效的表述。
        """
        r = room or self.position
        out: list[StateAssertion] = []
        for corridor in self.exits(r):
            out.append(
                StateAssertion(
                    key=corridor.key,
                    value=corridor.value,
                    frame=SPACE_FRAME,
                    source="maze",
                    note=f"{self.room(corridor.room).display()} 的{corridor.direction}向走廊",
                )
            )
            # 邻居房间 —— **信息对等**用的：字符画里写着「东 ···· → 岔路」，
            # 状态视图也得给同样的信息，否则 A/B 两组拿到的信息量就不一样了
            # （那测的就不是「表示方式」，是「谁看到得多」）。
            out.append(
                StateAssertion(
                    key=f"maze.neighbor.{corridor.room}.{corridor.direction}",
                    value=corridor.target or "",
                    frame=SPACE_FRAME,
                    source="maze",
                    note=f"{self.room(corridor.room).display()} 的{corridor.direction}向通向哪个房间",
                )
            )
        out.append(
            StateAssertion(
                key="maze.goal.room",
                value=self.apple,
                frame=SPACE_FRAME,
                source="maze",
                note="目标所在的房间",
            )
        )
        out.append(
            StateAssertion(
                key="maze.at.room",
                value=self.position,
                frame=SPACE_FRAME,
                source="maze",
                note="当前所在房间",
            )
        )
        return out

    def sync_state(self, store: Any) -> None:
        """把当前状态写进 `StateStore`（版本自增 + 冲突消解照旧）。

        **只保留当下有效的断言**：换了房间之后，上一个房间的走廊事实
        对「现在该做什么」不再相关 —— 留着会让推给模型的视图掺进过期事实。
        所以这里会把**本模块上次写过的、本次不再有效**的键**退役**掉
        （`StateStore.remove()` —— 版本链保留，历史不丢）。
        """
        assertions = self.state_assertions()
        current = {a.key for a in assertions}
        for key in sorted(self._synced_keys - current):
            store.remove(key)
        for assertion in assertions:
            store.set(
                assertion.key,
                assertion.value,
                frame=assertion.frame,
                source=assertion.source,
                note=assertion.note,
            )
        self._synced_keys = current

    # ── 移动 ────────────────────────────────────────────────────────────────

    def move(self, direction: str) -> MoveResult:
        """确定性移动 —— 不看模型说什么，只看世界。

        撞墙不算「走了一步」（步数不增），否则 A/B/C 的平均步数会被撞墙次数污染。
        """
        corridor = self.corridor(self.position, direction)
        if corridor.blocked or not corridor.target:
            return MoveResult(
                ok=False,
                direction=direction,
                reason=f"{self.room(self.position).display()}的{direction}向被墙堵着",
                at_goal=self.at_goal(),
                steps=self.steps,
            )
        self.position = corridor.target
        self.steps += 1
        return MoveResult(
            ok=True,
            direction=direction,
            reason=f"走到{self.room(self.position).display()}",
            at_goal=self.at_goal(),
            steps=self.steps,
        )

    # ── 自检 ────────────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "rooms": {k: v.display() for k, v in self.rooms.items()},
            "position": self.position,
            "goal": self.goal,
            "apple": self.apple,
            "steps": self.steps,
            "corridors": [
                {
                    "room": c.room,
                    "direction": c.direction,
                    "target": c.target,
                    "blocked": c.blocked,
                }
                for c in sorted(
                    self.corridors.values(), key=lambda c: (c.room, DIRECTIONS.index(c.direction))
                )
            ],
        }
