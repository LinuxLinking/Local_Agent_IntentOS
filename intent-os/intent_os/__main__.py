"""`python -m intent_os` 的入口。

把 CLI 挂到包上，而不是单独放一个脚本 —— 这样
`python -m intent_os audit replay <log>` 在任何装了 intent_os 的地方都能跑。
"""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
