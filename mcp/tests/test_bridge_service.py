"""
test_bridge_service.py
========================
针对 SEAgentMCPBridgeService 的自动化测试套件

测试场景：
  Q1: 服务启动与健康状态校验
  Q2: 自动任务下发（TaskIntent v2 → RosbridgeClient → SysTaskCmd）
  Q3: 任务管理指令传递（suspend/resume/delete/clear_block）
  Q4: 自动遥测同步（SysStatus 遥测数据落盘至 RobotStateInfo）
  Q5: 任务生命周期追踪（等待任务推演至 FINISH）
  Q6: 完整端到端：下发 → 执行中 → 成功完成闭环
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

from bridge_service import SEAgentMCPBridgeService
from mock_rosbridge_server import MockRosbridgeServer, received_publishes, active_tasks
from src.state_info import RobotStateInfo

PORT = 9095


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
def spec_file(tmp_path):
    source = SEAGENT_ROOT / "config" / "ros2_protocol_spec.yaml"
    target = tmp_path / "ros2_protocol_spec.yaml"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


@pytest.fixture
def bridge(rosbridge_server, state_info):
    service = SEAgentMCPBridgeService(
        host="127.0.0.1", port=PORT, state_info=state_info, connect_timeout=3.0
    )
    service.start()
    time.sleep(0.2)
    yield service
    service.stop()


# ============================================================================
# 测试用例
# ============================================================================

class TestBridgeService:

    def test_Q1_service_start_and_healthy(self, bridge):
        """[Q1] 服务应正确启动并进入 healthy 状态"""
        assert bridge.is_healthy()

    def test_Q2_dispatch_intent_success(self, bridge, rosbridge_server):
        """[Q2] 自动下发 TaskIntent v2，Mock rosbridge 成功接收"""
        intent = {
            "schema_version": 2,
            "task_type": "tree_valve_operation",
            "priority": 15,
            "location": {"oilfield": "流花11-1油田", "water_depth_m": 300.0},
            "task": {"type": "tree_valve_operation", "details": {
                "target": {"latitude": 20.815, "longitude": 115.735},
                "speed_ms": 1.5,
            }},
        }
        tid = bridge.dispatch_intent(intent)
        time.sleep(0.2)

        assert 0x80001 <= tid <= 0x8FFFF
        pubs = rosbridge_server.get_received_publishes()
        assert len(pubs) >= 1
        cmd = pubs[-1]["payload"]
        assert cmd["task_type"] == 4
        assert cmd["pos_target"][0]["position"]["z"] == pytest.approx(-300.0)

    def test_Q3_task_management_commands(self, bridge, rosbridge_server):
        """[Q3] 任务管理指令下发（suspend, resume, delete, clear_block）"""
        intent = {"schema_version": 2, "task_type": "underwater_move",
                  "location": {"water_depth_m": 50.0}, "task": {"details": {"target": {"latitude": 20.0, "longitude": 115.0}}}}
        tid = bridge.dispatch_intent(intent)
        time.sleep(0.1)

        bridge.suspend_task(tid)
        time.sleep(0.1)
        bridge.resume_task(tid)
        time.sleep(0.1)
        bridge.emergency_clear_block()
        time.sleep(0.2)

        cmds = rosbridge_server.get_received_publishes()
        actions = [c["payload"]["params"][0] for c in cmds if c["payload"]["task_type"] == 0]
        assert 0.0 in actions  # SUSPEND
        assert 1.0 in actions  # RESUME
        assert 7.0 in actions  # CLEAR_BLOCK

    def test_Q4_telemetry_tracker_memory_without_protocol_yaml_writes(self, bridge, spec_file):
        """[Q4] 遥测只进入运行时内存，协议 YAML 始终保持静态"""
        protocol_before = spec_file.read_bytes()
        time.sleep(0.4)
        t = bridge.tracker.latest_telemetry()
        assert t is not None
        assert t.water_depth == pytest.approx(312.4)
        assert t.altitude == pytest.approx(2.5)

        intent = {
            "schema_version": 2, "intent_id": "Q4-INTENT", "task_type": "pipeline_inspection",
            "location": {"water_depth_m": 80.0},
            "task": {"details": {
                "start_point": {"latitude": 20.0, "longitude": 115.0},
                "end_point": {"latitude": 20.1, "longitude": 115.2},
            }},
        }
        tid = bridge.dispatch_intent(intent)

        # 等待至少一条系统状态回传进入内存快照。
        status_seen = None
        progress_seen = None
        timeout = time.monotonic() + 3.0
        expected_id = f"0x{tid:X}"
        while time.monotonic() < timeout:
            snap = bridge.runtime_snapshot()
            active_tasks = snap.get("active_tasks", []) or []
            for item in active_tasks:
                if str(item.get("task_id")) == expected_id:
                    status_seen = item.get("status")
                    progress_seen = item.get("progress")
                    if status_seen and status_seen != "SENT":
                        break
            if status_seen and status_seen != "SENT":
                break
            time.sleep(0.15)

        assert status_seen in {"READY", "PLAN", "ENTER", "ONGOING", "FINISH", "FAIL", "PAUSE", "EXIT"}
        assert isinstance(progress_seen, (int, float))
        assert progress_seen >= 0.0
        assert spec_file.read_bytes() == protocol_before

    def test_Q5_wait_for_task_finish(self, bridge):
        """[Q5] 下发任务并等待任务在机器人侧推演至 FINISH"""
        intent = {
            "schema_version": 2, "task_type": "pipeline_inspection",
            "location": {"water_depth_m": 80.0},
            "task": {"details": {
                "start_point": {"latitude": 20.0, "longitude": 115.0},
                "end_point": {"latitude": 20.1, "longitude": 115.2},
            }}
        }
        tid = bridge.dispatch_intent(intent)

        # 阻塞等待完成
        final_item = bridge.wait_for_task_finish(tid, timeout=5.0)
        assert final_item is not None
        assert final_item.status == 5  # FINISH
        assert final_item.status_name == "FINISH"

    def test_Q6_full_e2e_dispatch_track_sync(self, bridge, rosbridge_server):
        """[Q6] 完整闭环：下发意图 → 遥测追踪 → 等待完成"""
        # 1. 验证下发前遥测
        time.sleep(0.2)
        t1 = bridge.tracker.latest_telemetry()
        assert t1 is not None and t1.water_depth == pytest.approx(312.4)

        # 2. 下发采油树阀门任务（规划水深 300m）
        intent = {
            "schema_version": 2, "task_type": "tree_valve_operation",
            "location": {"water_depth_m": 300.0},
            "task": {"details": {"target": {"latitude": 20.815, "longitude": 115.735}}}
        }
        tid = bridge.dispatch_intent(intent)

        # 3. 等待机器人侧执行完成
        item = bridge.wait_for_task_finish(tid, timeout=5.0)
        assert item is not None and item.is_finished()
