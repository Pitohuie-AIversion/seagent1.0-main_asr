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
from types import SimpleNamespace
from unittest.mock import Mock

TESTS_DIR = Path(__file__).resolve().parent
MCP_DIR = TESTS_DIR.parent
CORE_DIR = MCP_DIR / "core"
MOCK_DIR = MCP_DIR / "mock"
SEAGENT_ROOT = MCP_DIR.parent

for p in [TESTS_DIR, CORE_DIR, MOCK_DIR, MCP_DIR, SEAGENT_ROOT]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from mcp.shim.run_mcp_bridge import parse_args, main
from mcp.shim.bridge_service import SEAgentMCPBridgeService
from mcp.shim.mock_rosbridge_server import MockRosbridgeServer


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

    def test_runner_loads_configuration_from_project_root(self, monkeypatch):
        """目录拆分后，CLI 仍须读取仓库 config，而非不存在的 mcp/config。"""
        from mcp.mock import run_mcp_bridge as runner

        monkeypatch.setattr(runner, "parse_args", lambda: SimpleNamespace(
            host="127.0.0.1", port=9090, mock=False, sync_interval=0.01,
        ))
        state_factory = Mock()
        bridge_factory = Mock()
        monkeypatch.setattr(runner, "RobotStateInfo", state_factory)
        monkeypatch.setattr(runner, "SEAgentMCPBridgeService", bridge_factory)
        monkeypatch.setattr(runner.time, "sleep", Mock(side_effect=KeyboardInterrupt))

        runner.main()

        config_dir = Path(__file__).resolve().parents[3] / "config"
        state_factory.assert_called_once_with(
            state_file=config_dir / "state.yaml",
            fleet_file=config_dir / "robot_fleet.yaml",
        )
        assert bridge_factory.call_args.kwargs["state_info"] is state_factory.return_value
        bridge_factory.return_value.start.assert_called_once()
        bridge_factory.return_value.stop.assert_called_once()
