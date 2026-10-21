"""
src/web/routes_chat.py - 核心对话流、SSE 打字机事件推流、会话重置与状态查询路由
"""

import inspect
import json
import logging
import uuid
from typing import Any
from flask import Blueprint, current_app, jsonify, request
import src.web.state as state
from src.web.chat_request_cache import ChatRequestCache, RequestIdConflict, RequestReplayUnavailable
from src.web.chat_stream import StreamCapacityError, start_chat_stream
from src.exceptions import (
    ContextBudgetError,
    IdReservationError,
    IntentIdConflict,
    TaskPersistenceError,
    TaskRollbackError,
)
from src.session.history_manager import save_conversation
from src.dispatch.task_dispatch import dispatch_completed_task, save_dispatch_history
from src.slots.slot_store import SlotVersionConflict
from src.handlers.command_predicates import is_final_publish_confirmation
from src.session.ui_state_builder import build_frontend_ui_state, confirmation_conflict
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


def _request_error(error: str, message: str, request_id: str | None = None):
    payload = {
        "ok": False,
        "code": 400,
        "error": error,
        "msg": message,
        "retryable": False,
    }
    if request_id is not None:
        payload["request_id"] = request_id
    return jsonify(payload), 400


def _read_json_object():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, _request_error("InvalidJSON", "请求体必须是有效的 JSON 对象。")
    return data, None


def _read_chat_request():
    """Validate transport input before looking up or mutating a conversation."""
    data, error = _read_json_object()
    if error is not None:
        return None, error

    for key in ('message', 'session_id', 'request_id', 'expected_confirmation_token'):
        value = data.get(key)
        if isinstance(value, str):
            try:
                value.encode('utf-8')
            except UnicodeEncodeError:
                # Reject before session lookup or mutation, and never echo an invalid identifier.
                return None, _request_error('InvalidRequest', f'{key} 包含无效 Unicode 字符。')

    raw_request_id = data.get("request_id")
    request_id = (
        raw_request_id if isinstance(raw_request_id, str) and raw_request_id.strip()
        else f"req_{uuid.uuid4().hex[:8]}"
    )
    for key in ("session_id", "request_id"):
        value = data.get(key)
        if value is not None and value != "" and (not isinstance(value, str) or not value.strip()):
            return None, _request_error("InvalidRequest", f"{key} 必须是非空字符串。", request_id)

    msg = data.get("message", "")
    if not isinstance(msg, str):
        return None, _request_error("InvalidRequest", "消息内容必须是字符串。", request_id)
    msg = msg.strip()
    if not msg:
        return None, _request_error("EmptyMessage", "消息内容不能为空。", request_id)
    sid = data.get("session_id") or str(uuid.uuid4())
    confirmation_token = data.get("expected_confirmation_token")
    if confirmation_token is not None and (
        not isinstance(confirmation_token, str) or len(confirmation_token) > 512
    ):
        return None, _request_error(
            "InvalidRequest", "expected_confirmation_token 必须是有效的摘要确认标识。", request_id,
        )
    return (sid, request_id, msg, confirmation_token), None


def _dispatch_ros2_on_done_transition(mgr, phase_before):
    """Dispatch only the state-machine edge into done, never a later done request."""
    task_intent = getattr(mgr, "final_result", None)
    if phase_before == "done" or mgr.phase != "done" or not task_intent:
        return None
    return dispatch_completed_task(mgr, get_mcp_bridge())


def _persist_and_dispatch_done_transition(mgr, phase_before):
    """当会话首次到达 done 阶段时，先保存会话快照再尝试下发 ROS 2。"""
    mgr._history_persistence_warning = None

    def history_failed():
        mgr._history_persistence_warning = (
            "当前会话状态未能保存；执行结果请以当前发送状态为准，请勿重复发送控制指令。"
        )

    save_fn = _get_backend_symbol("save_conversation", save_conversation)
    if getattr(mgr, "_pending_published_intent", None) is not None:
        try:
            save_dispatch_history(mgr, save_fn)
        except Exception as exc:
            logging.error("保存待核对发布记录失败: %s", exc, exc_info=True)
            history_failed()
        return getattr(mgr, "ros2_dispatch", None)
    if mgr.phase != "done":
        return getattr(mgr, "ros2_dispatch", None)
    if phase_before == "done":
        try:
            save_dispatch_history(mgr, save_fn)
        except Exception:
            logger.exception("保存后续执行控制状态失败")
            history_failed()
        return getattr(mgr, "ros2_dispatch", None)

    try:
        save_dispatch_history(mgr, save_fn)
    except Exception as exc:
        logging.error("保存历史快照失败: %s", exc, exc_info=True)
        history_failed()

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
        history_failed()
    return ros2_dispatch


def _chat_failure(exc, request_id):
    if isinstance(exc, SlotVersionConflict):
        code, name, message, retryable = 409, 'SlotVersionConflict', f'并发版本冲突: {exc}', True
    elif isinstance(exc, IntentIdConflict):
        code, name, message, retryable = 409, 'IntentIdConflict', 'Intent ID 存在冲突，未覆盖已有任务文件。', True
    elif isinstance(exc, (TaskPersistenceError, IdReservationError, TaskRollbackError)):
        code, name, message, retryable = 500, type(exc).__name__, '任务文件保存失败，任务未能成功下发。', True
    elif isinstance(exc, ContextBudgetError):
        code, name, message, retryable = 503, 'ContextBudgetError', str(exc), False
    elif isinstance(exc, RequestIdConflict):
        code, name, message, retryable = 409, 'RequestIdConflict', '该 request_id 已用于另一条消息，请使用新的请求标识。', False
    elif isinstance(exc, RequestReplayUnavailable):
        code, name, message, retryable = 409, 'RequestReplayUnavailable', '该请求已处理，结果超过重放容量；请刷新会话状态核对，不要重复执行增量操作。', False
    elif isinstance(exc, ValueError):
        code, name, message, retryable = 400, 'ValidationError', f'槽位校验失败: {exc}', False
    else:
        code, name, message, retryable = 500, 'InternalServerError', '服务器内部错误，请稍后重试。', True
    logger.log(logging.ERROR if code == 500 else logging.WARNING,
               'Chat request failed: request_id=%s error=%s', request_id, name, exc_info=True)
    return {'ok': False, 'code': code, 'error': name, 'msg': message,
            'request_id': request_id, 'retryable': retryable}, code, []


def _chat_result(mgr, sid, request_id, reply, ros2_dispatch):
    ui_builder = _get_backend_symbol('build_frontend_ui_state', build_frontend_ui_state)
    persistence_warning = getattr(mgr, "_history_persistence_warning", None)
    if persistence_warning:
        reply = f"{reply}\n⚠️ {persistence_warning}"
    return {
        'code': 200, 'session_id': sid, 'request_id': request_id, 'reply': reply,
        'ui_state': ui_builder(mgr),
        # Compatibility fields remain available alongside the authoritative UI contract.
        'done': mgr.phase == 'done', 'rejected': mgr.phase == 'rejected',
        'collected': mgr._last_built_json,
        'missing': [miss['key'] if isinstance(miss, dict) else str(miss) for miss in mgr._last_missing],
        'task_type': mgr.task_state.get('task_type_key'), 'task_id': mgr.task_state.get('task_id'),
        'task_id_preview': mgr.task_id_preview, 'emergency': mgr.mode == 'emergency',
        'final_json': mgr._last_built_json if mgr.phase == 'done' else None,
        'ros2_dispatch': ros2_dispatch,
        'persistence_warning': persistence_warning,
    }


def _process_chat_request(mgr, sid, request_id, msg, step_sink=None, confirmation_token=None):
    """One serialized transaction shared by JSON and SSE, including replay."""
    with mgr._session_lock:
        with state._sessions_lock:
            if state._sessions_manager.get(sid) is not mgr:
                return {'ok': False, 'code': 409, 'error': 'SessionReset',
                        'msg': '当前会话已重新开始，请在新会话中重试。',
                        'request_id': request_id, 'retryable': True}, 409, []
        cache = getattr(mgr, '_http_request_cache', None)
        if cache is None:
            cache = mgr._http_request_cache = ChatRequestCache()
        try:
            previous = cache.get(request_id, msg)
        except (RequestIdConflict, RequestReplayUnavailable) as exc:
            return _chat_failure(exc, request_id)
        if previous is not None:
            return previous['payload'], previous['status'], previous['events']

        requires_confirmation = is_final_publish_confirmation(msg)
        if mgr.phase == "done":
            from src.dispatch.task_dispatch import is_runtime_risk_confirmation
            requires_confirmation = requires_confirmation or is_runtime_risk_confirmation(msg)
        if requires_confirmation:
            conflict = confirmation_conflict(mgr, confirmation_token)
            if conflict is not None:
                return {**conflict, "request_id": request_id}, 409, []

        events = []

        def event_sink(event_type, data):
            if not isinstance(data, dict):
                return
            if event_type == 'step' and step_sink is not None:
                step_sink(event_type, data)
            elif event_type in {'slot', 'warning'}:
                events.append((event_type, data))

        try:
            phase_before = mgr.phase
            mgr.mcp_bridge = get_mcp_bridge()
            process_kwargs = {'request_id': request_id}
            if step_sink is not None:
                process_kwargs['event_sink'] = event_sink
                try:
                    inspect.signature(mgr.process).bind(msg, **process_kwargs)
                except TypeError:
                    # Inspect before execution; an internal TypeError must never trigger a second mutation.
                    process_kwargs.pop('event_sink')
            reply = mgr.process(msg, **process_kwargs)
            print_status(mgr)
            dispatch = _persist_and_dispatch_done_transition(mgr, phase_before)
            payload = _chat_result(mgr, sid, request_id, reply, dispatch)
            # Freeze and validate while still holding the lock, before a later turn can edit shared slots.
            packet = json.loads(json.dumps({'payload': payload, 'status': 200, 'events': events}, ensure_ascii=False))
        except Exception as exc:
            payload, status, _ = _chat_failure(exc, request_id)
            packet = {'payload': payload, 'status': status, 'events': []}
        # Failed/uncertain outcomes also replay: a failure after mutation must not apply a delta twice.
        cache.put(request_id, msg, packet)
        return packet['payload'], packet['status'], packet['events']


@chat_bp.route('/api/chat', methods=['POST'])
@_require_api_token
def api_chat():
    request_id = 'req_unknown'
    try:
        parsed, error = _read_chat_request()
        if error is not None:
            return error
        sid, request_id, msg, confirmation_token = parsed
        manager_getter = _get_backend_symbol('get_or_create_manager', get_or_create_manager)
        payload, status, _ = _process_chat_request(
            manager_getter(sid), sid, request_id, msg, confirmation_token=confirmation_token,
        )
        return jsonify(payload), status
    except Exception as exc:
        payload, status, _ = _chat_failure(exc, request_id)
        return jsonify(payload), status


@chat_bp.route('/api/chat/stream', methods=['POST'])
@_require_api_token
def api_chat_stream():
    request_id = 'req_unknown'
    try:
        parsed, error = _read_chat_request()
        if error is not None:
            return error
        sid, request_id, msg, confirmation_token = parsed
        manager_getter = _get_backend_symbol('get_or_create_manager', get_or_create_manager)
        mgr = manager_getter(sid)
        return start_chat_stream(
            lambda step_sink: _process_chat_request(
                mgr, sid, request_id, msg, step_sink, confirmation_token=confirmation_token,
            ),
            app=current_app._get_current_object(), session_id=sid, request_id=request_id,
        )
    except StreamCapacityError:
        return jsonify({'ok': False, 'code': 503, 'error': 'ServerBusy',
                        'msg': '当前处理中的请求较多，请稍后重试。',
                        'request_id': request_id, 'retryable': True}), 503, {'Retry-After': '1'}
    except Exception as exc:
        payload, status, _ = _chat_failure(exc, request_id)
        return jsonify(payload), status


@chat_bp.route("/api/reset", methods=["POST"])
@_require_api_token
def api_reset():
    data, error = _read_json_object()
    if error is not None:
        return error
    sid = data.get("session_id")
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
        # A reset may have removed or replaced this manager while we waited.
        # Never refresh or expose state from a detached session instance.
        with state._sessions_lock:
            if state._sessions_manager.get(sid) is not mgr:
                return jsonify({
                    "ok": False, "code": 409, "error": "SessionReset",
                    "msg": "当前会话已重新开始，请重新获取会话状态。",
                    "retryable": True,
                }), 409
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
