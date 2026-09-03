"""Contracts derived from the ROS group's Sealien UI protocol and ROS package."""

from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

import mcp.shim.rosbridge_client as rosbridge_client
from mcp.shim.rosbridge_client import (
    PilotMode,
    RosbridgeClient,
    SysTaskCmd,
    TaskManageAction,
    TaskType,
    build_task_manage_cmd,
    intent_to_syscmd,
    validate_sys_task_cmd,
)
from mcp.shim.sealien_protocol import ProtocolValidationError


SEAGENT_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_SPEC = SEAGENT_ROOT / "config" / "ros2_protocol_spec.yaml"


def _target_intent(task_type: str = "underwater_move") -> dict:
    return {
        "schema_version": 2,
        "task_type": task_type,
        "priority": 15,
        "fail_stop": True,
        "location": {"water_depth_m": 80.0},
        "task": {
            "type": task_type,
            "details": {
                "target": {"latitude": 20.0, "longitude": 115.0},
            },
        },
    }


def _search_cable_intent() -> dict:
    return {
        "schema_version": 2,
        "task_type": "pipeline_inspection",
        "priority": 15,
        "fail_stop": True,
        "location": {"water_depth_m": 80.0},
        "task": {
            "type": "pipeline_inspection",
            "details": {
                "start_point": {"latitude": 20.0, "longitude": 115.0},
                "end_point": {"latitude": 20.1, "longitude": 115.2},
            },
        },
    }


def test_ros_group_primary_topics_use_reference_package_types():
    assert rosbridge_client.TASK_TOPIC == "/task_cmd"
    assert rosbridge_client.CONFIG_TOPIC == "/task/sys_config"
    assert rosbridge_client.STATUS_TOPIC == "/task/system_status"
    assert rosbridge_client.TASK_MESSAGE_TYPE == (
        "sealien_ctrlpilot_llmbridge/msg/SysTaskCmd"
    )
    assert rosbridge_client.CONFIG_MESSAGE_TYPE == (
        "sealien_ctrlpilot_llmbridge/msg/SysConfig"
    )
    assert rosbridge_client.STATUS_MESSAGE_TYPE == (
        "sealien_ctrlpilot_llmbridge/msg/SysStatus"
    )


def test_protocol_spec_covers_all_ui_protocol_topics_and_directions():
    spec = yaml.safe_load(PROTOCOL_SPEC.read_text(encoding="utf-8"))
    topics = spec["websocket_gateway"]["topics"]
    assert {
        key: (value["name"], value["type"], value["direction"])
        for key, value in topics.items()
    } == {
        "task_cmd": (
            "/task_cmd",
            "sealien_ctrlpilot_llmbridge/msg/SysTaskCmd",
            "publish",
        ),
        "sys_config": (
            "/task/sys_config",
            "sealien_ctrlpilot_llmbridge/msg/SysConfig",
            "publish",
        ),
        "system_status": (
            "/task/system_status",
            "sealien_ctrlpilot_llmbridge/msg/SysStatus",
            "subscribe",
        ),
        "compressed_image": (
            "/vision/compressd_image",
            "sensor_msgs/msg/CompressedImage",
            "subscribe",
        ),
        "image": ("/vision/image", "sensor_msgs/msg/Image", "subscribe"),
        "keypoints": (
            "/vision/keypoints",
            "sealien_ctrlpilot_msgmanagement/msg/Keypoints",
            "subscribe",
        ),
        "plug_hole": (
            "/vision/plug_hole",
            "sealien_ctrlpilot_msgmanagement/msg/ConnectChristmasTreePlug",
            "subscribe",
        ),
    }


def test_enabled_yaml_subscriptions_are_registered_from_catalog():
    client = RosbridgeClient()
    client._ws = Mock()
    client._ws.connected = True

    registered = client.subscribe_from_config()

    assert registered == [
        "system_status",
        "keypoints",
        "plug_hole",
        "depth_status",
        "imu_dvl_status",
        "thruster_status",
        "heartbeat",
    ]
    assert set(client._subscriptions) == {
        "/task/system_status",
        "/vision/keypoints",
        "/vision/plug_hole",
        "/sensor/depth",
        "/sensor/imu_dvl",
        "/sensor/thruster_status",
        "/system/heartbeat",
    }


def test_sys_task_cmd_payloads_follow_per_task_protocol_layout():
    move = intent_to_syscmd(_target_intent(), task_id=0x80001)
    assert move.frame_id == "odom"
    assert len(move.pos_target) == 1
    assert move.params == []

    manipulator = intent_to_syscmd(
        _target_intent("tree_valve_operation"), task_id=0x80002
    )
    assert manipulator.frame_id == ""
    assert len(manipulator.pos_target) == 1
    assert manipulator.params == []

    search = intent_to_syscmd(_search_cable_intent(), task_id=0x80003)
    assert search.frame_id == "odom"
    assert len(search.pos_target) == 2
    assert search.pos_target[0].x == pytest.approx(115.0)
    assert search.pos_target[1].x == pytest.approx(115.2)
    assert search.params == []


def test_unknown_task_and_incomplete_search_fail_closed():
    with pytest.raises(ProtocolValidationError, match="不支持"):
        intent_to_syscmd(_target_intent("unknown_task"), task_id=0x80001)

    incomplete = _search_cable_intent()
    incomplete["task"]["details"].pop("end_point")
    with pytest.raises(ProtocolValidationError, match="start_point.*end_point"):
        intent_to_syscmd(incomplete, task_id=0x80001)


def test_task_management_parameter_arity_is_enforced():
    with pytest.raises(ProtocolValidationError, match="target_task_id"):
        build_task_manage_cmd(TaskManageAction.SUSPEND)
    with pytest.raises(ProtocolValidationError, match="不使用 target_task_id"):
        build_task_manage_cmd(TaskManageAction.SUSPEND_ALL, target_task_id=0x80001)

    with pytest.raises(ProtocolValidationError, match="必须包含目标任务 ID"):
        validate_sys_task_cmd(
            SysTaskCmd(
                task_type=int(TaskType.TASK_MANAGE),
                task_id=0x80001,
                frame_id="",
                priority=0,
                pos_target=[],
                params=[float(TaskManageAction.SUSPEND)],
                fail_stop=False,
            )
        )
    with pytest.raises(ProtocolValidationError, match="不得包含目标任务 ID"):
        validate_sys_task_cmd(
            SysTaskCmd(
                task_type=int(TaskType.TASK_MANAGE),
                task_id=0x80001,
                frame_id="",
                priority=0,
                pos_target=[],
                params=[float(TaskManageAction.QUERY), 0x80002],
                fail_stop=False,
            )
        )


def test_pilot_mode_keeps_protocol_constant_spellings_and_readable_aliases():
    assert PilotMode.AUTODHIGHT == 5
    assert PilotMode.AUTODIRCETION == 6
    assert PilotMode.AUTOHEIGHT is PilotMode.AUTODHIGHT
    assert PilotMode.AUTODIRECTION is PilotMode.AUTODIRCETION


def test_rosbridge_helpers_use_protocol_topic_and_type_constants(monkeypatch):
    client = RosbridgeClient()
    publishes = []
    subscriptions = []
    monkeypatch.setattr(
        client,
        "publish",
        lambda topic, msg_type, payload: publishes.append((topic, msg_type, payload)),
    )
    monkeypatch.setattr(
        client,
        "subscribe",
        lambda topic, msg_type, callback: subscriptions.append((topic, msg_type)),
    )

    client.publish_task_cmd(_target_intent(), task_id=0x80011)
    client.set_pilot_mode(PilotMode.AUTODEPTH)
    client.subscribe_system_status(lambda _msg: None)
    client.subscribe_compressed_image(lambda _msg: None)
    client.subscribe_image(lambda _msg: None)
    client.subscribe_keypoints(lambda _msg: None)
    client.subscribe_plug_hole(lambda _msg: None)

    assert publishes == [
        (
            "/task_cmd",
            "sealien_ctrlpilot_llmbridge/msg/SysTaskCmd",
            move_payload := publishes[0][2],
        ),
        (
            "/task/sys_config",
            "sealien_ctrlpilot_llmbridge/msg/SysConfig",
            {"ctr_mode": 4},
        ),
    ]
    assert set(move_payload) == {
        "task_type",
        "task_id",
        "frame_id",
        "priority",
        "pos_target",
        "params",
        "fail_stop",
    }
    assert subscriptions == [
        ("/task/system_status", "sealien_ctrlpilot_llmbridge/msg/SysStatus"),
        ("/vision/compressd_image", "sensor_msgs/msg/CompressedImage"),
        ("/vision/image", "sensor_msgs/msg/Image"),
        ("/vision/keypoints", "sealien_ctrlpilot_msgmanagement/msg/Keypoints"),
        (
            "/vision/plug_hole",
            "sealien_ctrlpilot_msgmanagement/msg/ConnectChristmasTreePlug",
        ),
    ]
