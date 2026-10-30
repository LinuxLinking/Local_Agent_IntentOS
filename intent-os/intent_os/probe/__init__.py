"""intent_os.probe —— 环境探针（RFC 15 · Step 5.1）。

**做什么**：把「当前环境是什么样」变成**结构化的、可判定的数据**，
供 Step 5.2 的路由器和模型使用（RFC 15 §3.1）。

**三条设计约束**（RFC 15 §3.1）：

1. **零模型调用** —— 探针是确定性代码，不是「需要模型猜」的东西。
2. **探针结果是数据，不是提示词** —— 模型通过 `check_network()` 这类工具**主动取**。
3. **探针失败必须如实记**（unknown 而不是 false）—— 探测不到 ≠ 探测到没有。

──────── D2 边界（RFC 15 §3.1 的警告 · Q23）────────────────────────────

- **探针** = 环境能力（有没有 gh / 能不能上网 / 有没有 SSH key）→
  白皮书明确要求它**可以被做成工具**（`check_network()`）。
- **状态断言**（StateStore）= 外置状态（世界 / 任务的坐标）→
  按 D2 **绝不走工具**。

所以本包**不 import** `intent_os.memory.state`（状态断言模块），也不 import 任何模型客户端 ——
这条边界由 `tests/test_probe_environment.py` 的**源码扫描断言**守住。

模块分工：`environment.py` 数据形状 · `probes.py` 探针实现 · `tools.py` 探针工具。
"""
from .environment import (
    ENVIRONMENT_FILENAME,
    SECTIONS,
    TOOL_NAMES,
    EnvironmentSnapshot,
    ProbeResult,
    utc_now_iso,
)
from .probes import (
    DEFAULT_PROBES,
    DEFAULT_HTTP_HOST,
    DEFAULT_HTTP_PORT,
    DEFAULT_RATE_URL,
    ENV_API_RATE,
    ENV_API_URL,
    ENV_HTTP_HOST,
    ENV_HTTP_PORT,
    ENV_SANDBOX,
    GH_TOKEN_ENV,
    NETWORK_TIMEOUT,
    SSH_KEY_NAMES,
    SUBPROCESS_TIMEOUT,
    TOOL_VERSION_FLAGS,
    ProbeFn,
    default_probes,
    default_sandbox_dir,
    probe_api_rate_remaining,
    probe_dns_ok,
    probe_environment,
    probe_gh_token_present,
    probe_http_reachable,
    probe_proxy,
    probe_sandbox_writable,
    probe_ssh_key_path,
    probe_ssh_key_present,
    probe_tool_installed,
    probe_tool_version,
)
from .tools import (
    PROBE_TOOLS,
    ProbeTool,
    call_probe_tool,
    check_credentials,
    check_network,
    check_tools,
    get_probe_tool,
    probe_tool_names,
    probe_tool_side_effect,
)

__all__ = [
    # 数据形状
    "EnvironmentSnapshot",
    "ProbeResult",
    "ENVIRONMENT_FILENAME",
    "SECTIONS",
    "TOOL_NAMES",
    "utc_now_iso",
    # 探针
    "probe_environment",
    "default_probes",
    "DEFAULT_PROBES",
    "ProbeFn",
    "probe_http_reachable",
    "probe_dns_ok",
    "probe_proxy",
    "probe_tool_installed",
    "probe_tool_version",
    "probe_ssh_key_present",
    "probe_ssh_key_path",
    "probe_gh_token_present",
    "probe_api_rate_remaining",
    "probe_sandbox_writable",
    "default_sandbox_dir",
    "DEFAULT_HTTP_HOST",
    "DEFAULT_HTTP_PORT",
    "DEFAULT_RATE_URL",
    "NETWORK_TIMEOUT",
    "SUBPROCESS_TIMEOUT",
    "TOOL_VERSION_FLAGS",
    "SSH_KEY_NAMES",
    "GH_TOKEN_ENV",
    "ENV_HTTP_HOST",
    "ENV_HTTP_PORT",
    "ENV_API_URL",
    "ENV_API_RATE",
    "ENV_SANDBOX",
    # 探针工具
    "check_network",
    "check_tools",
    "check_credentials",
    "PROBE_TOOLS",
    "ProbeTool",
    "get_probe_tool",
    "probe_tool_names",
    "probe_tool_side_effect",
    "call_probe_tool",
]
