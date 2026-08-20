"""
test_architecture_validation.py
================================
基于确定架构的全面集成验证测试

确定架构：
    [云端 SEAgent] ──WebSocket──> [Topside rosbridge] ──rclpy/DDS──> [水下 ROV ROS2]

测试分层：
    G: WebSocket/rosbridge 协议层（robotmcp.WebSocketManager）
       验证云端-岸基通信的正确性
    H: 任务下发完整链路（SEAgent TaskIntent → rosbridge JSON → ROV /task_cmd）
    I: 遥测回传完整链路（ROV /task/system_status → rosbridge → SEAgent StateInfo）
    J: 完整往返闭环（下发 + 回传 + 数据隔离）
"""

import json
import sys
import time
import threading
import pytest
from pathlib import Path

SEAGENT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SEAGENT_ROOT))
sys.path.insert(0, str(MCP_DIR))

from mock_rosbridge_server import MockRosbridgeServer


# ============================================================================
# 测试数据
# ============================================================================

ROSBRIDGE_PORT = 9091  # 避免与真实 rosbridge 9090 冲突

TASK_TYPE_MAPPING = {
    "pipeline_inspection": 2,
    "cable_burial": 1,
    "valve_operation": 4,
    "tree_valve_operation": 4,
    "underwater_move": 5,
}


def _make_task_intent(task_type="tree_valve_operation", depth=300.0,
                      lat=20.815, lon=115.735, oilfield="流花11-1油田"):
    return {
        "schema_version": 2,
        "task_id": "CT-20260820-001",
        "task_type": task_type,
        "priority": 7,
        "location": {"oilfield": oilfield, "water_depth_m": depth},
        "task": {
            "type": task_type,
            "details": {
                "target": {"latitude": lat, "longitude": lon},
            },
        },
        "equipment": {
            "robot_type": "work_class_rov",
            "payload": ["液压机械臂", "双目视觉"],
            "support_vessel": {"name": "海洋石油681"},
        },
        "conditions": {"validation": {"overall_status": "valid"}},
    }


def _intent_to_syscmd(intent: dict) -> dict:
    """SEAgent 适配层：TaskIntent v2 → SysTaskCmd（对应 seagent_mcp_adapter.dispatch_task_intent 逻辑）"""
    task_type_str = intent.get("task_type", "")
    task_cmd_type = TASK_TYPE_MAPPING.get(task_type_str, 5)
    coords = intent.get("task", {}).get("details", {}).get("target", {})
    depth = float(intent.get("location", {}).get("water_depth_m", 0.0))
    return {
        "task_type": task_cmd_type,
        "task_id": 0x80001,
        "frame_id": "odom",
        "priority": 15,
        "pos_target": [{
            "position": {"x": coords.get("longitude", 0.0),
                         "y": coords.get("latitude", 0.0),
                         "z": -depth},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        }],
        "params": [depth, 1.5],
        "fail_stop": True,
    }


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(scope="module")
def rosbridge_server():
    """启动 Mock rosbridge WebSocket 服务器，模块级别复用"""
    srv = MockRosbridgeServer(port=ROSBRIDGE_PORT)
    srv.start()
    time.sleep(0.3)  # 等待 server 就绪
    yield srv
    srv.stop()


@pytest.fixture(autouse=True)
def clear_publishes(rosbridge_server):
    """每个测试前清空接收记录"""
    from mock_rosbridge_server import received_publishes
    received_publishes.clear()
    yield


@pytest.fixture
def ws_manager(rosbridge_server):
    """创建 robotmcp WebSocketManager 连接到 Mock rosbridge"""
    from ros_mcp.utils.websocket import WebSocketManager
    mgr = WebSocketManager("127.0.0.1", ROSBRIDGE_PORT, default_timeout=3.0)
    yield mgr
    mgr.close()


@pytest.fixture
def state_info(tmp_path):
    """创建临时 SEAgent RobotStateInfo"""
    sys.path.insert(0, str(SEAGENT_ROOT))
    from src.state_info import RobotStateInfo
    sys.path.pop(0)
    state_file = tmp_path / "state.yaml"
    state_file.write_text("store_version: 0\nrobots: {}\n", encoding="utf-8")
    fleet_file = SEAGENT_ROOT / "config" / "robot_fleet.yaml"
    return RobotStateInfo(state_file=state_file, fleet_file=fleet_file)


# ============================================================================
# [G] WebSocket / rosbridge 协议层测试
# ============================================================================

class TestWebSocketRosbridgeLayer:
    """验证 robotmcp WebSocketManager 与 Mock rosbridge 的通信正确性"""

    def test_G1_connect_to_rosbridge(self, ws_manager):
        """[G1] WebSocketManager 应能成功连接 Mock rosbridge（模拟云端→Topside 握手）"""
        err = ws_manager.connect()
        assert err is None, f"WebSocket 连接失败: {err}"

    def test_G2_publish_task_cmd_format(self, ws_manager, rosbridge_server):
        """[G2] 向 /task_cmd 发布 rosbridge publish 消息，格式应符合 rosbridge v2.0"""
        syscmd = _intent_to_syscmd(_make_task_intent())
        msg = {"op": "publish", "topic": "/task_cmd", "msg": syscmd}

        err = ws_manager.send(msg)
        assert err is None, f"WebSocket send 失败: {err}"

        # 等待服务端处理
        time.sleep(0.2)
        publishes = rosbridge_server.get_received_publishes()
        assert len(publishes) >= 1
        last = publishes[-1]
        assert last["topic"] == "/task_cmd"
        assert last["payload"]["task_type"] == 4
        assert last["payload"]["frame_id"] == "odom"

    def test_G3_subscribe_and_receive_telemetry(self, ws_manager):
        """[G3] 订阅 /task/system_status，应收到 Mock rosbridge 推送的遥测数据"""
        sub_msg = {"op": "subscribe", "topic": "/task/system_status", "type": "std_msgs/String"}
        ws_manager.send(sub_msg)

        raw = ws_manager.receive(timeout=3.0)
        assert raw is not None, "未收到任何数据"

        data = json.loads(raw)
        assert data.get("op") == "publish"
        assert data.get("topic") == "/task/system_status"
        robots = data.get("msg", {}).get("fleet_status", {})
        assert "WROV-250-001" in robots

    def test_G4_request_response_roundtrip(self, ws_manager):
        """[G4] request() 方法应完成发送+接收的完整往返"""
        syscmd = _intent_to_syscmd(_make_task_intent())
        msg = {"op": "publish", "topic": "/task_cmd", "msg": syscmd}
        response = ws_manager.request(msg, timeout=3.0)
        # Mock server 回复 ack
        assert "error" not in response, f"request 失败: {response}"

    def test_G5_websocket_connection_survives_multiple_sends(self, ws_manager, rosbridge_server):
        """[G5] 连续发送多条指令，WebSocket 连接应保持稳定"""
        for i in range(3):
            intent = _make_task_intent(depth=float(100 + i * 50))
            syscmd = _intent_to_syscmd(intent)
            msg = {"op": "publish", "topic": "/task_cmd", "msg": syscmd}
            err = ws_manager.send(msg)
            assert err is None, f"第 {i+1} 条发送失败: {err}"

        time.sleep(0.3)
        publishes = rosbridge_server.get_received_publishes()
        assert len(publishes) == 3


# ============================================================================
# [H] 任务下发完整链路测试
# ============================================================================

class TestTaskDispatchFullChain:
    """验证 SEAgent → rosbridge WebSocket → ROV /task_cmd 完整下发链路"""

    def test_H1_tree_valve_operation_chain(self, ws_manager, rosbridge_server):
        """[H1] 采油树阀门操作：TaskIntent v2 → SysTaskCmd → rosbridge → ROV"""
        intent = _make_task_intent("tree_valve_operation", depth=300.0, lat=20.815, lon=115.735)
        syscmd = _intent_to_syscmd(intent)

        ws_manager.send({"op": "publish", "topic": "/task_cmd", "msg": syscmd})
        time.sleep(0.2)

        received = rosbridge_server.get_received_publishes()
        assert len(received) == 1
        cmd = received[0]["payload"]

        assert cmd["task_type"] == 4            # tree_valve_operation
        assert cmd["pos_target"][0]["position"]["z"] == pytest.approx(-300.0)
        assert cmd["pos_target"][0]["position"]["x"] == pytest.approx(115.735)
        assert cmd["pos_target"][0]["position"]["y"] == pytest.approx(20.815)
        assert cmd["fail_stop"] is True
        assert cmd["frame_id"] == "odom"
        assert cmd["params"][0] == pytest.approx(300.0)
        assert cmd["params"][1] == pytest.approx(1.5)

    def test_H2_pipeline_inspection_chain(self, ws_manager, rosbridge_server):
        """[H2] 管道巡检：不同任务类型下发验证"""
        intent = _make_task_intent("pipeline_inspection", depth=80.0, lat=21.0, lon=109.5,
                                   oilfield="涠洲油田")
        syscmd = _intent_to_syscmd(intent)

        ws_manager.send({"op": "publish", "topic": "/task_cmd", "msg": syscmd})
        time.sleep(0.2)

        cmd = rosbridge_server.get_received_publishes()[-1]["payload"]
        assert cmd["task_type"] == 2            # pipeline_inspection
        assert cmd["pos_target"][0]["position"]["z"] == pytest.approx(-80.0)
        assert cmd["pos_target"][0]["position"]["x"] == pytest.approx(109.5)

    def test_H3_cable_burial_chain(self, ws_manager, rosbridge_server):
        """[H3] 电缆埋设：task_type=1 验证"""
        intent = _make_task_intent("cable_burial", depth=120.0, lat=19.5, lon=111.2)
        syscmd = _intent_to_syscmd(intent)

        ws_manager.send({"op": "publish", "topic": "/task_cmd", "msg": syscmd})
        time.sleep(0.2)

        cmd = rosbridge_server.get_received_publishes()[-1]["payload"]
        assert cmd["task_type"] == 1            # cable_burial
        assert cmd["pos_target"][0]["position"]["z"] == pytest.approx(-120.0)

    def test_H4_syscmd_required_fields_all_present(self, ws_manager, rosbridge_server):
        """[H4] 任何任务类型生成的 SysTaskCmd 必须含全部 7 个必填字段"""
        required = {"task_type", "task_id", "frame_id", "priority",
                    "pos_target", "params", "fail_stop"}
        for task_type in TASK_TYPE_MAPPING:
            intent = _make_task_intent(task_type, depth=200.0)
            syscmd = _intent_to_syscmd(intent)
            missing = required - syscmd.keys()
            assert not missing, f"{task_type}: SysTaskCmd 缺少字段 {missing}"

    def test_H5_depth_always_negative_z(self, ws_manager, rosbridge_server):
        """[H5] 水深（正值）应始终转换为 pos_target.position.z 的负值"""
        for depth in [50.0, 200.0, 500.0, 1000.0]:
            intent = _make_task_intent(depth=depth)
            syscmd = _intent_to_syscmd(intent)
            z = syscmd["pos_target"][0]["position"]["z"]
            assert z == pytest.approx(-depth), f"depth={depth} → z={z} 错误"

    def test_H6_rosbridge_records_timestamp(self, ws_manager, rosbridge_server):
        """[H6] Mock rosbridge 应为每条指令记录接收时间戳"""
        from datetime import datetime
        intent = _make_task_intent()
        ws_manager.send({"op": "publish", "topic": "/task_cmd", "msg": _intent_to_syscmd(intent)})
        time.sleep(0.2)

        record = rosbridge_server.get_received_publishes()[-1]
        ts = record.get("received_at")
        assert ts is not None
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed is not None


# ============================================================================
# [I] 遥测回传完整链路测试
# ============================================================================

class TestTelemetryFeedbackFullChain:
    """验证 ROV /task/system_status → rosbridge → SEAgent StateInfo 完整回传链路"""

    def _sync_telemetry(self, ws_manager, state_info):
        """通过 WebSocket 订阅遥测并写入 SEAgent StateInfo"""
        sys.path.insert(0, str(SEAGENT_ROOT))
        from datetime import datetime, timezone
        sys.path.pop(0)

        sub = {"op": "subscribe", "topic": "/task/system_status"}
        ws_manager.send(sub)
        raw = ws_manager.receive(timeout=3.0)
        assert raw is not None, "未收到遥测推送"

        data = json.loads(raw)
        robots = data.get("msg", {}).get("fleet_status", {})

        for unit_id, rdata in robots.items():
            now_str = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            try:
                state_info.set_status(equipment_name=unit_id, params={
                    "status": "online" if rdata.get("online") else "offline",
                    "battery_level": rdata.get("battery_percentage"),
                    "water_depth": rdata.get("current_depth"),
                    "update_timestamp": now_str,
                    "updated_at": now_str,
                })
            except Exception:
                pass
        return robots

    def test_I1_telemetry_received_from_rosbridge(self, ws_manager, state_info):
        """[I1] 从 Mock rosbridge 接收遥测，数据应包含 2 台机器人"""
        robots = self._sync_telemetry(ws_manager, state_info)
        assert "WROV-250-001" in robots
        assert "LROV-150-001" in robots

    def test_I2_depth_written_to_state_info(self, ws_manager, state_info):
        """[I2] WROV-250-001 水深 312.4m 应正确写入 SEAgent StateInfo"""
        self._sync_telemetry(ws_manager, state_info)
        snapshot = state_info.get_unit_state_snapshot("WROV-250-001")
        assert snapshot["state"]["water_depth"] == pytest.approx(312.4)

    def test_I3_battery_written_to_state_info(self, ws_manager, state_info):
        """[I3] WROV-250-001 电量 94.5% 应正确写入 SEAgent StateInfo"""
        self._sync_telemetry(ws_manager, state_info)
        snapshot = state_info.get_unit_state_snapshot("WROV-250-001")
        assert snapshot["state"]["battery_level"] == pytest.approx(94.5)

    def test_I4_online_status_written_to_state_info(self, ws_manager, state_info):
        """[I4] online=True 应写入 SEAgent StateInfo 为 status='online'"""
        self._sync_telemetry(ws_manager, state_info)
        snapshot = state_info.get_unit_state_snapshot("WROV-250-001")
        assert snapshot["state"]["status"] == "online"

    def test_I5_lrov_telemetry_written(self, ws_manager, state_info):
        """[I5] LROV-150-001 水深 85m、电量 88% 应正确写入 StateInfo"""
        self._sync_telemetry(ws_manager, state_info)
        snapshot = state_info.get_unit_state_snapshot("LROV-150-001")
        assert snapshot["state"]["water_depth"] == pytest.approx(85.0)
        assert snapshot["state"]["battery_level"] == pytest.approx(88.0)


# ============================================================================
# [J] 完整往返闭环测试
# ============================================================================

class TestFullRoundTrip:
    """完整闭环：遥测回传建立感知 + 任务下发 + 数据严格隔离"""

    def _sync_telemetry(self, ws_manager, state_info):
        sys.path.insert(0, str(SEAGENT_ROOT))
        from datetime import datetime, timezone
        sys.path.pop(0)
        sub = {"op": "subscribe", "topic": "/task/system_status"}
        ws_manager.send(sub)
        raw = ws_manager.receive(timeout=3.0)
        robots = json.loads(raw).get("msg", {}).get("fleet_status", {})
        for unit_id, rdata in robots.items():
            now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            try:
                state_info.set_status(equipment_name=unit_id, params={
                    "status": "online" if rdata.get("online") else "offline",
                    "battery_level": rdata.get("battery_percentage"),
                    "water_depth": rdata.get("current_depth"),
                    "update_timestamp": now, "updated_at": now,
                })
            except Exception:
                pass
        return robots

    def test_J1_telemetry_then_dispatch_data_isolation(self, ws_manager, rosbridge_server, state_info):
        """[J1] 先回传遥测（水深312.4m），再下发任务（规划水深300m），两个水深互不干扰"""
        # Step 1: 遥测回传
        robots = self._sync_telemetry(ws_manager, state_info)
        assert robots["WROV-250-001"]["current_depth"] == pytest.approx(312.4)

        # Step 2: 下发任务（规划水深 300m）
        intent = _make_task_intent("tree_valve_operation", depth=300.0)
        syscmd = _intent_to_syscmd(intent)
        ws_manager.send({"op": "publish", "topic": "/task_cmd", "msg": syscmd})
        time.sleep(0.2)

        # Step 3: 验证遥测未被任务数据污染
        snapshot = state_info.get_unit_state_snapshot("WROV-250-001")
        assert snapshot["state"]["water_depth"] == pytest.approx(312.4)  # 仍是遥测值

        # Step 4: 验证 SysTaskCmd 坐标来自 TaskIntent（不是遥测）
        cmd = rosbridge_server.get_received_publishes()[-1]["payload"]
        assert cmd["pos_target"][0]["position"]["z"] == pytest.approx(-300.0)  # 规划值

    def test_J2_multi_task_sequential_dispatch(self, ws_manager, rosbridge_server, state_info):
        """[J2] 连续下发 3 个不同任务，每个任务的 SysTaskCmd 独立正确"""
        tasks = [
            ("tree_valve_operation", 300.0, 4),
            ("pipeline_inspection",  80.0,  2),
            ("cable_burial",         120.0, 1),
        ]
        for task_type, depth, expected_type in tasks:
            intent = _make_task_intent(task_type, depth=depth)
            syscmd = _intent_to_syscmd(intent)
            ws_manager.send({"op": "publish", "topic": "/task_cmd", "msg": syscmd})
            time.sleep(0.15)

        all_cmds = rosbridge_server.get_received_publishes()
        assert len(all_cmds) == 3

        for i, (task_type, depth, expected_type) in enumerate(tasks):
            cmd = all_cmds[i]["payload"]
            assert cmd["task_type"] == expected_type, \
                f"第{i+1}条: task_type 期望{expected_type}, 实际{cmd['task_type']}"
            assert cmd["pos_target"][0]["position"]["z"] == pytest.approx(-depth), \
                f"第{i+1}条: z 期望{-depth}, 实际{cmd['pos_target'][0]['position']['z']}"

    def test_J3_telemetry_reflects_realtime_not_planned(self, ws_manager, state_info):
        """[J3] StateInfo 中的水深应始终是遥测值（312.4m），与下发的规划水深（300m）严格分离"""
        self._sync_telemetry(ws_manager, state_info)

        for depth in [100.0, 200.0, 500.0]:
            intent = _make_task_intent(depth=depth)
            syscmd = _intent_to_syscmd(intent)
            ws_manager.send({"op": "publish", "topic": "/task_cmd", "msg": syscmd})
            time.sleep(0.1)
            # 每次下发后，StateInfo 水深不变（仍是遥测值）
            snapshot = state_info.get_unit_state_snapshot("WROV-250-001")
            assert snapshot["state"]["water_depth"] == pytest.approx(312.4), \
                f"下发 depth={depth} 后 StateInfo 水深被污染为 {snapshot['state']['water_depth']}"

    def test_J4_full_architecture_summary(self, ws_manager, rosbridge_server, state_info):
        """[J4] 完整架构验证：1次遥测回传 + 2次任务下发，最终状态一致"""
        # 遥测
        self._sync_telemetry(ws_manager, state_info)

        # 两次下发
        for task_type, depth in [("tree_valve_operation", 300.0), ("pipeline_inspection", 80.0)]:
            ws_manager.send({
                "op": "publish",
                "topic": "/task_cmd",
                "msg": _intent_to_syscmd(_make_task_intent(task_type, depth=depth)),
            })
            time.sleep(0.15)

        # 验证最终状态
        cmds = rosbridge_server.get_received_publishes()
        assert len(cmds) == 2
        assert cmds[0]["payload"]["task_type"] == 4
        assert cmds[0]["payload"]["pos_target"][0]["position"]["z"] == pytest.approx(-300.0)
        assert cmds[1]["payload"]["task_type"] == 2
        assert cmds[1]["payload"]["pos_target"][0]["position"]["z"] == pytest.approx(-80.0)

        # StateInfo 水深仍是遥测值
        snapshot = state_info.get_unit_state_snapshot("WROV-250-001")
        assert snapshot["state"]["water_depth"] == pytest.approx(312.4)
        assert snapshot["state"]["battery_level"] == pytest.approx(94.5)
