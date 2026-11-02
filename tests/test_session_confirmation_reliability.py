"""HTTP confirmations belong to a viewed task; reloads preserve accepted turns."""

import concurrent.futures
import copy
import json
import threading
from pathlib import Path

import pytest

import web_backend
import src.hot_reload as hot_reload
import src.web.routes_chat as chat_routes
import src.web.routes_time_history as history_routes
import src.web.state as state
from src.dialogue_manager import DialogueManager
from src.session.ui_state_builder import build_frontend_ui_state
from tests.interaction_plan_support import slot_candidate


@pytest.fixture
def ready_task(monkeypatch, tmp_path):
    from tests.test_failure_recovery_benchmark import FailureRecoveryBenchmarkTest
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    monkeypatch.setenv("DISABLE_HOT_RELOAD", "1")
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", [])
    case = FailureRecoveryBenchmarkTest()
    case.setUp()
    case._setup_full_confirming_task()
    manager = case.dm
    manager.session_id = "confirmation-reliability"
    monkeypatch.setattr(state, "_sessions_manager", {manager.session_id: manager})
    monkeypatch.setattr(state, "_shared_llm", manager.llm)
    monkeypatch.setattr(state, "_shared_kb", manager.kb)
    monkeypatch.setattr(state, "_shared_mcp_bridge", None)
    monkeypatch.setattr(chat_routes, "print_status", lambda _: None)
    return case, tmp_path


def send(manager, endpoint, message, request_id, token=None):
    body = {"session_id": manager.session_id, "request_id": request_id, "message": message}
    if token is not None:
        body["expected_confirmation_token"] = token
    with web_backend.app.test_client() as client:
        response = client.post(endpoint, json=body)
        if response.mimetype != "text/event-stream":
            return response.status_code, response.get_json()
        for frame in response.get_data(as_text=True).split("\n\n"):
            if frame.startswith(("event: result\n", "event: error\n")):
                payload = json.loads(frame.split("data: ", 1)[1])
                return payload["code"], payload
    raise AssertionError("missing terminal response")


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
def test_unversioned_confirmation_cannot_publish(ready_task, endpoint):
    case, tmp_path = ready_task
    before = case.dm.slot_store.export_snapshot()
    status, payload = send(case.dm, endpoint, "确认发布", "missing-token")
    assert status == 409
    assert payload["error"] == "ConfirmationStale"
    assert payload["ui_state"]["confirmation_token"]
    assert case.dm.slot_store.export_snapshot() == before
    assert not list((tmp_path / "task").glob("task_intent_*.json"))


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
def test_old_tab_cannot_publish_new_parameters_but_reviewed_retry_can(ready_task, endpoint):
    case, tmp_path = ready_task
    manager = case.dm
    old_token = build_frontend_ui_state(manager)["confirmation_token"]
    case._queue_write(slot_candidate("water_depth", 320, raw_value="320米"))
    assert send(manager, endpoint, "把水深改成320米", "other-tab")[0] == 200
    assert manager.phase == "confirming"
    before = manager.slot_store.export_snapshot()

    status, conflict = send(manager, endpoint, "确认发布", "stale-confirm", old_token)

    assert status == 409 and conflict["error"] == "ConfirmationStale"
    assert manager.slot_store.export_snapshot() == before
    assert conflict["ui_state"]["confirmation_token"] != old_token
    assert not list((tmp_path / "task").glob("task_intent_*.json"))
    fresh_token = conflict["ui_state"]["confirmation_token"]
    success = send(manager, endpoint, "确认发布", "reviewed-confirm", fresh_token)
    assert success[0] == 200 and manager.phase == "done"
    artifacts = list((tmp_path / "task").glob("task_intent_*.json"))
    assert len(artifacts) == 1
    assert json.loads(artifacts[0].read_text())["location"]["water_depth_m"] == 320
    # Replay precedes token validation, so a lost success cannot publish twice.
    assert send(manager, endpoint, "确认发布", "reviewed-confirm", fresh_token) == success
    assert len(list((tmp_path / "task").glob("task_intent_*.json"))) == 1


def test_reset_cannot_reuse_a_confirmation_for_identical_slot_version(ready_task):
    case, _ = ready_task
    old = case.dm
    snapshot = old.export_snapshot()
    old_token = build_frontend_ui_state(old)["confirmation_token"]
    with web_backend.app.test_client() as client:
        assert client.post("/api/reset", json={"session_id": old.session_id}).status_code == 200
    replacement = DialogueManager(old.llm, old.kb, session_id=old.session_id)
    replacement.load_snapshot(snapshot)
    state._sessions_manager[old.session_id] = replacement
    assert replacement.slot_store.version == snapshot["slot_store"]["store_version"]
    assert build_frontend_ui_state(replacement)["confirmation_token"] != old_token
    assert send(replacement, "/api/chat", "确认发布", "after-reset", old_token)[0] == 409


def test_repeated_read_only_render_keeps_confirmation_token_stable(ready_task):
    case, _ = ready_task
    before = case.dm.export_snapshot()
    first = build_frontend_ui_state(case.dm)
    second = build_frontend_ui_state(case.dm)
    assert first["confirmation_token"] == second["confirmation_token"]
    assert case.dm.export_snapshot() == before


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
@pytest.mark.parametrize("token", ["\ud800", 123, "x" * 513])
def test_malformed_confirmation_token_fails_before_mutation(ready_task, endpoint, token):
    case, _ = ready_task
    before = case.dm.export_snapshot()
    assert send(case.dm, endpoint, "确认发布", "invalid-token", token)[0] == 400
    assert case.dm.export_snapshot() == before


def test_shared_dispatch_token_guard_handles_invalid_unicode(ready_task):
    from src.session.ui_state_builder import confirmation_conflict
    case, _ = ready_task
    assert confirmation_conflict(case.dm, "\ud800")["error"] == "ConfirmationStale"


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
def test_runtime_risk_confirmation_rejects_a_previous_review(ready_task, monkeypatch, endpoint):
    case, _ = ready_task
    manager = case.dm
    manager.phase = "done"
    manager.runtime_execution_review = {"status": "pending", "validation_fingerprint": "previous-risk"}
    manager.ros2_dispatch = {"state": "BLOCKED", "retry_allowed": True}
    old_token = build_frontend_ui_state(manager)["confirmation_token"]
    manager.runtime_execution_review["validation_fingerprint"] = "current-risk"
    calls = []
    monkeypatch.setattr(manager, "process", lambda *args, **kwargs: calls.append(args[0]) or "已核对当前风险。")

    status, conflict = send(manager, endpoint, "确认执行风险", "old-risk", old_token)

    assert status == 409
    assert calls == []
    assert conflict["done"] is True
    assert conflict["ros2_dispatch"] == manager.ros2_dispatch
    assert conflict["ui_state"]["actions"]["can_acknowledge_runtime_warning"] is True
    assert conflict["ui_state"]["runtime_execution_review"]["validation_fingerprint"] == "current-risk"
    assert send(manager, endpoint, "确认执行风险", "current-risk",
                conflict["ui_state"]["confirmation_token"])[0] == 200
    assert calls == ["确认执行风险"]


def test_history_forks_and_saves_current_draft_without_reusing_its_confirmation(ready_task, monkeypatch):
    case, tmp_path = ready_task
    manager = case.dm
    manager.conversation_history.append({"role": "user", "content": "尚未发布的当前草稿"})
    before = copy.deepcopy(manager.export_snapshot())
    old_token = build_frontend_ui_state(manager)["confirmation_token"]
    monkeypatch.setattr(history_routes, "load_history", lambda _: copy.deepcopy(before))
    with web_backend.app.test_client() as client:
        response = client.post("/api/history/load", json={
            "history_id": "example.json", "session_id": manager.session_id, "restore_mode": "fork",
        })
    assert response.status_code == 200
    data = response.get_json()
    assert data["session_id"] != manager.session_id
    assert data["previous_session_id"] == manager.session_id
    assert manager.export_snapshot() == before
    assert state._sessions_manager[manager.session_id] is manager
    assert data["ui_state"]["confirmation_token"] != old_token
    backup = tmp_path / "history" / data["preserved_draft_history_id"]
    assert json.loads(backup.read_text())["phase"] == "confirming"
    assert not list((tmp_path / "task").glob("task_intent_*.json"))


def test_failed_draft_backup_keeps_old_session_and_does_not_open_history(ready_task, monkeypatch):
    case, _ = ready_task
    manager = case.dm
    before = manager.export_snapshot()
    monkeypatch.setattr(history_routes, "load_history", lambda _: copy.deepcopy(before))
    monkeypatch.setattr(history_routes, "save_conversation", lambda **_: (_ for _ in ()).throw(OSError("disk full")))
    with web_backend.app.test_client() as client:
        response = client.post("/api/history/load", json={
            "history_id": "example.json", "session_id": manager.session_id,
        })
    assert response.status_code == 500
    assert response.get_json()["error"] == "DraftPreservationFailed"
    assert state._sessions_manager == {manager.session_id: manager}
    assert manager.export_snapshot() == before


def test_reload_waits_for_accepted_turn_and_keeps_its_replay(ready_task, monkeypatch):
    case, _ = ready_task
    manager = case.dm
    original_process = manager.process
    entered, release, copied = threading.Event(), threading.Event(), threading.Event()
    original_export = manager.export_snapshot
    calls = []

    def blocked(*args, **kwargs):
        calls.append(args[0])
        entered.set()
        assert release.wait(10)
        return original_process(*args, **kwargs)

    def observed_export():
        copied.set()
        return original_export()

    monkeypatch.setattr(manager, "process", blocked)
    monkeypatch.setattr(manager, "export_snapshot", observed_export)
    old_token = build_frontend_ui_state(manager)["confirmation_token"]
    case._queue_write(slot_candidate("water_depth", 320, raw_value="320米"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        chat = pool.submit(send, manager, "/api/chat", "把水深改成320米", "during-reload")
        assert entered.wait(5)
        reload = pool.submit(hot_reload.perform_reload, [str(Path("config/state.yaml").resolve())])
        try:
            assert not copied.wait(0.3), "reload copied a task while its accepted turn was still running"
            acquired = state._sessions_lock.acquire(timeout=0.3)
            assert acquired, "reload waited for the session while holding the registry lock"
            state._sessions_lock.release()
        finally:
            release.set()
        response = chat.result(timeout=15)
        assert reload.result(timeout=15)[0] is True
    active = state._sessions_manager[manager.session_id]
    assert active is not manager
    assert active.task_state["water_depth"] == response[1]["collected"]["water_depth"] == 320
    assert build_frontend_ui_state(active)["confirmation_token"] != old_token
    assert send(active, "/api/chat", "把水深改成320米", "during-reload") == response
    assert len(calls) == 1


def test_failed_reload_preserves_live_manager_instead_of_erasing_it(ready_task, monkeypatch):
    case, _ = ready_task
    manager = case.dm
    before = manager.export_snapshot()
    monkeypatch.setattr(manager, "export_snapshot", lambda: (_ for _ in ()).throw(ValueError("broken snapshot")))
    result = hot_reload.perform_reload([str(Path("config/state.yaml").resolve())])
    assert result[0] is False
    assert state._sessions_manager[manager.session_id] is manager
    assert manager.task_state == before["task_state"]


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
def test_control_result_remains_replayable_when_history_save_fails(ready_task, monkeypatch, endpoint):
    case, _ = ready_task
    manager = case.dm
    manager.phase = "done"
    calls = []
    monkeypatch.setattr(manager, "process", lambda *args, **kwargs: calls.append(args[0]) or "控制请求已处理。")
    monkeypatch.setattr(chat_routes, "save_dispatch_history", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    response = send(manager, endpoint, "停止任务", "control-with-save-failure")
    assert response[0] == 200
    assert "未能保存" in response[1]["persistence_warning"]
    assert "请勿重复发送控制指令" in response[1]["reply"]
    assert send(manager, endpoint, "停止任务", "control-with-save-failure") == response
    assert calls == ["停止任务"]
