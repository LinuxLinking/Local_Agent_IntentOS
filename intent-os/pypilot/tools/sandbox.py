"""沙箱路径校验。"""
from pathlib import Path

from pypilot.config import SANDBOX_DIR

def safe_path(rel: str) -> Path:
    """把相对路径 resolve 到 sandbox 内，越界抛 ValueError。"""
    rel = str(rel).strip().strip('"').strip("'")
    # 去掉前导 ./ 或 .\
    while rel.startswith(("./", ".\\")):
        rel = rel[2:]
    # 如果用户给了绝对路径但就在 sandbox 内，也接受（取相对部分）
    abs_path = Path(rel).resolve() if Path(rel).is_absolute() else (SANDBOX_DIR / rel).resolve()
    sandbox_resolved = SANDBOX_DIR.resolve()
    try:
        abs_path.relative_to(sandbox_resolved)
    except ValueError:
        raise ValueError(f"路径越界，只能操作 sandbox 内文件: {rel}")
    return abs_path
