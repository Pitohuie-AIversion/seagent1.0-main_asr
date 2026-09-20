"""Exercise active-session identity and the built-in authenticated proxy."""

import io
import json
import threading
from unittest.mock import patch

import pytest

import dashboard_server
import web_backend
from src.dialogue_manager import DialogueManager
import src.web.routes_time_history as history_routes
import src.web.state as state


def test_history_restore_rejects_manager_removed_by_reset(monkeypatch):
    sid = "history-reset-identity-regression"
    manager = DialogueManager(session_id=sid)
    snapshot = manager.export_snapshot()
    snapshot["conversation_history"] = [{"role": "user", "content": "historical context"}]
    monkeypatch.setitem(state._sessions_manager, sid, manager)
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", [])
    monkeypatch.setattr(history_routes, "load_history", lambda _: snapshot)
    selected = threading.Event()
    reset_done = threading.Event()
    responses = []

    def select_before_reset(requested_sid):
        assert requested_sid == sid
        selected.set()
        assert reset_done.wait(5)
        return manager

    def restore():
        with web_backend.app.test_client() as client:
            responses.append(client.post("/api/history/load", json={
                "history_id": "example.json", "session_id": sid,
            }))

    monkeypatch.setattr(history_routes, "get_or_create_manager", select_before_reset)
    thread = threading.Thread(target=restore)
    thread.start()
    try:
        assert selected.wait(5)
        with web_backend.app.test_client() as client:
            assert client.post("/api/reset", json={"session_id": sid}).status_code == 200
    finally:
        reset_done.set()
        thread.join(5)
    assert not thread.is_alive()
    assert responses[0].status_code == 409
    assert responses[0].get_json()["error"] == "SessionReset"
    assert sid not in state._sessions_manager
    assert manager.conversation_history != snapshot["conversation_history"]


def test_history_restore_preserves_active_session_identity(monkeypatch):
    active_sid = "active-history-target"
    manager = DialogueManager(session_id=active_sid)
    snapshot = manager.export_snapshot()
    snapshot["session_id"] = "historical-source-session"
    snapshot["conversation_history"] = [{"role": "user", "content": "historical context"}]
    monkeypatch.setitem(state._sessions_manager, active_sid, manager)
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", [])
    monkeypatch.setattr(history_routes, "load_history", lambda _: snapshot)
    with web_backend.app.test_client() as client:
        response = client.post("/api/history/load", json={
            "history_id": "example.json", "session_id": active_sid,
        })
    assert response.status_code == 200
    assert manager.session_id == response.get_json()["session_id"] == active_sid
    assert snapshot["session_id"] == "historical-source-session"
    assert state._sessions_manager[active_sid] is manager
    assert manager.conversation_history == snapshot["conversation_history"]
    assert "ros2_dispatch" in response.get_json()


def test_dashboard_proxy_forwards_credentials_without_replaying():
    requests = []

    class UpstreamResponse(io.BytesIO):
        status = 200

    def urlopen(request, timeout):
        requests.append(request)
        assert timeout == 8.0
        return UpstreamResponse(json.dumps({"code": 200}).encode())

    with patch.object(dashboard_server.urllib.request, "urlopen", urlopen):
        with dashboard_server.app.test_client() as client:
            response = client.post("/api/config/gateway", json={"host": "localhost", "port": 9090},
                headers={"Authorization": "Bearer example-token", "X-API-Token": "alternate-token"})
    assert response.status_code == 200
    assert len(requests) == 1
    sent_headers = dict((k.lower(), v) for k, v in requests[0].header_items())
    assert sent_headers["authorization"] == "Bearer example-token"
    assert sent_headers["x-api-token"] == "alternate-token"
    assert "example-token" not in requests[0].full_url


def test_both_dashboard_hosts_serve_shared_authentication_assets():
    for application, page in ((web_backend.app, "/"), (web_backend.app, "/dashboard"),
                              (dashboard_server.app, "/")):
        with application.test_client() as client:
            response = client.get(page)
            assert response.status_code == 200
            html = response.get_data(as_text=True)
            assert 'id="apiCredentialsDialog"' in html
            assert 'src="static/js/api-auth.js"' in html
            assert client.get("/static/js/api-auth.js").status_code == 200


@pytest.mark.parametrize("dispatch_state", ["FAILED", "UNKNOWN", "SCHEDULED", "SENT"])
def test_dispatch_outcome_survives_history_restore_and_session_refresh(monkeypatch, tmp_path, dispatch_state):
    from src.dispatch.task_dispatch import save_dispatch_history
    from tests.interaction_plan_support import ScriptedLLM, make_plan
    from tests.test_slot_consistency import seed_complete_valid_pipeline_task

    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", [])
    source = DialogueManager(llm=ScriptedLLM(default_plan=make_plan("READ")),
                             session_id="historical-dispatch-source")
    seed_complete_valid_pipeline_task(source, source.kb)
    source.process("确认发布")
    if source.phase == "blocked_soft":
        source.process("忽略警告")
        source.process("确认发布")
    assert source.phase == "done"
    outcome = {"state": dispatch_state, "message": "persisted dispatch evidence",
               "retry_allowed": dispatch_state != "SENT"}
    source.ros2_dispatch = outcome.copy()
    history_id = save_dispatch_history(source)

    sid = "active-dispatch-target"
    target = DialogueManager(llm=source.llm, kb=source.kb, session_id=sid)
    monkeypatch.setitem(state._sessions_manager, sid, target)
    with web_backend.app.test_client() as client:
        restored = client.post("/api/history/load", json={"session_id": sid, "history_id": history_id})
        assert restored.status_code == 200, restored.get_json()
        refreshed = client.get("/api/session/state", query_string={"session_id": sid})
    assert restored.get_json()["ui_state"]["phase"] == "done"
    assert restored.get_json()["ros2_dispatch"] == outcome
    assert refreshed.get_json()["ros2_dispatch"] == outcome
    assert target.session_id == sid
    assert target.final_result == source.final_result
