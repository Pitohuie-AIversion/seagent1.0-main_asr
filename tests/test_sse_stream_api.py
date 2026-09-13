"""
tests/test_sse_stream_api.py - Server-Sent Events (SSE) 流式接口契约测试
"""

import json
import threading
from unittest.mock import Mock
import pytest
import web_backend
from web_backend import app, init_manager
from src.dialogue_manager import DialogueManager


@pytest.fixture
def client():
    app.config["TESTING"] = True
    dm = DialogueManager()
    init_manager(dm)
    with app.test_client() as client:
        yield client


class TestSSEStreamAPI:
    """测试 /api/chat/stream SSE 事件流更新机制"""

    def test_empty_message_returns_400(self, client):
        resp = client.post(
            "/api/chat/stream",
            json={"session_id": "test_sse_01", "message": "   "},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error"] == "EmptyMessage"

    def test_sse_stream_event_flow(self, client):
        resp = client.post(
            "/api/chat/stream",
            json={"session_id": "test_sse_02", "message": "现在几点？"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("Content-Type", "")

        raw_stream = resp.get_data(as_text=True)
        assert "event: ping" in raw_stream
        assert "event: step" in raw_stream
        assert "event: delta" in raw_stream
        assert "event: result" in raw_stream
        assert "event: end" in raw_stream

        # 解析 result 事件
        result_payload = None
        for line in raw_stream.split("\n"):
            if line.startswith("data: ") and '"ui_state"' in line:
                result_payload = json.loads(line[6:])
                break

        assert result_payload is not None
        assert result_payload["code"] == 200
        assert "ui_state" in result_payload
        assert "reply" in result_payload
        assert result_payload["session_id"] == "test_sse_02"

    def test_fine_grained_step_event_flow(self, client):
        resp = client.post(
            "/api/chat/stream",
            json={"session_id": "test_sse_steps_01", "message": "安排水下管道巡检任务"},
        )
        assert resp.status_code == 200
        raw_stream = resp.get_data(as_text=True)

        # 收集所有 step 事件中的 payload
        step_payloads = []
        lines = raw_stream.split("\n")
        for i, line in enumerate(lines):
            if line == "event: step" and i + 1 < len(lines):
                data_line = lines[i + 1]
                if data_line.startswith("data: "):
                    step_payloads.append(json.loads(data_line[6:]))

        assert len(step_payloads) >= 2, f"Expected multiple fine-grained steps, got: {step_payloads}"
        step_names = [item.get("step") for item in step_payloads]
        assert "guard_check" in step_names
        assert any(s in step_names for s in ["intent_routing", "slot_filling", "synthesizing"])

    def test_reload_events_stream(self, client):
        resp = client.get("/api/dev/reload-events/stream?after=0")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("Content-Type", "")
        first_chunk = next(resp.response).decode("utf-8")
        assert "event: ping" in first_chunk
        assert "connected" in first_chunk


def test_disconnect_after_delta_completes_dispatch_and_releases_session_lock(monkeypatch):
    manager = DialogueManager()
    sid = "sse-disconnect-regression"
    def finish_task(*args, **kwargs):
        manager.phase = "done"
        return "任务已经完成，等待机器人确认。"

    monkeypatch.setattr(manager, "process", finish_task)
    monkeypatch.setitem(web_backend._sessions_manager, sid, manager)
    monkeypatch.setattr(web_backend, "get_or_create_manager", lambda _: manager)
    monkeypatch.setattr(web_backend, "build_frontend_ui_state", lambda _: {})
    save = Mock()
    dispatch = Mock(return_value={"state": "SENT"})
    monkeypatch.setattr(web_backend, "save_conversation", save)
    monkeypatch.setattr(web_backend, "_dispatch_ros2_on_done_transition", dispatch)
    with app.test_client() as client:
        response = client.post("/api/chat/stream", json={"session_id": sid, "message": "确认"}, buffered=False)
        try:
            for chunk in response.response:
                if b"event: delta" in chunk:
                    break
            save.assert_called_once()
            dispatch.assert_called_once()
            acquired = []
            def try_lock():
                locked = manager._session_lock.acquire(timeout=0.5)
                acquired.append(locked)
                if locked:
                    manager._session_lock.release()
            worker = threading.Thread(target=try_lock)
            worker.start()
            worker.join(timeout=2)
            assert acquired == [True]
        finally:
            response.close()
