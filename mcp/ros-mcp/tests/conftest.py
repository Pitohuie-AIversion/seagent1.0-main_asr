"""
mcp/tests/conftest.py
======================
Pytest 配置与环境初始化文件：
自动将 mcp/core、mcp/mock 及项目根目录添加至 sys.path，
确保重构到物理子目录后所有测试无需变动导入即可 100% 运行。
"""

import sys
from pathlib import Path

MCP_TESTS_DIR = Path(__file__).resolve().parent
MCP_ROOT = MCP_TESTS_DIR.parent
CORE_DIR = MCP_ROOT / "core"
MOCK_DIR = MCP_ROOT / "mock"
SEAGENT_ROOT = MCP_ROOT.parent

for p in [CORE_DIR, MOCK_DIR, MCP_TESTS_DIR, SEAGENT_ROOT]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
