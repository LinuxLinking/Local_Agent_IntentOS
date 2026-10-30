"""内置工具、注册表与统一调度。

所有工具返回字符串；路径统一经 safe_path 校验；
run_python 限定在 sandbox 目录内执行并设超时。
"""
import os
import subprocess
import sys

from pypilot.config import RUN_TIMEOUT, SANDBOX_DIR
from pypilot.tools.sandbox import safe_path

def read_file(path: str) -> str:
    p = safe_path(path)
    if not p.exists():
        return f"文件不存在: {p.name}"
    if p.is_dir():
        return f"这是目录不是文件: {p.name}"
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"[二进制文件，无法以文本读取: {p.name}]"

def write_file(path: str, content: str) -> str:
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # content 可能被 LLM 包成字符串字面量，但 JSON 解析后已经是 str，直接写
    text = str(content)
    p.write_text(text, encoding="utf-8")
    return f"已写入 {p.name}（{len(text)} 字符，路径: {p.relative_to(SANDBOX_DIR)}）"

def list_dir(path: str = ".") -> str:
    p = safe_path(path)
    if not p.exists():
        return f"路径不存在: {p}"
    if p.is_file():
        return f"这是文件不是目录: {p.name}"
    entries = []
    for c in sorted(p.iterdir()):
        kind = "目录" if c.is_dir() else "文件"
        size = f"{c.stat().st_size}B" if c.is_file() else ""
        entries.append(f"{kind}\t{c.name}\t{size}")
    if not entries:
        return f"空目录: {p.name}"
    return "\n".join(entries)

def run_python(path: str) -> str:
    p = safe_path(path)
    if not p.exists():
        return f"文件不存在: {p.name}"
    if p.suffix != ".py":
        return f"只支持执行 .py 文件，得到的是 {p.name}"
    try:
        # 父子进程两端都固定 UTF-8，避免非默认 locale 下解码失败导致输出丢失
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(p)],
            cwd=str(SANDBOX_DIR),
            capture_output=True,
            timeout=RUN_TIMEOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        out = f"退出码: {r.returncode}\n"
        if r.stdout:
            out += f"--- stdout ---\n{r.stdout}"
        if r.stderr:
            out += f"--- stderr ---\n{r.stderr}"
        if not r.stdout and not r.stderr:
            out += "（无输出）"
        return out.rstrip()
    except subprocess.TimeoutExpired:
        return f"执行超时（>{RUN_TIMEOUT}s），已终止"

# 工具注册表：name -> (func, arg_schema_desc)
# 值的形状保持 (func, str) 元组 —— main 的 /tools 命令按 `for name, (_, sig)` 解包。
TOOL_REGISTRY = {
    "read_file": (read_file, '{"path": "相对sandbox的文件路径"}'),
    "write_file": (write_file, '{"path": "文件路径", "content": "完整文件内容"}'),
    "list_dir": (list_dir, '{"path": "目录路径，根用 ."}'),
    "run_python": (run_python, '{"path": "要执行的 .py 文件路径"}'),
}

def call_tool(name: str, action_input) -> str:
    """统一调度入口，兜所有异常成字符串 Observation。"""
    if name not in TOOL_REGISTRY:
        return f"未知工具: {name}。可用工具: {list(TOOL_REGISTRY)}"
    func, _ = TOOL_REGISTRY[name]
    try:
        if isinstance(action_input, dict):
            return func(**action_input)
        elif action_input is None or action_input == "":
            return func()
        else:
            return func(action_input)
    except TypeError as e:
        # 通常是缺参数或参数名不对
        sig = TOOL_REGISTRY[name][1]
        return f"参数错误 [{name}]: {e}。期望参数: {sig}"
    except Exception as e:
        return f"工具执行出错 [{name}]: {type(e).__name__}: {e}"
