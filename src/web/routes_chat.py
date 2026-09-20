"""
src/web/routes_chat.py - 核心对话流、SSE 打字机事件推流、会话重置与状态查询路由
"""

import inspect
import json
import logging
import uuid
from typing import Any
from flask import Blueprint, Response, jsonify, request, stream_with_context
import src.web.state as state
from src.exceptions import (
    IdReservationError,
    IntentIdConflict,
    TaskPersistenceError,
    TaskRollbackError,
)
from src.session.history_manager import save_conversation
from src.dispatch.task_dispatch import dispatch_completed_task, save_dispatch_history
from src.slots.slot_store import SlotVersionConflict
from src.session.ui_state_builder import build_frontend_ui_state
from src.web.state import (
    _require_api_token,
    get_mcp_bridge,
    get_or_create_manager,
    print_status,
)

logger = logging.getLogger(__name__)

chat_bp = Blueprint("chat", __name__)


def _get_backend_symbol(name: str, fallback: Any) -> Any:
    return state.get_service_symbol(name, fallback)


def _dispatch_ros2_on_done_transition(mgr, phase_before):
    """Dispatch only the state-machine edge into done, never a later done request."""
    task_intent = getattr(mgr, "final_result", None)
    if phase_before == "done" or mgr.phase != "done" or not task_intent:
        return None
    return dispatch_completed_task(mgr, get_mcp_bridge())


def _persist_and_dispatch_done_transition(mgr, phase_before):
    """当会话首次到达 done 阶段时，先保存会话快照再尝试下发 ROS 2。"""
    save_fn = _get_backend_symbol("save_conversation", save_conversation)
    if getattr(mgr, "_pending_published_intent", None) is not None:
        try:
            save_dispatch_history(mgr, save_fn)
        except Exception as exc:
            logging.error("保存待核对发布记录失败: %s", exc, exc_info=True)
        return getattr(mgr, "ros2_dispatch", None)
    if phase_before == "done" or mgr.phase != "done":
        return getattr(mgr, "ros2_dispatch", None)

    try:
        save_dispatch_history(mgr, save_fn)
    except Exception as exc:
        logging.error("保存历史快照失败: %s", exc, exc_info=True)

    dispatch_fn = _get_backend_symbol(
        "_dispatch_ros2_on_done_transition", _dispatch_ros2_on_done_transition
    )
    ros2_dispatch = dispatch_fn(mgr, phase_before)
    if isinstance(ros2_dispatch, dict):
        mgr.ros2_dispatch = ros2_dispatch
    try:
        save_dispatch_history(mgr, save_fn)
    except Exception as exc:
        logging.error("保存下发结果失败: %s", exc, exc_info=True)
    return ros2_dispatch


@chat_bp.route("/api/chat", methods=["POST"])
@_require_api_token
def api_chat():
    try:
        data = request.json or {}
        sid = data.get("session_id") or str(uuid.uuid4())
        request_id = data.get("request_id") or f"req_{uuid.uuid4().hex[:8]}"
        msg = data.get("message", "").strip()
        if not msg:
            return jsonify({
                "ok": False,
                "code": 400,
                "error": "EmptyMessage",
                "msg": "消息内容不能为空。",
                "request_id": request_id,
                "retryable": False,
            }), 400

        manager_getter = _get_backend_symbol("get_or_create_manager", get_or_create_manager)
        mgr = manager_getter(sid)

        with mgr._session_lock:
            with state._sessions_lock:
                if state._sessions_manager.get(sid) is not mgr:
                    return jsonify({
                        "ok": False,
                        "code": 409,
                        "error": "SessionReset",
                        "msg": "当前会话已重新开始，请在新会话中重试。",
                        "request_id": request_id,
                        "retryable": True,
                    }), 409
            phase_before = mgr.phase
            reply = mgr.process(
                msg,
                request_id=request_id,
            )
            print_status(mgr)
            ros2_dispatch = _persist_and_dispatch_done_transition(mgr, phase_before)

            ui_builder = _get_backend_symbol("build_frontend_ui_state", build_frontend_ui_state)
            ui_state = ui_builder(mgr)
            resp_data = {
                "code": 200,
                "session_id": sid,
                "request_id": request_id,
                "reply": reply,
                # ui_state: 统一前端状态契约（Issue #31）
                "ui_state": ui_state,
                # compat fields: 旧字段保留兼容，前端新逻辑应使用 ui_state
                "done": mgr.phase == "done",
                "rejected": mgr.phase == "rejected",
                "collected": mgr._last_built_json,
                "missing": [miss["key"] if isinstance(miss, dict) else str(miss) for miss in mgr._last_missing],
                "task_type": mgr.task_state.get("task_type_key"),
                "task_id": mgr.task_state.get("task_id"),
                "task_id_preview": mgr.task_id_preview,
                "emergency": mgr.mode == "emergency",
                "final_json": mgr._last_built_json if mgr.phase == "done" else None,
                "ros2_dispatch": ros2_dispatch,
            }
        for k, v in resp_data.items():
            try:
                json.dumps(v)
            except Exception as e:
                raise TypeError(f"Field '{k}' is not JSON serializable: {type(v)} -> {v}") from e

        return jsonify(resp_data)
    except SlotVersionConflict as svc:
        logging.error(f"Slot version conflict in /api/chat: {svc}", exc_info=True)
        return jsonify({
            "ok": False,
            "code": 409,
            "error": "SlotVersionConflict",
            "msg": f"并发版本冲突: {str(svc)}",
            "request_id": request_id if 'request_id' in locals() else "req_unknown",
            "retryable": True,
        }), 409
    except IntentIdConflict as iic:
        logging.error(f"Intent ID conflict in /api/chat: {iic}", exc_info=True)
        return jsonify({
            "ok": False,
            "code": 409,
            "error": "IntentIdConflict",
            "msg": "Intent ID 存在冲突，未覆盖已有任务文件。",
            "request_id": request_id if 'request_id' in locals() else "req_unknown",
            "retryable": True,
        }), 409
    except (TaskPersistenceError, IdReservationError, TaskRollbackError) as tpe:
        logging.error(f"Task persistence error in /api/chat: {tpe}", exc_info=True)
        return jsonify({
            "ok": False,
            "code": 500,
            "error": type(tpe).__name__,
            "msg": "任务文件保存失败，任务未能成功下发。",
            "request_id": request_id if 'request_id' in locals() else "req_unknown",
            "retryable": True,
        }), 500
    except ValueError as ve:
        logging.error(f"Validation error in /api/chat: {ve}", exc_info=True)
        return jsonify({
            "ok": False,
            "code": 400,
            "error": "ValidationError",
            "msg": f"槽位校验失败: {str(ve)}",
            "request_id": request_id if 'request_id' in locals() else "req_unknown",
            "retryable": False,
        }), 400
    except Exception as exc:
        logging.error(f"Unhandled exception in /api/chat: {exc}", exc_info=True)
        return jsonify({
            "ok": False,
            "code": 500,
            "error": "InternalServerError",
            "msg": "服务器内部错误，请稍后重试。",
            "request_id": request_id if 'request_id' in locals() else "req_unknown",
            "retryable": True,
        }), 500


@chat_bp.route("/api/chat/stream", methods=["POST"])
@_require_api_token
def api_chat_stream():
    """SSE 流式会话更新机制（Server-Sent Events），提供细粒度事件流与实时更新契约。"""
    try:
        data = request.json or {}
        sid = data.get("session_id") or str(uuid.uuid4())
        request_id = data.get("request_id") or f"req_{uuid.uuid4().hex[:8]}"
        msg = data.get("message", "").strip()
        if not msg:
            return jsonify({
                "ok": False,
                "code": 400,
                "error": "EmptyMessage",
                "msg": "消息内容不能为空。",
                "request_id": request_id,
                "retryable": False,
            }), 400

        manager_getter = _get_backend_symbol("get_or_create_manager", get_or_create_manager)
        mgr = manager_getter(sid)

        session_error = None
        reply = ""
        result_json = ""
        step_events: list[dict] = []
        slot_events: list[dict] = []
        warning_events: list[dict] = []

        def event_sink(event_type: str, ev_data: dict) -> None:
            if not isinstance(ev_data, dict):
                return
            if event_type == "step":
                step_events.append(ev_data)
            elif event_type == "slot":
                slot_events.append(ev_data)
            elif event_type == "warning":
                warning_events.append(ev_data)

        with mgr._session_lock:
            with state._sessions_lock:
                if state._sessions_manager.get(sid) is not mgr:
                    session_error = {"code": 409, "error": "SessionReset", "msg": "当前会话已重新开始，请在新会话中重试。", "request_id": request_id, "retryable": True}

            if session_error is None:
                try:
                    phase_before = mgr.phase
                    process_kwargs = {"request_id": request_id, "event_sink": event_sink}
                    try:
                        inspect.signature(mgr.process).bind(msg, **process_kwargs)
                    except TypeError:
                        # Inspect compatibility before execution: an internal TypeError may follow a mutation.
                        process_kwargs.pop("event_sink")
                    reply = mgr.process(msg, **process_kwargs)

                    ros2_dispatch = _persist_and_dispatch_done_transition(mgr, phase_before)
                    ui_builder = _get_backend_symbol("build_frontend_ui_state", build_frontend_ui_state)
                    ui_state = ui_builder(mgr)
                    resp_data = {
                        "code": 200,
                        "session_id": sid,
                        "request_id": request_id,
                        "reply": reply,
                        # ui_state: 统一前端状态契约（Issue #31）
                        "ui_state": ui_state,
                        # compat fields: 旧字段保留兼容，前端新逻辑应使用 ui_state
                        "done": mgr.phase == "done",
                        "rejected": mgr.phase == "rejected",
                        "collected": mgr._last_built_json,
                        "missing": [miss["key"] if isinstance(miss, dict) else str(miss) for miss in mgr._last_missing],
                        "task_type": mgr.task_state.get("task_type_key"),
                        "task_id": mgr.task_state.get("task_id"),
                        "task_id_preview": mgr.task_id_preview,
                        "emergency": mgr.mode == "emergency",
                        "final_json": mgr._last_built_json if mgr.phase == "done" else None,
                        "ros2_dispatch": ros2_dispatch,
                    }
                    for k, v in resp_data.items():
                        json.dumps(v)
                    result_json = json.dumps(resp_data, ensure_ascii=False)
                except SlotVersionConflict as svc:
                    logging.error(f"Slot version conflict in /api/chat/stream: {svc}", exc_info=True)
                    session_error = {
                        "code": 409,
                        "error": "SlotVersionConflict",
                        "msg": f"并发版本冲突: {str(svc)}",
                        "request_id": request_id,
                        "retryable": True,
                    }
                except IntentIdConflict as iic:
                    logging.error(f"Intent ID conflict in /api/chat/stream: {iic}", exc_info=True)
                    session_error = {
                        "code": 409,
                        "error": "IntentIdConflict",
                        "msg": "Intent ID 存在冲突，未覆盖已有任务文件。",
                        "request_id": request_id,
                        "retryable": True,
                    }
                except (TaskPersistenceError, IdReservationError, TaskRollbackError) as tpe:
                    logging.error(f"Task persistence error in /api/chat/stream: {tpe}", exc_info=True)
                    session_error = {
                        "code": 500,
                        "error": type(tpe).__name__,
                        "msg": "任务文件保存失败，任务未能成功下发。",
                        "request_id": request_id,
                        "retryable": True,
                    }
                except ValueError as ve:
                    logging.error(f"Validation error in /api/chat/stream: {ve}", exc_info=True)
                    session_error = {
                        "code": 400,
                        "error": "ValidationError",
                        "msg": f"槽位校验失败: {str(ve)}",
                        "request_id": request_id,
                        "retryable": False,
                    }
                except Exception as exc:
                    logging.error(f"Unhandled exception in /api/chat/stream: {exc}", exc_info=True)
                    session_error = {
                        "code": 500,
                        "error": "InternalServerError",
                        "msg": "服务器内部错误，请稍后重试。",
                        "request_id": request_id,
                        "retryable": True,
                    }

        def event_stream():
            if session_error is not None:
                yield f"event: error\ndata: {json.dumps(session_error, ensure_ascii=False)}\n\n"
                return

            yield f"event: ping\ndata: {json.dumps({'status': 'connected', 'session_id': sid, 'request_id': request_id})}\n\n"
            if step_events:
                for step_ev in step_events:
                    yield f"event: step\ndata: {json.dumps(step_ev, ensure_ascii=False)}\n\n"
            else:
                yield f"event: step\ndata: {json.dumps({'step': 'processing', 'message': '正在分析指令与状态...', 'phase': mgr.phase})}\n\n"

            for slot_ev in slot_events:
                yield f"event: slot\ndata: {json.dumps(slot_ev, ensure_ascii=False)}\n\n"

            for warn_ev in warning_events:
                yield f"event: warning\ndata: {json.dumps(warn_ev, ensure_ascii=False)}\n\n"

            chunk_size = 4
            for i in range(0, len(reply), chunk_size):
                delta_text = reply[i:i + chunk_size]
                yield f"event: delta\ndata: {json.dumps({'delta': delta_text, 'request_id': request_id}, ensure_ascii=False)}\n\n"
            yield f"event: result\ndata: {result_json}\n\n"
            yield "event: end\ndata: [DONE]\n\n"

        return Response(
            stream_with_context(event_stream()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
    except Exception as exc:
        logging.error(f"Unhandled exception in /api/chat/stream: {exc}", exc_info=True)
        return jsonify({
            "ok": False,
            "code": 500,
            "error": "InternalServerError",
            "msg": "服务器内部错误，请稍后重试。",
            "request_id": request_id if 'request_id' in locals() else "req_unknown",
            "retryable": True,
        }), 500


@chat_bp.route("/api/reset", methods=["POST"])
@_require_api_token
def api_reset():
    sid = (request.json or {}).get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        return jsonify({
            "ok": False,
            "code": 400,
            "error": "MissingSessionId",
            "msg": "session_id 不能为空",
            "retryable": False,
        }), 400

    with state._sessions_lock:
        mgr = state._sessions_manager.get(sid)
    if mgr is None:
        with state._sess_lock:
            state._sessions.pop(sid, None)
        return jsonify({"ok": True, "reset": True})

    with mgr._session_lock:
        with state._sessions_lock:
            if state._sessions_manager.get(sid) is not mgr:
                return jsonify({"ok": True, "reset": True})
            state._sessions_manager.pop(sid, None)
        with state._sess_lock:
            state._sessions.pop(sid, None)
        mgr.reset()

    return jsonify({"ok": True, "reset": True})


@chat_bp.route("/api/session/state", methods=["GET"])
def get_session_state():
    sid = request.args.get("session_id")
    if not sid:
        return jsonify({"ok": False, "code": 400, "error": "MissingSessionId", "msg": "session_id 不能为空", "retryable": False}), 400
    refresh_constraints = str(request.args.get("refresh_constraints", "")).strip().lower() in {"1", "true", "yes", "on"}

    with state._sessions_lock:
        mgr = state._sessions_manager.get(sid)

    if not mgr:
        return jsonify({"ok": True, "code": 200, "exists": False}), 200

    constraint_refresh = None
    with mgr._session_lock:
        if refresh_constraints:
            try:
                constraint_refresh = mgr.refresh_external_state_constraints()
            except Exception as exc:
                logger.warning("session state constraint refresh failed: session_id=%s error=%s", sid, exc, exc_info=True)
                constraint_refresh = {
                    "refreshed": False,
                    "reason": "refresh_error",
                    "message": str(exc),
                }
        ui_state = build_frontend_ui_state(mgr)
        return jsonify({
            "ok": True,
            "code": 200,
            "exists": True,
            "session_id": sid,
            # ui_state: 统一前端状态契约（Issue #31）
            "ui_state": ui_state,
            "constraint_refresh": constraint_refresh,
            # compat fields: 旧字段保留兼容，前端新逻辑应使用 ui_state
            "done": mgr.phase == "done",
            "rejected": mgr.phase == "rejected",
            "collected": mgr._last_built_json,
            "missing": [miss["key"] if isinstance(miss, dict) else str(miss) for miss in mgr._last_missing],
            "task_type": mgr.task_state.get("task_type_key"),
            "task_id": mgr.task_state.get("task_id"),
            "task_id_preview": mgr.task_id_preview,
            "emergency": mgr.mode == "emergency",
            "history": mgr.conversation_history,
            "final_json": mgr._last_built_json if mgr.phase == "done" else None,
            "ros2_dispatch": getattr(mgr, "ros2_dispatch", None),
        })
