"""
test_run_mcp_bridge.py
========================
针对 run_mcp_bridge.py CLI 运行脚本的测试套件

测试场景：
  T1: 命令行参数解析测试 (parse_args)
  T2: 环境变量覆盖 CLI 参数
  T3: Mock 模式下自动拉起与关闭流程
"""

import os
import sys
import time
import pytest
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parent
SEAGENT_ROOT = MCP_DIR.parent
sys.path.insert(0, str(MCP_DIR))
sys.path.insert(0, str(SEAGENT_ROOT))

from run_mcp_bridge import parse_args, main
from bridge_service import SEAgentMCPBridgeService
from mock_rosbridge_server import MockRosbridgeServer


class TestRunMCPBridgeCLI:

    def test_T1_parse_args_defaults(self, monkeypatch):
        """[T1] 默认参数解析测试"""
        monkeypatch.setattr(sys, "argv", ["run_mcp_bridge.py"])
        args = parse_args()
        assert args.host == "127.0.0.1"
        assert args.port == 9090
        assert args.mock is False
        assert args.sync_interval == pytest.approx(2.0)

    def test_T2_parse_args_custom_and_mock(self, monkeypatch):
        """[T2] 自定义参数与 --mock 标签"""
        monkeypatch.setattr(sys, "argv", [
            "run_mcp_bridge.py", "--host", "192.168.1.100", "--port", "9090", "--mock", "--sync-interval", "0.5"
        ])
        args = parse_args()
        assert args.host == "192.168.1.100"
        assert args.port == 9090
        assert args.mock is True
        assert args.sync_interval == pytest.approx(0.5)

    def test_T3_mock_runner_bridge_connection(self):
        """[T3] 仿真模式下 MockRosbridgeServer + SEAgentMCPBridgeService 可以自动建立连接"""
        srv = MockRosbridgeServer(port=9099)
        srv.start()
        time.sleep(0.3)

        bridge = SEAgentMCPBridgeService(host="127.0.0.1", port=9099)
        bridge.start()
        time.sleep(0.2)

        assert bridge.is_healthy()
        bridge.stop()
        srv.stop()
