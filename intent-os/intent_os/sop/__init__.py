"""「大学生工程师」记忆（RFC 15 §3.4-D · 白皮书 §2.4 · Phase 5.5 · 吸收原 Phase 4.2）。

白皮书原话：**智力有限，但靠经验与图纸培养能力**。三个子模块各管一件事：

| 模块 | 管什么 | 不是什么 |
|---|---|---|
| `template.py` · **SOP 图纸** | 步骤骨架 + 若干「空」（模型只填空，不改骨架） | 不是记忆检索 |
| `experience.py` · **经验错题本** | 哪条路在**这个环境**里走过不通（带环境指纹） | 不是步骤模板 |
| `constraints.py` · **约束记忆** | 代码层的安全红线（转账限额 / 危险命令） | 不是提示词建议 |

──────── 三条红线 ──────────────────────────────────────────────────────────

1. **不训练模型**（D1）：图纸是数据、经验是台账，都不喂回模型做微调。
2. **D2 边界**：经验/约束都是**台账**，不是状态断言 —— 不 import `memory/state.py`。
3. **不静默降级**：模板不合法 / 槽没填 / 经验文件坏了 → 如实报错。

用法：

```python
sops = SopRegistry([demo_write_and_run_sop()])
plan = sops.fill("write-and-run", {"path": "sandbox/demo.py", "content": "print(1)"})
book = ExperienceBook(Path("experience.jsonl"))
banned = book.banned_tools("capability:git_clone", env)   # 规划前检索（同环境指纹才复用）
limits = ConstraintMemory.default_redlines()
runtime = IntentOSRuntime(..., constraints=limits, experience=book)
```
"""
from __future__ import annotations

from .constraints import (
    ConstraintError,
    ConstraintMemory,
    ConstraintRule,
    Violation,
)
from .experience import (
    EnvFingerprint,
    Experience,
    ExperienceBook,
    ExperienceError,
)
from .template import (
    SlotSpec,
    SopError,
    SopNode,
    SopRegistry,
    SopTemplate,
    demo_write_and_run_sop,
)

__all__ = [
    # SOP 图纸
    "SopTemplate",
    "SopNode",
    "SlotSpec",
    "SopRegistry",
    "SopError",
    "demo_write_and_run_sop",
    # 经验错题本
    "ExperienceBook",
    "Experience",
    "EnvFingerprint",
    "ExperienceError",
    # 约束记忆
    "ConstraintMemory",
    "ConstraintRule",
    "ConstraintError",
    "Violation",
]
