#!/usr/bin/env python3
"""隔离模式测试运行器：显式注入 backend 路径后执行全部单元测试。

运行：python3 -I /workspace/scripts/run_tests.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(str(BACKEND / "app" / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
