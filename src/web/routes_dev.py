"""
src/web/routes_dev.py - 开发者热重载与 SSE 事件流路由
"""

import json
import logging
import time
from flask import Blueprint, Response, jsonify, request, stream_with_context

from src.web.state import _require_api_token

logger = logging.getLogger(__name__)

dev_bp = Blueprint("dev", __name__)


@dev_bp.before_app_request
def auto_hot_reload_check():
    """在每个请求处理前，检测 src/ 和 config/ 是否有代码变更并自动热重载"""
    # 忽略静态资源与轮询时间接口的重载检查，降低微小开销
    if request.path.startswith("/static/") or request.path == "/api/time/current":
        return
    try:
        from src.hot_reload import maybe_auto_reload
        maybe_auto_reload()
    except Exception as exc:
        logger.warning("[Hot-Reload] auto check failed: %s", exc)


@dev_bp.route("/api/dev/reload", methods=["GET", "POST"])
@_require_api_token
def manual_dev_reload():
    """开发者手动热重载接口"""
    from src.hot_reload import code_reload_enabled, force_reload
    if not code_reload_enabled():
        return jsonify({"ok": False, "code": 403, "msg": "代码热重载未启用"}), 403
    res = force_reload()
    return jsonify(res)


@dev_bp.route("/api/dev/reload-events", methods=["GET"])
@_require_api_token
def dev_reload_events():
    """返回热重载事件，供前端轮询并刷新当前会话状态。"""
    from src.hot_reload import get_reload_events

    after = request.args.get("after", 0)
    return jsonify({
        "ok": True,
        "code": 200,
        "events": get_reload_events(after_event_id=after),
    })


@dev_bp.route("/api/dev/reload-events/stream", methods=["GET"])
@_require_api_token
def dev_reload_events_stream():
    """返回热重载 SSE 事件流，实时推送后端更新，替代短轮询机制。"""
    from src.hot_reload import get_reload_events

    after_id_str = request.args.get("after", "0")
    try:
        after_id = int(after_id_str)
    except Exception:
        after_id = 0

    def event_stream():
        nonlocal after_id
        yield f"event: ping\ndata: {json.dumps({'status': 'connected', 'last_id': after_id})}\n\n"
        initial_events = get_reload_events(after_event_id=after_id)
        for ev in initial_events:
            ev_id = int(ev.get("event_id", 0))
            if ev_id > after_id:
                after_id = ev_id
            yield f"event: reload\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

        start_time = time.time()
        while time.time() - start_time < 30:
            time.sleep(1.0)
            new_events = get_reload_events(after_event_id=after_id)
            if new_events:
                for ev in new_events:
                    ev_id = int(ev.get("event_id", 0))
                    if ev_id > after_id:
                        after_id = ev_id
                    yield f"event: reload\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
            else:
                yield f"event: ping\ndata: {json.dumps({'time': time.time()})}\n\n"

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
