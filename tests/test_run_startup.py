"""
test_run_startup.py
====================
针对 run.py 启动脚本与 MCP 自动注入流程的测试套件

场景：
  - 测试在 OFFLINE_MOCK 模式下调用 startup()，自动拉起 Mock rosbridge (9091) 并完成 Web MCP 初始化
"""

import os
import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))


class TestRunStartupMCP(unittest.TestCase):

    def setUp(self):
        os.environ["OFFLINE_MOCK"] = "1"
        os.environ["MCP_PORT"] = "9100"

    def tearDown(self):
        os.environ.pop("MCP_PORT", None)
        os.environ.pop("MCP_EMBEDDED_MOCK", None)

    def test_mock_startup_with_mcp(self):
        """验证 OFFLINE_MOCK=1 下 startup() 正确引导 MCP 桥接服务"""
        import run
        import web_backend

        run.startup()
        time.sleep(0.3)

        bridge = web_backend.get_mcp_bridge()
        self.assertIsNotNone(bridge)
        self.assertTrue(bridge.is_healthy())
        self.assertEqual(bridge.port, 9100)

        # 清理
        bridge.stop()
        if run._mock_rosbridge_srv:
            run._mock_rosbridge_srv.stop()
            run._mock_rosbridge_srv = None
        web_backend.init_mcp_bridge_service(None)

    def test_offline_models_can_connect_to_external_rosbridge(self):
        """离线模型模式可连接外部 ROS2 rosbridge，而不重复占用其端口。"""
        from mcp.shim.mock_rosbridge_server import MockRosbridgeServer
        import run
        import web_backend

        external_rosbridge = MockRosbridgeServer(port=9101)
        external_rosbridge.start()
        os.environ["MCP_PORT"] = "9101"
        os.environ["MCP_EMBEDDED_MOCK"] = "0"
        try:
            run.startup()
            time.sleep(0.3)

            bridge = web_backend.get_mcp_bridge()
            self.assertIsNotNone(bridge)
            self.assertTrue(bridge.is_healthy())
            self.assertEqual(bridge.port, 9101)
            self.assertIsNone(run._mock_rosbridge_srv)
        finally:
            bridge = web_backend.get_mcp_bridge()
            if bridge is not None:
                bridge.stop()
            external_rosbridge.stop()
            web_backend.init_mcp_bridge_service(None)


if __name__ == "__main__":
    unittest.main()
