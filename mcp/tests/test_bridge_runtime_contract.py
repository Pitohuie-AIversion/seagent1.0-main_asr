"""Regression contracts for the SEAgent -> rosbridge -> dashboard runtime path."""

from unittest.mock import Mock
from datetime import datetime, timezone
import pytest

from mcp.shim.bridge_service import SEAgentMCPBridgeService
import mcp.shim.rosbridge_client as rosbridge_client
from mcp.shim.rosbridge_client import RosbridgeClient, TaskStatus
from mcp.core.task_status_tracker import ROVTelemetry, TaskStatusItem


def _intent(intent_id="PI-20260828-001"):
    return {
        "schema_version": 2,
        "intent_id": intent_id,
        "task_id": intent_id,
        "task_type": "pipeline_inspection",
        "location": {"water_depth_m": 80.0},
        "task": {"details": {
            "start_point": {"latitude": 20.0, "longitude": 115.0},
            "end_point": {"latitude": 20.1, "longitude": 115.2},
        }},
    }


def test_dispatch_is_idempotent_for_same_final_intent():
    bridge = SEAgentMCPBridgeService()
    bridge._running = True
    bridge.client.is_connected = Mock(return_value=True)
    bridge.client.publish_task_cmd = Mock()

    first = bridge.dispatch_intent(_intent())
    second = bridge.dispatch_intent(_intent())

    assert first == second
    assert 0x80001 <= first <= 0x8FFFF
    bridge.client.publish_task_cmd.assert_called_once()


def test_dispatch_uses_stable_fingerprint_when_internal_intent_has_no_id():
    bridge = SEAgentMCPBridgeService()
    bridge._running = True
    bridge.client.is_connected = Mock(return_value=True)
    bridge.client.publish_task_cmd = Mock()
    bridge.client.set_pilot_mode = Mock()
    intent = {"schema_version": 2, "task_type": "underwater_move"}

    assert bridge.dispatch_intent(intent) == bridge.dispatch_intent(dict(intent))
    bridge.client.publish_task_cmd.assert_called_once()


def test_dispatch_reconnects_same_gateway_when_transport_drops():
    bridge = SEAgentMCPBridgeService(host="192.168.5.250", port=9090)
    bridge._running = True
    bridge.client.is_connected = Mock(return_value=False)
    bridge.reconnect = Mock()
    bridge.client.publish_task_cmd = Mock()

    task_id = bridge.dispatch_intent(_intent("PI-20260828-reconnect"))

    assert task_id >= 0x80001
    bridge.reconnect.assert_called_once_with("192.168.5.250", 9090)
    bridge.client.publish_task_cmd.assert_called_once()


def test_relative_body_move_is_resolved_from_fresh_pose_and_yaw():
    bridge = SEAgentMCPBridgeService()
    bridge._running = True
    bridge.client.is_connected = Mock(return_value=True)
    bridge.client.publish_task_cmd = Mock()
    bridge.client.set_pilot_mode = Mock()
    bridge.tracker._latest = ROVTelemetry(
        received_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        pose_x=10.0,
        pose_y=20.0,
        pose_z=-30.0,
        raw_msg={"pose": {"pose": {"orientation": {
            "x": 0.0, "y": 0.0, "z": 2 ** -0.5, "w": 2 ** -0.5,
        }}}},
    )
    intent = {
        "schema_version": 2,
        "intent_id": "UM-relative-test",
        "task_type": "underwater_move",
        "task": {"type": "underwater_move", "details": {"target": {
            "x": 0.3, "y": 0.0, "z": 0.0,
            "relative": True, "frame_id": "base_link",
        }}},
    }

    bridge.dispatch_intent(intent)

    published = bridge.client.publish_task_cmd.call_args.args[0]
    target = published["task"]["details"]["target"]
    assert target["x"] == pytest.approx(10.0, abs=1e-6)
    assert target["y"] == pytest.approx(20.3, abs=1e-6)
    assert target["z"] == pytest.approx(-30.0)
    assert target["frame_id"] == "odom"
    bridge.client.set_pilot_mode.assert_called_once()


def test_relative_move_fails_closed_without_fresh_telemetry():
    bridge = SEAgentMCPBridgeService()
    bridge._running = True
    bridge.client.is_connected = Mock(return_value=True)
    bridge.client.publish_task_cmd = Mock()
    intent = {
        "schema_version": 2,
        "intent_id": "UM-relative-stale",
        "task_type": "underwater_move",
        "task": {"type": "underwater_move", "details": {"target": {
            "x": 0.3, "y": 0.0, "z": 0.0,
            "relative": True, "frame_id": "base_link",
        }}},
    }

    with pytest.raises(RuntimeError, match="新鲜"):
        bridge.dispatch_intent(intent)
    bridge.client.publish_task_cmd.assert_not_called()


def test_runtime_snapshot_marks_transport_send_separately_from_ros_status():
    bridge = SEAgentMCPBridgeService()
    bridge._running = True
    bridge.client.is_connected = Mock(return_value=True)
    bridge.client.publish_task_cmd = Mock(return_value=0x80022)

    bridge.dispatch_intent(_intent("PI-20260828-002"))
    snapshot = bridge.runtime_snapshot()

    assert snapshot["active_tasks"][0]["status"] == "SENT"
    assert snapshot["active_tasks"][0]["progress"] == 0.0
    assert snapshot["active_tasks"][0]["intent_id"] == "PI-20260828-002"


def test_delete_all_clears_bridge_dispatch_and_tracker_state():
    bridge = SEAgentMCPBridgeService()
    bridge._running = True
    bridge.client.is_connected = Mock(return_value=True)
    bridge.client.publish_task_cmd = Mock()
    bridge.client.delete_all = Mock(return_value=0x80023)
    bridge.dispatch_intent(_intent("PI-20260828-003"))
    bridge.tracker._latest = ROVTelemetry(task_list=[
        TaskStatusItem(task_id=0x80001, task_type=5, status=3, status_name="ONGOING")
    ])

    assert bridge.delete_all_tasks() == 0x80023

    assert bridge.runtime_snapshot()["active_tasks_count"] == 0
    bridge.client.delete_all.assert_called_once_with()


def test_legacy_delete_all_uses_native_clear_command(monkeypatch):
    monkeypatch.setattr(rosbridge_client, "LEGACY_MSGMANAGEMENT", True)
    client = RosbridgeClient()
    client.publish = Mock()
    client._last_legacy_task_id = 0x80001

    command_id = client.delete_all()

    assert 0x80001 <= command_id <= 0x8FFFF
    topic, msg_type, payload = client.publish.call_args.args
    assert topic == rosbridge_client.TASK_TOPIC
    assert msg_type == rosbridge_client.TASK_MESSAGE_TYPE
    assert payload == {
        "task": 255,
        "hole_id": 0,
        "x": 0.0,
        "y": 0.0,
        "z": 0.0,
        "roll": 0.0,
        "pitch": 0.0,
        "yaw": 0.0,
    }
    assert client._last_legacy_task_id == 0


def test_legacy_per_task_delete_fails_without_publishing_move(monkeypatch):
    monkeypatch.setattr(rosbridge_client, "LEGACY_MSGMANAGEMENT", True)
    client = RosbridgeClient()
    client.publish = Mock()

    try:
        client.delete_task(0x80001)
        raise AssertionError("legacy per-task delete must fail closed")
    except rosbridge_client.ProtocolValidationError:
        pass

    client.publish.assert_not_called()


def test_legacy_mode_uses_direct_controller_threshold(monkeypatch):
    monkeypatch.setattr(rosbridge_client, "LEGACY_MSGMANAGEMENT", True)
    monkeypatch.setattr(rosbridge_client, "LEGACY_PLAN_THRESHOLD", 10000.0)
    client = RosbridgeClient()
    client.publish = Mock()

    client.set_pilot_mode(rosbridge_client.PilotMode.MISSION1)

    payload = client.publish.call_args.args[2]
    assert payload["plan_threshold"] == 10000.0
    assert payload["ctr_mode"] == 9


def test_legacy_task_status_is_normalized_to_seagent_lifecycle():
    from mcp.core.task_status_tracker import TaskStatusTracker

    for raw, expected in (
        (0, TaskStatus.PLAN),
        (1, TaskStatus.ENTER),
        (2, TaskStatus.ONGOING),
        (3, TaskStatus.EXIT),
        (4, TaskStatus.FINISH),
        (5, TaskStatus.PAUSE),
    ):
        telemetry = TaskStatusTracker._parse_sys_status({
            "task_status": raw,
            "_seagent_task_id": 0x80001,
        })
        assert telemetry.task_list[0].status == expected


def test_task_status_enum_matches_ros_message_contract():
    assert {status.value: status.name for status in TaskStatus} == {
        0: "READY",
        1: "PLAN",
        2: "ENTER",
        3: "ONGOING",
        4: "EXIT",
        5: "FINISH",
        6: "PAUSE",
        7: "FAIL",
    }


def test_publish_fails_closed_when_rosapi_cannot_confirm_topic_type():
    client = RosbridgeClient(connect_timeout=0.01)
    fake_ws = Mock()
    fake_ws.connected = True
    client._ws = fake_ws
    client.call_service = Mock(
        return_value={"result": True, "values": {"type": ""}}
    )

    try:
        client.publish("/task_cmd", "example_msgs/Task", {"task_id": 1})
        raise AssertionError("publish should fail when advertise did not create a ROS topic")
    except RuntimeError as exc:
        assert "未成功声明" in str(exc)

    sent_messages = [call.args[0] for call in fake_ws.send.call_args_list]
    assert any('"op": "advertise"' in message for message in sent_messages)
    assert not any('"op": "publish"' in message for message in sent_messages)


def test_production_task_id_sequence_survives_process_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_ROS2_ID_DIR", str(tmp_path))
    original_counter = rosbridge_client._task_id_counter
    try:
        rosbridge_client._task_id_counter = 0
        first = rosbridge_client.generate_task_id()
        rosbridge_client._task_id_counter = 0
        second = rosbridge_client.generate_task_id()
    finally:
        rosbridge_client._task_id_counter = original_counter

    assert second == first + 1
    assert (tmp_path / ".ros2_task_id_sequence").read_text() == "2"
