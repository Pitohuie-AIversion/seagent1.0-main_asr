"""Exercise SSE progress without allowing HTTP lifetime to own a transaction."""

import json
import threading

import pytest
from flask import Flask, current_app, has_request_context, jsonify

import src.web.chat_stream as streaming


@pytest.fixture
def app(monkeypatch):
    application = Flask(__name__)
    application.config["TESTING"] = True
    monkeypatch.setattr(streaming, "_STREAM_WORKERS", threading.BoundedSemaphore(8))
    return application


def _start(app, process_turn):
    return streaming.start_chat_stream(
        process_turn, app=app, session_id="live-session", request_id="live-request",
    )


def _result(reply="已校验回复"):
    return {"code": 200, "reply": reply, "ui_state": {}, "request_id": "live-request"}


def _events(text):
    parsed = []
    for block in text.split("\n\n"):
        lines = block.splitlines()
        if len(lines) >= 2 and lines[0].startswith("event: ") and lines[1].startswith("data: "):
            raw = lines[1][6:]
            parsed.append((lines[0][7:], raw if raw == "[DONE]" else json.loads(raw)))
    return parsed


def test_ping_and_real_step_arrive_before_transaction_completes(app):
    release = threading.Event()
    entered = threading.Event()

    def process_turn(step_sink):
        assert current_app._get_current_object() is app
        assert not has_request_context()
        step_sink("step", {"step": "slot_filling", "message": "正在提取"})
        # The transport ignores state events until the transaction returns.
        step_sink("slot", {"key": "water_depth", "value": 300})
        entered.set()
        assert release.wait(5)
        return _result(), 200, [("slot", {"key": "water_depth", "value": 300})]

    @app.post("/stream")
    def route():
        return _start(app, process_turn)

    with app.test_client() as client:
        response = client.post("/stream", buffered=False)
        try:
            chunks = iter(response.response)
            first = next(chunks).decode()
            assert "event: ping" in first and '"status": "connected"' in first
            assert entered.wait(2)
            assert not release.is_set()
            queued = next(chunks).decode()
            assert "event: step" in queued and '"step": "queued"' in queued
            assert "请求已接收，正在等待处理。" in queued
            progress = next(chunks).decode()
            assert "event: step" in progress and "slot_filling" in progress
            assert "event: slot" not in first + queued + progress
            assert "event: delta" not in first + queued + progress
            assert "event: result" not in first + queued + progress
            release.set()
            tail = b"".join(chunks).decode()
            events = _events(tail)
            assert [name for name, _ in events].count("result") == 1
            assert [name for name, _ in events].count("end") == 1
            assert events[0] == ("slot", {"key": "water_depth", "value": 300})
            assert "".join(data["delta"] for name, data in events if name == "delta") == "已校验回复"
        finally:
            release.set()
            response.close()


def test_heartbeat_arrives_while_transaction_is_waiting(app, monkeypatch):
    monkeypatch.setattr(streaming, "_STREAM_HEARTBEAT_SECONDS", 0.01)
    release = threading.Event()

    def process_turn(step_sink):
        assert release.wait(5)
        return _result(), 200, []

    response = _start(app, process_turn)
    try:
        chunks = iter(response.response)
        assert "connected" in next(chunks)
        queued = next(chunks)
        assert '"step": "queued"' in queued
        assert "请求已接收，正在等待处理。" in queued
        heartbeat = next(chunks)
        assert "event: ping" in heartbeat and '"status": "processing"' in heartbeat
        assert not release.is_set()
        release.set()
        assert "event: result" in "".join(chunks)
    finally:
        release.set()
        response.close()


def test_disconnect_after_ping_preserves_transaction_and_releases_capacity(app, monkeypatch):
    capacity = threading.BoundedSemaphore(1)
    monkeypatch.setattr(streaming, "_STREAM_WORKERS", capacity)
    release = threading.Event()
    committed = threading.Event()
    session_lock = threading.Lock()
    effects = []

    def process_turn(step_sink):
        with session_lock:
            assert release.wait(5)
            effects.extend(["process", "save-before-dispatch", "dispatch", "save-after-dispatch"])
        committed.set()
        return _result(), 200, []

    response = _start(app, process_turn)
    try:
        assert "connected" in next(iter(response.response))
        response.close()
        assert not committed.is_set()
        release.set()
        assert committed.wait(2)
        assert capacity.acquire(timeout=2)
        capacity.release()
        assert effects == ["process", "save-before-dispatch", "dispatch", "save-after-dispatch"]
        assert session_lock.acquire(timeout=1)
        session_lock.release()
        recovery = _start(app, lambda sink: (_result(), 200, []))
        assert "event: result" in recovery.get_data(as_text=True)
        recovery.close()
    finally:
        release.set()
        response.close()


def test_full_progress_queue_and_disconnect_do_not_block_commit(app, monkeypatch):
    capacity = threading.BoundedSemaphore(1)
    monkeypatch.setattr(streaming, "_STREAM_WORKERS", capacity)
    all_progress_emitted = threading.Event()
    release = threading.Event()
    committed = threading.Event()

    def process_turn(step_sink):
        for index in range(1000):
            step_sink("step", {"step": "processing", "index": index})
        all_progress_emitted.set()
        assert release.wait(5)
        committed.set()
        return _result(), 200, []

    response = _start(app, process_turn)
    try:
        assert all_progress_emitted.wait(2)
        assert "connected" in next(iter(response.response))
        response.close()
        release.set()
        assert committed.wait(2)
        assert capacity.acquire(timeout=2)
        capacity.release()
    finally:
        release.set()
        response.close()


def test_capacity_rejection_is_503_and_recovers_after_workers_finish(app):
    release = threading.Event()
    calls = []

    def process_turn(step_sink):
        calls.append(True)
        assert release.wait(5)
        return _result(), 200, []

    @app.post("/stream")
    def route():
        try:
            return _start(app, process_turn)
        except streaming.StreamCapacityError:
            return jsonify({"code": 503, "error": "ServerBusy", "retryable": True}), 503

    responses = []
    client = app.test_client()
    try:
        for _ in range(8):
            response = client.post("/stream", buffered=False)
            assert response.status_code == 200
            responses.append(response)
        rejected = client.post("/stream", buffered=False)
        assert rejected.status_code == 503
        assert rejected.get_json() == {"code": 503, "error": "ServerBusy", "retryable": True}
        release.set()
        for response in responses:
            assert "event: result" in response.get_data(as_text=True)
        assert len(calls) == 8
        recovery = client.post("/stream", buffered=False)
        assert recovery.status_code == 200
        assert "event: result" in recovery.get_data(as_text=True)
        recovery.close()
        assert len(calls) == 9
    finally:
        release.set()
        for response in responses:
            response.close()


@pytest.mark.parametrize("unexpected", [False, True])
def test_worker_error_has_no_result_and_releases_capacity(app, monkeypatch, unexpected):
    monkeypatch.setattr(streaming, "_STREAM_WORKERS", threading.BoundedSemaphore(1))

    def process_turn(step_sink):
        step_sink("step", {"step": "guard_check"})
        if unexpected:
            raise RuntimeError("unexpected worker failure")
        return {"code": 409, "error": "SessionReset", "request_id": "live-request", "retryable": True}, 409, []

    response = _start(app, process_turn)
    events = _events(response.get_data(as_text=True))
    response.close()
    names = [name for name, _ in events]
    assert names[0] == "ping"
    assert names.count("error") == 1
    assert "result" not in names and "delta" not in names and "end" not in names
    error = next(data for name, data in events if name == "error")
    assert error["error"] == ("InternalServerError" if unexpected else "SessionReset")
    assert error["code"] == (500 if unexpected else 409)
    assert error["request_id"] == "live-request"
    recovery = _start(app, lambda sink: (_result(), 200, []))
    assert "event: result" in recovery.get_data(as_text=True)
    recovery.close()


@pytest.mark.parametrize('bad_value', [object(), '\ud800'])
def test_serialization_failure_never_streams_partial_success(app, bad_value):
    payload = _result()
    payload["unserializable"] = bad_value
    response = _start(app, lambda sink: (payload, 200, [("slot", {"key": "water_depth"})]))
    raw = response.get_data(as_text=True)
    response.close()
    assert "InternalServerError" in raw
    assert "event: error" in raw
    assert all(f"event: {kind}" not in raw for kind in ("slot", "delta", "result", "end"))


def test_thread_start_failure_releases_capacity(app, monkeypatch):
    monkeypatch.setattr(streaming, "_STREAM_WORKERS", threading.BoundedSemaphore(1))

    def fail_start(self):
        raise RuntimeError("cannot start worker")

    with monkeypatch.context() as patcher:
        patcher.setattr(threading.Thread, "start", fail_start)
        with pytest.raises(RuntimeError, match="cannot start worker"):
            _start(app, lambda sink: (_result(), 200, []))

    recovery = _start(app, lambda sink: (_result(), 200, []))
    assert "event: result" in recovery.get_data(as_text=True)
    recovery.close()
