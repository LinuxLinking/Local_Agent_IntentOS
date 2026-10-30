# Intent OS

> **Languages:** [English](#english) · [中文](#中文) · [日本語](#日本語) · [Français](#français)

---

## English

> A runtime layer that compiles user intent into **verifiable, auditable, rollback-safe** execution chains — sitting on top of an LLM.

Intent OS is **not** an agent, and **not** another agent framework. It is **a runtime layer** that sits *above* an agent: the agent is replaceable scaffolding, this layer is the product. Whatever agent you plug in at L2, the **intent compilation · acceptance verification · permissioned scheduling · audit hash chain · elastic rerouting** above it does not change.

The model decides *how*; this layer ensures *it's correct, traceable, and reversible when it isn't*.

> **Status: prototype / research implementation.** Public API may still change.

### Why this exists

Every agent harness eventually hits the same wall: the model calls tools directly, results live only in chat logs, a single failed step stalls the whole run, and there is no way to prove *what actually happened*. Intent OS is an engineering backstop for that gap — it does not replace the model or the agent, it wraps them with a deterministic, inspectable execution layer.

| A plain agent harness | With Intent OS on top |
|---|---|
| intent is a prompt; unclear what will execute | intent → structured `Plan` DAG; every node carries **acceptance criteria** |
| the model decides whether to do side effects | side effects must pass an **approval gate**, risk-tiered |
| results live in chat logs | receipts enter a **hash-chained audit log** (verifiable, replayable, sealable) |
| one failed step stalls the run | failure triggers **elastic rerouting**, or **suspension** for a human |
| "is the environment usable?" is a guess | probes return tri-state: **true / false / unknown** |

### What this looks like

A run is not "the model typed something into a shell." It is a compiled, verifiable chain. (Fields below are real; values are illustrative.)

**1. Intent → Plan — every node carries acceptance criteria**

```json
{
  "id": "a1b2c3d4",
  "intent": {
    "goal": "generate fizzbuzz and verify it runs",
    "verification": [
      {"kind": "file_exists", "tool": "write_file", "target": "fizz.py", "expected": true},
      {"kind": "exit_code",  "tool": "run_python", "target": "fizz.py", "expected": 0}
    ],
    "side_effects": [
      {"tool": "write_file",  "scope": "sandbox/*", "kind": "write",    "reversibility": "easy"},
      {"tool": "run_python",  "scope": "sandbox/*", "kind": "external", "reversibility": "easy"}
    ]
  },
  "nodes": [
    {"name": "write_target", "tool": "write_file", "args": {"path": "fizz.py"}, "depends_on": []},
    {"name": "run_target",   "tool": "run_python", "args": {"path": "fizz.py"}, "depends_on": ["write_target"]}
  ]
}
```

**2. Every receipt enters a hash-chained audit log**

```jsonl
{"seq": 0, "event": "intent_received",   "data": {...}, "prev_hash": "000…000", "hash": "3f2a…"}
{"seq": 1, "event": "plan_generated",    "data": {...}, "prev_hash": "3f2a…",  "hash": "8c1b…"}
{"seq": 2, "event": "approval_requested","data": {"plan_id": "a1b2c3d4"}, "prev_hash": "8c1b…", "hash": "e7d4…"}
{"seq": 3, "event": "node_started",      "data": {"name": "write_target"}, "prev_hash": "e7d4…", "hash": "12ab…"}
{"seq": 4, "event": "verify_passed",     "data": {"kind": "exit_code"}, "prev_hash": "12ab…", "hash": "9c3e…"}
{"seq": 5, "event": "run_finished",      "data": {"status": "ok"}, "prev_hash": "9c3e…", "hash": "4d77…"}
```

Tamper with any line and every hash after it breaks. `audit verify` checks the chain; `audit seal` freezes it read-only; `audit replay` rebuilds a run's narrative from the events.

**3. Side effects do not execute without an approval gate**

```
plan a1b2c3d4 requests:
  scope: sandbox/*
    write_file  [write]     reversibility=easy
    run_python  [external]  reversibility=easy
  → nothing executes until approved
```

### Quick start

Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/) are required.

```bash
git clone https://github.com/LinuxLinking/Local_Agent_IntentOS.git
cd Local_Agent_IntentOS/intent-os
uv sync

uv run python main_intent.py            # template mode (default · deterministic · zero model cost)
uv run python main_intent.py --llm      # LLM-first compilation (falls back to template on failure)
```

Then express an intent:

```
write a fizzbuzz to fizz.py and run it
fix broken.py
```

> `main.py` is a **reference agent** (a local ReAct coding agent on Ollama) kept as a comparison baseline *without* Intent OS on top — the product entry point is `main_intent.py`.

REPL commands:

| Command | Action |
|---|---|
| `/exit` `/quit` | quit |
| `/audit` | view current audit log |
| `/approve-on` / `/approve-off` | auto-approve / confirm each time |
| `/llm-on` / `/llm-off` | LLM-first / template compilation (default) |
| `/mode` | show current compile & approval mode |
| `/state` / `/set-state <k> <v>` | read / write authoritative state |

Audit CLI:

```bash
uv run python -m intent_os audit show   audit/intent_audit.jsonl   # event stream
uv run python -m intent_os audit replay audit/intent_audit.jsonl   # rebuild a run's narrative
uv run python -m intent_os audit verify audit/intent_audit.jsonl   # verify the hash chain (tamper check)
uv run python -m intent_os audit seal   audit/intent_audit.jsonl   # seal as read-only
```

### As a library

The public API exposes 76 names from the package top level (lazy imports — importing the package does not drag in the LLM compiler):

```python
from intent_os import (
    Intent, parse_intent,          # L4 intent compilation
    Plan, PlanNode, generate_plan, validate_plan,
    AuditTrail, ApprovalGate,      # L1 trust layer
    IntentOSRuntime,               # L4 + L1 + L2 orchestration
)

intent = parse_intent("write a fizzbuzz to fizz.py and run it")
plan = generate_plan(intent)
validate_plan(plan)                # cycles / duplicates / missing deps
```

### Architecture

```
L5 user
  │
L4 intent compilation   natural language → Intent → Plan DAG (each node carries acceptance criteria)
  │
L3 working memory       semantic filesystem · context virtual memory · state assertions (source of truth)
  │
L1 trust layer  ←── approval gate · audit hash chain · premise checks · trace replay
  │
L2 orchestration        ToolExecutor protocol · Inline / Agent impls · MCP bus · checkpoint
  │
L0 model & hardware     (out of this layer's scope — the agent is replaceable scaffolding)
```

### Core mechanisms

- **A Plan must carry acceptance criteria to execute** — nodes without a verification spec are not allowed into the chain.
- **Side effects must pass L1** — file writes, command execution go through the approval gate, risk-tiered.
- **All receipts are auditable** — events enter a hash chain (`verify` checks tampering, `seal` makes it read-only).
- **Elastic rerouting** — on node failure, the failed edge is excluded and the path is recomputed; only when no path exists is the task escalated.
- **Suspend & resume** — a run can be serialized; the resume point is precise to a node and survives across processes.
- **Environment probes are tri-state** — `true / false / unknown`; *can't resolve* ≠ *unreachable*, unknown is never treated as failure.
- **External state does not travel through tool calls** — the state interface and the tool interface are separated in code; the model can't hide state as a side effect.

### Limitations

- **Prototype quality** — public API may still change; no stability guarantee yet.
- **No public test suite** — regression runs in the maintainer's mainline; a public clone can `import` and run the REPL, but the test suite is not published.
- **LLM mode needs a local model** — template mode is deterministic and zero-model; `--llm` mode requires a local Ollama instance.
- **CLI only** — no GUI; a front-end is a separate concern.
- **Single-user, local-first** — no multi-tenancy, no cross-user concurrency.
- **Sandbox-bound** — all file reads/writes and code execution are confined to `sandbox/`; nothing outside is touched.
- **No model training** — by design; interaction traces are never distilled back into the model.

### Repository scope

This repository publishes **code and license only**. Internal design documents and the test suite are not in scope (you can `import` and run the REPL, but there is no test directory).

### License

**MIT** · copyright **LinuxLinking and contributors** (see [LICENSE](LICENSE)).

---

## 中文

> 把「用户表达的意图」编译成**可验证、可审计、可回滚**的执行链 —— 一层坐在 LLM 之上的运行时。

Intent OS **不是**一个 Agent，也**不是**又一个 Agent 框架。它是**一层运行时**，坐在 Agent **之上**：Agent 是可替换的脚手架，这一层才是产品。无论 L2 插哪个 Agent，上层的**意图编译 · 验收验证 · 权限调度 · 审计哈希链 · 弹性重路由**都不变。

模型负责「想怎么干」，这一层负责「干得对不对、能不能查、出了问题能不能退回去」。

> **状态：原型 / 研究性实现。** 公开 API 仍会变。

### 为什么做这个

每个 Agent harness 最终都会撞同一堵墙：模型直接调工具、结果只留在对话记录里、一步失败就整条卡死、无法证明「到底发生了什么」。Intent OS 是为这道缝隙做的工程兜底——它不替代模型或 Agent，而是给它们套上一层确定性、可检查的执行层。

| 普通 Agent harness | 上层加 Intent OS |
|---|---|
| 意图是个 prompt，说不清会执行什么 | 意图 → 结构化 `Plan` DAG，每个节点带**验收标准** |
| 模型自己决定要不要做副作用 | 副作用必须过**审批闸门**，按风险分级 |
| 结果只留在对话记录 | 回执进**哈希链审计流**（可校验、可回放、可封存） |
| 一步失败就整条卡死 | 失败触发**弹性重路由**，或**挂起**等人 |
| 「环境是否可用」靠猜 | 探针三态：**真 / 假 / 未知** |

### 实际长什么样

一次 run 不是「模型在 shell 里敲了点什么」，而是一条编译出来、可验证的链。（下方字段为真实结构，值为示意。）

**1. 意图 → Plan —— 每个节点带验收标准**

```json
{
  "id": "a1b2c3d4",
  "intent": {
    "goal": "生成 fizzbuzz 并验证可运行",
    "verification": [
      {"kind": "file_exists", "tool": "write_file", "target": "fizz.py", "expected": true},
      {"kind": "exit_code",  "tool": "run_python", "target": "fizz.py", "expected": 0}
    ],
    "side_effects": [
      {"tool": "write_file", "scope": "sandbox/*", "kind": "write",    "reversibility": "easy"},
      {"tool": "run_python", "scope": "sandbox/*", "kind": "external", "reversibility": "easy"}
    ]
  },
  "nodes": [
    {"name": "write_target", "tool": "write_file", "args": {"path": "fizz.py"}, "depends_on": []},
    {"name": "run_target",   "tool": "run_python", "args": {"path": "fizz.py"}, "depends_on": ["write_target"]}
  ]
}
```

**2. 每条回执进哈希链审计日志**

```jsonl
{"seq": 0, "event": "intent_received",   "data": {...}, "prev_hash": "000…000", "hash": "3f2a…"}
{"seq": 1, "event": "plan_generated",    "data": {...}, "prev_hash": "3f2a…",  "hash": "8c1b…"}
{"seq": 2, "event": "approval_requested","data": {"plan_id": "a1b2c3d4"}, "prev_hash": "8c1b…", "hash": "e7d4…"}
{"seq": 3, "event": "node_started",      "data": {"name": "write_target"}, "prev_hash": "e7d4…", "hash": "12ab…"}
{"seq": 4, "event": "verify_passed",     "data": {"kind": "exit_code"}, "prev_hash": "12ab…", "hash": "9c3e…"}
{"seq": 5, "event": "run_finished",      "data": {"status": "ok"}, "prev_hash": "9c3e…", "hash": "4d77…"}
```

改动任意一行，其后所有哈希都对不上。`audit verify` 校验链条；`audit seal` 冻结为只读；`audit replay` 从事件重建一次 run 的叙事。

**3. 副作用不过审批闸门不执行**

```
plan a1b2c3d4 requests:
  scope: sandbox/*
    write_file  [write]     reversibility=easy
    run_python  [external]  reversibility=easy
  → 不批准不执行
```

### 快速开始

需要 Python ≥ 3.12 与 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/LinuxLinking/Local_Agent_IntentOS.git
cd Local_Agent_IntentOS/intent-os
uv sync

uv run python main_intent.py            # 模板模式（默认 · 确定性 · 零模型开销）
uv run python main_intent.py --llm      # LLM 优先编译（失败自动回退模板）
```

进去后直接说人话：

```
写个 fizzbuzz 存到 fizz.py 然后跑一下
修 broken.py
```

> `main.py` 是一个**参考 Agent**（本地 Ollama 上的 ReAct 编码 Agent），作为**不挂 Intent OS** 的对照基线保留——产品入口是 `main_intent.py`。

REPL 内命令：

| 命令 | 作用 |
|---|---|
| `/exit` `/quit` | 退出 |
| `/audit` | 查看当前审计日志 |
| `/approve-on` / `/approve-off` | 自动批准 / 逐次确认 |
| `/llm-on` / `/llm-off` | LLM 优先 / 模板编译（默认） |
| `/mode` | 看当前编译模式与审批模式 |
| `/state` / `/set-state <k> <v>` | 读 / 写权威状态 |

审计 CLI：

```bash
uv run python -m intent_os audit show   audit/intent_audit.jsonl   # 事件流水
uv run python -m intent_os audit replay audit/intent_audit.jsonl   # 重建一次 run 的叙事
uv run python -m intent_os audit verify audit/intent_audit.jsonl   # 校验哈希链（查篡改）
uv run python -m intent_os audit seal   audit/intent_audit.jsonl   # 封存为只读
```

### 作为库使用

公开 API 共 76 个名字，从包顶层直接取（惰性导入，不拖 LLM 编译器）：

```python
from intent_os import (
    Intent, parse_intent,          # L4 意图编译
    Plan, PlanNode, generate_plan, validate_plan,
    AuditTrail, ApprovalGate,      # L1 信任层
    IntentOSRuntime,               # L4 + L1 + L2 编排
)

intent = parse_intent("写个 fizzbuzz 存到 fizz.py 然后跑一下")
plan = generate_plan(intent)
validate_plan(plan)                # 环 / 重名 / 缺依赖
```

### 架构

```
L5 用户
  │
L4 意图编译层   自然语言 → Intent → Plan DAG（每个节点带验收标准）
  │
L3 工作内存层   语义文件系统 · 上下文虚拟内存 · 状态断言（权威源）
  │
L1 信任层  ←── 审批闸门 · 审计哈希链 · 前提校验 · 轨迹回放
  │
L2 编排执行层   ToolExecutor 协议 · Inline / Agent 实现 · MCP 总线 · Checkpoint
  │
L0 模型与硬件   （不在本层责任范围 —— Agent 是可替换的脚手架）
```

### 核心机制

- **Plan 必须带验收标准才能执行** —— 没有验收规格的节点不允许进执行链。
- **副作用必须过 L1** —— 写文件、跑命令这类动作走审批闸门，按风险分级。
- **所有回执可审计** —— 事件进哈希链（`verify` 可查篡改，`seal` 可封存为只读）。
- **弹性重路由** —— 节点失败时排除失败边、重算路径；无路可走时才提请升级。
- **挂起与恢复** —— 流程可序列化，恢复点精确到节点，跨进程可续。
- **环境探针三态** —— `true / false / unknown`；**解析不了 ≠ 不可达**，不把未知当失败。
- **外置状态不走工具调用** —— 状态接口与工具接口在代码里分开，模型不能把状态当副作用藏进去。

### 目前的限制

- **原型阶段** —— 公开 API 可能变动，暂无稳定性承诺。
- **不公开测试套件** —— 回归在维护者主线跑；公开 clone 能 import、能跑 REPL，但无测试目录。
- **LLM 模式需要本地模型** —— 模板模式确定性、零模型开销；`--llm` 模式需要本地 Ollama。
- **仅 CLI** —— 无 GUI，前端是另一个议题。
- **单用户、本地优先** —— 无多租户、无跨用户并发。
- **沙箱限定** —— 所有文件读写与代码执行都在 `sandbox/` 内，目录之外一律拒绝。
- **不做模型训练** —— 设计选择；交互轨迹不喂回模型。

### 仓库范围

本仓库只发布**代码与许可证**：内部设计文档与测试套件不在公开范围内（clone 下来可以 import、可以跑 REPL，但没有测试目录）。

### 许可证

**MIT** · 版权归 **LinuxLinking and contributors**（见 [LICENSE](LICENSE)）。

---

## 日本語

> ユーザーの意図を**検証可能・監査可能・ロールバック可能**な実行チェーンにコンパイルする —— LLM の上に載るランタイム層。

Intent OS は一つの Agent ではなく、もう一つの Agent フレームワークでもありません。それは**ランタイム層**であり、Agent の**上**に位置します：Agent は取り外し可能な足場、この層がプロダクトです。L2 にどの Agent を差しても、その上の**意図コンパイル・検収検証・権限スケジューリング・監査ハッシュチェーン・弾力的再ルーティング**は変わりません。

モデルは「どうやるか」を決め、この層は「正しいか・追跡できるか・問題時に戻せるか」を担保します。

> **状態：プロトタイプ / 研究実装。** 公開 API はまだ変わる可能性があります。

### なぜこれを作るのか

どの Agent harness も最終的に同じ壁にぶつかります：モデルが直接ツールを呼ぶ、結果はチャットログにしか残らない、一歩の失敗で全体が止まる、「実際に何が起きたか」を証明できない。Intent OS はこの隙間を埋える工学的な安全網です——モデルも Agent も置き換えず、決定的で検査可能な実行層で包みます。

| 一般的な Agent harness | その上に Intent OS |
|---|---|
| 意図はプロンプト、何が実行されるか不明 | 意図 → 構造化 `Plan` DAG、各ノードに**検収基準** |
| モデルが副作用の実行を決める | 副作用は**承認ゲート**を通る、リスク階層別 |
| 結果はチャットログのみ | レシートは**ハッシュチェーン監査**へ（検証・再生・封印可能） |
| 一歩の失敗で全体停止 | 失敗で**弾力的再ルーティング**、または**サスペンド**で人へ |
| 「環境は使えるか」は推測 | プローブは三状態：**true / false / unknown** |

### 実際の姿

run とは「モデルがシェルに何か打った」ではなく、コンパイルされた検証可能なチェーンです。（下記フィールドは実物、値は示意です。）

**1. 意図 → Plan —— 各ノードに検収基準**

```json
{
  "id": "a1b2c3d4",
  "intent": {
    "goal": "fizzbuzz を生成し実行を検証",
    "verification": [
      {"kind": "file_exists", "tool": "write_file", "target": "fizz.py", "expected": true},
      {"kind": "exit_code",  "tool": "run_python", "target": "fizz.py", "expected": 0}
    ],
    "side_effects": [
      {"tool": "write_file", "scope": "sandbox/*", "kind": "write",    "reversibility": "easy"},
      {"tool": "run_python", "scope": "sandbox/*", "kind": "external", "reversibility": "easy"}
    ]
  },
  "nodes": [
    {"name": "write_target", "tool": "write_file", "args": {"path": "fizz.py"}, "depends_on": []},
    {"name": "run_target",   "tool": "run_python", "args": {"path": "fizz.py"}, "depends_on": ["write_target"]}
  ]
}
```

**2. 全レシートがハッシュチェーン監査ログへ**

```jsonl
{"seq": 0, "event": "intent_received",   "data": {...}, "prev_hash": "000…000", "hash": "3f2a…"}
{"seq": 1, "event": "plan_generated",    "data": {...}, "prev_hash": "3f2a…",  "hash": "8c1b…"}
{"seq": 2, "event": "approval_requested","data": {"plan_id": "a1b2c3d4"}, "prev_hash": "8c1b…", "hash": "e7d4…"}
{"seq": 3, "event": "node_started",      "data": {"name": "write_target"}, "prev_hash": "e7d4…", "hash": "12ab…"}
{"seq": 4, "event": "verify_passed",     "data": {"kind": "exit_code"}, "prev_hash": "12ab…", "hash": "9c3e…"}
{"seq": 5, "event": "run_finished",      "data": {"status": "ok"}, "prev_hash": "9c3e…", "hash": "4d77…"}
```

どの行も改ざんすれば以降の全ハッシュが壊れます。`audit verify` がチェーンを検証、`audit seal` は読み取り専用で封印、`audit replay` はイベントから run の叙事を再構築します。

**3. 副作用は承認ゲートを通らないと実行されない**

```
plan a1b2c3d4 requests:
  scope: sandbox/*
    write_file  [write]     reversibility=easy
    run_python  [external]  reversibility=easy
  → 承認されないと実行しない
```

### クイックスタート

Python ≥ 3.12 と [uv](https://docs.astral.sh/uv/) が必要です。

```bash
git clone https://github.com/LinuxLinking/Local_Agent_IntentOS.git
cd Local_Agent_IntentOS/intent-os
uv sync

uv run python main_intent.py            # テンプレートモード（デフォルト・決定的・モデル不要）
uv run python main_intent.py --llm      # LLM 優先コンパイル（失敗時テンプレートにフォールバック）
```

あとは自然言語で指示します：

```
write a fizzbuzz to fizz.py and run it
fix broken.py
```

> `main.py` は**参照用 Agent**（ローカル Ollama の ReAct コーディング Agent）で、Intent OS を**載せない**比較ベースラインとして残したものです——プロダクト入口は `main_intent.py` です。

REPL コマンド：

| コマンド | 動作 |
|---|---|
| `/exit` `/quit` | 終了 |
| `/audit` | 現在の監査ログを表示 |
| `/approve-on` / `/approve-off` | 自動承認 / 毎回確認 |
| `/llm-on` / `/llm-off` | LLM 優先 / テンプレートコンパイル（デフォルト） |
| `/mode` | 現在のコンパイル・承認モードを表示 |
| `/state` / `/set-state <k> <v>` | 権威状態の読み取り / 書き込み |

監査 CLI：

```bash
uv run python -m intent_os audit show   audit/intent_audit.jsonl   # イベントストリーム
uv run python -m intent_os audit replay audit/intent_audit.jsonl   # run の再構築
uv run python -m intent_os audit verify audit/intent_audit.jsonl   # ハッシュチェーン検証（改ざん検出）
uv run python -m intent_os audit seal   audit/intent_audit.jsonl   # 読み取り専用で封印
```

### ライブラリとして

公開 API は 76 名前をパッケージトップレベルから提供します（遅延インポート、LLM コンパイラを引き込みません）：

```python
from intent_os import (
    Intent, parse_intent,          # L4 意図コンパイル
    Plan, PlanNode, generate_plan, validate_plan,
    AuditTrail, ApprovalGate,      # L1 信頼層
    IntentOSRuntime,               # L4 + L1 + L2 オーケストレーション
)
```

### アーキテクチャ

```
L5 ユーザー
  │
L4 意図コンパイル層   自然言語 → Intent → Plan DAG（各ノードに検収基準）
  │
L3 ワーキングメモリ層   セマンティックファイルシステム・コンテキスト仮想メモリ・状態表明（権威源）
  │
L1 信頼層  ←── 承認ゲート・監査ハッシュチェーン・前提チェック・トレース再生
  │
L2 オーケストレーション層   ToolExecutor プロトコル・Inline / Agent 実装・MCP バス・チェックポイント
  │
L0 モデルとハードウェア   （この層の責務範囲外 —— Agent は取り外し可能な足場）
```

### 中核機構

- **Plan は検収基準を持たないと実行できない** —— 検収仕様のないノードは実行チェーンに入りません。
- **副作用は L1 を通る** —— ファイル書き込み、コマンド実行は承認ゲート経由、リスク階層別。
- **すべてのレシートは監査可能** —— イベントはハッシュチェーンに入る（`verify` で改ざん検出、`seal` で読み取り専用）。
- **弾力的再ルーティング** —— ノード失敗時に失敗エッジを除外し経路を再計算；経路が無い時のみエスカレーション。
- **サスペンドとレジューム** —— run は直列化可能、レジューム地点はノード単位、プロセス間で継続可能。
- **環境プローブは三状態** —— `true / false / unknown`；**解決不能 ≠ 到達不能**、未知を失敗扱いしない。
- **外部状態はツール呼び出しを経由しない** —— 状態インターフェースとツールインターフェースはコード内で分離。

### 現在の制限

- **プロトタイプ段階** —— 公開 API は変更される可能性があり、安定性の保証はありません。
- **テストスイート非公開** —— 回帰は保守者の主线で実行；公開 clone は `import`・REPL 実行可能ですがテストディレクトリはありません。
- **LLM モードはローカルモデルが必要** —— テンプレートモードは決定的・モデル不要；`--llm` モードはローカル Ollama が必要です。
- **CLI のみ** —— GUI なし、フロントエンドは別課題。
- **単一ユーザー・ローカル優先** —— マルチテナンシーなし、ユーザー間並行性なし。
- **サンドボックス制約** —— すべてのファイル読み書きとコード実行は `sandbox/` 内に限定、外部には触れない。
- **モデル訓練なし** —— 設計選択；相互作用トレースはモデルに還元しません。

### リポジトリ範囲

本リポジトリは**コードとライセンスのみ**を公開します。内部設計文書とテストスイートは範囲外です。

### ライセンス

**MIT** · 著作権 **LinuxLinking and contributors**（[LICENSE](LICENSE) 参照）。

---

## Français

> Une couche d'exécution qui compile l'intention de l'utilisateur en chaînes d'exécution **vérifiables, auditées, réversibles** — au-dessus d'un LLM.

Intent OS n'est pas un agent et n'est pas un énième framework d'agents. C'est **une couche d'exécution** qui se situe *au-dessus* d'un agent : l'agent est un échafaudage amovible, cette couche est le produit. Quel que soit l'agent branché en L2, la **compilation d'intention · vérification d'acceptation · ordonnancement avec permissions · chaîne de hachage d'audit · re-routage élastique** au-dessus ne change pas.

Le modèle décide *comment* ; cette couche garantit *que c'est correct, traçable, et réversible quand ça ne l'est pas*.

> **Statut : prototype / implémentation de recherche.** L'API publique peut encore changer.

### Pourquoi cela existe

Chaque harness d'agents finit par heurter le même mur : le modèle appelle les outils directement, les résultats ne vivent que dans les journaux de conversation, une seule étape échouée bloque tout, et il est impossible de prouver *ce qui s'est réellement passé*. Intent OS est un garde-fou d'ingénierie pour ce manque — elle ne remplace ni le modèle ni l'agent, elle les enveloppe d'une couche d'exécution déterministe et inspectable.

| Un harness d'agents classique | Avec Intent OS au-dessus |
|---|---|
| l'intention est un prompt ; unclear ce qui s'exécutera | intention → `Plan` DAG structuré ; chaque nœud porte des **critères d'acceptation** |
| le modèle décide des effets de bord | les effets de bord passent une **porte d'approbation**, par niveau de risque |
| les résultats restent dans les journaux | les reçus entrent dans une **chaîne de hachage d'audit** (vérifiable, rejouable, scellable) |
| un échec bloque toute l'exécution | l'échec déclenche un **re-routage élastique**, ou une **suspension** pour un humain |
| « l'environnement est-il utilisable ? » relève du devin | les sondes sont tri-états : **true / false / unknown** |

### À quoi cela ressemble

Une exécution n'est pas « le modèle a tapé quelque chose dans un shell ». C'est une chaîne compilée et vérifiable. (Les champs ci-dessous sont réels ; les valeurs sont illustratives.)

**1. Intention → Plan — chaque nœud porte des critères d'acceptation**

```json
{
  "id": "a1b2c3d4",
  "intent": {
    "goal": "générer fizzbuzz et vérifier l'exécution",
    "verification": [
      {"kind": "file_exists", "tool": "write_file", "target": "fizz.py", "expected": true},
      {"kind": "exit_code",  "tool": "run_python", "target": "fizz.py", "expected": 0}
    ],
    "side_effects": [
      {"tool": "write_file", "scope": "sandbox/*", "kind": "write",    "reversibility": "easy"},
      {"tool": "run_python", "scope": "sandbox/*", "kind": "external", "reversibility": "easy"}
    ]
  },
  "nodes": [
    {"name": "write_target", "tool": "write_file", "args": {"path": "fizz.py"}, "depends_on": []},
    {"name": "run_target",   "tool": "run_python", "args": {"path": "fizz.py"}, "depends_on": ["write_target"]}
  ]
}
```

**2. Chaque reçu entre dans une chaîne de hachage d'audit**

```jsonl
{"seq": 0, "event": "intent_received",   "data": {...}, "prev_hash": "000…000", "hash": "3f2a…"}
{"seq": 1, "event": "plan_generated",    "data": {...}, "prev_hash": "3f2a…",  "hash": "8c1b…"}
{"seq": 2, "event": "approval_requested","data": {"plan_id": "a1b2c3d4"}, "prev_hash": "8c1b…", "hash": "e7d4…"}
{"seq": 3, "event": "node_started",      "data": {"name": "write_target"}, "prev_hash": "e7d4…", "hash": "12ab…"}
{"seq": 4, "event": "verify_passed",     "data": {"kind": "exit_code"}, "prev_hash": "12ab…", "hash": "9c3e…"}
{"seq": 5, "event": "run_finished",      "data": {"status": "ok"}, "prev_hash": "9c3e…", "hash": "4d77…"}
```

Altérez une ligne, et tous les hachages suivants se brisent. `audit verify` vérifie la chaîne ; `audit seal` la gèle en lecture seule ; `audit replay` reconstruit le récit d'une exécution à partir des événements.

**3. Les effets de bord ne s'exécutent pas sans une porte d'approbation**

```
plan a1b2c3d4 requests:
  scope: sandbox/*
    write_file  [write]     reversibility=easy
    run_python  [external]  reversibility=easy
  → rien ne s'exécute sans approbation
```

### Démarrage rapide

Python ≥ 3.12 et [uv](https://docs.astral.sh/uv/) requis.

```bash
git clone https://github.com/LinuxLinking/Local_Agent_IntentOS.git
cd Local_Agent_IntentOS/intent-os
uv sync

uv run python main_intent.py            # mode modèle (par défaut · déterministe · sans modèle)
uv run python main_intent.py --llm      # compilation LLM d'abord (retombe sur le modèle en cas d'échec)
```

Puis exprimez votre intention :

```
write a fizzbuzz to fizz.py and run it
fix broken.py
```

> `main.py` est un **agent de référence** (un agent ReAct de codage sur Ollama local) conservé comme baseline de comparaison *sans* Intent OS au-dessus — le point d'entrée du produit est `main_intent.py`.

Commandes REPL :

| Commande | Action |
|---|---|
| `/exit` `/quit` | quitter |
| `/audit` | voir le journal d'audit courant |
| `/approve-on` / `/approve-off` | approbation automatique / confirmation à chaque fois |
| `/llm-on` / `/llm-off` | LLM d'abord / compilation par modèle (par défaut) |
| `/mode` | afficher les modes de compilation et d'approbation |
| `/state` / `/set-state <k> <v>` | lire / écrire l'état autoritaire |

CLI d'audit :

```bash
uv run python -m intent_os audit show   audit/intent_audit.jsonl   # flux d'événements
uv run python -m intent_os audit replay audit/intent_audit.jsonl   # reconstruire le récit d'une exécution
uv run python -m intent_os audit verify audit/intent_audit.jsonl   # vérifier la chaîne de hachage (détection d'altération)
uv run python -m intent_os audit seal   audit/intent_audit.jsonl   # sceller en lecture seule
```

### Comme bibliothèque

L'API publique expose 76 noms au niveau supérieur du paquet (imports paresseux — importer le paquet ne charge pas le compilateur LLM) :

```python
from intent_os import (
    Intent, parse_intent,          # L4 compilation d'intention
    Plan, PlanNode, generate_plan, validate_plan,
    AuditTrail, ApprovalGate,      # L1 couche de confiance
    IntentOSRuntime,               # orchestration L4 + L1 + L2
)
```

### Architecture

```
L5 utilisateur
  │
L4 compilation d'intention   langage naturel → Intent → Plan DAG (chaque nœud porte des critères d'acceptation)
  │
L3 mémoire de travail        système de fichiers sémantique · mémoire virtuelle de contexte · assertions d'état (source de vérité)
  │
L1 couche de confiance ←── porte d'approbation · chaîne de hachage d'audit · vérification de prémisses · relecture de trace
  │
L2 orchestration             protocole ToolExecutor · impl. Inline / Agent · bus MCP · point de contrôle
  │
L0 modèle et matériel        (hors du périmètre de cette couche — l'agent est un échafaudage amovible)
```

### Mécanismes centraux

- **Un Plan doit porter des critères d'acceptation pour s'exécuter** — un nœud sans spécification de vérification n'entre pas dans la chaîne.
- **Les effets de bord passent par L1** — écritures de fichiers, exécution de commandes transitent par la porte d'approbation, par niveau de risque.
- **Tous les reçus sont audités** — les événements entrent dans une chaîne de hachage (`verify` détecte les altérations, `seal` rend en lecture seule).
- **Re-routage élastique** — en cas d'échec de nœud, l'arête faillie est exclue et le chemin recalculé ; ce n'est qu'aucun chemin n'existe que la tâche est escaladée.
- **Suspendre et reprendre** — une exécution peut être sérialisée ; le point de reprise est précis au nœud et survit entre processus.
- **Les sondes d'environnement sont tri-états** — `true / false / unknown` ; *impossible à résoudre* ≠ *inaccessible*, l'inconnu n'est jamais traité comme un échec.
- **L'état externe ne transite pas par les appels d'outils** — l'interface d'état et l'interface d'outils sont séparées dans le code.

### Limitations actuelles

- **Qualité prototype** — l'API publique peut encore changer ; aucune garantie de stabilité pour l'instant.
- **Pas de suite de tests publique** — les régressions tournent sur la ligne principale du mainteneur ; un clone public peut `importer` et lancer le REPL, mais la suite de tests n'est pas publiée.
- **Le mode LLM nécessite un modèle local** — le mode modèle est déterministe et sans modèle ; le mode `--llm` nécessite une instance Ollama locale.
- **CLI uniquement** — pas de GUI ; un front-end est un sujet distinct.
- **Mono-utilisateur, local d'abord** — pas de multi-tenancy, pas de concurrence entre utilisateurs.
- **Confiné au bac à sable** — toutes les lectures/écritures et l'exécution de code sont confinées à `sandbox/` ; rien en dehors n'est touché.
- **Pas d'entraînement de modèle** — par conception ; les traces d'interaction ne sont jamais distillées dans le modèle.

### Périmètre du dépôt

Ce dépôt publie **uniquement le code et la licence**. Les documents de conception internes et la suite de tests ne sont pas dans le périmètre (vous pouvez `importer` et lancer le REPL, mais il n'y a pas de répertoire de tests).

### Licence

**MIT** · droits d'auteur **LinuxLinking and contributors** (voir [LICENSE](LICENSE)).
