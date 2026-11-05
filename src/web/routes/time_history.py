"""
src/web/routes_time_history.py - 虚拟时钟校准与多会话历史快照恢复端点
"""

import logging
import uuid
from contextlib import nullcontext
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

import src.web.state as state
from src.dialogue_manager import DialogueManager
from src.dispatch.task_dispatch import save_dispatch_history
from src.session.history_manager import list_history, load_history, save_conversation
from src.session.ui_state_builder import build_frontend_ui_state
from src.temporal.simulated_time import get_simulated_time
from src.web.state import _require_api_token

logger = logging.getLogger(__name__)

time_history_bp = Blueprint("time_history", __name__)


@time_history_bp.route("/api/time/current", methods=["GET"])
@_require_api_token
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
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"code": 400, "msg": "请求体必须是 JSON 对象"}), 400
    time_str = data.get("time")
    if not isinstance(time_str, str) or not time_str.strip():
        return jsonify({"code": 400, "msg": "time 必须是非空时间字符串"}), 400
    try:
        dt = datetime.fromisoformat(time_str.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        # Validate timezone conversion before changing the shared clock.
        dt.astimezone(ZoneInfo("Asia/Shanghai"))
    except (ValueError, OverflowError) as exc:
        return jsonify({"code": 400, "msg": f"时间格式错误: {exc}"}), 400
    try:
        sim = get_simulated_time()
        sim.set_current_time(dt)
        return jsonify({
            "code": 200,
            "msg": "时间设置成功",
            "current_time": sim.get_current_time().isoformat(),
        })
    except Exception:
        logger.exception("设置模拟时间失败")
        return jsonify({"code": 500, "msg": "时间设置失败，请稍后重试"}), 500


@time_history_bp.route("/api/history/list", methods=["GET"])
@_require_api_token
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
    """Open history in a new session and retain a recoverable current draft."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"code": 400, "msg": "请求体必须是 JSON 对象"}), 400
    history_id = data.get("history_id")
    sid = data.get("session_id")
    if any(not isinstance(value, str) or not value.strip() for value in (history_id, sid)):
        return jsonify({"code": 400, "msg": "history_id 和 session_id 必须是非空字符串"}), 400
    if data.get("restore_mode", "fork") != "fork":
        return jsonify({"code": 400, "msg": "历史记录必须在新会话中打开，restore_mode 应为 fork。"}), 400

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

    with state._sessions_lock:
        previous = state._sessions_manager.get(sid)
    with previous._session_lock if previous is not None else nullcontext():
        with state._sessions_lock:
            if previous is not None and state._sessions_manager.get(sid) is not previous:
                return jsonify({
                    "ok": False, "code": 409, "error": "SessionReset",
                    "msg": "当前会话已重新开始，请在新会话中重试。",
                    "retryable": True,
                }), 409
        new_sid = str(uuid.uuid4())
        mgr = DialogueManager(
            previous.llm if previous is not None else state._shared_llm,
            previous.kb if previous is not None else state._shared_kb,
            session_id=new_sid,
        )
        try:
            mgr.load_snapshot({**snapshot, "session_id": new_sid})
        except (TypeError, ValueError) as exc:
            logging.warning("历史快照结构校验失败: history_id=%r, error=%s", history_id, exc)
            return jsonify({"code": 400, "msg": f"历史快照结构非法: {exc}"}), 400
        except Exception as exc:
            logging.error("恢复历史快照失败: history_id=%r", history_id, exc_info=True)
            return jsonify({"code": 500, "msg": f"恢复历史记录失败: {exc}"}), 500

        preserved_draft_history_id = None
        if previous is not None and previous.phase not in {"done", "rejected"} and (
            previous.task_state.get("task_type_key") or previous.conversation_history
        ):
            try:
                def save_draft(**fields):
                    # Draft backups must not overwrite an archived intent's
                    # canonical history or create a published task artifact.
                    return save_conversation(**{**fields, "intent_id": None})
                preserved_draft_history_id = save_dispatch_history(previous, save_draft)
            except Exception:
                logger.exception("打开历史前保存当前草稿失败")
                return jsonify({
                    "code": 500, "error": "DraftPreservationFailed",
                    "msg": "当前草稿未能保存，已保留原会话；请稍后重试。",
                }), 500
        with state._sessions_lock:
            state._sessions_manager[new_sid] = mgr
        ui_state = build_frontend_ui_state(mgr)
        return jsonify({
            "code": 200,
            "session_id": new_sid,
            "previous_session_id": sid,
            "preserved_draft_history_id": preserved_draft_history_id,
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
