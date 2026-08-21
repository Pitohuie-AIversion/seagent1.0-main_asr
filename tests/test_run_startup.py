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


if __name__ == "__main__":
    unittest.main()
