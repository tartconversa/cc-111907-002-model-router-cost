#!/usr/bin/env python3
"""运行器：让 `python3 -I run.py ...` 在隔离模式下也能找到 settlement 包。

Python 3.14 的 -I 隐含 -P，不会把脚本目录自动加入 sys.path，也忽略
PYTHONPATH 与用户 site-packages（防止来源目录里被植入的 json.py 等被加载）。
这里显式插入的是本运行器所在的仓库根——可信代码路径，不削弱隔离效果。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from settlement.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
