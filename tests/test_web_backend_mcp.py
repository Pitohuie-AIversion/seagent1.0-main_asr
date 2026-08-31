"""
test_web_backend_mcp.py
========================
针对 web_backend.py 中新增的 ROS 2 MCP Web API 接口的单元与集成测试

测试接口：
  - GET  /api/mcp/status       (MCP 连接状态与遥测查询)
  - POST /api/mcp/dispatch     (自定义意图/会话结果下发)
  - POST /api/mcp/task-manage  (任务挂起/恢复/删除/清除阻塞)
  - POST /api/mcp/ctrl-task    (灯光/继电器设备控制)
"""

import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "mcp"
CORE_DIR = MCP_DIR / "core"
MOCK_DIR = MCP_DIR / "mock"

for p in [CORE_DIR, MOCK_DIR, MCP_DIR, PROJECT_ROOT]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import web_backend
from mock_rosbridge_server import MockRosbridgeServer, received_publishes, active_tasks
from bridge_service import SEAgentMCPBridgeService
from src.state_info import RobotStateInfo

PORT = 9098


class WebBackendMCPTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = MockRosbridgeServer(port=PORT)
        cls.server.start()
        time.sleep(0.3)

        web_backend.app.testing = True
        cls.client = web_backend.app.test_client()

        # 初始化 state_info 与 bridge_service
        state_file = PROJECT_ROOT / "scratch" / "test_state.yaml"
        state_file.write_text("store_version: 0\nrobots: {}\n", encoding="utf-8")
        fleet_file = PROJECT_ROOT / "config" / "robot_fleet.yaml"
        cls.state_info = RobotStateInfo(state_file=state_file, fleet_file=fleet_file)

        cls.bridge = SEAgentMCPBridgeService(
            host="127.0.0.1", port=PORT, state_info=cls.state_info, connect_timeout=3.0
        )
        cls.bridge.start()
        time.sleep(0.2)
        web_backend.init_mcp_bridge_service(cls.bridge)

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()
        cls.server.stop()
        web_backend.init_mcp_bridge_service(None)

    def setUp(self):
        received_publishes.clear()
        active_tasks.clear()

    # ------------------------------------------------------------------
    # 1. GET /api/mcp/status
    # ------------------------------------------------------------------

    def test_01_get_mcp_status_connected(self):
        """[01] GET /api/mcp/status 状态为 connected，返回 host, port 及遥测数据"""
        res = self.client.get("/api/mcp/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["code"], 200)
        self.assertTrue(data["mcp_connected"])
        self.assertEqual(data["port"], PORT)

    def test_02_get_mcp_status_uninitialized(self):
        """[02] 当 MCP 未初始化时，GET /api/mcp/status 返回 mcp_connected=False"""
        web_backend.init_mcp_bridge_service(None)
        res = self.client.get("/api/mcp/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertFalse(data["mcp_connected"])
        # 恢复
        web_backend.init_mcp_bridge_service(self.bridge)

    # ------------------------------------------------------------------
    # 2. POST /api/mcp/dispatch
    # ------------------------------------------------------------------

    def test_03_dispatch_custom_intent_is_rejected(self):
        """[03] 自定义 task_intent 不能绕过 SEAgent 会话确认与约束校验"""
        payload = {
            "task_intent": {
                "schema_version": 2,
                "task_type": "tree_valve_operation",
                "priority": 15,
                "location": {"water_depth_m": 300.0},
                "task": {"details": {"target": {"latitude": 20.815, "longitude": 115.735}}},
            }
        }
        res = self.client.post("/api/mcp/dispatch", json=payload)
        self.assertEqual(res.status_code, 403)
        data = res.get_json()
        self.assertEqual(data["code"], 403)
        self.assertEqual(self.server.get_received_publishes(), [])

    def test_04_dispatch_missing_params(self):
        """[04] POST /api/mcp/dispatch 既无 session_id 也无 task_intent 应返回 400"""
        res = self.client.post("/api/mcp/dispatch", json={})
        self.assertEqual(res.status_code, 400)

    def test_04b_auto_dispatch_only_on_first_transition_to_done(self):
        manager = SimpleNamespace(
            phase="done",
            _last_built_json={
                "intent_id": "PI-20260828-099",
                "task_type": "pipeline_inspection",
            },
        )
        bridge = Mock()
        bridge.is_healthy.return_value = True
        bridge.dispatch_intent.return_value = 0x80099
        web_backend.init_mcp_bridge_service(bridge)
        try:
            first = web_backend._dispatch_ros2_on_done_transition(manager, "confirming")
            repeated = web_backend._dispatch_ros2_on_done_transition(manager, "done")
        finally:
            web_backend.init_mcp_bridge_service(self.bridge)

        self.assertEqual(first["state"], "SENT")
        self.assertIsNone(repeated)
        bridge.dispatch_intent.assert_called_once()

    # ------------------------------------------------------------------
    # 3. POST /api/mcp/task-manage
    # ------------------------------------------------------------------

    def test_05_task_manage_suspend_and_resume(self):
        """[05] POST /api/mcp/task-manage 依次发送 suspend 和 resume"""
        # 预先下发任务
        tid = self.bridge.dispatch_intent({
            "schema_version": 2, "task_type": "pipeline_inspection",
            "location": {"water_depth_m": 80.0}, "task": {"details": {
                "start_point": {"latitude": 20.0, "longitude": 115.0},
                "end_point": {"latitude": 20.1, "longitude": 115.2},
            }}
        })
        time.sleep(0.1)

        # 挂起
        res1 = self.client.post("/api/mcp/task-manage", json={"action": "suspend", "task_id": tid})
        self.assertEqual(res1.status_code, 200)

        # 恢复
        res2 = self.client.post("/api/mcp/task-manage", json={"action": "resume", "task_id": tid})
        self.assertEqual(res2.status_code, 200)

        time.sleep(0.2)
        cmds = [c for c in self.server.get_received_publishes() if c["payload"]["task_type"] == 0]
        self.assertGreaterEqual(len(cmds), 2)

    def test_06_task_manage_clear_block(self):
        """[06] POST /api/mcp/task-manage action=clear_block"""
        res = self.client.post("/api/mcp/task-manage", json={"action": "clear_block"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["code"], 200)

    def test_07_task_manage_invalid_action(self):
        """[07] POST /api/mcp/task-manage 非法 action 返回 400"""
        res = self.client.post("/api/mcp/task-manage", json={"action": "unknown_action"})
        self.assertEqual(res.status_code, 400)

    # ------------------------------------------------------------------
    # 4. POST /api/mcp/ctrl-task
    # ------------------------------------------------------------------

    def test_08_ctrl_task_light(self):
        """[08] POST /api/mcp/ctrl-task 开关灯设备控制 (device_id=1, value=50.0)"""
        res = self.client.post("/api/mcp/ctrl-task", json={"device_id": 1, "value": 50.0})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["code"], 200)

        time.sleep(0.2)
        cmds = [c for c in self.server.get_received_publishes() if c["payload"]["task_type"] == 6]
        self.assertGreaterEqual(len(cmds), 1)
        self.assertEqual(cmds[-1]["payload"]["params"], [1.0, 50.0])

    def test_09_ctrl_task_missing_device_id(self):
        """[09] POST /api/mcp/ctrl-task 缺少 device_id 返回 400"""
        res = self.client.post("/api/mcp/ctrl-task", json={"value": 10.0})
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
