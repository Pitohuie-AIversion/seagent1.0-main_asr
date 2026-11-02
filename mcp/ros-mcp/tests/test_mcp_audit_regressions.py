"""Offline regressions for durable dispatch and finite protocol values."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from mcp.shim.bridge_service import SEAgentMCPBridgeService
from mcp.shim.rosbridge_client import (
    Pose, RosbridgeClient, SysTaskCmd, TaskType, intent_to_syscmd,
)
from mcp.shim.sealien_protocol import ProtocolValidationError, geodetic_to_odom_position


def intent():
    return {
        "intent_id": "audit-once", "task_type": "underwater_move",
        "location": {"water_depth_m": 50},
        "task": {"details": {"target": {"latitude": 20, "longitude": 115}}},
    }


def bridge_with_transport_mock(path):
    bridge = SEAgentMCPBridgeService(dispatch_records_dir=path)
    bridge.is_healthy = Mock(return_value=True)
    bridge.client.publish_task_cmd = Mock()
    return bridge


@pytest.mark.parametrize("damaged", ["{broken", "[]", '{"audit-once": {}}'])
def test_corrupted_records_block_dispatch_and_preserve_evidence(tmp_path, damaged):
    bridge = bridge_with_transport_mock(tmp_path)
    first = bridge.dispatch_intent(intent())
    path = tmp_path / bridge.DISPATCH_RECORD_FILE
    original = path.read_bytes()
    path.write_text(damaged)

    with pytest.raises(RuntimeError, match="下发记录"):
        bridge.dispatch_intent(intent())
    assert path.read_text() == damaged
    assert bridge._dispatch_records["audit-once"]["task_id"] == first
    bridge.client.publish_task_cmd.assert_called_once()
    with pytest.raises(RuntimeError, match="下发记录"):
        bridge_with_transport_mock(tmp_path)
    assert path.read_text() == damaged

    path.write_bytes(original)
    assert bridge.dispatch_intent(intent()) == first
    restarted = bridge_with_transport_mock(tmp_path)
    assert restarted.dispatch_intent(intent()) == first
    restarted.client.publish_task_cmd.assert_not_called()
    bridge.client.publish_task_cmd.assert_called_once()


def test_unreadable_records_block_dispatch_until_read_access_restored(tmp_path, monkeypatch):
    bridge = bridge_with_transport_mock(tmp_path)
    first = bridge.dispatch_intent(intent())
    path = tmp_path / bridge.DISPATCH_RECORD_FILE
    original = path.read_bytes()
    with monkeypatch.context() as patch:
        read_text = Path.read_text
        def fail_read(target, *args, **kwargs):
            if target == path:
                raise PermissionError("read denied")
            return read_text(target, *args, **kwargs)
        patch.setattr(Path, "read_text", fail_read)
        with pytest.raises(RuntimeError, match="下发记录"):
            bridge.dispatch_intent(intent())
        with pytest.raises(RuntimeError, match="下发记录"):
            bridge_with_transport_mock(tmp_path)
    assert path.read_bytes() == original
    assert bridge.dispatch_intent(intent()) == first
    bridge.client.publish_task_cmd.assert_called_once()


def test_disappearing_records_block_dispatch_on_existing_instance(tmp_path):
    bridge = bridge_with_transport_mock(tmp_path)
    first = bridge.dispatch_intent(intent())
    path = tmp_path / bridge.DISPATCH_RECORD_FILE
    original = path.read_bytes()
    path.unlink()
    with pytest.raises(RuntimeError, match="下发记录"):
        bridge.dispatch_intent(intent())
    path.write_bytes(original)
    assert bridge.dispatch_intent(intent()) == first
    bridge.client.publish_task_cmd.assert_called_once()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["water_depth_m", "depth", "latitude", "longitude"])
def test_intent_nonfinite_coordinates_are_rejected(value, field):
    payload = intent()
    target = payload["location"] if field == "water_depth_m" else payload["task"]["details"]["target"]
    target[field] = value
    with pytest.raises(ProtocolValidationError):
        intent_to_syscmd(payload, task_id=0x80001)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["x", "y", "z", "qx", "qy", "qz", "qw"])
def test_raw_nonfinite_pose_never_reaches_publish(value, field):
    pose = Pose()
    setattr(pose, field, value)
    cmd = SysTaskCmd(task_type=TaskType.MOVE_TASK, task_id=0x80001, pos_target=[pose])
    client = RosbridgeClient()
    client.publish = Mock()
    with pytest.raises(ProtocolValidationError):
        client.publish_syscmd_raw(cmd)
    client.publish.assert_not_called()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_params_never_reach_publish(value):
    cmd = SysTaskCmd(task_type=TaskType.CTRL_TASK, task_id=0x80001, frame_id="", params=[1.0, value])
    client = RosbridgeClient()
    client.publish = Mock()
    with pytest.raises(ProtocolValidationError):
        client.publish_syscmd_raw(cmd)
    client.publish.assert_not_called()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_projection_rejects_nonfinite_depth(value):
    with pytest.raises(ProtocolValidationError):
        geodetic_to_odom_position(20, 115, value)


def test_wire_json_never_serializes_nan():
    client = RosbridgeClient()
    client._ws = Mock(connected=True)
    with pytest.raises(ValueError):
        client._send({"value": float("nan")})
    client._ws.send.assert_not_called()
