"""
src/web/routes_time_history.py - 虚拟时钟校准与多会话历史快照恢复端点
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Blueprint, jsonify, request

from src.session.history_manager import list_history, load_history
from src.temporal.simulated_time import get_simulated_time
from src.session.ui_state_builder import build_frontend_ui_state
import src.web.state as state
from src.web.state import _require_api_token, get_or_create_manager

logger = logging.getLogger(__name__)

time_history_bp = Blueprint("time_history", __name__)


@time_history_bp.route("/api/time/current", methods=["GET"])
def get_current_time():
    sim = get_simulated_time()
    current = sim.get_current_time()
    return jsonify({
        "code": 200,
        "current_time": current.isoformat(),
        "timestamp": current.timestamp(),
    })


@time_history_bp.route("/api/time/set", methods=["POST"])
@_require_api_token
def set_current_time():
    data = request.get_json() or {}
    time_str = data.get("time")
    if not time_str:
        return jsonify({"code": 400, "msg": "缺少 time 字段"}), 400
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        sim = get_simulated_time()
        sim.set_current_time(dt)
        return jsonify({
            "code": 200,
            "msg": "时间设置成功",
            "current_time": sim.get_current_time().isoformat(),
        })
    except Exception as e:
        return jsonify({"code": 500, "msg": f"时间格式错误: {str(e)}"}), 500


@time_history_bp.route("/api/history/list", methods=["GET"])
def api_history_list():
    """返回历史记录列表"""
    try:
        records = list_history()
        return jsonify({"code": 200, "data": records})
    except Exception as e:
        return jsonify({"code": 500, "msg": str(e)}), 500


@time_history_bp.route("/api/history/load", methods=["POST"])
@_require_api_token
def api_history_load():
    """加载指定的历史快照，并恢复到当前会话"""
    data = request.get_json() or {}
    history_id = data.get("history_id")
    sid = data.get("session_id")
    if not history_id or not sid:
        return jsonify({"code": 400, "msg": "缺少 history_id 或 session_id"}), 400

    try:
        snapshot = load_history(history_id)
    except ValueError as exc:
        logging.warning("拒绝非法历史快照请求: history_id=%r, error=%s", history_id, exc)
        return jsonify({"code": 400, "msg": f"历史记录参数或内容非法: {exc}"}), 400
    except OSError as exc:
        logging.error("读取历史快照失败: history_id=%r", history_id, exc_info=True)
        return jsonify({"code": 500, "msg": f"读取历史记录失败: {exc}"}), 500

    if not snapshot:
        return jsonify({"code": 404, "msg": "历史记录不存在"}), 404

    mgr = get_or_create_manager(sid)
    with mgr._session_lock:
        with state._sessions_lock:
            if state._sessions_manager.get(sid) is not mgr:
                return jsonify({
                    "ok": False, "code": 409, "error": "SessionReset",
                    "msg": "当前会话已重新开始，请在新会话中重试。",
                    "retryable": True,
                }), 409
        try:
            # The historical session is the source, not the identity of the
            # active session registered under sid.
            mgr.load_snapshot({**snapshot, "session_id": sid})
        except (TypeError, ValueError) as exc:
            logging.warning("历史快照结构校验失败: history_id=%r, error=%s", history_id, exc)
            return jsonify({"code": 400, "msg": f"历史快照结构非法: {exc}"}), 400
        except Exception as exc:
            logging.error("恢复历史快照失败: history_id=%r", history_id, exc_info=True)
            return jsonify({"code": 500, "msg": f"恢复历史记录失败: {exc}"}), 500

        ui_state = build_frontend_ui_state(mgr)
        return jsonify({
            "code": 200,
            "session_id": sid,
            "conversation_history": mgr.conversation_history,
            # ui_state: 统一前端状态契约（Issue #31）
            "ui_state": ui_state,
            "ros2_dispatch": getattr(mgr, "ros2_dispatch", None),
            # compat fields: 旧字段保留兼容，前端新逻辑应使用 ui_state
            "built_json": mgr._last_built_json,
            "missing": [miss["key"] for miss in mgr._last_missing],
            "task_type": mgr.task_state.get("task_type_key"),
            "mode": mgr.mode,
            "phase": mgr.phase,
        })
