"""Unit tests for src/robot_gateway/drivers/ros2.py (BUG-1)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp.core.rosbridge_client import (
    ProtocolValidationError,
    PublishOutcomeUnknown,
    RosbridgeClient,
)
from mcp.core.task_status_tracker import ROVTelemetry, TaskStatusTracker
from src.robot_gateway import (
    CommandExecutionStatus,
    ROS2BridgeDriver,
    RobotConnectionStatus,
)


class _DummyTracker:
    def __init__(self, client):
        self.client = client
        self.started = 0
        self.stopped = 0
        self.telemetry = None

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def latest_telemetry(self):
        return self.telemetry


def test_ros2_driver_and_mcp_core_imports():
    driver = ROS2BridgeDriver(unit_id="ROS2-TEST-001", host="127.0.0.1", port=9090)
    assert driver.unit_id == "ROS2-TEST-001"
    assert driver.status == RobotConnectionStatus.DISCONNECTED
    assert not driver.is_connected()


def test_connect_real_rosbridge_semantics_none_and_is_connected_true(monkeypatch):
    created_clients = []
    created_trackers = []

    class FakeRealClient:
        def __init__(self, host, port):
            self.host = host
            self.port = port
            self.connected = False
            created_clients.append(self)

        def connect(self):
            self.connected = True
            return None  # Real RosbridgeClient.connect() returns None

        def is_connected(self):
            return self.connected

        def disconnect(self):
            self.connected = False

    def make_tracker(client):
        tracker = _DummyTracker(client)
        created_trackers.append(tracker)
        return tracker

    monkeypatch.setattr("mcp.core.rosbridge_client.RosbridgeClient", FakeRealClient)
    monkeypatch.setattr("mcp.core.task_status_tracker.TaskStatusTracker", make_tracker)

    driver = ROS2BridgeDriver(unit_id="ROS2-REAL-001", host="10.0.0.5", port=9091)
    assert driver.connect() is True
    assert driver.status == RobotConnectionStatus.CONNECTED
    assert driver.is_connected() is True
    assert len(created_clients) == 1
    assert created_clients[0].host == "10.0.0.5"
    assert created_clients[0].port == 9091
    assert len(created_trackers) == 1
    assert created_trackers[0].started == 1


def test_connect_real_rosbridge_none_but_is_connected_false_cleans_up(monkeypatch):
    client = MagicMock(spec=RosbridgeClient)
    client.connect.return_value = None
    client.is_connected.return_value = False
    tracker = MagicMock(spec=TaskStatusTracker)

    monkeypatch.setattr("mcp.core.rosbridge_client.RosbridgeClient", lambda host, port: client)
    monkeypatch.setattr("mcp.core.task_status_tracker.TaskStatusTracker", lambda c: tracker)

    driver = ROS2BridgeDriver()
    assert driver.connect() is False
    assert driver.status == RobotConnectionStatus.ERROR
    assert driver.is_connected() is False
    assert driver._client is None
    assert driver._tracker is None
    tracker.start.assert_not_called()
    client.disconnect.assert_called_once()


def test_connect_mock_returns_true_and_false_with_cleanup(monkeypatch):
    # Case 1: Mock returning True
    client_ok = MagicMock()
    client_ok.connect.return_value = True
    client_ok.is_connected.return_value = True
    tracker_ok = MagicMock()

    monkeypatch.setattr("mcp.core.rosbridge_client.RosbridgeClient", lambda host, port: client_ok)
    monkeypatch.setattr("mcp.core.task_status_tracker.TaskStatusTracker", lambda c: tracker_ok)

    driver_ok = ROS2BridgeDriver()
    assert driver_ok.connect() is True
    assert driver_ok.status == RobotConnectionStatus.CONNECTED
    tracker_ok.start.assert_called_once()

    # Case 2: MagicMock returning False even if is_connected() is default truthy MagicMock
    client_fail = MagicMock()
    client_fail.connect.return_value = False
    tracker_fail = MagicMock()

    monkeypatch.setattr("mcp.core.rosbridge_client.RosbridgeClient", lambda host, port: client_fail)
    monkeypatch.setattr("mcp.core.task_status_tracker.TaskStatusTracker", lambda c: tracker_fail)

    driver_fail = ROS2BridgeDriver()
    assert driver_fail.connect() is False
    assert driver_fail.status == RobotConnectionStatus.ERROR
    assert driver_fail.is_connected() is False
    assert driver_fail._client is None
    assert driver_fail._tracker is None
    tracker_fail.start.assert_not_called()
    client_fail.disconnect.assert_called_once()


def test_connect_exception_in_connect_or_tracker_start_cleans_up(monkeypatch):
    # Exception in client.connect()
    client_exc = MagicMock(spec=RosbridgeClient)
    client_exc.connect.side_effect = ConnectionRefusedError("connection refused")

    monkeypatch.setattr("mcp.core.rosbridge_client.RosbridgeClient", lambda host, port: client_exc)
    monkeypatch.setattr("mcp.core.task_status_tracker.TaskStatusTracker", MagicMock())

    driver1 = ROS2BridgeDriver()
    assert driver1.connect() is False
    assert driver1.status == RobotConnectionStatus.ERROR
    assert driver1.is_connected() is False
    assert driver1._client is None
    assert driver1._tracker is None
    client_exc.disconnect.assert_called_once()

    # Exception in tracker.start()
    client_ok = MagicMock(spec=RosbridgeClient)
    client_ok.connect.return_value = None
    client_ok.is_connected.return_value = True
    tracker_exc = MagicMock(spec=TaskStatusTracker)
    tracker_exc.start.side_effect = RuntimeError("subscribe failed")

    monkeypatch.setattr("mcp.core.rosbridge_client.RosbridgeClient", lambda host, port: client_ok)
    monkeypatch.setattr("mcp.core.task_status_tracker.TaskStatusTracker", lambda c: tracker_exc)

    driver2 = ROS2BridgeDriver()
    assert driver2.connect() is False
    assert driver2.status == RobotConnectionStatus.ERROR
    assert driver2.is_connected() is False
    assert driver2._client is None
    assert driver2._tracker is None
    tracker_exc.stop.assert_called_once()
    client_ok.disconnect.assert_called_once()


def test_poll_telemetry_invokes_real_tracker_method_and_handles_none(monkeypatch):
    mock_client = MagicMock(spec=RosbridgeClient)
    mock_client.connect.return_value = None
    mock_client.is_connected.return_value = True

    real_tracker = TaskStatusTracker(mock_client)
    monkeypatch.setattr("mcp.core.rosbridge_client.RosbridgeClient", lambda host, port: mock_client)
    monkeypatch.setattr("mcp.core.task_status_tracker.TaskStatusTracker", lambda c: real_tracker)

    driver = ROS2BridgeDriver(unit_id="ROS2-ROV-007")
    # Disconnected fallback
    snap_disc = driver.poll_telemetry()
    assert snap_disc.health_status == "DISCONNECTED"

    assert driver.connect() is True
    # Connected but no telemetry yet -> STANDBY
    snap_standby = driver.poll_telemetry()
    assert snap_standby.health_status == "STANDBY"
    assert snap_standby.unit_id == "ROS2-ROV-007"

    # Feed an authentic SysStatus.msg payload into real TaskStatusTracker
    real_tracker._on_sys_status(
        {
            "pose": {"pose": {"position": {"x": 12.5, "y": -3.2, "z": -420.0}}},
            "twist": {"linear": {"x": 0.8, "y": 0.1, "z": -0.05}},
            "alt": 18.5,
            "ctr_mode": 2,
            "health": 1,
            "task_list": [],
        }
    )

    snap = driver.poll_telemetry()
    assert snap.unit_id == "ROS2-ROV-007"
    assert snap.pose_x == 12.5
    assert snap.pose_y == -3.2
    assert snap.pose_z == -420.0
    assert snap.water_depth_m == 420.0
    assert snap.vel_x == 0.8
    assert snap.vel_y == 0.1
    assert snap.vel_z == -0.05
    assert snap.altitude_m == 18.5
    assert snap.control_mode == 2
    assert snap.health_status == 1
    assert isinstance(snap.raw_data, dict)
    assert snap.raw_data["alt"] == 18.5

    # Guard against raw_msg=None from a custom telemetry object
    driver._tracker = SimpleNamespace(
        latest_telemetry=lambda: ROVTelemetry(
            received_at="2026-11-26T00:00:00Z",
            pose_z=-100.0,
            raw_msg=None,  # type: ignore[arg-type]
        )
    )
    snap_none_raw = driver.poll_telemetry()
    assert snap_none_raw.water_depth_m == 100.0
    assert snap_none_raw.raw_data == {}


def test_submit_task_paths(monkeypatch):
    driver = ROS2BridgeDriver()
    # Disconnected
    res_disc = driver.submit_task({"task_type": "pipeline_inspection"})
    assert res_disc.status == CommandExecutionStatus.FAILED
    assert "not connected" in (res_disc.error_message or "")

    client = MagicMock(spec=RosbridgeClient)
    client.connect.return_value = None
    client.is_connected.return_value = True
    monkeypatch.setattr("mcp.core.rosbridge_client.RosbridgeClient", lambda host, port: client)
    monkeypatch.setattr("mcp.core.task_status_tracker.TaskStatusTracker", lambda c: _DummyTracker(c))
    assert driver.connect() is True

    # Success
    client.publish_task_cmd.return_value = 0x80001
    res_ok = driver.submit_task({"task_type": "pipeline_inspection"})
    assert res_ok.status == CommandExecutionStatus.SENT
    assert res_ok.task_id == str(0x80001)
    assert res_ok.task_id_hex == "0x80001"

    # Empty / 0 task_id
    client.publish_task_cmd.return_value = 0
    res_zero = driver.submit_task({"task_type": "pipeline_inspection"})
    assert res_zero.status == CommandExecutionStatus.FAILED

    # ProtocolValidationError and PublishOutcomeUnknown fail closed
    client.publish_task_cmd.side_effect = ProtocolValidationError("bad field")
    res_proto = driver.submit_task({"task_type": "pipeline_inspection"})
    assert res_proto.status == CommandExecutionStatus.FAILED
    assert "bad field" in (res_proto.error_message or "")

    client.publish_task_cmd.side_effect = PublishOutcomeUnknown("timeout waiting ack")
    res_unknown = driver.submit_task({"task_type": "pipeline_inspection"})
    assert res_unknown.status == CommandExecutionStatus.FAILED
    assert "timeout waiting ack" in (res_unknown.error_message or "")


def test_cancel_task_prefers_delete_task_and_handles_edge_cases(monkeypatch):
    driver = ROS2BridgeDriver()
    assert driver.cancel_task("0x80001").status == CommandExecutionStatus.FAILED

    # Real RosbridgeClient spec (has delete_task, no cancel_task)
    client_spec = MagicMock(spec=RosbridgeClient)
    client_spec.connect.return_value = None
    client_spec.is_connected.return_value = True
    client_spec.delete_task.return_value = 0x80002

    monkeypatch.setattr("mcp.core.rosbridge_client.RosbridgeClient", lambda host, port: client_spec)
    monkeypatch.setattr("mcp.core.task_status_tracker.TaskStatusTracker", lambda c: _DummyTracker(c))
    assert driver.connect() is True

    for raw in ("0x80001", "0X80001", "  524289  "):
        res = driver.cancel_task(raw)
        assert res.status == CommandExecutionStatus.ACCEPTED
        assert res.task_id_hex == "0x80002"
        client_spec.delete_task.assert_called_with(0x80001)

    # Invalid IDs
    for bad in ("bad_id", "", "   ", "-5"):
        res_bad = driver.cancel_task(bad)
        assert res_bad.status == CommandExecutionStatus.FAILED

    # Exception on delete_task
    client_spec.delete_task.side_effect = RuntimeError("rosbridge error")
    res_exc = driver.cancel_task("0x80001")
    assert res_exc.status == CommandExecutionStatus.FAILED

    # Unconstrained MagicMock (has BOTH delete_task and cancel_task) must prefer delete_task
    generic_mock = MagicMock()
    generic_mock.is_connected.return_value = True
    generic_mock.delete_task.return_value = 0x80005
    driver._client = generic_mock
    res_gen = driver.cancel_task("0x80001")
    assert res_gen.status == CommandExecutionStatus.ACCEPTED
    generic_mock.delete_task.assert_called_once_with(0x80001)
    generic_mock.cancel_task.assert_not_called()

    # Legacy mock with ONLY cancel_task still works
    class LegacyCancelOnlyClient:
        def is_connected(self):
            return True

        def cancel_task(self, tid):
            return tid == 0x80001

    driver._client = LegacyCancelOnlyClient()
    res_legacy = driver.cancel_task("0x80001")
    assert res_legacy.status == CommandExecutionStatus.ACCEPTED


def test_emergency_stop_prefers_suspend_all_and_handles_edge_cases(monkeypatch):
    driver = ROS2BridgeDriver()
    assert driver.emergency_stop().status == CommandExecutionStatus.FAILED

    # Real RosbridgeClient spec (has suspend_all, no emergency_stop)
    client_spec = MagicMock(spec=RosbridgeClient)
    client_spec.connect.return_value = None
    client_spec.is_connected.return_value = True
    client_spec.suspend_all.return_value = 0x80003

    monkeypatch.setattr("mcp.core.rosbridge_client.RosbridgeClient", lambda host, port: client_spec)
    monkeypatch.setattr("mcp.core.task_status_tracker.TaskStatusTracker", lambda c: _DummyTracker(c))
    assert driver.connect() is True

    res_ok = driver.emergency_stop()
    assert res_ok.status == CommandExecutionStatus.ACCEPTED
    assert res_ok.task_id_hex == "0x80003"
    client_spec.suspend_all.assert_called_once()

    # Failure / exception
    client_spec.suspend_all.return_value = 0
    assert driver.emergency_stop().status == CommandExecutionStatus.FAILED

    client_spec.suspend_all.side_effect = RuntimeError("ws closed")
    assert driver.emergency_stop().status == CommandExecutionStatus.FAILED

    # Unconstrained MagicMock must prefer suspend_all over emergency_stop
    generic_mock = MagicMock()
    generic_mock.is_connected.return_value = True
    generic_mock.suspend_all.return_value = 0x80009
    driver._client = generic_mock
    res_gen = driver.emergency_stop()
    assert res_gen.status == CommandExecutionStatus.ACCEPTED
    generic_mock.suspend_all.assert_called_once()
    generic_mock.emergency_stop.assert_not_called()

    # Legacy mock with ONLY emergency_stop still works
    class LegacyEstopOnlyClient:
        def is_connected(self):
            return True

        def emergency_stop(self):
            return True

    driver._client = LegacyEstopOnlyClient()
    assert driver.emergency_stop().status == CommandExecutionStatus.ACCEPTED


def test_disconnect_ordering_and_exception_resilience(monkeypatch):
    events = []

    class OrderedClient:
        def connect(self):
            return None

        def is_connected(self):
            return True

        def disconnect(self):
            events.append("client.disconnect")

    class OrderedTracker:
        def __init__(self, client):
            self.client = client

        def start(self):
            events.append("tracker.start")

        def stop(self):
            events.append("tracker.stop")

    monkeypatch.setattr("mcp.core.rosbridge_client.RosbridgeClient", lambda host, port: OrderedClient())
    monkeypatch.setattr("mcp.core.task_status_tracker.TaskStatusTracker", OrderedTracker)

    driver = ROS2BridgeDriver()
    assert driver.connect() is True
    driver.disconnect()
    assert events == ["tracker.start", "tracker.stop", "client.disconnect"]
    assert driver._tracker is None
    assert driver._client is None
    assert driver.status == RobotConnectionStatus.DISCONNECTED
    assert driver.is_connected() is False

    # Exception during stop & disconnect must not crash and must reset state
    driver._tracker = SimpleNamespace(stop=MagicMock(side_effect=RuntimeError("stop boom")))
    driver._client = SimpleNamespace(
        disconnect=MagicMock(side_effect=RuntimeError("disc boom")),
        is_connected=lambda: True,
    )
    driver._status = RobotConnectionStatus.CONNECTED

    driver.disconnect()
    assert driver._tracker is None
    assert driver._client is None
    assert driver.status == RobotConnectionStatus.DISCONNECTED
    assert driver.is_connected() is False
