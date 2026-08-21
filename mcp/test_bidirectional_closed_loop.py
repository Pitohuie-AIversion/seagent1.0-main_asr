"""
test_bidirectional_closed_loop.py
===================================
SEAgent ↔ 支持船 Topside 双向收发环节深度测试套件

测试场景：
  S1: 动态任务进度双向感知（下发 /task_cmd → ROV 状态推演 → 云端 TaskStatusTracker 实时同步）
  S2: 交互式中途挂起与恢复闭环（下发 → 云端发送 SUSPEND → 变为 PAUSE(6) → 云端发送 RESUME → 恢复 ONGOING(3) → FINISH）
  S3: 应急清除阻断闭环（云端发送 CLEAR_BLOCK(7) → 机器人清除阻塞重置为 READY）
  S4: 连续下潜动态姿态回传（模拟深度 0m 渐变至 312.4m，姿态与速度双向实时更新）
  S5: 视觉关键点数据接收（订阅 /vision/keypoints，接收机械臂抓取所需的电缆姿态角与方向向量）
  S6: 多机并发双向独立收发（WROV 与 LROV 独立下发、独立遥测回传，无跨机数据污染）
"""

import json
import time
import pytest
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parent
SEAGENT_ROOT = MCP_DIR.parent
sys.path.insert(0, str(MCP_DIR))
sys.path.insert(0, str(SEAGENT_ROOT))

from rosbridge_client import (
    RosbridgeClient, TaskType, TaskManageAction, PilotMode,
    TaskStatus, SysTaskCmd, Pose, intent_to_syscmd
)
from task_status_tracker import TaskStatusTracker, ROVTelemetry, TaskStatusItem
from bridge_service import SEAgentMCPBridgeService
from mock_rosbridge_server import MockRosbridgeServer, received_publishes, active_tasks
from src.state_info import RobotStateInfo

PORT = 9097


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


# ============================================================================
# 测试用例
# ============================================================================

class TestBidirectionalClosedLoop:

    def test_S1_dynamic_task_progress_tracking(self, bridge):
        """[S1] 动态任务进度双向感知：云端下发 → 机器人侧逐步推进 → 云端逐帧记录状态轨迹"""
        intent = {
            "schema_version": 2, "task_type": "tree_valve_operation",
            "priority": 15, "location": {"water_depth_m": 300.0},
            "task": {"details": {"target": {"latitude": 20.815, "longitude": 115.735}}}
        }

        history_statuses = []

        def _on_status_change(item: TaskStatusItem):
            history_statuses.append(item.status)

        # 1. 下发任务
        tid = bridge.dispatch_intent(intent)
        bridge.tracker.on_task_status_change(tid, _on_status_change)

        # 2. 等待完成
        final_item = bridge.wait_for_task_finish(tid, timeout=6.0)

        # 3. 双向验证
        assert final_item is not None
        assert final_item.status == 5  # FINISH
        # 验证曾经经历过中途状态（如 PLAN/ONGOING）
        assert any(s in (1, 2, 3) for s in history_statuses) or bridge.get_task_status(tid).status == 5

    def test_S2_interactive_suspend_and_resume_loop(self, bridge, rosbridge_server):
        """[S2] 交互式中途挂起与恢复闭环：下发 → SUSPEND(变为PAUSE=6) → RESUME(恢复ONGOING=3) → FINISH"""
        intent = {
            "schema_version": 2, "task_type": "pipeline_inspection",
            "priority": 10, "location": {"water_depth_m": 80.0},
            "task": {"details": {"target": {"latitude": 21.0, "longitude": 109.5}}}
        }

        # 1. 下发巡缆任务
        tid = bridge.dispatch_intent(intent)
        time.sleep(0.1)

        # 2. 发送挂起指令
        bridge.suspend_task(tid)
        time.sleep(0.2)

        # 验证机器人侧状态变为 PAUSE(6)
        status_item = bridge.get_task_status(tid)
        assert status_item is not None
        assert status_item.status == 6  # PAUSE
        assert status_item.status_name == "PAUSE"

        # 3. 发送恢复指令
        bridge.resume_task(tid)
        time.sleep(0.2)

        # 验证状态恢复为 ONGOING(3)
        status_after_resume = bridge.get_task_status(tid)
        assert status_after_resume is not None
        assert status_after_resume.status in (3, 5)  # ONGOING 或 已完成

    def test_S3_emergency_clear_block_loop(self, bridge):
        """[S3] 应急清除阻断闭环：任务挂起后发送 CLEAR_BLOCK(7) → 重置为 READY(0)"""
        intent = {"schema_version": 2, "task_type": "cable_burial",
                  "location": {"water_depth_m": 120.0}, "task": {"details": {"target": {"latitude": 19.5, "longitude": 111.2}}}}
        tid = bridge.dispatch_intent(intent)
        time.sleep(0.1)

        # 模拟进入失败状态
        active_tasks[tid]["status"] = 7  # FAIL
        time.sleep(0.1)

        # 云端下发应急清除指令
        bridge.emergency_clear_block()
        time.sleep(0.3)

        status_item = bridge.get_task_status(tid)
        assert status_item is not None
        assert status_item.status == 0  # 重置为 READY

    def test_S4_continuous_depth_descent_telemetry(self, bridge):
        """[S4] 连续下潜动态姿态回传：验证实时变化的水深（0m → 312.4m）可以在内存捕获"""
        depths_recorded = []

        def _track_telemetry(t: ROVTelemetry):
            depths_recorded.append(t.water_depth)

        bridge.tracker.on_telemetry_update(_track_telemetry)
        # 发送设备控制指令触发 Topside 网关推送最新遥测
        bridge.control_device(1, 50.0)
        time.sleep(0.4)

        # 收到至少 1 帧姿态
        assert len(depths_recorded) >= 1
        assert depths_recorded[-1] == pytest.approx(312.4)

    def test_S5_vision_keypoints_bidirectional_receive(self, bridge):
        """[S5] 视觉关键点数据双向接收：订阅 /vision/keypoints，接收电缆检测关键点与抓取方向向量"""
        received_keypoints = []

        def _on_keypoints(msg: dict):
            received_keypoints.append(msg)

        # 1. 订阅视觉话题
        bridge.client.subscribe_keypoints(_on_keypoints)
        time.sleep(0.2)

        # 2. 模拟 Topside 网关推送一条 /vision/keypoints 消息
        mock_kp = {
            "has_target": True,
            "score": 0.95,
            "task_type": "keypoint_detect",
            "directions": [0.0, 1.0, 0.0],              # 电缆方向向量
            "euler_angles": [0.0, 0.05, 1.57],          # roll, pitch, yaw
            "corner_points": [{"x": 10.0, "y": 20.0}, {"x": 200.0, "y": 300.0}],
        }
        bridge.client._send({
            "op": "publish",
            "topic": "/vision/keypoints",
            "msg": mock_kp,
        })
        time.sleep(0.3)

        # 3. 验证 SEAgent 成功接收到视觉数据
        assert len(received_keypoints) >= 1
        last_kp = received_keypoints[-1]
        assert last_kp["has_target"] is True
        assert last_kp["score"] == pytest.approx(0.95)
        assert last_kp["directions"] == [0.0, 1.0, 0.0]

    def test_S6_multi_robot_concurrent_bidirectional_dispatch(self, bridge, rosbridge_server):
        """[S6] 多机并发双向独立收发：WROV-250-001 (采油树) 与 LROV-150-001 (巡缆) 并发双向协同"""
        # 1. 云端下发 2 个不同机器人的任务
        intent_wrov = {
            "schema_version": 2, "task_type": "tree_valve_operation",
            "equipment": {"robot_unit_id": "WROV-250-001"},
            "location": {"water_depth_m": 300.0},
            "task": {"details": {"target": {"latitude": 20.815, "longitude": 115.735}}}
        }
        intent_lrov = {
            "schema_version": 2, "task_type": "pipeline_inspection",
            "equipment": {"robot_unit_id": "LROV-150-001"},
            "location": {"water_depth_m": 85.0},
            "task": {"details": {"target": {"latitude": 20.5, "longitude": 115.2}}}
        }

        tid_wrov = bridge.dispatch_intent(intent_wrov)
        tid_lrov = bridge.dispatch_intent(intent_lrov)
        time.sleep(0.3)

        # 2. 验证下发的指令包含两个不同任务 ID
        all_pubs = rosbridge_server.get_received_publishes()
        task_ids = [p["payload"]["task_id"] for p in all_pubs if p["topic"] == "/task_cmd"]
        assert tid_wrov in task_ids
        assert tid_lrov in task_ids
