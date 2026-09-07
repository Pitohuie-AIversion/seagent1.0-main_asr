"""
tests/test_sse_stream_api.py - Server-Sent Events (SSE) 流式接口契约测试
"""

import json
import pytest
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

    def test_reload_events_stream(self, client):
        resp = client.get("/api/dev/reload-events/stream?after=0")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("Content-Type", "")
        first_chunk = next(resp.response).decode("utf-8")
        assert "event: ping" in first_chunk
        assert "connected" in first_chunk
