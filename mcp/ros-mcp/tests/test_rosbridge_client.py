"""
test_rosbridge_client.py
=========================
针对 RosbridgeClient + TaskStatusTracker + 完整内部协议的测试套件

验证范围：
  K: rosbridge_client 协议构造（无网络，纯数据层）
  L: RosbridgeClient WebSocket 任务下发（连接 Mock rosbridge）
  M: TASK_MANAGE 任务管理指令（挂起/恢复/删除/清除阻塞）
  N: CTRL_TASK / AUV_TASK / sys_config 专项指令
  O: TaskStatusTracker 遥测解析与任务状态追踪
  P: 完整闭环（下发 → 追踪状态变化 → FINISH）
"""

import json
import sys
import time
import pytest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
MCP_DIR = TESTS_DIR.parent
CORE_DIR = MCP_DIR / "core"
MOCK_DIR = MCP_DIR / "mock"
SEAGENT_ROOT = MCP_DIR.parent

for p in [TESTS_DIR, CORE_DIR, MOCK_DIR, MCP_DIR, SEAGENT_ROOT]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from mcp.shim.rosbridge_client import (
    TaskType, TaskManageAction, PilotMode, TaskStatus,
    SysTaskCmd, Pose,
    intent_to_syscmd, build_task_manage_cmd,
    generate_task_id, SEAGENT_TO_ROS2_TASK_TYPE,
)
from mcp.shim.mock_rosbridge_server import MockRosbridgeServer, received_publishes, active_tasks

ROSBRIDGE_PORT = 9092  # 与 test_architecture_validation.py (9091) 隔离


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(scope="module")
def rosbridge_server():
    srv = MockRosbridgeServer(port=ROSBRIDGE_PORT)
    srv.start()
    time.sleep(0.3)
    yield srv
    srv.stop()


@pytest.fixture(autouse=True)
def clear_state(rosbridge_server):
    """每个测试前清空共享状态"""
    received_publishes.clear()
    active_tasks.clear()
    yield


@pytest.fixture
def ws_client(rosbridge_server):
    """已连接的 RosbridgeClient 实例"""
    from mcp.shim.rosbridge_client import RosbridgeClient
    client = RosbridgeClient("127.0.0.1", ROSBRIDGE_PORT, connect_timeout=3.0)
    client.connect()
    yield client
    client.disconnect()


# ============================================================================
# 测试数据工厂
# ============================================================================

def _intent(task_type="tree_valve_operation", depth=300.0, lat=20.815, lon=115.735,
            priority=15, fail_stop=True):
    details = {
        "target": {"latitude": lat, "longitude": lon},
        "speed_ms": 1.5,
    }
    if task_type == "pipeline_inspection":
        details = {
            "start_point": {"latitude": lat, "longitude": lon},
            "end_point": {"latitude": lat + 0.1, "longitude": lon + 0.2},
        }
    return {
        "schema_version": 2,
        "task_type": task_type,
        "priority": priority,
        "fail_stop": fail_stop,
        "location": {"oilfield": "流花11-1油田", "water_depth_m": depth},
        "task": {"type": task_type, "details": details},
        "equipment": {"robot_type": "work_class_rov"},
    }


def _auv_intent(waypoints=None, **auv_params):
    wps = waypoints or [
        {"latitude": 20.0, "longitude": 115.0, "depth": 50.0},
        {"latitude": 20.5, "longitude": 115.5, "depth": 80.0},
    ]
    return {
        "schema_version": 2,
        "task_type": "auv_mission",
        "priority": 10,
        "fail_stop": False,
        "location": {"water_depth_m": 80.0},
        "task": {"type": "auv_mission", "details": {
            "waypoints": wps,
            "auv_params": {
                "speed_ms":     auv_params.get("speed_ms",     1.2),
                "dive_angle":   auv_params.get("dive_angle",   0.3),
                "ascend_angle": auv_params.get("ascend_angle", 0.3),
                "auto_return":  auv_params.get("auto_return",  1.0),
                "return_depth": auv_params.get("return_depth", 5.0),
                "return_speed": auv_params.get("return_speed", 0.8),
            },
        }},
    }


# ============================================================================
# [K] 协议构造层测试（纯数据，无网络）
# ============================================================================

class TestProtocolConstruction:
    """验证 rosbridge_client 的数据层转换逻辑与内部协议对齐"""

    def test_K1_task_type_enum_values(self):
        """[K1] TaskType 枚举值与 UI接口协议.md 完全一致"""
        assert TaskType.TASK_MANAGE == 0
        assert TaskType.CLAMP_CABLE == 1
        assert TaskType.SEARCH_CABLE == 2
        assert TaskType.CLAMP_PIN == 3
        assert TaskType.INSERT_PLUG == 4
        assert TaskType.MOVE_TASK == 5
        assert TaskType.CTRL_TASK == 6
        assert TaskType.AUV_TASK == 10

    def test_K2_seagent_task_type_mapping_complete(self):
        """[K2] SEAgent 所有语义任务类型都应有对应映射"""
        for name, ros2_type in SEAGENT_TO_ROS2_TASK_TYPE.items():
            assert isinstance(ros2_type, TaskType), f"{name} 映射不是 TaskType 实例"

    def test_K3_intent_to_syscmd_tree_valve(self):
        """[K3] 采油树阀门 → INSERT_PLUG=4, z=-300.0, fail_stop=True"""
        cmd = intent_to_syscmd(_intent("tree_valve_operation", depth=300.0))
        assert cmd.task_type == 4
        assert cmd.pos_target[0].z == pytest.approx(-300.0)
        assert cmd.pos_target[0].x == pytest.approx(115.735)
        assert cmd.fail_stop is True
        assert cmd.priority == 15

    def test_K4_intent_to_syscmd_pipeline_inspection(self):
        """[K4] 管道巡检 → SEARCH_CABLE=2, z=-80.0"""
        cmd = intent_to_syscmd(_intent("pipeline_inspection", depth=80.0))
        assert cmd.task_type == 2
        assert cmd.pos_target[0].z == pytest.approx(-80.0)

    def test_K5_intent_to_syscmd_cable_burial(self):
        """[K5] 电缆埋设 → CLAMP_CABLE=1"""
        cmd = intent_to_syscmd(_intent("cable_burial", depth=120.0))
        assert cmd.task_type == 1

    def test_K6_intent_to_syscmd_ctrl_task(self):
        """[K6] 灯光控制 → CTRL_TASK=6, pos_target 为空, params=[device_id, value]"""
        intent = {
            "schema_version": 2,
            "task_type": "light_control",
            "priority": 15,
            "fail_stop": False,
            "location": {"water_depth_m": 0.0},
            "task": {"type": "light_control", "details": {
                "control": {"device_id": 1, "value": 50.0},
            }},
        }
        cmd = intent_to_syscmd(intent)
        assert cmd.task_type == 6
        assert cmd.pos_target == []
        assert cmd.params[0] == pytest.approx(1.0)   # device_id
        assert cmd.params[1] == pytest.approx(50.0)  # value (PWM 50%)

    def test_K7_intent_to_syscmd_auv_task(self):
        """[K7] AUV 任务 → AUV_TASK=10, 6 个 params, 多航线 pos_target"""
        cmd = intent_to_syscmd(_auv_intent(speed_ms=1.2, auto_return=1.0))
        assert cmd.task_type == 10
        assert len(cmd.pos_target) == 2           # 两个航线关键点
        assert len(cmd.params) == 6               # 6 个 AUV 参数
        assert cmd.params[0] == pytest.approx(1.2)  # speed_ms
        assert cmd.params[3] == pytest.approx(1.0)  # auto_return

    def test_K8_task_id_is_ai_prefix(self):
        """[K8] AI 生成的 task_id 必须在 0x80001~0x8FFFF 范围内"""
        for _ in range(20):
            tid = generate_task_id()
            assert 0x80001 <= tid <= 0x8FFFF, f"task_id 0x{tid:X} 超出 AI 范围"

    def test_K9_build_task_manage_suspend(self):
        """[K9] TASK_MANAGE 挂起指令：priority=0, params=[0, target_id]"""
        cmd = build_task_manage_cmd(TaskManageAction.SUSPEND, target_task_id=0x80001)
        assert cmd.task_type == 0
        assert cmd.priority == 0
        assert cmd.params[0] == pytest.approx(0.0)      # SUSPEND
        assert cmd.params[1] == pytest.approx(0x80001)  # target_id

    def test_K10_build_task_manage_clear_block(self):
        """[K10] 清除阻塞：params=[7], 无 target_id"""
        cmd = build_task_manage_cmd(TaskManageAction.CLEAR_BLOCK)
        assert cmd.task_type == 0
        assert cmd.params[0] == pytest.approx(7.0)
        assert len(cmd.params) == 1

    def test_K11_syscmd_to_dict_structure(self):
        """[K11] SysTaskCmd.to_dict() 产生的字典必须含全部 7 个 SysTaskCmd.msg 必填字段"""
        cmd = intent_to_syscmd(_intent())
        d = cmd.to_dict()
        required = {"task_type", "task_id", "frame_id", "priority", "pos_target", "params", "fail_stop"}
        assert required.issubset(d.keys())
        assert isinstance(d["pos_target"], list)
        assert "position" in d["pos_target"][0]
        assert "orientation" in d["pos_target"][0]

    def test_K12_depth_always_negative_z(self):
        """[K12] 任意正水深必须转换为 pos_target.position.z 的负值"""
        for depth in [50.0, 150.0, 300.0, 1000.0]:
            cmd = intent_to_syscmd(_intent(depth=depth))
            assert cmd.pos_target[0].z == pytest.approx(-depth)


# ============================================================================
# [L] RosbridgeClient WebSocket 任务下发
# ============================================================================

class TestRosbridgeClientDispatch:
    """验证通过真实 WebSocket 连接向 Mock rosbridge 下发任务"""

    def test_L1_connect_success(self, ws_client):
        """[L1] RosbridgeClient 应成功连接到 Mock rosbridge"""
        assert ws_client.is_connected()

    def test_L2_publish_tree_valve_operation(self, ws_client, rosbridge_server):
        """[L2] 下发采油树阀门任务，Mock rosbridge 应收到正确 SysTaskCmd"""
        tid = ws_client.publish_task_cmd(_intent("tree_valve_operation", depth=300.0))
        time.sleep(0.3)

        publishes = rosbridge_server.get_received_publishes()
        assert len(publishes) >= 1
        cmd = publishes[-1]["payload"]
        assert cmd["task_type"] == 4
        assert cmd["pos_target"][0]["position"]["z"] == pytest.approx(-300.0)
        assert cmd["fail_stop"] is True
        assert cmd["task_id"] == tid

    def test_L3_publish_pipeline_inspection(self, ws_client, rosbridge_server):
        """[L3] 下发管道巡检任务，task_type=2"""
        ws_client.publish_task_cmd(_intent("pipeline_inspection", depth=80.0, lon=109.5))
        time.sleep(0.2)
        cmd = rosbridge_server.get_received_publishes()[-1]["payload"]
        assert cmd["task_type"] == 2
        assert cmd["pos_target"][0]["position"]["z"] == pytest.approx(-80.0)
        assert cmd["pos_target"][0]["position"]["x"] == pytest.approx(109.5)

    def test_L4_publish_cable_burial(self, ws_client, rosbridge_server):
        """[L4] 下发电缆埋设任务，task_type=1"""
        ws_client.publish_task_cmd(_intent("cable_burial", depth=120.0))
        time.sleep(0.2)
        cmd = rosbridge_server.get_received_publishes()[-1]["payload"]
        assert cmd["task_type"] == 1

    def test_L5_sequential_dispatch_maintains_order(self, ws_client, rosbridge_server):
        """[L5] 连续下发 3 个不同任务，Mock rosbridge 按序接收"""
        specs = [
            ("tree_valve_operation", 300.0, 4),
            ("pipeline_inspection",  80.0,  2),
            ("cable_burial",         120.0, 1),
        ]
        for task_type, depth, _ in specs:
            ws_client.publish_task_cmd(_intent(task_type, depth=depth))
            time.sleep(0.1)

        all_cmds = rosbridge_server.get_received_publishes()
        assert len(all_cmds) == 3
        for i, (_, depth, expected_type) in enumerate(specs):
            assert all_cmds[i]["payload"]["task_type"] == expected_type
            assert all_cmds[i]["payload"]["pos_target"][0]["position"]["z"] == pytest.approx(-depth)


# ============================================================================
# [M] TASK_MANAGE 任务管理指令
# ============================================================================

class TestTaskManageCommands:
    """验证 TASK_MANAGE 任务生命周期管理指令"""

    def test_M1_suspend_task(self, ws_client, rosbridge_server):
        """[M1] 下发任务后挂起它，Mock rosbridge 收到 TASK_MANAGE(0, task_id)"""
        tid = ws_client.publish_task_cmd(_intent())
        time.sleep(0.2)
        ws_client.suspend_task(tid)
        time.sleep(0.2)

        cmds = rosbridge_server.get_received_publishes()
        manage_cmds = [c for c in cmds if c["payload"]["task_type"] == 0]
        assert len(manage_cmds) >= 1
        params = manage_cmds[-1]["payload"]["params"]
        assert params[0] == pytest.approx(0.0)   # SUSPEND
        assert params[1] == pytest.approx(float(tid))

    def test_M2_resume_task(self, ws_client, rosbridge_server):
        """[M2] 恢复指定任务，params[0]=1"""
        tid = ws_client.publish_task_cmd(_intent())
        time.sleep(0.1)
        ws_client.suspend_task(tid)
        time.sleep(0.1)
        ws_client.resume_task(tid)
        time.sleep(0.2)

        cmds = rosbridge_server.get_received_publishes()
        manage_cmds = [c for c in cmds if c["payload"]["task_type"] == 0]
        resume_cmd = [c for c in manage_cmds if c["payload"]["params"][0] == pytest.approx(1.0)]
        assert len(resume_cmd) >= 1

    def test_M3_delete_task(self, ws_client, rosbridge_server):
        """[M3] 删除指定任务，params[0]=4"""
        tid = ws_client.publish_task_cmd(_intent())
        time.sleep(0.1)
        ws_client.delete_task(tid)
        time.sleep(0.2)

        cmds = rosbridge_server.get_received_publishes()
        delete_cmds = [c for c in cmds if c["payload"]["task_type"] == 0
                       and c["payload"]["params"][0] == pytest.approx(4.0)]
        assert len(delete_cmds) >= 1

    def test_M4_suspend_all(self, ws_client, rosbridge_server):
        """[M4] 挂起所有任务，params=[2]（无 target_id）"""
        ws_client.publish_task_cmd(_intent())
        ws_client.publish_task_cmd(_intent("pipeline_inspection"))
        time.sleep(0.1)
        ws_client.suspend_all()
        time.sleep(0.2)

        cmds = rosbridge_server.get_received_publishes()
        suspend_all = [c for c in cmds if c["payload"]["task_type"] == 0
                       and c["payload"]["params"][0] == pytest.approx(2.0)]
        assert len(suspend_all) >= 1
        # 挂起所有时不应有 target_id
        assert len(suspend_all[-1]["payload"]["params"]) == 1

    def test_M5_clear_block(self, ws_client, rosbridge_server):
        """[M5] 清除阻塞（Emergency），params=[7]"""
        ws_client.clear_block()
        time.sleep(0.2)

        cmds = rosbridge_server.get_received_publishes()
        clear_cmds = [c for c in cmds if c["payload"]["task_type"] == 0
                      and c["payload"]["params"][0] == pytest.approx(7.0)]
        assert len(clear_cmds) >= 1

    def test_M6_task_manage_priority_is_highest(self, ws_client, rosbridge_server):
        """[M6] TASK_MANAGE 指令的 priority 必须为 0（最高优先级）"""
        ws_client.suspend_all()
        time.sleep(0.2)
        cmds = rosbridge_server.get_received_publishes()
        manage_cmds = [c for c in cmds if c["payload"]["task_type"] == 0]
        assert manage_cmds[-1]["payload"]["priority"] == 0


# ============================================================================
# [N] CTRL_TASK / AUV_TASK / sys_config 专项指令
# ============================================================================

class TestSpecialCommands:
    """验证特殊任务类型与系统配置指令"""

    def test_N1_ctrl_task_light(self, ws_client, rosbridge_server):
        """[N1] 灯光控制：CTRL_TASK=6, params=[1, 50] (设备1, PWM 50%)"""
        ws_client.ctrl_task(device_id=1, value=50.0)
        time.sleep(0.2)

        cmds = rosbridge_server.get_received_publishes()
        ctrl_cmds = [c for c in cmds if c["payload"]["task_type"] == 6]
        assert len(ctrl_cmds) >= 1
        params = ctrl_cmds[-1]["payload"]["params"]
        assert params[0] == pytest.approx(1.0)   # device_id=1
        assert params[1] == pytest.approx(50.0)  # value=50
        assert ctrl_cmds[-1]["payload"]["pos_target"] == []  # 灯光不需要位姿

    def test_N2_ctrl_task_relay(self, ws_client, rosbridge_server):
        """[N2] 继电器控制：device_id=3, value=1 (打开)"""
        ws_client.ctrl_task(device_id=3, value=1.0)
        time.sleep(0.2)
        cmds = rosbridge_server.get_received_publishes()
        ctrl_cmds = [c for c in cmds if c["payload"]["task_type"] == 6]
        assert ctrl_cmds[-1]["payload"]["params"][0] == pytest.approx(3.0)
        assert ctrl_cmds[-1]["payload"]["params"][1] == pytest.approx(1.0)

    def test_N3_auv_task_dispatch(self, ws_client, rosbridge_server):
        """[N3] AUV 任务：AUV_TASK=10, 2 个航线关键点, 6 个 params"""
        intent = _auv_intent(speed_ms=1.2, auto_return=1.0, return_depth=5.0)
        ws_client.publish_task_cmd(intent)
        time.sleep(0.2)

        cmds = rosbridge_server.get_received_publishes()
        auv_cmds = [c for c in cmds if c["payload"]["task_type"] == 10]
        assert len(auv_cmds) >= 1
        cmd = auv_cmds[-1]["payload"]
        assert len(cmd["pos_target"]) == 2
        assert len(cmd["params"]) == 6
        assert cmd["params"][0] == pytest.approx(1.2)  # speed_ms
        assert cmd["params"][3] == pytest.approx(1.0)  # auto_return

    def test_N4_set_pilot_mode_autodepth(self, ws_client, rosbridge_server):
        """[N4] 设置飞行器模式为 AUTODEPTH, 发布到 /task/sys_config"""
        ws_client.set_pilot_mode(PilotMode.AUTODEPTH)
        time.sleep(0.2)

        all_pubs = rosbridge_server.get_received_publishes()
        sys_config = [p for p in all_pubs if p["topic"] == "/task/sys_config"]
        assert len(sys_config) >= 1
        assert sys_config[-1]["payload"]["ctr_mode"] == 4  # AUTODEPTH=4

    def test_N5_set_pilot_mode_mission(self, ws_client, rosbridge_server):
        """[N5] 设置路径跟踪模式 MISSION1=9"""
        ws_client.set_pilot_mode(PilotMode.MISSION1)
        time.sleep(0.2)
        all_pubs = rosbridge_server.get_received_publishes()
        sys_config = [p for p in all_pubs if p["topic"] == "/task/sys_config"]
        assert sys_config[-1]["payload"]["ctr_mode"] == 9


# ============================================================================
# [O] TaskStatusTracker 遥测解析
# ============================================================================

class TestTaskStatusTracker:
    """验证 TaskStatusTracker 正确解析 SysStatus.msg"""

    def test_O1_parse_sys_status_fields(self):
        """[O1] _parse_sys_status 正确提取位姿、速度、高度、控制模式与健康状态"""
        from mcp.shim.task_status_tracker import TaskStatusTracker
        msg = {
            "pose": {
                "pose": {
                    "position": {"x": 115.3, "y": 20.8, "z": -312.4},
                    "orientation": {"x": 0.0, "y": 0.0, "z": 0.707, "w": 0.707},
                }
            },
            "twist": {"linear": {"x": 0.3, "y": 0.0, "z": -0.05}, "angular": {}},
            "alt": 2.5,
            "ctr_mode": 4,
            "health": 0,
            "task_list": [],
        }
        t = TaskStatusTracker._parse_sys_status(msg)
        assert t.pose_x == pytest.approx(115.3)
        assert t.pose_z == pytest.approx(-312.4)
        assert t.water_depth == pytest.approx(312.4)
        assert t.altitude == pytest.approx(2.5)
        assert t.ctr_mode == 4
        assert t.health == 0

    def test_O2_parse_task_list_ongoing(self):
        """[O2] 解析 task_list 中 ONGOING 状态的任务"""
        from mcp.shim.task_status_tracker import TaskStatusTracker
        msg = {
            "pose": {}, "twist": {}, "alt": 0.0, "ctr_mode": 0, "health": 0,
            "task_list": [
                {"task": {"task_type": 4, "task_id": 0x80001}, "status": 3},  # ONGOING
                {"task": {"task_type": 2, "task_id": 0x80002}, "status": 5},  # FINISH
            ],
        }
        t = TaskStatusTracker._parse_sys_status(msg)
        assert len(t.task_list) == 2
        assert t.task_list[0].status == 3
        assert t.task_list[0].is_active()
        assert not t.task_list[0].is_finished()
        assert t.task_list[1].status == 5
        assert t.task_list[1].is_finished()

    def test_O3_parse_task_status_names(self):
        """[O3] 每个 TaskStatus 状态值都应有正确的名称映射"""
        from mcp.shim.task_status_tracker import TaskStatusTracker
        for status_val, expected_name in [
            (0, "READY"), (1, "PLAN"), (3, "ONGOING"), (5, "FINISH"), (7, "FAIL")
        ]:
            msg = {
                "pose": {}, "twist": {}, "alt": 0.0, "ctr_mode": 0, "health": 0,
                "task_list": [{"task": {"task_type": 4, "task_id": 1}, "status": status_val}],
            }
            t = TaskStatusTracker._parse_sys_status(msg)
            assert t.task_list[0].status_name == expected_name

    def test_O4_water_depth_is_absolute(self):
        """[O4] water_depth 属性应始终返回正值（取 z 的绝对值）"""
        from mcp.shim.task_status_tracker import TaskStatusTracker
        msg = {
            "pose": {"pose": {"position": {"x": 0.0, "y": 0.0, "z": -312.4}}},
            "twist": {}, "alt": 0.0, "ctr_mode": 0, "health": 0, "task_list": [],
        }
        t = TaskStatusTracker._parse_sys_status(msg)
        assert t.water_depth == pytest.approx(312.4)
        assert t.water_depth >= 0


# ============================================================================
# [P] 完整闭环：下发 → 订阅遥测 → 状态追踪
# ============================================================================

class TestFullProtocolRoundTrip:
    """验证从任务下发到状态回传的完整内部协议闭环"""

    def test_P1_dispatch_and_telemetry_isolation(self, ws_client, rosbridge_server, tmp_path):
        """[P1] 任务下发水深 (300m规划) 与遥测水深 (312.4m实测) 完全隔离"""
        from mcp.shim.task_status_tracker import TaskStatusTracker
        telemetry_received = []

        ws_client.subscribe_system_status(lambda msg: telemetry_received.append(msg))
        time.sleep(0.2)

        # 触发遥测推送（订阅时立即推送一次）
        assert len(telemetry_received) >= 1
        # 遥测中 z = -312.4
        pose_z = telemetry_received[0].get("pose", {}).get("pose", {}).get("position", {}).get("z", 0)
        assert pose_z == pytest.approx(-312.4)

        # 下发任务（规划水深 300m）
        tid = ws_client.publish_task_cmd(_intent("tree_valve_operation", depth=300.0))
        time.sleep(0.3)

        # 下发的 SysTaskCmd 坐标来自 TaskIntent（非遥测）
        cmds = [c for c in rosbridge_server.get_received_publishes()
                if c["topic"] == "/task_cmd" and c["payload"]["task_type"] != 0]
        assert cmds[-1]["payload"]["pos_target"][0]["position"]["z"] == pytest.approx(-300.0)

        # 遥测 z 未被任务下发坐标污染
        assert pose_z == pytest.approx(-312.4)

    def test_P2_full_protocol_coverage(self, ws_client, rosbridge_server):
        """[P2] 在单次测试中使用完整协议：普通任务 + 管理 + 控制 + 系统配置"""
        # 1. 下发采油树任务
        tid = ws_client.publish_task_cmd(_intent("tree_valve_operation", depth=300.0))
        time.sleep(0.1)

        # 2. 挂起任务
        ws_client.suspend_task(tid)
        time.sleep(0.1)

        # 3. 灯光控制（作业前照明）
        ws_client.ctrl_task(device_id=1, value=80.0)
        time.sleep(0.1)

        # 4. 设置控制模式
        ws_client.set_pilot_mode(PilotMode.AUTOHOLD1)
        time.sleep(0.1)

        # 5. 恢复任务
        ws_client.resume_task(tid)
        time.sleep(0.2)

        all_pubs = rosbridge_server.get_received_publishes()
        # 验证五类指令均正确发出
        topics = [p["topic"] for p in all_pubs]
        task_types = [p["payload"].get("task_type") for p in all_pubs if p["topic"] == "/task_cmd"]

        assert "/task_cmd" in topics
        assert "/task/sys_config" in topics
        assert 4 in task_types    # INSERT_PLUG
        assert 0 in task_types    # TASK_MANAGE (挂起+恢复各一条)
        assert 6 in task_types    # CTRL_TASK

        # 验证 TASK_MANAGE 挂起与恢复顺序
        manage_cmds = [p for p in all_pubs if p["topic"] == "/task_cmd"
                       and p["payload"]["task_type"] == 0]
        actions = [m["payload"]["params"][0] for m in manage_cmds]
        assert 0.0 in actions   # SUSPEND
        assert 1.0 in actions   # RESUME

    def test_P3_delete_all_clears_active_tasks(self, ws_client, rosbridge_server):
        """[P3] delete_all 后 Mock rosbridge 中已无活跃任务"""
        ws_client.publish_task_cmd(_intent("tree_valve_operation"))
        ws_client.publish_task_cmd(_intent("pipeline_inspection"))
        time.sleep(0.2)

        ws_client.delete_all()
        time.sleep(0.3)

        assert rosbridge_server.get_active_tasks() == {}
