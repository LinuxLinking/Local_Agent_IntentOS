"""配置项。"""
from pathlib import Path

# 项目根：pypilot/config.py 上跳一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# agent 工作沙箱：所有文件读写限定在此目录内
SANDBOX_DIR = PROJECT_ROOT / "sandbox"
SANDBOX_DIR.mkdir(exist_ok=True)

# Ollama 服务
OLLAMA_BASE_URL = "http://localhost:11434"
MODEL_NAME = "qwen2.5-coder-3b:latest"

TEMPERATURE = 0.7
# 运行上下文长度（模型原生支持到 32768，这里省内存）。
NUM_CTX = 8192

STOP_SEQUENCES = ["<|im_start|>", "<|im_end|>", "<|endoftext|>", "Observation:"]

# Agent loop
MAX_ITERATIONS = 8  # 单次任务最多几步 ReAct，防死循环
RUN_TIMEOUT = 30    # run_python 的执行超时秒数

RECURSION_LIMIT = 4 * MAX_ITERATIONS + 10

RESERVED_COMPLETION_TOKENS = 3500

CJK_CHARS_PER_TOKEN = 1.6

INCLUDE_AI_CONTEXT = False

ENABLE_SUMMARY = False
