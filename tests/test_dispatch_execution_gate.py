"""Execution prerequisites and durable send evidence across production entrypoints."""
import copy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.dispatch.task_dispatch import dispatch_completed_task
from src.validation.validator import ValidationResult, Violation
from mcp.core.bridge_service import SEAgentMCPBridgeService, DispatchOutcomeUnknown
from mcp.core.dialogue_mcp_integration import dispatch_dialogue_result


@pytest.fixture
def execution(monkeypatch):
    now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("src.dispatch.task_dispatch.get_current_datetime", lambda: now)
    validation = ValidationResult(overall_status="valid", validated_at=now.isoformat(),
        task_version=1, validation_version=1, validation_fingerprint="current", state_snapshot=None, violations=[])
    intent = {"intent_id": "TI2026091701", "task_type": "pipeline_inspection",
              "time": {"start": now.isoformat(), "end": (now + timedelta(hours=2)).isoformat()},
              "conditions": {"runtime_validation": {"required": True}}}
    manager = SimpleNamespace(phase="done", final_result=intent, task_state={"equipment_unit_id": "unit-1"},
        slot_store=SimpleNamespace(version=1), validator=SimpleNamespace(validate_task=Mock(return_value=validation)),
        _get_valid_acknowledgements=Mock(return_value=[]), ros2_dispatch=None)
    bridge = Mock()
    bridge.get_dispatch_record.return_value = None
    bridge.is_healthy.return_value = True
    bridge.dispatch_intent.return_value = 0x80001
    return manager, bridge, now, validation


def test_future_intent_is_held_even_with_healthy_transport(execution):
    manager, bridge, now, _ = execution
    manager.final_result["time"]["start"] = (now + timedelta(days=1)).isoformat()
    result = dispatch_completed_task(manager, bridge)
    assert result["state"] == "SCHEDULED"
    assert manager.ros2_dispatch == result
    bridge.dispatch_intent.assert_not_called()
    manager.validator.validate_task.assert_not_called()


def test_due_retry_runs_runtime_validation_and_preserves_original_intent(execution):
    manager, bridge, _, _ = execution
    original = copy.deepcopy(manager.final_result)
    manager.ros2_dispatch = {"state": "SCHEDULED"}
    assert dispatch_completed_task(manager, bridge)["state"] == "SENT"
    manager.validator.validate_task.assert_called_once_with(manager.task_state, task_version=1, purpose="runtime_execution")
    bridge.dispatch_intent.assert_called_once_with(original)
    assert manager.final_result == original


@pytest.mark.parametrize("status", ["blocked_hard", "validation_error", "pending_runtime_validation", "unknown_new_status"])
def test_due_task_is_blocked_by_fresh_runtime_result(execution, status):
    manager, bridge, _, validation = execution
    validation.overall_status = status
    result = dispatch_completed_task(manager, bridge)
    assert result["state"] == "BLOCKED"
    bridge.dispatch_intent.assert_not_called()


def test_expired_task_never_sends(execution):
    manager, bridge, now, _ = execution
    manager.final_result["time"]["end"] = now.isoformat()
    result = dispatch_completed_task(manager, bridge)
    assert result["state"] == "BLOCKED" and not result["retry_allowed"]
    bridge.dispatch_intent.assert_not_called()


def test_missing_runtime_context_fails_closed(execution):
    manager, bridge, _, _ = execution
    del manager.validator
    assert dispatch_completed_task(manager, bridge)["state"] == "BLOCKED"
    bridge.dispatch_intent.assert_not_called()


def test_new_soft_constraint_is_not_silently_accepted(execution):
    manager, bridge, _, validation = execution
    validation.overall_status = "blocked_soft"
    validation.violations = [Violation(constraint_id="C-SOFT", constraint_name="current", severity="soft", message="海流条件变化")]
    assert dispatch_completed_task(manager, bridge)["state"] == "BLOCKED"
    bridge.dispatch_intent.assert_not_called()


def test_non_web_entrypoint_uses_same_future_gate(execution):
    manager, bridge, now, _ = execution
    manager.final_result["time"]["start"] = (now + timedelta(days=1)).isoformat()
    result = dispatch_dialogue_result(manager, bridge)
    assert result["status"] == "pending"
    assert result["ros2_dispatch"]["state"] == "SCHEDULED"
    bridge.dispatch_intent.assert_not_called()


def _bridge(tmp_path):
    bridge = SEAgentMCPBridgeService(dispatch_records_dir=tmp_path)
    bridge.is_healthy = Mock(return_value=True)
    bridge.client.publish_task_cmd = Mock()
    return bridge


def test_interrupted_send_is_unknown_after_restart_without_resending(tmp_path, execution):
    manager, _, _, _ = execution
    bridge = _bridge(tmp_path)
    bridge.client.publish_task_cmd.side_effect = SystemExit("crash before socket write")
    with pytest.raises(SystemExit):
        bridge.dispatch_intent(manager.final_result, task_id=0x80001)
    restarted = _bridge(tmp_path)
    result = dispatch_completed_task(manager, restarted)
    assert result["state"] == "UNKNOWN"
    assert result["task_id"] == 0x80001
    restarted.client.publish_task_cmd.assert_not_called()
    with pytest.raises(DispatchOutcomeUnknown):
        restarted.dispatch_intent(manager.final_result)
    restarted.client.publish_task_cmd.assert_not_called()


def test_positive_receipt_resolves_unknown_without_a_second_send(tmp_path, execution):
    manager, _, _, _ = execution
    bridge = _bridge(tmp_path)
    bridge.client.publish_task_cmd.side_effect = SystemExit()
    with pytest.raises(SystemExit):
        bridge.dispatch_intent(manager.final_result, task_id=0x80001)
    restarted = _bridge(tmp_path)
    restarted.tracker.get_task_status = Mock(return_value=SimpleNamespace(task_type=2, task_id=0x80001))
    result = dispatch_completed_task(manager, restarted)
    assert result["state"] == "SENT"
    restarted.client.publish_task_cmd.assert_not_called()
    assert json.loads(restarted._dispatch_records_path.read_text())["TI2026091701"]["receipt_confirmed"] is True


def test_failed_transport_retry_reuses_reserved_ros_task_id(tmp_path, execution):
    manager, _, _, _ = execution
    bridge = _bridge(tmp_path)
    bridge.client.publish_task_cmd.side_effect = RuntimeError("offline")
    assert dispatch_completed_task(manager, bridge)["state"] == "FAILED"
    task_id = bridge._dispatch_records[manager.final_result["intent_id"]]["task_id"]
    restarted = _bridge(tmp_path)
    assert dispatch_completed_task(manager, restarted)["task_id"] == task_id
    assert restarted.client.publish_task_cmd.call_args.kwargs["task_id"] == task_id


def test_socket_write_exception_after_payload_is_unknown_and_never_resent(tmp_path, execution):
    from mcp.core.rosbridge_client import TASK_TOPIC, TASK_MESSAGE_TYPE
    manager, _, _, _ = execution
    bridge = _bridge(tmp_path)
    payloads = []
    def partial_write(payload):
        payloads.append(json.loads(payload))
        raise ConnectionError("connection lost after write started")
    bridge.client._ws = SimpleNamespace(connected=True, send=partial_write)
    bridge.client.publish_task_cmd = lambda *args, **kwargs: bridge.client._send({
        "op": "publish", "topic": TASK_TOPIC, "type": TASK_MESSAGE_TYPE, "msg": {"task_id": kwargs["task_id"]}})
    result = dispatch_completed_task(manager, bridge)
    assert result["state"] == "UNKNOWN" and len(payloads) == 1
    assert dispatch_completed_task(manager, bridge)["state"] == "UNKNOWN"
    assert len(payloads) == 1


def test_sent_record_write_failure_is_unknown_not_retryable_failed(tmp_path, execution, monkeypatch):
    manager, _, _, _ = execution
    bridge = _bridge(tmp_path)
    original = bridge._persist_dispatch_records
    def fail_sent():
        if any(record["dispatch_state"] == "SENT" for record in bridge._dispatch_records.values()):
            raise OSError("cannot persist completed send")
        return original()
    monkeypatch.setattr(bridge, "_persist_dispatch_records", fail_sent)
    result = dispatch_completed_task(manager, bridge)
    assert result["state"] == "UNKNOWN"
    bridge.client.publish_task_cmd.assert_called_once()
    assert dispatch_completed_task(manager, bridge)["state"] == "UNKNOWN"
    bridge.client.publish_task_cmd.assert_called_once()


def test_real_runtime_validator_rejects_expired_robot_telemetry(tmp_path, monkeypatch):
    from tests.test_failure_recovery_benchmark import FailureRecoveryBenchmarkTest
    from src.temporal.simulated_time import get_current_datetime
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    case = FailureRecoveryBenchmarkTest()
    case.setUp()
    case._setup_full_confirming_task()
    manager = case.dm
    manager.process("确认发布", request_id="publish_before_expiry")
    assert manager.phase == "done"
    unit = manager.task_state["equipment_unit_id"]
    snapshot = manager.kb.get_unit_state_snapshot(unit)
    old_time = (get_current_datetime() - timedelta(hours=2)).isoformat()
    snapshot["updated_at"] = old_time
    snapshot["state"]["updated_at"] = old_time
    snapshot["state"]["update_timestamp"] = old_time
    monkeypatch.setattr(manager.kb, "get_unit_state_snapshot", lambda *args, **kwargs: copy.deepcopy(snapshot))
    bridge = Mock()
    bridge.get_dispatch_record.return_value = None
    result = dispatch_completed_task(manager, bridge)
    assert result["state"] == "BLOCKED"
    assert old_time in result["error"]
    bridge.dispatch_intent.assert_not_called()


def test_legacy_history_without_dispatch_evidence_can_only_be_checked(execution):
    manager, bridge, _, _ = execution
    manager.ros2_dispatch = {"state": "UNKNOWN", "reason": "legacy_history"}
    assert dispatch_completed_task(manager, bridge)["state"] == "UNKNOWN"
    bridge.dispatch_intent.assert_not_called()
    bridge.get_dispatch_record.return_value = {"dispatch_state": "SENT", "task_id": 0x80001}
    assert dispatch_completed_task(manager, bridge)["state"] == "SENT"
    bridge.dispatch_intent.assert_not_called()
