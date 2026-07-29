#!/usr/bin/env python3
"""市场宽度快照 CLI。实现已挪进 quanti.regime.breadth —— 生产代码(每日快照
任务、API)要 import 它,而 scripts/ 不是包。这里只保留命令行入口。

用法: .venv/bin/python scripts/breadth_snapshot.py [--json|--selfcheck]
"""
from quanti.regime.breadth import main

if __name__ == "__main__":
    main()
