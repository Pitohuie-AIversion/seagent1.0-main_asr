"""Deliver progress while an accepted chat transaction finishes independently."""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from flask import Flask, Response

logger = logging.getLogger(__name__)

_STREAM_WORKER_LIMIT = 8
_STREAM_WORKERS = threading.BoundedSemaphore(_STREAM_WORKER_LIMIT)
_STREAM_HEARTBEAT_SECONDS = 5.0
_STREAM_PROGRESS_LIMIT = 64

EventSink = Callable[[str, dict], None]
ProcessTurn = Callable[[EventSink], tuple[dict, int, list[tuple[str, dict]]]]


class StreamCapacityError(RuntimeError):
    """No streaming worker can accept another transaction right now."""


def _frame(event_type: str, payload: Any) -> str:
    frame = f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    # Fail in the worker, before success events, instead of during socket encoding.
    frame.encode('utf-8')
    return frame


def start_chat_stream(
    process_turn: ProcessTurn,
    *,
    app: Flask,
    session_id: str,
    request_id: str,
) -> Response:
    """Start one bounded worker, then stream progress and its committed result.

    ``process_turn`` owns the entire session transaction and releases its locks
    before returning. Only progress is visible before that return; response
    facts and slot/warning events wait for successful result serialization.
    Closing the HTTP stream stops observation, not an accepted transaction.
    """
    capacity = _STREAM_WORKERS
    if not capacity.acquire(blocking=False):
        raise StreamCapacityError("流式请求繁忙，请稍后重试。")

    progress: queue.Queue[str | None] = queue.Queue(maxsize=_STREAM_PROGRESS_LIMIT)
    completed = threading.Event()
    disconnected = threading.Event()
    outcome: dict[str, Any] = {}

    def step_sink(event_type: str, payload: dict) -> None:
        if event_type != "step" or not isinstance(payload, dict) or disconnected.is_set():
            return
        try:
            frame = _frame("step", payload)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid SSE progress request_id=%s", request_id, exc_info=True)
            return
        try:
            progress.put_nowait(frame)
        except queue.Full:
            # Progress is advisory. A slow/disconnected observer must never
            # block persistence or consume an unbounded event buffer.
            return

    def work() -> None:
        try:
            with app.app_context():
                payload, status, events = process_turn(step_sink)
                result_frame = _frame("result" if status == 200 else "error", payload)
                reply = payload.get("reply", "") if status == 200 else ""
                if not isinstance(reply, str):
                    raise TypeError("Chat reply must be a string")
                event_frames = [
                    _frame(event_type, event_data)
                    for event_type, event_data in events
                    if status == 200 and event_type in {"slot", "warning"}
                ]
                outcome.update(status=status, result=result_frame, reply=reply, events=event_frames)
        except Exception:
            logger.exception("Unexpected SSE worker failure request_id=%s", request_id)
            outcome.update(
                status=500,
                result=_frame("error", {
                    "code": 500,
                    "error": "InternalServerError",
                    "msg": "服务器内部错误，请稍后重试。",
                    "request_id": request_id,
                    "retryable": True,
                }),
                reply="",
                events=[],
            )
        finally:
            capacity.release()
            completed.set()
            try:
                # Wake an idle reader immediately; a full queue already wakes
                # it, so completion never waits for the client to consume.
                progress.put_nowait(None)
            except queue.Full:
                pass

    try:
        worker = threading.Thread(target=work, name="seagent-chat-stream", daemon=True)
        worker.start()
    except Exception:
        capacity.release()
        raise

    def event_stream():
        try:
            yield _frame("ping", {
                "status": "connected", "session_id": session_id, "request_id": request_id,
            })
            yield _frame("step", {
                "step": "queued", "message": "请求已接收，正在等待处理。", "request_id": request_id,
            })
            while not completed.is_set() or not progress.empty():
                try:
                    frame = progress.get(timeout=_STREAM_HEARTBEAT_SECONDS)
                except queue.Empty:
                    if not completed.is_set():
                        yield _frame("ping", {"status": "processing", "request_id": request_id})
                    continue
                if frame is not None:
                    yield frame

            if outcome["status"] != 200:
                yield outcome["result"]
                return

            yield from outcome["events"]
            reply = outcome["reply"]
            for offset in range(0, len(reply), 4):
                yield _frame("delta", {"delta": reply[offset:offset + 4], "request_id": request_id})
            yield outcome["result"]
            yield "event: end\ndata: [DONE]\n\n"
        finally:
            disconnected.set()

    response = Response(event_stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
    response.call_on_close(disconnected.set)
    return response
