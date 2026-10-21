"""Published plans stay immutable while execution controls and reviews progress."""
import copy
import threading
from unittest.mock import Mock

import pytest

from src.dispatch.task_dispatch import dispatch_completed_task
from src.session.history_manager import list_history, load_history
from tests.interaction_plan_support import make_plan


def chat(client, manager, message):
    state = client.get("/api/session/state", query_string={"session_id": manager.session_id}).get_json()
    token = state.get("ui_state", {}).get("confirmation_token")
    return client.post("/api/chat", json={
        "session_id": manager.session_id, "message": message,
        "expected_confirmation_token": token,
    }).get_json()


@pytest.fixture(params=[False, True], ids=["legacy-state", "session-state-v2"])
def published(request, monkeypatch, tmp_path):
    from tests.test_failure_recovery_benchmark import FailureRecoveryBenchmarkTest
    import web_backend
    import src.web.state as state
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    monkeypatch.setattr("src.dialogue_manager.is_session_state_v2_enabled", lambda: request.param)
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", [])
    case = FailureRecoveryBenchmarkTest()
    case.setUp()
    case._setup_full_confirming_task()
    manager = case.dm
    manager.session_id = "execution-lifecycle-followup"
    monkeypatch.setitem(state._sessions_manager, manager.session_id, manager)
    bridge = Mock()
    bridge.get_dispatch_record.return_value = None
    bridge.is_healthy.return_value = False
    bridge.dispatch_intent.return_value = 0x80001
    bridge.suspend_task.return_value = 0x80002
    bridge.delete_task.return_value = 0x80003
    manager.mcp_bridge = bridge
    monkeypatch.setattr(state, "_shared_mcp_bridge", bridge)
    client = web_backend.app.test_client()
    result = chat(client, manager, "确认发布")
    if manager.phase == "blocked_soft":
        chat(client, manager, "忽略警告")
        result = chat(client, manager, "确认发布")
    assert manager.phase == "done", result
    assert result["ros2_dispatch"]["state"] == "FAILED"
    return client, manager, bridge, tmp_path


def control(client, manager, action):
    manager.llm.queue_plan(make_plan("CONTROL", emergency_action=action))
    labels = {"cancel": "取消", "pause": "暂停", "stop": "停止", "abort": "终止"}
    return chat(client, manager, labels[action] + "当前任务")


def dispatch(client, manager):
    state = client.get("/api/session/state", query_string={"session_id": manager.session_id}).get_json()
    return client.post("/api/mcp/dispatch", json={
        "session_id": manager.session_id,
        "expected_confirmation_token": state["ui_state"].get("confirmation_token"),
    }).get_json()["ros2_dispatch"]


def test_unsent_cancellation_is_durable_and_old_snapshot_cannot_dispatch(published):
    client, manager, bridge, _ = published
    old_snapshot = manager.export_snapshot()
    result = control(client, manager, "cancel")
    assert manager.last_control_request["outcome"] == "not_sent_cancelled", result
    assert result["ros2_dispatch"]["retry_allowed"] is False
    saved = load_history(list_history()[0]["id"])
    assert saved["last_control_request"]["outcome"] == "not_sent_cancelled"
    assert saved["conversation_history"][-1]["content"] == result["reply"]

    # Reintroduce a copy taken before cancellation: cancellation belongs to the
    # immutable intent, not just whichever session currently displays it.
    manager.load_snapshot(old_snapshot)
    bridge.is_healthy.return_value = True
    outcome = dispatch(client, manager)
    assert outcome["state"] == "BLOCKED"
    assert outcome["retry_allowed"] is False
    bridge.dispatch_intent.assert_not_called()
    bridge.delete_task.assert_not_called()


def test_unsent_pause_only_resumes_on_explicit_dispatch(published):
    client, manager, bridge, _ = published
    control(client, manager, "pause")
    bridge.is_healthy.return_value = True
    assert manager.last_control_request["outcome"] == "not_sent_paused"
    assert dispatch_completed_task(manager, bridge)["state"] == "BLOCKED"
    bridge.dispatch_intent.assert_not_called()
    assert dispatch(client, manager)["state"] == "SENT"
    bridge.dispatch_intent.assert_called_once()


def test_old_snapshot_must_show_the_pause_before_explicit_resume(published):
    client, manager, bridge, _ = published
    old_snapshot = manager.export_snapshot()
    control(client, manager, "pause")
    manager.load_snapshot(old_snapshot)
    bridge.is_healthy.return_value = True
    assert dispatch(client, manager)["state"] == "BLOCKED"
    assert manager.last_control_request["outcome"] == "not_sent_paused"
    bridge.dispatch_intent.assert_not_called()
    assert dispatch(client, manager)["state"] == "SENT"


@pytest.mark.parametrize("terminal_action,later_action", [
    ("cancel", "pause"), ("abort", "stop"),
])
def test_old_snapshot_cannot_weaken_cancel_into_a_resumable_pause(
    published, terminal_action, later_action,
):
    client, manager, bridge, _ = published
    old_snapshot = manager.export_snapshot()
    control(client, manager, terminal_action)
    manager.load_snapshot(old_snapshot)
    result = control(client, manager, later_action)
    assert manager.last_control_request["action"] == terminal_action
    assert manager.last_control_request["outcome"] == "not_sent_cancelled"
    assert result["ros2_dispatch"]["retry_allowed"] is False
    bridge.is_healthy.return_value = True
    assert dispatch(client, manager)["state"] == "BLOCKED"
    bridge.dispatch_intent.assert_not_called()


@pytest.mark.parametrize("action,method", [
    ("pause", "suspend_task"), ("stop", "suspend_task"),
    ("cancel", "delete_task"), ("abort", "delete_task"),
])
def test_sent_task_control_uses_existing_ros_adapter_without_claiming_robot_stopped(
    published, action, method,
):
    client, manager, bridge, _ = published
    bridge.get_dispatch_record.return_value = {"dispatch_state": "SENT", "task_id": 0x80001}
    bridge.is_healthy.return_value = True
    result = control(client, manager, action)
    getattr(bridge, method).assert_called_once_with(0x80001)
    assert manager.last_control_request["outcome"] == "sent"
    assert "遥测" in result["reply"]
    assert "机器人已停止" not in result["reply"]
    saved = load_history(list_history()[0]["id"])
    assert saved["last_control_request"]["command_task_id"] == getattr(bridge, method).return_value
    bridge.dispatch_intent.assert_not_called()


def test_control_transport_uncertainty_is_preserved_without_automatic_retry(published):
    from mcp.core.rosbridge_client import PublishOutcomeUnknown
    client, manager, bridge, _ = published
    bridge.get_dispatch_record.return_value = {"dispatch_state": "SENT", "task_id": 0x80001}
    bridge.is_healthy.return_value = True
    bridge.suspend_task.side_effect = PublishOutcomeUnknown("socket write interrupted")
    result = control(client, manager, "pause")
    assert manager.last_control_request["outcome"] == "unknown"
    assert "未确认" in result["reply"]
    assert "已暂停" not in result["reply"]
    saved = load_history(list_history()[0]["id"])
    manager.load_snapshot(saved)
    bridge.suspend_task.assert_called_once()


def pending_runtime_review(published):
    client, manager, bridge, _ = published
    manager.kb.state_info.set_status(manager.task_state["equipment_unit_id"], {"turbidity": 7.0})
    bridge.is_healthy.return_value = True
    assert dispatch(client, manager)["state"] == "BLOCKED"
    assert manager.runtime_execution_review["status"] == "pending"
    assert any(v["constraint_id"] == "C013" for v in manager.runtime_execution_review["violations"])
    return client, manager, bridge


@pytest.mark.parametrize("delivery_state", ["SENT", "UNKNOWN"])
@pytest.mark.parametrize("evidence_source", ["bridge", "session"])
def test_runtime_confirmation_preserves_existing_delivery_evidence(
    published, delivery_state, evidence_source,
):
    from mcp.core.bridge_service import DispatchOutcomeUnknown
    client, manager, bridge = pending_runtime_review(published)
    final_before = copy.deepcopy(manager.final_result)
    slots_before = {key: slot.to_dict() for key, slot in manager.slot_store.slots.items()}
    acknowledgements_before = copy.deepcopy(manager.slot_store.validation_acknowledgements)
    if evidence_source == "bridge":
        if delivery_state == "SENT":
            bridge.get_dispatch_record.return_value = {"dispatch_state": "SENT", "task_id": 0x80001}
        else:
            bridge.get_dispatch_record.side_effect = DispatchOutcomeUnknown(0x80001)
    else:
        manager.ros2_dispatch = {
            "state": delivery_state, "task_id": 0x80001,
            "message": "已有发送记录", "retry_allowed": False,
        }

    result = chat(client, manager, "确认执行风险")

    assert result["ros2_dispatch"]["state"] == delivery_state, result
    assert result["ros2_dispatch"]["task_id"] == 0x80001
    if delivery_state == "SENT" or evidence_source == "session":
        assert result["ros2_dispatch"]["retry_allowed"] is False
    assert manager.runtime_execution_review is None
    assert result["ui_state"]["actions"]["can_acknowledge_runtime_warning"] is False
    assert "尚未发送任务" not in result["reply"]
    assert manager.final_result == final_before
    assert {key: slot.to_dict() for key, slot in manager.slot_store.slots.items()} == slots_before
    assert manager.slot_store.validation_acknowledgements == acknowledgements_before
    saved = load_history(list_history()[0]["id"])
    assert saved["ros2_dispatch"]["state"] == delivery_state
    assert saved["runtime_execution_review"] is None
    bridge.dispatch_intent.assert_not_called()


@pytest.mark.parametrize("action", ["cancel", "pause"])
def test_old_runtime_review_reconciles_durable_control_before_confirmation(published, action):
    client, manager, bridge = pending_runtime_review(published)
    old_snapshot = manager.export_snapshot()
    final_before = copy.deepcopy(manager.final_result)
    control(client, manager, action)
    expected_control = copy.deepcopy(manager.last_control_request)
    manager.load_snapshot(old_snapshot)

    result = chat(client, manager, "确认执行风险")

    assert manager.control_state == f"{action}_requested", result
    assert manager.last_control_request == expected_control
    assert manager.runtime_execution_review is None
    assert result["ui_state"]["execution_control"]["last_control_request"] == expected_control
    assert result["ui_state"]["actions"]["can_acknowledge_runtime_warning"] is False
    assert result["ros2_dispatch"]["state"] == "BLOCKED"
    assert result["ros2_dispatch"]["reason"] == "execution_control"
    assert result["ros2_dispatch"]["retry_allowed"] is (action == "pause")
    assert manager.final_result == final_before
    saved = load_history(list_history()[0]["id"])
    assert saved["last_control_request"] == expected_control
    assert saved["runtime_execution_review"] is None
    bridge.dispatch_intent.assert_not_called()


def test_current_runtime_warning_can_be_acknowledged_without_republishing(published):
    client, manager, bridge = pending_runtime_review(published)
    final_before = copy.deepcopy(manager.final_result)
    slots_before = {key: slot.to_dict() for key, slot in manager.slot_store.slots.items()}
    task_files_before = list((published[3] / "task").glob("task_intent_*.json"))
    reply = chat(client, manager, "确认执行风险")
    assert manager.runtime_execution_review["status"] == "acknowledged", reply
    assert manager.phase == "done"
    assert manager.final_result == final_before
    assert {key: slot.to_dict() for key, slot in manager.slot_store.slots.items()} == slots_before
    assert list((published[3] / "task").glob("task_intent_*.json")) == task_files_before
    bridge.dispatch_intent.assert_not_called()
    assert dispatch(client, manager)["state"] == "SENT"
    bridge.dispatch_intent.assert_called_once_with(final_before)


def test_changed_runtime_conditions_require_new_visible_review_before_acknowledgement(published):
    client, manager, bridge = pending_runtime_review(published)
    reviewed = manager.runtime_execution_review["validation_fingerprint"]
    manager.kb.state_info.set_status(manager.task_state["equipment_unit_id"], {"turbidity": 12.0})
    result = chat(client, manager, "确认执行风险")
    assert manager.runtime_execution_review["status"] == "pending", result
    assert manager.runtime_execution_review["validation_fingerprint"] != reviewed
    assert "变化" in result["reply"]
    assert not any(ack.constraint_id == "C014" for ack in manager.slot_store.validation_acknowledgements)
    chat(client, manager, "确认执行风险")
    assert manager.runtime_execution_review["status"] == "acknowledged"
    manager.kb.state_info.set_status(manager.task_state["equipment_unit_id"], {"turbidity": 8.0})
    assert dispatch(client, manager)["state"] == "BLOCKED"
    assert manager.runtime_execution_review["status"] == "pending"
    bridge.dispatch_intent.assert_not_called()


def test_runtime_hard_block_cannot_be_acknowledged(published):
    client, manager, bridge = pending_runtime_review(published)
    manager.kb.state_info.set_status(manager.task_state["equipment_unit_id"], {"overall_status": "unavailable"})
    result = chat(client, manager, "确认执行风险")
    assert manager.runtime_execution_review["status"] == "blocked_hard", result
    assert manager.phase == "done"
    assert dispatch(client, manager)["state"] == "BLOCKED"
    bridge.dispatch_intent.assert_not_called()


def test_runtime_review_and_acknowledgement_survive_history_restore(published):
    client, manager, bridge = pending_runtime_review(published)
    result = chat(client, manager, "确认执行风险")
    assert manager.runtime_execution_review["status"] == "acknowledged", result
    saved = load_history(list_history()[0]["id"])
    assert saved["runtime_execution_review"] == manager.runtime_execution_review
    final_before = copy.deepcopy(manager.final_result)
    manager.load_snapshot(saved)
    assert manager.runtime_execution_review == saved["runtime_execution_review"]
    assert manager.final_result == final_before
    assert dispatch(client, manager)["state"] == "SENT"


def test_intent_cancellation_serializes_with_dispatch_from_another_session(published, monkeypatch):
    from src.dialogue_manager import DialogueManager
    from src.session.intent_router import IntentRouteResult
    import src.handlers.execution_control as controls
    _, manager, bridge, _ = published
    other = DialogueManager(manager.llm, manager.kb, session_id="forked-execution")
    other.load_snapshot(manager.export_snapshot())
    bridge.is_healthy.return_value = True
    record_written = threading.Event()
    release_control = threading.Event()
    dispatch_started = threading.Event()
    results = {}
    original_write = controls.write_execution_control

    def delayed_write(*args, **kwargs):
        original_write(*args, **kwargs)
        record_written.set()
        assert release_control.wait(5)

    monkeypatch.setattr(controls, "write_execution_control", delayed_write)
    route = IntentRouteResult(
        interaction_type="QUERY", confidence=1.0, reason="cancel",
        dialogue_mode="emergency_intervention", emergency_action="cancel",
    )

    def cancel():
        results["cancel"] = manager.execution_control_handler.handle_emergency_intervention(
            "取消当前任务", route,
        )

    def send():
        dispatch_started.set()
        results["dispatch"] = dispatch_completed_task(other, bridge)

    cancelling = threading.Thread(target=cancel)
    sending = threading.Thread(target=send)
    cancelling.start()
    try:
        assert record_written.wait(5)
        sending.start()
        assert dispatch_started.wait(5)
        assert "dispatch" not in results
    finally:
        release_control.set()
        cancelling.join(5)
        if sending.ident is not None:
            sending.join(5)
    assert not cancelling.is_alive() and not sending.is_alive()
    assert results["dispatch"]["state"] == "BLOCKED"
    bridge.dispatch_intent.assert_not_called()


@pytest.mark.parametrize("fail_after_transport", [False, True])
def test_control_persistence_failure_is_visible_and_never_replays_transport(
    published, monkeypatch, fail_after_transport,
):
    import src.handlers.execution_control as controls
    client, manager, bridge, _ = published
    bridge.get_dispatch_record.return_value = {"dispatch_state": "SENT", "task_id": 0x80001}
    bridge.is_healthy.return_value = True
    original_write = controls.write_execution_control
    writes = 0

    def fail_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == (2 if fail_after_transport else 1):
            raise OSError("isolated control disk failure")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(controls, "write_execution_control", fail_write)
    result = control(client, manager, "pause")
    assert manager.last_control_request["persistence_error"] is True
    assert "保存" in result["reply"]
    assert bridge.suspend_task.call_count == int(fail_after_transport)
    assert manager.last_control_request["outcome"] == ("unknown" if fail_after_transport else "failed")
    client.get("/api/session/state", query_string={"session_id": manager.session_id})
    assert bridge.suspend_task.call_count == int(fail_after_transport)
