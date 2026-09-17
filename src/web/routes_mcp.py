"""
src/web/routes_mcp.py - ROS 2 MCP 任务下发、网关切换与设备控制路由端点
"""

import logging
import os
import threading
import yaml
from flask import Blueprint, current_app, jsonify, request
import src.web.state as state
from src.task_dispatch import dispatch_completed_task, save_dispatch_history

from src.web.state import (
    CONFIG_DIR,
    _require_api_token,
    get_mcp_bridge,
    get_or_create_manager,
)

import sys
from pathlib import Path

logger = logging.getLogger(__name__)

mcp_bp = Blueprint("mcp", __name__)


def _get_config_dir() -> Path:
    backend = sys.modules.get("web_backend")
    if backend is not None and hasattr(backend, "CONFIG_DIR"):
        return Path(getattr(backend, "CONFIG_DIR"))
    return CONFIG_DIR


def _persist_active_gateway(host: str, port: int, mode: str) -> None:
    """Persist the mutable gateway in ros2_runtime.yaml atomically."""
    runtime_file = _get_config_dir() / "ros2_runtime.yaml"
    with open(runtime_file, "r", encoding="utf-8") as config_handle:
        data = yaml.safe_load(config_handle) or {}
    gateway = data.setdefault("gateway", {})
    gateway["host"] = host
    gateway["port"] = port
    gateway["mode"] = mode

    temporary_file = runtime_file.with_suffix(
        f".gateway_tmp_{os.getpid()}_{threading.get_ident()}"
    )
    try:
        with open(temporary_file, "w", encoding="utf-8") as config_handle:
            yaml.safe_dump(data, config_handle, allow_unicode=True, sort_keys=False)
            config_handle.flush()
            os.fsync(config_handle.fileno())
        os.replace(temporary_file, runtime_file)
        directory_fd = os.open(runtime_file.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_file.unlink(missing_ok=True)


@mcp_bp.route("/api/mcp/status", methods=["GET"])
@_require_api_token
def get_mcp_status():
    """查询云端 ↔ 支持船 Topside MCP 通信状态与遥测快照"""
    bridge = get_mcp_bridge()
    if bridge is None:
        resp = jsonify({
            "code": 200,
            "mcp_connected": False,
            "telemetry_fresh": False,
            "msg": "MCP 桥接服务未初始化",
            "host": None,
            "port": None,
            "snapshot": {},
        })
    else:
        resp = jsonify({"code": 200, **bridge.status_payload()})
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@mcp_bp.route("/api/mcp/dispatch", methods=["POST"])
@_require_api_token
def dispatch_mcp_task():
    """下发指定 TaskIntent 或当前会话完成的任务到 ROS 2 控制系统"""
    bridge = get_mcp_bridge()
    data = request.get_json(silent=True) or {}
    sid = data.get("session_id")
    custom_intent = data.get("task_intent")

    allow_custom = bool(current_app.config.get("ALLOW_MCP_CUSTOM_INTENT", False))
    if custom_intent and not allow_custom:
        return jsonify({
            "code": 403,
            "msg": "禁止绕过 SEAgent 会话确认与约束校验直接下发 task_intent",
        }), 403
    if not isinstance(sid, str) or not sid:
        return jsonify({"code": 400, "msg": "请提供已完成确认的 session_id"}), 400
    with state._sessions_lock:
        mgr = state._sessions_manager.get(sid)
    if mgr is None:
        return jsonify({"code": 400, "msg": "会话不存在，请先恢复已确认任务。"}), 400
    with mgr._session_lock:
        with state._sessions_lock:
            if state._sessions_manager.get(sid) is not mgr:
                return jsonify({"code": 409, "msg": "会话已重置，请刷新后重试。"}), 409
        if mgr.phase != "done" or not mgr.final_result:
            return jsonify({"code": 400, "msg": f"当前会话 {sid} 尚未处于 done 阶段，无可下发的任务"}), 400
        if custom_intent and custom_intent != mgr.final_result:
            return jsonify({"code": 403, "msg": "task_intent 必须与会话已确认归档的任务完全一致。"}), 403
        result = dispatch_completed_task(mgr, bridge)
        try:
            save_dispatch_history(mgr)
        except Exception as exc:
            logger.error("保存下发结果失败: %s", exc, exc_info=True)
        return jsonify({"code": 200, "msg": result["message"], "ros2_dispatch": result,
                        "dispatch_state": result["state"], "task_id": result.get("task_id"),
                        "task_id_hex": result.get("task_id_hex")})


@mcp_bp.route("/api/mcp/gateway", methods=["GET", "POST"])
@_require_api_token
def mcp_gateway():
    """Read or atomically switch the live rosbridge gateway."""
    bridge = get_mcp_bridge()
    if bridge is None:
        return jsonify({"code": 503, "msg": "MCP 桥接服务未初始化"}), 503
    if request.method == "GET":
        return jsonify({
            "code": 200,
            "gateway": {
                "host": bridge.host,
                "port": bridge.port,
                "mode": bridge.gateway_mode,
                "ws_url": f"ws://{bridge.host}:{bridge.port}",
                "connected": bridge.is_healthy(),
            },
        })

    data = request.get_json(silent=True) or {}
    host = str(data.get("host", "")).strip()
    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        return jsonify({"code": 400, "msg": "port 必须是整数"}), 400
    mode = str(data.get("mode") or ("real" if port == 9090 else "mock"))
    if not host or len(host) > 253 or any(ch.isspace() for ch in host):
        return jsonify({"code": 400, "msg": "host 格式非法"}), 400
    if not 1 <= port <= 65535:
        return jsonify({"code": 400, "msg": "port 必须在 1..65535 范围内"}), 400

    old_host, old_port, old_mode = bridge.host, bridge.port, bridge.gateway_mode
    try:
        bridge.reconnect(host, port, mode=mode)
        try:
            _persist_active_gateway(host, port, mode)
        except Exception:
            bridge.reconnect(old_host, old_port, mode=old_mode)
            raise
    except Exception as exc:
        logging.error("切换 ROS 2 网关失败: %s", exc, exc_info=True)
        return jsonify({"code": 502, "msg": f"网关切换失败: {exc}"}), 502
    return jsonify({
        "code": 200,
        "gateway": {
            "host": host,
            "port": port,
            "mode": mode,
            "ws_url": f"ws://{host}:{port}",
            "connected": True,
        },
    })


@mcp_bp.route("/api/mcp/task-manage", methods=["POST"])
@_require_api_token
def mcp_task_manage():
    """发送任务管理指令 (suspend, resume, delete, clear_block)"""
    bridge = get_mcp_bridge()
    if bridge is None or not bridge.is_healthy():
        return jsonify({"code": 503, "msg": "MCP 桥接服务未连接"}), 503

    data = request.get_json(silent=True) or {}
    action_str = str(data.get("action", "")).lower()
    target_task_id = data.get("task_id")

    try:
        if action_str == "suspend":
            if not target_task_id:
                return jsonify({"code": 400, "msg": "挂起任务需提供 task_id"}), 400
            tid = bridge.suspend_task(int(target_task_id))
        elif action_str == "resume":
            if not target_task_id:
                return jsonify({"code": 400, "msg": "恢复任务需提供 task_id"}), 400
            tid = bridge.resume_task(int(target_task_id))
        elif action_str == "delete":
            if not target_task_id:
                return jsonify({"code": 400, "msg": "删除任务需提供 task_id"}), 400
            tid = bridge.delete_task(int(target_task_id))
        elif action_str in ("clear_block", "clear"):
            tid = bridge.emergency_clear_block()
        else:
            return jsonify({"code": 400, "msg": f"未知的管理动作: {action_str}"}), 400

        return jsonify({
            "code": 200,
            "msg": f"任务管理指令 {action_str} 已下发",
            "cmd_task_id": tid,
        })
    except Exception as exc:
        logging.error("MCP 任务管理指令下发失败: %s", exc, exc_info=True)
        return jsonify({"code": 500, "msg": f"管理指令失败: {exc}"}), 500


@mcp_bp.route("/api/mcp/ctrl-task", methods=["POST"])
@_require_api_token
def mcp_ctrl_task():
    """设备控制指令（开关灯、继电器等）"""
    bridge = get_mcp_bridge()
    if bridge is None or not bridge.is_healthy():
        return jsonify({"code": 503, "msg": "MCP 桥接服务未连接"}), 503

    data = request.get_json(silent=True) or {}
    device_id = data.get("device_id")
    value = data.get("value", 0.0)

    if device_id is None:
        return jsonify({"code": 400, "msg": "缺少 device_id 参数"}), 400

    try:
        tid = bridge.control_device(device_id=int(device_id), value=float(value))
        return jsonify({
            "code": 200,
            "msg": f"设备控制指令已发送 (device={device_id}, value={value})",
            "cmd_task_id": tid,
        })
    except Exception as exc:
        logging.error("MCP 设备控制下发失败: %s", exc, exc_info=True)
        return jsonify({"code": 500, "msg": f"控制下发失败: {exc}"}), 500
