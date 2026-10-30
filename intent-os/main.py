"""本地代码助手 REPL。

用法：
    uv run python main.py
"""
import sys

# 非 UTF-8 终端下避免特殊字符打印导致崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass

from pypilot import Agent
from pypilot.config import SANDBOX_DIR
from pypilot.tools import TOOL_REGISTRY

BANNER = r"""
 ____  ____ ____   ___  ____ ____   ___  __  __
|  _ \| _) ) _  ) | __|| _ \ __  | / _ \(  )  /
|  __/|  \( (   || (   |  _/(_  / ( (_) )    (
|_|   |_|  \_\___||___||_|  |___|  \___/ \_/\_)

  本地 Coder Agent · 模型 qwen2.5-coder-3b · 沙箱 ./sandbox
  LangGraph 架构 · 内存记忆已开启（进程内）
  想让我写代码 / 改错，直接说。/exit 退出。
"""

def main():
    print(BANNER)
    verbose = True
    agent = Agent(verbose=verbose)
    print(f"已加载模型，沙箱: {agent.llm.model}  (verbose={'on' if verbose else 'off'})")
    print(f"沙箱目录: {SANDBOX_DIR}\n")

    while True:
        try:
            user = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not user:
            continue

        cmd = user.lower()
        if cmd in ("/exit", "/quit"):
            print("再见。")
            break
        if cmd == "/reset":
            agent.reset()
            print("[已清空对话历史]\n")
            continue
        if cmd == "/quiet":
            verbose = not verbose
            agent.verbose = verbose
            print(f"[verbose {'on' if verbose else 'off'}]\n")
            continue
        if cmd == "/tools":
            print("[可用工具]")
            for name, (_, sig) in TOOL_REGISTRY.items():
                print(f"  {name}: {sig}")
            print()
            continue

        result = agent.run(user)
        print("\n=== Final Answer ===")
        print(result)
        print()

if __name__ == "__main__":
    main()
