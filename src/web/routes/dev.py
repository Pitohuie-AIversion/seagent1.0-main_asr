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


@dev_bp.route("/api/dev/tools", methods=["GET"])
@_require_api_token
def dev_list_tools():
    """返回当前已注册的所有工具元数据与 JSON Schema"""
    from src.tools.registry import ToolRegistry

    registry = ToolRegistry.get_instance()
    role = request.args.get("role")

    schemas = registry.get_schemas(role=role)
    tools_list = []
    for defn in registry.list_tools():
        if role and role not in defn.roles and "all" not in defn.roles:
            continue
        tools_list.append({
            "name": defn.name,
            "description": defn.description,
            "roles": list(defn.roles),
            "is_read_only": defn.is_read_only,
            "parameters": defn.parameters_schema,
        })
    return jsonify({
        "ok": True,
        "code": 200,
        "role_filter": role,
        "tools_count": len(tools_list),
        "tools": tools_list,
        "openai_schemas": schemas,
    })


@dev_bp.route("/api/dev/tools/execute", methods=["POST"])
@_require_api_token
def dev_execute_tool():
    """开发者调试直接执行注册工具接口"""
    from src.tools.registry import ToolRegistry

    registry = ToolRegistry.get_instance()

    body = request.get_json(silent=True) or {}
    tool_name = body.get("tool_name")
    if not tool_name:
        return jsonify({"ok": False, "code": 400, "msg": "缺少 tool_name 参数"}), 400

    arguments = body.get("arguments", {})
    if not isinstance(arguments, dict):
        return jsonify({"ok": False, "code": 400, "msg": "arguments 必须为 dict"}), 400

    tool_defn = registry.get_tool(tool_name)
    if tool_defn is None:
        return jsonify({"ok": False, "code": 404, "msg": f"未找到名为 '{tool_name}' 的工具"}), 404

    try:
        result = registry.execute_tool(tool_name, arguments)
        return jsonify({
            "ok": True,
            "code": 200,
            "tool_name": tool_name,
            "result": result,
        })
    except Exception as exc:
        logger.exception("[DevTools] 执行工具 '%s' 失败: %s", tool_name, exc)
        return jsonify({
            "ok": False,
            "code": 500,
            "tool_name": tool_name,
            "error": str(exc),
        }), 500

