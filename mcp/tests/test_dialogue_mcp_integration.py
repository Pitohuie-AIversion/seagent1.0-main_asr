"""
test_dialogue_mcp_integration.py
==================================
SEAgent 对话流 ──> TaskIntent ──> MCP ROS 2 闭环测试

场景：
  R1: 挂载测试（attach_mcp_bridge）
  R2: 未完成对话直接下发引发 ValueError 校验
  R3: 模拟完整对话完成 (done 阶段) 触发 MCP 自动下发至 /task_cmd
  R4: 端到端：对话输入 → TaskIntent 落盘 → MCP 下发 → 机器人侧等待 FINISH 闭环
"""

import time
import pytest
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
MCP_DIR = TESTS_DIR.parent
CORE_DIR = MCP_DIR / "core"
MOCK_DIR = MCP_DIR / "mock"
SEAGENT_ROOT = MCP_DIR.parent

for p in [TESTS_DIR, CORE_DIR, MOCK_DIR, MCP_DIR, SEAGENT_ROOT]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from mcp.shim.bridge_service import SEAgentMCPBridgeService
from mcp.shim.dialogue_mcp_integration import attach_mcp_bridge, dispatch_dialogue_result
from mcp.shim.mock_rosbridge_server import MockRosbridgeServer, received_publishes, active_tasks
from src.state_info import RobotStateInfo

PORT = 9096


@pytest.fixture(scope="module")
def rosbridge_server():
    srv = MockRosbridgeServer(port=PORT)
    srv.start()
    time.sleep(0.3)
    yield srv
    srv.stop()


@pytest.fixture(autouse=True)
def clear_state(rosbridge_server):
    received_publishes.clear()
    active_tasks.clear()
    yield


@pytest.fixture
def state_info(tmp_path):
    state_file = tmp_path / "state.yaml"
    state_file.write_text("store_version: 0\nrobots: {}\n", encoding="utf-8")
    fleet_file = SEAGENT_ROOT / "config" / "robot_fleet.yaml"
    return RobotStateInfo(state_file=state_file, fleet_file=fleet_file)


@pytest.fixture
def bridge(rosbridge_server, state_info):
    service = SEAgentMCPBridgeService(
        host="127.0.0.1", port=PORT, state_info=state_info, connect_timeout=3.0
    )
    service.start()
    time.sleep(0.2)
    yield service
    service.stop()


class MockDialogueManager:
    """Mock DialogueManager 用于无 LLM 依赖的对话完成测试"""
    def __init__(self, phase="collecting", final_result=None):
        self.phase = phase
        self.final_result = final_result
        self.dispatched_ros2_task_id = None


# ============================================================================
# 测试用例
# ============================================================================

class TestDialogueMCPIntegration:

    def test_R1_attach_mcp_bridge(self, bridge):
        """[R1] 成功将 MCP Bridge 挂载到 DialogueManager"""
        dm = MockDialogueManager()
        attach_mcp_bridge(dm, bridge)
        assert getattr(dm, "mcp_bridge", None) == bridge

    def test_R2_dispatch_before_done_raises(self, bridge):
        """[R2] 非 done 阶段调用 dispatch_dialogue_result 应抛出 ValueError"""
        dm = MockDialogueManager(phase="collecting")
        attach_mcp_bridge(dm, bridge)

        with pytest.raises(ValueError, match="尚未处于 done 阶段"):
            dispatch_dialogue_result(dm)

    def test_R3_dispatch_done_result_success(self, bridge, rosbridge_server):
        """[R3] done 阶段成功触发 MCP 下发，Mock rosbridge 捕获 SysTaskCmd"""
        task_intent = {
            "schema_version": 2,
            "task_type": "tree_valve_operation",
            "priority": 15,
            "location": {"oilfield": "流花11-1油田", "water_depth_m": 300.0},
            "task": {"type": "tree_valve_operation", "details": {
                "target": {"latitude": 20.815, "longitude": 115.735},
                "speed_ms": 1.5,
            }},
        }
        dm = MockDialogueManager(phase="done", final_result=task_intent)
        attach_mcp_bridge(dm, bridge)

        res = dispatch_dialogue_result(dm)
        assert res["status"] == "success"
        assert dm.dispatched_ros2_task_id == res["task_id"]

        time.sleep(0.2)
        pubs = rosbridge_server.get_received_publishes()
        assert len(pubs) >= 1
        cmd = pubs[-1]["payload"]
        assert cmd["task_type"] == 4
        assert cmd["pos_target"][0]["position"]["z"] == pytest.approx(-300.0)

    def test_R4_full_dialogue_to_robot_finish_roundtrip(self, bridge):
        """[R4] 端到端：完成对话 → 下发至 ROS 2 → 等待机器人推演至 FINISH"""
        task_intent = {
            "schema_version": 2,
            "task_type": "pipeline_inspection",
            "priority": 10,
            "location": {"oilfield": "涠洲油田", "water_depth_m": 80.0},
            "task": {"type": "pipeline_inspection", "details": {
                "start_point": {"latitude": 21.0, "longitude": 109.5},
                "end_point": {"latitude": 21.1, "longitude": 109.7},
            }},
        }
        dm = MockDialogueManager(phase="done", final_result=task_intent)
        attach_mcp_bridge(dm, bridge)

        res = dispatch_dialogue_result(dm, wait_finish=True, timeout=5.0)
        assert res["status"] == "success"
        assert res["final_status_item"] is not None
        assert res["final_status_item"].status == 5  # FINISH
        assert res["final_status_item"].status_name == "FINISH"
