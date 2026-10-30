"""L4 · 前提声明（Premise）—— 模型在副作用前必须说出的「我认为此刻是这样」。

设计依据：
- design-foundation/07-r2-conclusions.md §4 暴论 #17
  「把『我不知道』变成可校验的显式前提」——声明 → 校验 → 纠正 三步闭环
- design-foundation/04-open-questions.md Q19（前提声明的最小可判定形式）
- design-foundation/07-r2-conclusions.md §1 D2（外置状态不走传统工具调用）

── 为什么要有「前提」这一层 ───────────────────────────────────────────────
现有的 `Intent.verification` 回答的是「做对了没」（事后证据）。
但纯文本 LLM 是「感官闭塞」的：它对世界的判断全靠语言里的**共现启发式**，
于是「我认为目标在左前方」这类前提从来没有被要求说出口，也就永远没被检查过。

Premise 补的是**事前**：
  verification —— 做完了吗？  （条件 + 证据）
  premise     —— 动手前的前提对吗？（可被权威状态机械判定）

── 形状：刻意扁平（P0 教训的延续）──────────────────────────────────────────
Phase 3.1 实测教训：qwen3.5:0.8b **填不动嵌套 schema**
（`verification` 数组被模型填成枚举字符串、`json_schema` 路径下复读 13 次）。
所以 premise 的**线上格式是扁平的纯字符串**，模型一次声明一条：

    target         要动的实体（"fizz.py"）
    target_kind    实体类型（"file"）
    state_key      断言的环境状态键（"wall.at.3m"）
    state_value    断言的值（"present"）
    relation       可选关系（"left_of"）
    relation_object  关系对象（"wall"）

「机械可判定」的关键：**`state_key` 是权威状态里的一个键**。
校验不是让 LLM 去判断，而是拿这个键去查状态表比值。

── 为什么要区分「冲突」和「未知」──────────────────────────────────────────
- **冲突（conflict）**：状态里明确写着另一个值 → 前提**错了** → 拦截
- **未知（unknown）**：状态里没这条 → 前提**无法被验证** → 不拦截，但必须
  告诉模型「你不知道这个」（暴论 #15 的 `unknown()` syscall）

把两者混为一谈是危险的：把「不知道」当成「错」会让系统过度拦截；
把「错」当成「不知道」则等于没有校验。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

@dataclass(frozen=True)
class Premise:
    """一条前提声明 —— 一次「我认为此刻是这样」。

    `state_key` 是与权威状态对接的唯一接口：
    校验 = 拿这个键去查状态表比值。没有键就没有可判定性。
    """

    target: str
    target_kind: str
    state_key: str
    state_value: str
    relation: str = ""
    relation_object: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "target_kind": self.target_kind,
            "state_key": self.state_key,
            "state_value": self.state_value,
            "relation": self.relation,
            "relation_object": self.relation_object,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Premise":
        return cls(
            target=str(d.get("target", "")),
            target_kind=str(d.get("target_kind", "")),
            state_key=str(d.get("state_key", "")),
            state_value=str(d.get("state_value", "")),
            relation=str(d.get("relation", "")),
            relation_object=str(d.get("relation_object", "")),
        )

    def is_checkable(self) -> bool:
        """有没有可机械判定的内容。

        没有 `state_key` 的前提**不可判定** —— 校验它等于没校验。
        """
        return bool(self.state_key.strip())

class PremiseSchema(BaseModel):
    """premise 的线上格式（给模型填的，刻意扁平）。

    字段全部是字符串 / 受限枚举，且**显式必填**（Phase 3.1 教训：
    带 default 的字段会被小模型直接省略）。
    """

    target: str = Field(description="要动的实体，如 fizz.py")
    target_kind: str = Field(description="实体类型，如 file / dir")
    state_key: str = Field(
        description="断言的环境状态键，如 wall.at.3m / sandbox.clean"
    )
    state_value: str = Field(description="断言该键的值，如 present / absent")
    relation: str = Field(description="可选关系，如 left_of；没有就填 none")
    relation_object: str = Field(
        description="关系对象，如 wall；relation 为 none 时填 none"
    )

def schema_to_premise(schema: PremiseSchema) -> Premise:
    """线上格式 → 内部形式。

    `relation` 的 "none" 是**显式的「没有关系」**，不是「不知道」——
    模型必须把这一格填满，否则我们会把它当成漏填。
    """
    relation = schema.relation.strip()
    relation_object = schema.relation_object.strip()
    if relation.lower() in ("", "none", "null", "-"):
        relation = ""
        relation_object = ""
    return Premise(
        target=schema.target.strip(),
        target_kind=schema.target_kind.strip(),
        state_key=schema.state_key.strip(),
        state_value=schema.state_value.strip(),
        relation=relation,
        relation_object=relation_object,
    )

def premise_to_schema(premise: Premise) -> PremiseSchema:
    """内部形式 → 线上格式（往返一致，便于模板路径也走同一条管线）。"""
    return PremiseSchema(
        target=premise.target,
        target_kind=premise.target_kind,
        state_key=premise.state_key,
        state_value=premise.state_value,
        relation=premise.relation or "none",
        relation_object=premise.relation_object or "none",
    )
