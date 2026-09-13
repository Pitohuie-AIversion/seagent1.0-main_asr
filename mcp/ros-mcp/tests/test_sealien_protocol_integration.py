"""
test_sealien_protocol_integration.py
=====================================
融合协议算法 (sealien_protocol) 的针对性单元测试集
测试涵盖：
1. WGS-84 高精度 ENU/odom 坐标投影算法准确性
2. 切线方位角 (yaw_between) 与四元数推算
3. TaskMessageGuard 与 RequestIdGuard 防重复提交阻断
4. intent_to_syscmd 的全链路融合验证与坐标转换
"""

import math
import pytest
from mcp.shim.sealien_protocol import (
    LocalOrigin,
    ProtocolValidationError,
    DuplicateRequestError,
    geodetic_to_enu,
    geodetic_to_odom_position,
    pose,
    yaw_between,
    TaskMessageGuard,
    RequestIdGuard,
    validate_priority,
    validate_task_id,
)
from mcp.shim.rosbridge_client import intent_to_syscmd, TaskType


class TestSealienProtocolIntegration:

    def test_T1_geodetic_to_enu_at_origin(self):
        """[T1] 在参考原点处的投影坐标必须为 (0.0, 0.0, 0.0)"""
        origin = LocalOrigin(latitude=22.80169, longitude=113.52497, altitude=0.0)
        east, north, up = geodetic_to_enu(
            latitude=22.80169,
            longitude=113.52497,
            altitude=0.0,
            origin=origin,
        )
        assert east == pytest.approx(0.0, abs=1e-5)
        assert north == pytest.approx(0.0, abs=1e-5)
        assert up == pytest.approx(0.0, abs=1e-5)

    def test_T2_geodetic_to_odom_position_calculation(self):
        """[T2] 偏移经纬度应准确计算 east/north，且 z 为 -water_depth_m"""
        origin = LocalOrigin(latitude=22.80169, longitude=113.52497, altitude=0.0)
        # 偏东约 100 米，偏北约 100 米的经纬度点
        east, north, z = geodetic_to_odom_position(
            latitude=22.80259,
            longitude=113.52594,
            water_depth_m=150.0,
            origin=origin,
        )
        assert z == pytest.approx(-150.0)
        assert east > 0.0
        assert north > 0.0

    def test_T3_yaw_between_and_pose_quaternion(self):
        """[T3] 航向角推算与四元数转换"""
        current_pos = {"x": 0.0, "y": 0.0}
        # 正东方向 (dx=10, dy=0) -> yaw = 0.0
        yaw_east = yaw_between(current_pos, 10.0, 0.0)
        assert yaw_east == pytest.approx(0.0)
        p_east = pose(10.0, 0.0, -10.0, yaw_east)
        assert p_east["orientation"]["z"] == pytest.approx(0.0)
        assert p_east["orientation"]["w"] == pytest.approx(1.0)

        # 正北方向 (dx=0, dy=10) -> yaw = pi/2
        yaw_north = yaw_between(current_pos, 0.0, 10.0)
        assert yaw_north == pytest.approx(math.pi / 2.0)
        p_north = pose(0.0, 10.0, -10.0, yaw_north)
        assert p_north["orientation"]["z"] == pytest.approx(math.sin(math.pi / 4.0))
        assert p_north["orientation"]["w"] == pytest.approx(math.cos(math.pi / 4.0))

    def test_T4_task_message_guard_deduplication(self):
        """[T4] TaskMessageGuard 重复负载拦截与唯一识别"""
        guard = TaskMessageGuard()
        msg = {
            "task_type": 5,
            "task_id": 0x80001,
            "frame_id": "odom",
            "priority": 15,
            "pos_target": [{"position": {"x": 1.0, "y": 2.0, "z": -10.0}}],
            "params": [10.0],
            "fail_stop": True,
        }
        # 第一次调用成功
        guard.claim(msg)

        # 第二次相同负载调用应抛出 DuplicateRequestError
        with pytest.raises(DuplicateRequestError):
            guard.claim(msg)

        # 修改 task_id 或参数后应可再次提交
        msg_diff = dict(msg)
        msg_diff["task_id"] = 0x80002
        guard.claim(msg_diff)

    def test_T5_request_id_guard(self):
        """[T5] RequestIdGuard 重复指令 ID 拦截"""
        guard = RequestIdGuard()
        guard.claim("REQ-1001")
        with pytest.raises(DuplicateRequestError):
            guard.claim("REQ-1001")

        with pytest.raises(ProtocolValidationError):
            guard.claim("")

    def test_T6_intent_to_syscmd_geodetic_mode(self):
        """[T6] intent_to_syscmd 开启 use_geodetic 时的准确转换能力"""
        intent = {
            "schema_version": 2,
            "task_type": "underwater_move",
            "priority": 15,
            "location": {"water_depth_m": 200.0, "use_geodetic": True},
            "task": {
                "type": "underwater_move",
                "details": {
                    "target": {"latitude": 22.80169, "longitude": 113.52497}
                },
            },
        }
        cmd = intent_to_syscmd(intent, use_geodetic=True)
        assert cmd.task_type == TaskType.MOVE_TASK
        assert cmd.pos_target[0].x == pytest.approx(0.0, abs=1e-4)
        assert cmd.pos_target[0].y == pytest.approx(0.0, abs=1e-4)
        assert cmd.pos_target[0].z == pytest.approx(-200.0)

    def test_T7_intent_to_syscmd_non_geodetic_mode(self):
        """[T7] 未开启 use_geodetic 时直接映射经纬度到 x/y"""
        intent = {
            "schema_version": 2,
            "task_type": "tree_valve_operation",
            "location": {"water_depth_m": 300.0},
            "task": {
                "type": "tree_valve_operation",
                "details": {
                    "target": {"latitude": 20.815, "longitude": 115.735}
                },
            },
        }
        cmd = intent_to_syscmd(intent)
        assert cmd.task_type == 4
        assert cmd.pos_target[0].x == pytest.approx(115.735)
        assert cmd.pos_target[0].y == pytest.approx(20.815)
        assert cmd.pos_target[0].z == pytest.approx(-300.0)

    def test_T8_rov_message_subscription_helpers(self):
        """[T8] 验证新增的 ROV 传感器订阅与特化指令下发接口注册完整"""
        from mcp.shim.rosbridge_client import RosbridgeClient
        client = RosbridgeClient(host="127.0.0.1", port=9090)
        assert hasattr(client, "subscribe_depth_status")
        assert hasattr(client, "subscribe_imu_dvl_status")
        assert hasattr(client, "subscribe_thruster_status")
        assert hasattr(client, "subscribe_heartbeat")
        assert hasattr(client, "publish_joystick_cmd")
        assert hasattr(client, "publish_thruster_cmd")
        assert hasattr(client, "publish_robotic_arm_request")
        assert hasattr(client, "publish_christmas_tree_plug_cmd")
