"""Invalid chat requests must fail before creating or mutating a session."""

import json
from unittest.mock import Mock

import pytest

import web_backend
import src.web.state as state
from src.dialogue_manager import DialogueManager


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(web_backend.app.config, "TESTING", True)
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", [])
    with web_backend.app.test_client() as client:
        yield client


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream", "/api/reset"])
@pytest.mark.parametrize("body", ["null", "[]", '"text"', "12", "true", "{broken"])
def test_invalid_json_object_returns_400_without_creating_session(client, monkeypatch, endpoint, body):
    get_manager = Mock(side_effect=AssertionError("invalid input reached session creation"))
    monkeypatch.setattr(web_backend, "get_or_create_manager", get_manager)

    response = client.post(endpoint, data=body, content_type="application/json")

    assert response.status_code == 400
    assert response.get_json()["error"] == "InvalidJSON"
    assert response.get_json()["retryable"] is False
    get_manager.assert_not_called()


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
@pytest.mark.parametrize("field,value", [
    ("message", None), ("message", 12), ("message", []), ("message", {}),
    ("session_id", []), ("session_id", {}), ("session_id", 12),
    ("session_id", "  "), ("session_id", False),
    ("request_id", []), ("request_id", {}), ("request_id", 12),
    ("request_id", "  "), ("request_id", False),
])
def test_invalid_fields_preserve_existing_session(client, monkeypatch, endpoint, field, value):
    sid = "invalid-chat-request-existing-session"
    manager = DialogueManager(session_id=sid)
    manager.conversation_history.append({"role": "user", "content": "existing task context"})
    before = manager.export_snapshot()
    monkeypatch.setitem(state._sessions_manager, sid, manager)
    process = Mock(side_effect=AssertionError("invalid input reached dialogue processing"))
    monkeypatch.setattr(manager, "process", process)
    get_manager = Mock(side_effect=AssertionError("invalid input reached session lookup"))
    monkeypatch.setattr(web_backend, "get_or_create_manager", get_manager)
    payload = {"session_id": sid, "request_id": "req_invalid_input", "message": "继续任务"}
    payload[field] = value

    response = client.post(endpoint, json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"] == "InvalidRequest"
    assert response.get_json()["retryable"] is False
    assert isinstance(response.get_json()["request_id"], str)
    assert manager.export_snapshot() == before
    process.assert_not_called()
    get_manager.assert_not_called()


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
def test_bad_request_does_not_prevent_next_valid_turn(client, monkeypatch, endpoint):
    sid = "chat-validation-recovery"
    manager = DialogueManager(session_id=sid)
    monkeypatch.setitem(state._sessions_manager, sid, manager)

    rejected = client.post(endpoint, json={"session_id": sid, "message": None})
    assert rejected.status_code == 400
    response = client.post(endpoint, json={"session_id": sid, "message": "现在几点？"})

    assert response.status_code == 200
    if endpoint.endswith("/stream"):
        events = response.get_data(as_text=True).split("\n\n")
        result = next(event for event in events if event.startswith("event: result\n"))
        payload = json.loads(result.split("data: ", 1)[1])
    else:
        payload = response.get_json()
    assert payload["session_id"] == sid
    assert payload["reply"]
    assert manager.conversation_history[-2]["content"] == "现在几点？"
