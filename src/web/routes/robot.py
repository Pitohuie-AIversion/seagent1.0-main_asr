"""
src/web/routes_robot.py - 机器人状态更新与实时遥测快照端点
"""

import logging
import re
import uuid
from flask import Blueprint, jsonify, request

import src.web.state as state
from src.exceptions import (
    StatePersistenceError,
    StateSelectorError,
    StateVersionConflict,
)
from src.web.state import _require_api_token, get_mcp_bridge

logger = logging.getLogger(__name__)

robot_bp = Blueprint("robot", __name__)


@robot_bp.route("/api/robot/set-state-info", methods=["POST"])
@_require_api_token
def set_robot_state_info():
    supplied_request_id = request.headers.get("X-Request-ID", "")
    request_id = (
        supplied_request_id
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", supplied_request_id)
        else f"req_{uuid.uuid4().hex[:12]}"
    )
    if state._shared_kb is None:
        return jsonify({
            "ok": False,
            "code": 503,
            "error": "service_unavailable",
            "msg": "Robot state service is not initialized",
            "request_id": request_id,
            "retryable": True,
        }), 503

    robot_name = None
    expected_version = None
    status_ref = None
    current_version = None
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        robot_name = data.get("robot_name")
        params = data.get("params")
        expected_version = data.get("expected_version")

        if not isinstance(robot_name, str) or not robot_name.strip():
            raise ValueError("robot_name must be a non-empty string")
        if not isinstance(params, dict) or not params:
            raise ValueError("params must be a non-empty JSON object")

        status_ref = state._shared_kb.state_info.resolve_status_ref(robot_name)
        state_before = state._shared_kb.state_info.get_robot_state(robot_name)
        if isinstance(state_before, dict):
            current_version = state_before.get("version", 0)

        result = state._shared_kb.state_info.set_status(
            robot_name,
            params,
            expected_version=expected_version,
        )
        refreshed_sessions = []
        with state._sessions_lock:
            active_sessions = list(state._sessions_manager.items())
        for sid, mgr in active_sessions:
            try:
                with mgr._session_lock:
                    refresh = mgr.refresh_external_state_constraints(
                        result["status_ref"],
                    )
                if refresh.get("refreshed"):
                    refreshed_sessions.append({
                        "session_id": sid,
                        "phase": refresh.get("phase"),
                        "hard_violations": refresh.get("hard_violations", 0),
                        "soft_violations": refresh.get("soft_violations", 0),
                    })
            except Exception as refresh_exc:
                logger.warning(
                    "Robot state session refresh failed: request_id=%s session_id=%s status_ref=%s err=%s",
                    request_id,
                    sid,
                    result["status_ref"],
                    refresh_exc,
                )
        logger.info(
            "Robot state updated: request_id=%s status_ref=%s "
            "expected_version=%s current_version=%s",
            request_id,
            result["status_ref"],
            expected_version,
            result["version"],
        )
        return jsonify({
            "ok": True,
            "code": 200,
            "msg": "状态更新成功",
            "robot": robot_name,
            "status_ref": result["status_ref"],
            "version": result["version"],
            "store_version": result["store_version"],
            "updated_at": result["updated_at"],
            "state": result["state"],
            "refreshed_sessions": refreshed_sessions,
            "request_id": request_id,
        })
    except StateVersionConflict as exc:
        logger.warning(
            "Robot state version conflict: request_id=%s status_ref=%s "
            "expected_version=%s current_version=%s",
            request_id,
            exc.status_ref,
            exc.expected_version,
            exc.current_version,
        )
        return jsonify({
            "ok": False,
            "code": 409,
            "error": "StateVersionConflict",
            "msg": "Robot state version is stale",
            "status_ref": exc.status_ref,
            "expected_version": exc.expected_version,
            "current_version": exc.current_version,
            "request_id": request_id,
            "retryable": True,
        }), 409
    except (StateSelectorError, TypeError, ValueError) as exc:
        logger.warning(
            "Invalid robot state update: request_id=%s status_ref=%s "
            "expected_version=%s current_version=%s error=%s",
            request_id,
            status_ref,
            expected_version,
            current_version,
            type(exc).__name__,
        )
        return jsonify({
            "ok": False,
            "code": 400,
            "error": type(exc).__name__,
            "msg": "Invalid robot state update request",
            "request_id": request_id,
            "retryable": False,
        }), 400
    except StatePersistenceError:
        logger.exception(
            "Robot state persistence failed: request_id=%s status_ref=%s "
            "expected_version=%s current_version=%s",
            request_id,
            status_ref,
            expected_version,
            current_version,
        )
        return jsonify({
            "ok": False,
            "code": 500,
            "error": "StatePersistenceError",
            "msg": "Robot state persistence failed",
            "request_id": request_id,
            "retryable": True,
        }), 500
    except Exception:
        logger.exception(
            "Unhandled robot state update failure: request_id=%s status_ref=%s "
            "expected_version=%s current_version=%s",
            request_id,
            status_ref,
            expected_version,
            current_version,
        )
        return jsonify({
            "ok": False,
            "code": 500,
            "error": "internal_error",
            "msg": "Internal server error",
            "request_id": request_id,
            "retryable": False,
        }), 500


@robot_bp.route("/api/telemetry", methods=["GET"])
def get_telemetry_snapshot():
    """Compatibility view backed by live ROS telemetry, never by protocol YAML."""
    bridge = get_mcp_bridge()
    snapshot = bridge.runtime_snapshot() if bridge is not None else {}
    resp = jsonify({"code": 200, "snapshot": snapshot})
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp
