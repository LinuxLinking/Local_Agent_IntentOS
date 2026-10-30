"""环境探针 · 确定性实现（RFC 15 §3.1）。

**零模型调用、零第三方依赖** —— 只用标准库
（`shutil` / `os` / `pathlib` / `socket` / `subprocess` / `urllib`）。

三条规则：

1. **探测不到 → unknown（value=None），绝不写 False**（红线：不静默降级）。
2. **单个探针抛异常不许拖垮整份快照** —— 记 unknown + 说清原因。
3. **每个探针是独立的小函数**；注册表 `default_probes()` 可以整体替换，
   测试因此能注入假探针，不依赖真实网络。

D2 边界见 `environment.py` 的模块说明：**探针 ≠ 状态断言** ——
本模块不 import 状态断言模块，也不 import 任何模型客户端。
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from .environment import EnvironmentSnapshot, ProbeResult, utc_now_iso

#: 探针函数签名：零参数，返回一条 ProbeResult（key 由注册表说了算）
ProbeFn = Callable[[], ProbeResult]

#: 默认探测目标（白皮书案例是 GitHub）
DEFAULT_HTTP_HOST = "api.github.com"
DEFAULT_HTTP_PORT = 443
#: 默认的 API 速率探测地址
DEFAULT_RATE_URL = "https://api.github.com/rate_limit"

#: 超时（秒）—— 探针跑在主链路上，必须**快**
NETWORK_TIMEOUT = 1.5
SUBPROCESS_TIMEOUT = 3.0

#: 每个工具查版本用的参数（ssh 的版本信息走 stderr）
TOOL_VERSION_FLAGS: Mapping[str, str] = {
    "gh": "--version",
    "ssh": "-V",
    "git": "--version",
}

#: SSH 私钥的常见文件名
SSH_KEY_NAMES: tuple[str, ...] = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")

#: GitHub token 的环境变量名
GH_TOKEN_ENV: tuple[str, ...] = ("GH_TOKEN", "GITHUB_TOKEN")

#: 可注入的覆盖（测试用；也让探针能跟着环境走）
ENV_HTTP_HOST = "INTENT_OS_PROBE_HTTP_HOST"
ENV_HTTP_PORT = "INTENT_OS_PROBE_HTTP_PORT"
ENV_API_URL = "INTENT_OS_PROBE_API_URL"
ENV_API_RATE = "INTENT_OS_PROBE_API_RATE_REMAINING"
ENV_SANDBOX = "INTENT_OS_PROBE_SANDBOX"

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")

def _result(key: str, value: Any, detail: str = "") -> ProbeResult:
    return ProbeResult(key=key, value=value, detail=detail)

def _http_target() -> tuple[str, int]:
    """探测目标主机 / 端口（可用环境变量覆盖 —— 测试不打外网）。"""
    host = os.environ.get(ENV_HTTP_HOST) or DEFAULT_HTTP_HOST
    raw_port = os.environ.get(ENV_HTTP_PORT) or str(DEFAULT_HTTP_PORT)
    try:
        port = int(raw_port)
    except ValueError:
        port = DEFAULT_HTTP_PORT
    return host, port

# ── 网络 ──────────────────────────────────────────────────────────────────────

def probe_http_reachable() -> ProbeResult:
    """目标主机在 TCP 层通不通（HTTP 可达性的近似）。

    - 连上 → True
    - 拒绝 / 超时 → False（**探测到没有**）
    - DNS 解析不了 / 其它说不清的异常 → unknown（**探测不到 ≠ 没有**）
    """
    key = "network.http_reachable"
    host, port = _http_target()
    try:
        with socket.create_connection((host, port), timeout=NETWORK_TIMEOUT):
            return _result(key, True, f"TCP connect {host}:{port} 成功")
    except socket.gaierror as e:
        return _result(key, None, f"DNS 解析 {host} 失败 → 无法判定可达性：{e}")
    except (socket.timeout, TimeoutError) as e:
        return _result(key, False, f"连接 {host}:{port} 超时（{NETWORK_TIMEOUT}s）：{e}")
    except ConnectionRefusedError as e:
        return _result(key, False, f"连接 {host}:{port} 被拒绝：{e}")
    except OSError as e:
        return _result(key, False, f"连接 {host}:{port} 失败：{e}")

def probe_dns_ok() -> ProbeResult:
    """DNS 能不能解析目标主机。"""
    key = "network.dns_ok"
    host, _ = _http_target()
    try:
        socket.getaddrinfo(host, None)
        return _result(key, True, f"DNS 解析 {host} 成功")
    except socket.gaierror as e:
        return _result(key, False, f"DNS 解析 {host} 失败：{e}")
    except Exception as e:  # noqa: BLE001 —— 探针不许把异常抛给调用方
        return _result(key, None, f"DNS 探测异常：{type(e).__name__}: {e}")

def probe_proxy() -> ProbeResult:
    """代理配置（只看环境变量 —— 不触网、不读用户配置文件）。"""
    key = "network.proxy"
    try:
        for name in (
            "HTTPS_PROXY",
            "https_proxy",
            "HTTP_PROXY",
            "http_proxy",
            "ALL_PROXY",
            "all_proxy",
        ):
            value = os.environ.get(name)
            if value:
                return _result(key, value, f"来自环境变量 {name}")
        return _result(key, "", "未配置代理（查过 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY）")
    except Exception as e:  # noqa: BLE001
        return _result(key, None, f"读取环境变量失败：{type(e).__name__}: {e}")

# ── 工具 ──────────────────────────────────────────────────────────────────────

def probe_tool_installed(tool: str) -> ProbeResult:
    """外部工具在不在 PATH 里。"""
    key = f"tools.{tool}.installed"
    try:
        path = shutil.which(tool)
    except Exception as e:  # noqa: BLE001
        return _result(key, None, f"查 PATH 失败：{type(e).__name__}: {e}")
    if path:
        return _result(key, True, f"在 PATH 里：{path}")
    return _result(key, False, f"PATH 里没有 {tool}")

def probe_tool_version(tool: str, flag: str | None = None) -> ProbeResult:
    """外部工具的版本号。

    - 没装 → `""`（**已确认没有版本**）
    - 装了但问不出来（超时 / 没输出）→ unknown
    """
    key = f"tools.{tool}.version"
    flag = flag or TOOL_VERSION_FLAGS.get(tool, "--version")
    exe = shutil.which(tool)
    if not exe:
        return _result(key, "", f"{tool} 未安装 → 没有版本")
    try:
        proc = subprocess.run(
            [exe, flag],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _result(key, None, f"`{tool} {flag}` 超时（{SUBPROCESS_TIMEOUT}s）→ 版本 unknown")
    except OSError as e:
        return _result(key, None, f"`{tool} {flag}` 启动失败：{type(e).__name__}: {e}")
    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if not out:
        return _result(key, None, f"`{tool} {flag}` 没有输出（退出码 {proc.returncode}）→ 版本 unknown")
    first = out.splitlines()[0].strip()
    match = _VERSION_RE.search(first)
    value = match.group(0) if match else first
    return _result(key, value, f"via `{tool} {flag}`：{first}")

# ── 凭据 ──────────────────────────────────────────────────────────────────────

def _ssh_dir(explicit: Path | None = None) -> tuple[Path | None, str]:
    if explicit is not None:
        return Path(explicit), "显式指定"
    try:
        return Path.home() / ".ssh", "默认 ~/.ssh"
    except (RuntimeError, OSError) as e:
        return None, f"无法确定 HOME：{type(e).__name__}: {e}"

def _find_ssh_key(explicit: Path | None = None) -> tuple[str | None, str]:
    """找 SSH 私钥。返回 (路径, 说明)。

    路径为 `""` → **已确认没有**；为 None → **探测不到**（unknown）。
    """
    directory, origin = _ssh_dir(explicit)
    if directory is None:
        return None, origin
    try:
        if not directory.is_dir():
            return "", f"{directory} 不存在（{origin}）→ 确认没有 SSH key"
        for name in SSH_KEY_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate), f"找到 {candidate}"
        for candidate in sorted(directory.glob("id_*")):
            if candidate.is_file() and candidate.suffix != ".pub":
                return str(candidate), f"找到 {candidate}"
        return "", f"{directory} 里没有私钥文件（{origin}）→ 确认没有 SSH key"
    except OSError as e:
        return None, f"读取 {directory} 失败：{type(e).__name__}: {e}"

def probe_ssh_key_present(explicit: Path | None = None) -> ProbeResult:
    """有没有 SSH 私钥。"""
    key = "credentials.ssh_key_present"
    path, detail = _find_ssh_key(explicit)
    if path is None:
        return _result(key, None, detail)
    return _result(key, bool(path), detail)

def probe_ssh_key_path(explicit: Path | None = None) -> ProbeResult:
    """SSH 私钥在哪（没有 → 空字符串；探测不到 → unknown）。"""
    key = "credentials.ssh_key_path"
    path, detail = _find_ssh_key(explicit)
    return _result(key, path, detail)

def probe_gh_token_present() -> ProbeResult:
    """GitHub token 在不在环境变量里（只看变量，不调 API）。"""
    key = "credentials.gh_token_present"
    try:
        for name in GH_TOKEN_ENV:
            if os.environ.get(name):
                return _result(key, True, f"环境变量 {name} 已设置")
        return _result(key, False, f"环境变量 {' / '.join(GH_TOKEN_ENV)} 都未设置")
    except Exception as e:  # noqa: BLE001
        return _result(key, None, f"读取环境变量失败：{type(e).__name__}: {e}")

# ── 限额 ──────────────────────────────────────────────────────────────────────

def probe_api_rate_remaining(api_url: str | None = None) -> ProbeResult:
    """API 速率配额还剩多少。

    - 环境变量 `INTENT_OS_PROBE_API_RATE_REMAINING` 优先（注入用，不触网）
    - 否则读 API 响应头 `X-RateLimit-Remaining`
    - 拿不到 → unknown（**绝不猜 0**）
    """
    key = "limits.api_rate_remaining"
    injected = os.environ.get(ENV_API_RATE)
    if injected is not None:
        try:
            return _result(key, int(injected), f"来自环境变量 {ENV_API_RATE}")
        except ValueError:
            return _result(key, None, f"环境变量 {ENV_API_RATE} 不是整数：{injected!r}")
    url = api_url or os.environ.get(ENV_API_URL) or DEFAULT_RATE_URL
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "intent-os-probe/1.0"}
        )
        with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
            remaining = response.headers.get("X-RateLimit-Remaining")
    except Exception as e:  # noqa: BLE001
        return _result(key, None, f"读不到 API 速率（{url}）：{type(e).__name__}: {e}")
    if remaining is None:
        return _result(key, None, f"{url} 没有 X-RateLimit-Remaining 头")
    try:
        return _result(key, int(remaining), f"来自 {url} 的 X-RateLimit-Remaining")
    except ValueError:
        return _result(key, None, f"X-RateLimit-Remaining 不是整数：{remaining!r}")

def default_sandbox_dir() -> Path:
    """沙盒目录（可用 `INTENT_OS_PROBE_SANDBOX` 覆盖；默认仓库里的 sandbox/）。"""
    env = os.environ.get(ENV_SANDBOX)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "sandbox"

def probe_sandbox_writable(sandbox: Path | None = None) -> ProbeResult:
    """沙盒目录能不能写。

    **只读判定**（os.access + stat）—— 不往沙盒里写临时文件。
    """
    key = "limits.sandbox_writable"
    directory = Path(sandbox) if sandbox is not None else default_sandbox_dir()
    try:
        if not directory.exists():
            return _result(key, None, f"沙盒目录 {directory} 不存在 → 无法判定")
        if not directory.is_dir():
            return _result(key, None, f"{directory} 不是目录 → 无法判定")
        writable = os.access(directory, os.W_OK)
        return _result(key, writable, f"{directory} 可写={writable}（os.access W_OK）")
    except OSError as e:
        return _result(key, None, f"判定 {directory} 可写性失败：{type(e).__name__}: {e}")

# ── 注册表与编排 ──────────────────────────────────────────────────────────────

def default_probes() -> dict[str, ProbeFn]:
    """默认探针集（每次返回一份新拷贝，调用方可以随便改）。

    key 用点号分层：`段.对象.属性`（与 RFC §3.1 的 JSON 形状一一对应）。
    """
    return {
        "network.http_reachable": probe_http_reachable,
        "network.proxy": probe_proxy,
        "network.dns_ok": probe_dns_ok,
        "tools.gh.installed": lambda: probe_tool_installed("gh"),
        "tools.gh.version": lambda: probe_tool_version("gh"),
        "tools.ssh.installed": lambda: probe_tool_installed("ssh"),
        "tools.ssh.version": lambda: probe_tool_version("ssh"),
        "tools.git.installed": lambda: probe_tool_installed("git"),
        "tools.git.version": lambda: probe_tool_version("git"),
        "credentials.ssh_key_present": probe_ssh_key_present,
        "credentials.ssh_key_path": probe_ssh_key_path,
        "credentials.gh_token_present": probe_gh_token_present,
        "limits.api_rate_remaining": probe_api_rate_remaining,
        "limits.sandbox_writable": probe_sandbox_writable,
    }

#: 默认探针集（只读引用；要用请调 `default_probes()`）
DEFAULT_PROBES: Mapping[str, ProbeFn] = default_probes()

def probe_environment(
    probers: Mapping[str, ProbeFn] | None = None,
    *,
    probed_at: str | None = None,
) -> EnvironmentSnapshot:
    """跑一轮探针，返回快照。

    - `probers=None` → 用默认探针集
    - 传入 mapping → **完全替换**默认探针集（测试注入假探针用）
    - 探针可以返回 `ProbeResult`，也可以直接返回裸值（裸值没有 detail）
    - **单个探针抛异常 → 该 key 记 unknown（value=None）+ 说清原因**，
      整份快照照常产出（红线：不静默降级，也不因为一个探针就崩）
    """
    active = default_probes() if probers is None else dict(probers)
    stamp = probed_at or utc_now_iso()
    results: list[ProbeResult] = []
    for key, fn in active.items():
        try:
            raw = fn()
        except Exception as e:  # noqa: BLE001 —— 一个探针坏掉不许拖垮整份快照
            results.append(
                ProbeResult(
                    key=key,
                    value=None,
                    detail=f"探针异常：{type(e).__name__}: {e}",
                    probed_at=stamp,
                )
            )
            continue
        if raw is None:
            results.append(
                ProbeResult(
                    key=key, value=None, detail="探针没有给出值", probed_at=stamp
                )
            )
            continue
        if isinstance(raw, ProbeResult):
            results.append(
                ProbeResult(
                    key=key, value=raw.value, detail=raw.detail, probed_at=stamp
                )
            )
        else:
            results.append(ProbeResult(key=key, value=raw, detail="", probed_at=stamp))
    return EnvironmentSnapshot(results=tuple(results))
