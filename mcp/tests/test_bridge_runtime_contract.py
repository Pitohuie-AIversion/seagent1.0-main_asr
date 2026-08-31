"""Regression contracts for the SEAgent -> rosbridge -> dashboard runtime path."""

from unittest.mock import Mock

from mcp.shim.bridge_service import SEAgentMCPBridgeService
import mcp.shim.rosbridge_client as rosbridge_client
from mcp.shim.rosbridge_client import RosbridgeClient, TaskStatus


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
    intent = {"schema_version": 2, "task_type": "underwater_move"}

    assert bridge.dispatch_intent(intent) == bridge.dispatch_intent(dict(intent))
    bridge.client.publish_task_cmd.assert_called_once()


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
