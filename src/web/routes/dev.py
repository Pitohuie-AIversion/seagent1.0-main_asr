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


@dev_bp.route("/api/dev/backends", methods=["GET"])
@_require_api_token
def dev_list_backends():
    """返回当前已装配的模型推理后端列表、拓扑配置与健康探测状态"""
    from src.web.state import _shared_llm, _sessions_manager

    llm = _shared_llm
    if llm is None and _sessions_manager:
        for mgr in _sessions_manager.values():
            if getattr(mgr, "llm", None) is not None:
                llm = mgr.llm
                break

    if llm is None:
        try:
            from src.llm_client import LLMClient
            llm = LLMClient()
        except Exception as exc:
            logger.warning("[DevBackends] 无法获取或初始化 LLMClient: %s", exc)
            return jsonify({
                "ok": True,
                "code": 200,
                "backends_count": 0,
                "backends": [],
                "default_backend": "none",
            })

    backends_info = []
    backends_dict = getattr(llm, "backends", {})
    default_backend_name = getattr(llm, "default_backend_name", "default")

    for b_name, b_inst in backends_dict.items():
        b_type = b_inst.__class__.__name__
        model_name = getattr(b_inst, "model_name", "local-engine")
        api_base = getattr(b_inst, "base_url", None)

        is_healthy = True
        health_note = "ready"
        if hasattr(b_inst, "health_check"):
            try:
                is_healthy, health_note = b_inst.health_check()
            except Exception as exc:
                is_healthy = False
                health_note = str(exc)

        backends_info.append({
            "name": b_name,
            "type": b_type,
            "model_name": model_name,
            "api_base": api_base,
            "is_default": (b_name == default_backend_name),
            "healthy": is_healthy,
            "status_note": health_note,
        })

    return jsonify({
        "ok": True,
        "code": 200,
        "default_backend": default_backend_name,
        "backends_count": len(backends_info),
        "backends": backends_info,
    })


@dev_bp.route("/api/dev/task-graph/validate", methods=["POST"])
@_require_api_token
def dev_validate_task_graph():
    """验证并分析 TaskGraph DAG 拓扑、并行执行分代及机器人候选分配"""
    from src.task_graph import TaskGraph, CyclicDependencyError, TaskGraphAllocator
    from src.web.state import _shared_kb

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "code": 400, "msg": "请求体必须为 JSON 对象"}), 400

    try:
        graph = TaskGraph.from_dict(payload)
    except CyclicDependencyError as exc:
        return jsonify({
            "ok": False,
            "code": 400,
            "error_type": "CyclicDependencyError",
            "msg": f"任务图中存在循环依赖: {exc}",
        }), 400
    except Exception as exc:
        return jsonify({
            "ok": False,
            "code": 400,
            "error_type": "SchemaError",
            "msg": f"解析任务图失败: {exc}",
        }), 400

    batches = graph.get_execution_batches()
    ready_nodes = [n.to_dict() for n in graph.get_ready_nodes()]

    robots = []
    if _shared_kb is not None and hasattr(_shared_kb, "units"):
        units = getattr(_shared_kb, "units", {})
        for u_id, u_info in units.items():
            robots.append({
                "unit_id": u_id,
                "family": u_info.get("family", ""),
                "max_depth_m": u_info.get("max_depth_m", 3000),
                "capabilities": u_info.get("capabilities", ["visual_inspection", "cable_burial", "manipulator_operation"]),
            })
    allocator = TaskGraphAllocator(robots)
    allocations = allocator.allocate_ready_nodes(graph)

    return jsonify({
        "ok": True,
        "code": 200,
        "graph_id": graph.graph_id,
        "node_count": graph.node_count,
        "edge_count": graph.edge_count,
        "execution_batches": batches,
        "ready_nodes": ready_nodes,
        "allocations": allocations,
        "mermaid": graph.to_mermaid(),
    })


@dev_bp.route("/api/dev/gateway/drivers", methods=["GET"])
@_require_api_token
def dev_list_gateway_drivers():
    """返回当前 Robot Gateway 注册的所有机器人驱动状态"""
    from src.robot_gateway import RobotGateway, SimulatedRobotDriver

    gw = RobotGateway.get_instance()
    if not gw.list_drivers():
        gw.register_driver(SimulatedRobotDriver(unit_id="SIM-ROV-001"), is_default=True)

    return jsonify({
        "ok": True,
        "code": 200,
        "drivers": gw.list_drivers(),
    })


@dev_bp.route("/api/dev/gateway/telemetry", methods=["GET"])
@_require_api_token
def dev_poll_fleet_telemetry():
    """聚合获取所有已注册机器人的标准化遥测快照"""
    from src.robot_gateway import RobotGateway, SimulatedRobotDriver

    gw = RobotGateway.get_instance()
    if not gw.list_drivers():
        gw.register_driver(SimulatedRobotDriver(unit_id="SIM-ROV-001"), is_default=True)

    telemetry_map = {uid: snap.to_dict() for uid, snap in gw.poll_fleet_telemetry().items()}
    return jsonify({
        "ok": True,
        "code": 200,
        "fleet_telemetry": telemetry_map,
    })


@dev_bp.route("/api/dev/world-model/snapshot", methods=["GET"])
@_require_api_token
def dev_world_model_snapshot():
    """获取水下 4D 世界模型数字孪生快照 (设施、地理围栏与遥测)"""
    from src.world_model import SubseaWorldModel

    wm = SubseaWorldModel.get_instance()
    snapshot = wm.export_snapshot()
    return jsonify({
        "ok": True,
        "code": 200,
        "world_model": snapshot,
    })


@dev_bp.route("/api/dev/world-model/check-safety", methods=["POST"])
@_require_api_token
def dev_world_model_check_safety():
    """校验单点或作业轨迹的空间地理围栏与水下设施物理包络安全"""
    from src.world_model import SubseaWorldModel

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "code": 400, "msg": "请求体必须为 JSON 对象"}), 400

    wm = SubseaWorldModel.get_instance()
    min_clearance = float(payload.get("min_clearance_m", 5.0))

    if "waypoints" in payload and isinstance(payload["waypoints"], list):
        result = wm.check_path_safety(payload["waypoints"], min_clearance_m=min_clearance)
    elif "lat" in payload and "lon" in payload:
        result = wm.check_point_safety(
            lat=float(payload["lat"]),
            lon=float(payload["lon"]),
            depth_m=float(payload.get("depth_m", 0.0)),
            min_clearance_m=min_clearance,
        )
    else:
        return jsonify({
            "ok": False,
            "code": 400,
            "msg": "缺少必要坐标参数，请提供 (lat, lon, depth_m) 或 waypoints 轨迹列表",
        }), 400

    return jsonify({
        "ok": True,
        "code": 200,
        "safety_assessment": result.to_dict(),
    })


@dev_bp.route("/api/dev/weak-comm/encode-task", methods=["POST"])
@_require_api_token
def dev_weak_comm_encode_task():
    """将 JSON 任务计划压缩编码为 32 字节水声微弱通信二进制数据包"""
    import json
    from src.weak_comm import encode_task_intent, decode_task_intent

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "code": 400, "msg": "请求体必须为 JSON 对象"}), 400

    intent = payload.get("intent", payload)
    raw_json_bytes = json.dumps(intent, ensure_ascii=False).encode("utf-8")
    original_size = len(raw_json_bytes)

    binary_frame = encode_task_intent(intent, seq=int(payload.get("seq", 1)))
    compressed_size = len(binary_frame)
    compression_ratio = round(original_size / max(compressed_size, 1), 1)

    decoded = decode_task_intent(binary_frame)

    return jsonify({
        "ok": True,
        "code": 200,
        "original_size_bytes": original_size,
        "compressed_size_bytes": compressed_size,
        "compression_ratio": f"{compression_ratio}x",
        "frame_hex": binary_frame.hex(),
        "decoded_verification": decoded,
    })


@dev_bp.route("/api/dev/weak-comm/simulate", methods=["POST"])
@_require_api_token
def dev_weak_comm_simulate():
    """模拟水下声学高丢包率信道与 ARQ 自适应重传"""
    from src.weak_comm import (
        AcousticARQSession,
        AcousticChannelSimulator,
        encode_task_intent,
    )

    payload = request.get_json(silent=True) or {}
    loss_rate = float(payload.get("packet_loss_rate", 0.3))
    packet_count = int(payload.get("packet_count", 5))

    sim = AcousticChannelSimulator(packet_loss_rate=loss_rate, random_seed=42)
    session = AcousticARQSession()

    results = []
    for i in range(1, packet_count + 1):
        test_intent = {
            "intent_id": f"TI_WEAK_{i}",
            "task_type": "pipeline_inspection",
            "water_depth": 150.0 + i * 10,
        }
        frame = encode_task_intent(test_intent, seq=i)
        delivered_frame = sim.transmit(frame)
        if delivered_frame:
            decoded, ack = session.receive_frame(delivered_frame)
            results.append({"seq": i, "status": "DELIVERED", "crc_ok": decoded is not None})
        else:
            results.append({"seq": i, "status": "DROPPED", "crc_ok": False})

    return jsonify({
        "ok": True,
        "code": 200,
        "simulation_stats": {
            "packets_transmitted": sim.packets_transmitted,
            "packets_dropped": sim.packets_dropped,
            "loss_rate_simulated": loss_rate,
            "results": results,
        },
    })






