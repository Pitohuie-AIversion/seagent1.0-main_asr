"""Unit tests for the Robot Abstraction Layer (RAL) and Robot Gateway."""

import pytest
from src.robot_gateway import (
    CommandExecutionStatus,
    RobotCommandResult,
    RobotConnectionStatus,
    StandardTelemetrySnapshot,
    SimulatedRobotDriver,
    RobotGateway,
)


def test_models_and_telemetry_snapshot():
    snap = StandardTelemetrySnapshot(
        unit_id="ROV-75-001",
        timestamp_iso="2026-11-12T10:00:00Z",
        pose_x=12.5,
        pose_y=-45.0,
        pose_z=-320.5,
        heading_deg=180.0,
        battery_pct=95.0,
        active_task_id="task_101",
        active_task_status="ONGOING",
    )
    assert snap.unit_id == "ROV-75-001"
    assert snap.water_depth_m == 320.5

    data = snap.to_dict()
    assert data["unit_id"] == "ROV-75-001"
    assert data["water_depth_m"] == 320.5
    assert data["pose_z"] == -320.5
    assert data["active_task_id"] == "task_101"

    cmd_res = RobotCommandResult(
        command_id="cmd_001",
        status=CommandExecutionStatus.SENT,
        task_id="task_101",
        message="Task transmitted",
    )
    res_dict = cmd_res.to_dict()
    assert res_dict["command_id"] == "cmd_001"
    assert res_dict["status"] == "SENT"


def test_simulated_driver_lifecycle():
    driver = SimulatedRobotDriver(unit_id="SIM-ROV-001", initial_depth_m=100.0)
    assert driver.unit_id == "SIM-ROV-001"
    assert not driver.is_connected()

    # Connection
    assert driver.connect() is True
    assert driver.is_connected()
    assert driver.status == RobotConnectionStatus.CONNECTED

    # Submit task with depth adjustment
    intent = {
        "intent_id": "intent_pipe_001",
        "task_id": "0x1001",
        "task_type": "pipeline_inspection",
        "water_depth": 550.0,
    }
    result = driver.submit_task(intent)
    assert result.status == CommandExecutionStatus.SENT
    assert result.task_id == "0x1001"

    # Telemetry reflects descent to target depth and active task
    telem = driver.poll_telemetry()
    assert telem.water_depth_m == 550.0
    assert telem.active_task_id == "0x1001"
    assert telem.active_task_status == "ONGOING"

    # Complete task
    driver.complete_active_task()
    telem_after = driver.poll_telemetry()
    assert telem_after.active_task_status == "FINISH"

    # Emergency stop
    stop_res = driver.emergency_stop()
    assert stop_res.status == CommandExecutionStatus.ACCEPTED
    assert driver.control_mode == "EMERGENCY_STOP"

    # Disconnect
    driver.disconnect()
    assert not driver.is_connected()


def test_simulated_driver_fault_injection():
    driver = SimulatedRobotDriver(unit_id="SIM-FAULT-001")

    # Connection error fault
    driver.inject_fault("connection_error")
    assert driver.connect() is False
    assert driver.status == RobotConnectionStatus.ERROR

    # Clear fault and reconnect
    driver.inject_fault(None)
    assert driver.connect() is True

    # Hardware fault rejection
    driver.inject_fault("hardware_fault")
    assert driver.health == "CRITICAL_FAULT"

    res = driver.submit_task({"task_id": "test_task"})
    assert res.status == CommandExecutionStatus.REJECTED
    assert "hardware fault detected" in (res.error_message or "")


def test_robot_gateway_driver_registry_and_routing():
    gateway = RobotGateway()

    rov1 = SimulatedRobotDriver(unit_id="ROV-OBS-001")
    rov2 = SimulatedRobotDriver(unit_id="ROV-WORK-002")

    gateway.register_driver(rov1, is_default=True)
    gateway.register_driver(rov2, is_default=False)

    drivers_list = gateway.list_drivers()
    assert len(drivers_list) == 2
    assert any(d["unit_id"] == "ROV-OBS-001" and d["is_default"] for d in drivers_list)

    # Dispatch without explicit robot routes to default (ROV-OBS-001)
    res1 = gateway.dispatch_task({"task_id": "task_default", "water_depth": 200.0})
    assert res1.status == CommandExecutionStatus.SENT
    assert rov1.active_task_id == "task_default"
    assert rov2.active_task_id is None

    # Dispatch targeted to ROV-WORK-002
    res2 = gateway.dispatch_task({"task_id": "task_work", "water_depth": 800.0}, unit_id="ROV-WORK-002")
    assert res2.status == CommandExecutionStatus.SENT
    assert rov2.active_task_id == "task_work"

    # Fleet telemetry aggregation
    fleet_telemetry = gateway.poll_fleet_telemetry()
    assert "ROV-OBS-001" in fleet_telemetry
    assert "ROV-WORK-002" in fleet_telemetry
    assert fleet_telemetry["ROV-OBS-001"].water_depth_m == 200.0
    assert fleet_telemetry["ROV-WORK-002"].water_depth_m == 800.0

    # Emergency stop all
    stop_results = gateway.emergency_stop_all()
    assert stop_results["ROV-OBS-001"].status == CommandExecutionStatus.ACCEPTED
    assert stop_results["ROV-WORK-002"].status == CommandExecutionStatus.ACCEPTED
    assert rov1.control_mode == "EMERGENCY_STOP"
    assert rov2.control_mode == "EMERGENCY_STOP"

    # Unregister driver
    gateway.unregister_driver("ROV-WORK-002")
    assert len(gateway.list_drivers()) == 1
    assert gateway.get_driver("ROV-WORK-002") is None


def test_api_dev_gateway_endpoints():
    from web_backend import app
    client = app.test_client()

    # GET /api/dev/gateway/drivers
    resp_drivers = client.get("/api/dev/gateway/drivers")
    assert resp_drivers.status_code == 200
    data_drivers = resp_drivers.get_json()
    assert data_drivers["ok"] is True
    assert isinstance(data_drivers["drivers"], list)
    assert len(data_drivers["drivers"]) >= 1

    # GET /api/dev/gateway/telemetry
    resp_telem = client.get("/api/dev/gateway/telemetry")
    assert resp_telem.status_code == 200
    data_telem = resp_telem.get_json()
    assert data_telem["ok"] is True
    assert "fleet_telemetry" in data_telem
    assert isinstance(data_telem["fleet_telemetry"], dict)


def test_robot_gateway_bridge_compliance_and_dispatch_completed_task(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from datetime import datetime, timedelta, timezone
    from src.dispatch.task_dispatch import dispatch_completed_task
    from src.validation.validator import ValidationResult

    now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("src.dispatch.task_dispatch.get_current_datetime", lambda: now)

    gateway = RobotGateway()
    driver = SimulatedRobotDriver(unit_id="ROV-BRIDGE-001")
    assert not gateway.is_healthy()
    gateway.register_driver(driver, is_default=True)
    driver.connect()
    assert gateway.is_healthy()

    validation = ValidationResult(
        overall_status="valid",
        validated_at=now.isoformat(),
        task_version=1,
        validation_version=1,
        validation_fingerprint="current",
        state_snapshot=None,
        violations=[],
    )
    intent = {
        "intent_id": "TI-GATEWAY-001",
        "task_type": "pipeline_inspection",
        "time": {"start": now.isoformat(), "end": (now + timedelta(hours=2)).isoformat()},
        "conditions": {"runtime_validation": {"required": True}},
        "equipment_unit_id": "ROV-BRIDGE-001",
    }
    manager = SimpleNamespace(
        phase="done",
        final_result=intent,
        task_state={"equipment_unit_id": "ROV-BRIDGE-001"},
        slot_store=SimpleNamespace(version=1),
        validator=SimpleNamespace(validate_task=Mock(return_value=validation)),
        _get_valid_acknowledgements=Mock(return_value=[]),
        ros2_dispatch=None,
    )

    # Dispatch through RobotGateway as bridge
    result = dispatch_completed_task(manager, gateway)
    assert result["state"] == "SENT"
    assert "task_id" in result
    task_id = result["task_id"]

    # Verify dispatch record and idempotency
    record = gateway.get_dispatch_record(intent)
    assert record is not None
    assert record["dispatch_state"] == "SENT"
    assert record["task_id"] == task_id

    # Repeated dispatch returns SENT idempotently without re-execution
    repeat_res = dispatch_completed_task(manager, gateway)
    assert repeat_res["state"] == "SENT"
    assert repeat_res["task_id"] == task_id

