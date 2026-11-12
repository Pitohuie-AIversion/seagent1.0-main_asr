"""Mission Orchestrator executing end-to-end DAG task batches, gateway dispatch, and replanning."""
from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Any

from src.observability import trace_span
from src.replanning import DynamicReplanningEngine, ReplanningTrigger, ReplanningTriggerType
from src.robot_gateway import RobotGateway
from src.task_graph import TaskGraph, TaskGraphAllocator, TaskNode, TaskNodeStatus
from src.world_model import SubseaWorldModel
from .decomposer import MissionDecomposer
from .models import MissionState, MissionSummary, SubtaskExecutionRecord

logger = logging.getLogger(__name__)


class MissionOrchestrator:
    """Coordinates DAG mission lifecycle: decomposition, 4D safety preflight,
    fleet allocation, gateway dispatch, and feedback-driven replanning."""

    def __init__(
        self,
        gateway: RobotGateway | None = None,
        world_model: SubseaWorldModel | None = None,
        replanning_engine: DynamicReplanningEngine | None = None,
    ):
        self.gateway = gateway or RobotGateway.get_instance()
        self.world_model = world_model or SubseaWorldModel.get_instance()
        self.replanning_engine = replanning_engine or DynamicReplanningEngine()
        self._execution_history: dict[str, list[SubtaskExecutionRecord]] = {}
        self._replanning_counts: dict[str, int] = {}

    def create_mission(self, intent: dict[str, Any]) -> TaskGraph:
        """Decompose a high-level TaskIntent into an executable TaskGraph DAG."""
        with trace_span("seagent.mission.create", attributes={"intent_id": str(intent.get("intent_id"))}):
            graph = MissionDecomposer.decompose(intent)
            self._execution_history[graph.graph_id] = []
            self._replanning_counts[graph.graph_id] = 0
            return graph

    def run_preflight_safety(
        self,
        graph: TaskGraph,
        min_clearance_m: float = 5.0,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        """Perform 4D geofence, subsea asset clearance, and ocean current window audit for all nodes in the DAG."""
        with trace_span("seagent.mission.preflight_safety", attributes={"graph_id": graph.graph_id}) as span:
            violations: list[dict[str, Any]] = []

            for node in graph.nodes.values():
                coords = node.parameters.get("coordinates") or {}
                lat = coords.get("lat") or node.parameters.get("lat")
                lon = coords.get("lon") or node.parameters.get("lon")
                depth = node.water_depth

                if lat is not None and lon is not None:
                    current_limit = node.parameters.get("max_current_mps") or node.parameters.get("current_limit_mps")
                    if current_limit is None and node.assigned_robot_id:
                        try:
                            from seagent_marine_current.integration import get_deterministic_current_limit
                            current_limit = get_deterministic_current_limit({
                                "equipment_unit_id": node.assigned_robot_id,
                                "task_type": node.task_type,
                            })
                        except Exception:
                            pass

                    assessment = self.world_model.check_point_safety(
                        lat=float(lat),
                        lon=float(lon),
                        depth_m=depth,
                        min_clearance_m=min_clearance_m,
                        current_limit_mps=current_limit,
                    )
                    if not assessment.is_safe:
                        for v in assessment.violations:
                            v_dict = v.to_dict() if hasattr(v, "to_dict") else dict(v)
                            v_dict["node_id"] = node.node_id
                            violations.append(v_dict)
                    if assessment.warnings:
                        for w in assessment.warnings:
                            w_dict = w.to_dict() if hasattr(w, "to_dict") else dict(w)
                            w_dict["node_id"] = node.node_id
                            violations.append(w_dict)

            hard_violations = [v for v in violations if v.get("severity") == "hard"]
            is_passed = len(hard_violations) == 0

            span.set_attribute("passed", is_passed)
            span.set_attribute("violations_count", len(violations))

            if not is_passed:
                has_current = any(v.get("type") == "high_current_hazard" for v in hard_violations)
                if has_current:
                    msg = f"任务图包含 {len(hard_violations)} 处硬约束阻断违规（包含水下急流超限险情）"
                else:
                    msg = f"任务图包含 {len(hard_violations)} 处硬约束地理围栏阻断违规"
                logger.warning("[MissionOrchestrator] Preflight failed for %s: %s", graph.graph_id, msg)
                return False, msg, violations

            msg = "预检通过" if not violations else f"预检通过，包含 {len(violations)} 项软警告"
            return True, msg, violations

    def allocate_fleet(
        self,
        graph: TaskGraph,
        available_robots: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        """Allocate available robots from the Gateway fleet to ready and unassigned nodes."""
        with trace_span("seagent.mission.allocate", attributes={"graph_id": graph.graph_id}):
            if available_robots is None:
                available_robots = []
                for d_info in self.gateway.list_drivers():
                    available_robots.append({
                        "unit_id": d_info["unit_id"],
                        "status": d_info["status"],
                        "max_depth_m": 3000.0,
                        "capabilities": ["all"],
                    })

            allocator = TaskGraphAllocator(available_robots)
            return allocator.allocate_ready_nodes(graph)

    @staticmethod
    def _extract_node_waypoints(node: TaskNode) -> list[dict[str, Any]]:
        """Extract spatial geodetic waypoints from a task node for 4D corridor evaluation."""
        if "waypoints" in node.parameters and isinstance(node.parameters["waypoints"], list):
            return node.parameters["waypoints"]
        coords = node.parameters.get("coordinates") or {}
        lat = node.parameters.get("lat") or coords.get("lat")
        lon = node.parameters.get("lon") or coords.get("lon")
        depth = node.water_depth or node.parameters.get("depth_m") or coords.get("depth_m") or 0.0
        if lat is not None and lon is not None:
            return [{"lat": float(lat), "lon": float(lon), "depth_m": float(depth)}]
        return []

    def step_mission(
        self,
        graph: TaskGraph,
        available_robots: list[dict[str, Any]] | None = None,
        force_fail_node_id: str | None = None,
        failure_reason: str = "Subsea execution failure",
        use_acoustic_comm: bool = False,
        acoustic_channel: Any | None = None,
        parallel_dispatch: bool = True,
        max_workers: int = 4,
        check_4d_corridor: bool = True,
    ) -> dict[str, Any]:
        """Execute one topological stage of the mission DAG.
        Dispatches READY nodes to the Robot Gateway in parallel across distinct robots
        with 4D corridor deconfliction, and triggers Dynamic Replanning upon failure."""
        with trace_span("seagent.mission.step", attributes={"graph_id": graph.graph_id}) as span:
            ready_nodes = graph.get_ready_nodes()
            history = self._execution_history.setdefault(graph.graph_id, [])

            if not ready_nodes:
                is_done = graph.is_completed()
                has_fail = graph.has_failures()
                st = MissionState.COMPLETED if is_done else (MissionState.ABORTED if has_fail else MissionState.IN_PROGRESS)
                return {
                    "mission_id": graph.graph_id,
                    "state": st.value,
                    "dispatched_nodes": [],
                    "replanned_events": [],
                    "parallel_executed": False,
                    "is_completed": is_done,
                    "has_failures": has_fail,
                    "execution_batches": graph.get_execution_batches(),
                    "mermaid": graph.to_mermaid(),
                }

            # Ensure unassigned ready nodes are assigned
            unassigned = [n for n in ready_nodes if not n.assigned_robot_id]
            if unassigned:
                self.allocate_fleet(graph, available_robots=available_robots)

            # Robot serialization: A single physical robot executes only one task at a time.
            # Select up to one ready node per distinct robot for concurrent dispatch in this wave.
            dispatchable_nodes: list[TaskNode] = []
            seen_robots: set[str] = set()
            for node in ready_nodes:
                r_id = node.assigned_robot_id or f"UNIT_{node.node_id}"
                if r_id not in seen_robots:
                    seen_robots.add(r_id)
                    dispatchable_nodes.append(node)
                else:
                    logger.info(
                        "[MissionOrchestrator] Robot %s already assigned in current wave; deferring node %s",
                        r_id, node.node_id,
                    )

            # 4D Temporal-Spatial Corridor Conflict Detection & Altitude Separation Deconfliction
            if check_4d_corridor and len(dispatchable_nodes) > 1:
                from src.world_model.path_planner import _calc_geodetic_dist

                reserved_trajectories: list[tuple[str, list[dict[str, Any]]]] = []
                final_dispatchable: list[TaskNode] = []

                for node in dispatchable_nodes:
                    node_wps = self._extract_node_waypoints(node)
                    target_unit = node.assigned_robot_id or f"UNIT_{node.node_id}"
                    if not node_wps:
                        final_dispatchable.append(node)
                        continue

                    has_conflict = False
                    for other_unit, other_wps in reserved_trajectories:
                        if other_unit == target_unit:
                            continue
                        for wp1 in node_wps:
                            for wp2 in other_wps:
                                dist_m = _calc_geodetic_dist(
                                    float(wp1["lat"]), float(wp1["lon"]), float(wp1.get("depth_m", 0.0)),
                                    float(wp2["lat"]), float(wp2["lon"]), float(wp2.get("depth_m", 0.0)),
                                )
                                if dist_m < 20.0:  # 2 * R_min (10m) = 20m separation
                                    has_conflict = True
                                    break
                            if has_conflict:
                                break
                        if has_conflict:
                            break

                    if has_conflict:
                        curr_depth = float(node.water_depth or node.parameters.get("depth_m", 0.0))
                        alt_offset = -25.0 if curr_depth > 40.0 else 25.0
                        new_depth = max(5.0, curr_depth + alt_offset)
                        node.water_depth = new_depth
                        node.parameters["depth_m"] = new_depth
                        if "coordinates" in node.parameters and isinstance(node.parameters["coordinates"], dict):
                            node.parameters["coordinates"]["depth_m"] = new_depth
                        node_wps = [{"lat": wp["lat"], "lon": wp["lon"], "depth_m": new_depth} for wp in node_wps]
                        if "waypoints" in node.parameters and isinstance(node.parameters["waypoints"], list):
                            node.parameters["waypoints"] = node_wps

                        logger.info(
                            "[MissionOrchestrator] Multi-robot corridor conflict detected for node %s on unit %s. Auto-applied altitude separation offset: %.1fm (new depth: %.1fm)",
                            node.node_id, target_unit, alt_offset, new_depth,
                        )

                    reserved_trajectories.append((target_unit, node_wps))
                    final_dispatchable.append(node)

                dispatchable_nodes = final_dispatchable

            # Reserve corridors in SubseaWorldModel for all active subtasks
            if check_4d_corridor:
                for node in dispatchable_nodes:
                    node_wps = self._extract_node_waypoints(node)
                    if node_wps and node.assigned_robot_id:
                        self.world_model.reserve_robot_corridor(
                            robot_id=node.assigned_robot_id,
                            waypoints=node_wps,
                            radius_m=15.0,
                        )

            # Single node dispatch worker
            def _dispatch_single_node(node: TaskNode, seq_num: int) -> dict[str, Any]:
                target_unit = node.assigned_robot_id
                start_t = time.time()

                is_injected_fail = (force_fail_node_id == node.node_id)
                is_acoustic_dropped = False
                raw_acoustic_frame: bytes | None = None

                if use_acoustic_comm:
                    try:
                        from src.weak_comm.codec import encode_task_intent
                        from src.weak_comm.channel_simulator import AcousticChannelSimulator

                        intent_payload = {
                            "intent_id": node.node_id,
                            "task_type": node.task_type,
                            "equipment_unit_id": target_unit or "ROV-001",
                            "water_depth": node.water_depth,
                            "parameters": node.parameters,
                        }
                        raw_acoustic_frame = encode_task_intent(intent_payload, seq=seq_num)
                        sim = acoustic_channel or AcousticChannelSimulator(packet_loss_rate=0.0)
                        tx_result = sim.transmit(raw_acoustic_frame)
                        if tx_result is None:
                            is_acoustic_dropped = True
                    except Exception as comm_exc:
                        logger.warning("[MissionOrchestrator] Acoustic encoding failed: %s", comm_exc)

                if is_injected_fail:
                    dispatch_status = TaskNodeStatus.FAILED
                    err_msg = failure_reason
                    telemetry = {"simulated_fault": True}
                elif use_acoustic_comm and is_acoustic_dropped:
                    dispatch_status = TaskNodeStatus.FAILED
                    err_msg = f"水声弱网通信链路中断: 32字节帧(seq={seq_num})在水下信道丢失，ARQ确认超时"
                    telemetry = {
                        "acoustic_comm": {
                            "seq": seq_num,
                            "frame_size": len(raw_acoustic_frame) if raw_acoustic_frame else 32,
                            "delivered": False,
                            "error": "packet_loss",
                        }
                    }
                else:
                    task_payload = {
                        "task_id": node.node_id,
                        "task_type": node.task_type,
                        "parameters": node.parameters,
                    }
                    exec_res = self.gateway.dispatch_task(task_payload, unit_id=target_unit)
                    is_ok = str(exec_res.status.value if hasattr(exec_res.status, "value") else exec_res.status) in ("SENT", "ACCEPTED")
                    dispatch_status = TaskNodeStatus.COMPLETED if is_ok else TaskNodeStatus.FAILED
                    err_msg = exec_res.error_message or (exec_res.message if not is_ok else None)
                    telemetry = exec_res.raw_response or {}
                    if use_acoustic_comm and raw_acoustic_frame is not None:
                        telemetry["acoustic_comm"] = {
                            "seq": seq_num,
                            "frame_size": len(raw_acoustic_frame),
                            "delivered": True,
                            "codec": "acoustic_32byte_crc16",
                        }

                return {
                    "node": node,
                    "target_unit": target_unit,
                    "start_t": start_t,
                    "finish_t": time.time(),
                    "dispatch_status": dispatch_status,
                    "err_msg": err_msg,
                    "telemetry": telemetry,
                }

            # Concurrent execution across distinct robots
            if parallel_dispatch and len(dispatchable_nodes) > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(dispatchable_nodes))) as executor:
                    futures = [
                        executor.submit(_dispatch_single_node, node, len(history) + idx + 1)
                        for idx, node in enumerate(dispatchable_nodes)
                    ]
                    exec_results = [f.result() for f in futures]
            else:
                exec_results = [
                    _dispatch_single_node(node, len(history) + idx + 1)
                    for idx, node in enumerate(dispatchable_nodes)
                ]

            dispatched_nodes = []
            replanned_events = []

            for item in exec_results:
                node = item["node"]
                target_unit = item["target_unit"]
                dispatch_status = item["dispatch_status"]
                err_msg = item["err_msg"]
                telemetry = item["telemetry"]

                rec = SubtaskExecutionRecord(
                    node_id=node.node_id,
                    task_type=node.task_type,
                    robot_id=target_unit,
                    status=dispatch_status,
                    dispatched_at=item["start_t"],
                    finished_at=item["finish_t"],
                    error=err_msg,
                    telemetry_snapshot=telemetry,
                )
                history.append(rec)
                dispatched_nodes.append(node.node_id)

                if dispatch_status == TaskNodeStatus.COMPLETED:
                    graph.update_node_status(node.node_id, TaskNodeStatus.COMPLETED, result={"telemetry": telemetry})
                    logger.info("[MissionOrchestrator] Node %s completed successfully on unit %s", node.node_id, target_unit)
                else:
                    logger.warning("[MissionOrchestrator] Node %s failed: %s. Triggering replanning...", node.node_id, err_msg)
                    trigger = ReplanningTrigger(
                        trigger_type=ReplanningTriggerType.SUBTASK_FAILED,
                        node_id=node.node_id,
                        unit_id=target_unit,
                        reason=err_msg or "Execution failed",
                    )
                    plan = self.replanning_engine.evaluate_failure(trigger, graph, available_robots=available_robots)
                    self._replanning_counts[graph.graph_id] = self._replanning_counts.get(graph.graph_id, 0) + 1

                    if plan.is_recoverable and plan.actions:
                        graph = self.replanning_engine.apply_replanning_plan(plan, graph)
                        replanned_events.append(plan.to_dict())
                        logger.info("[MissionOrchestrator] Replanning applied for %s: %s", node.node_id, plan.summary)
                    else:
                        graph.update_node_status(node.node_id, TaskNodeStatus.FAILED, error=err_msg)

            if check_4d_corridor:
                self.world_model.clear_corridor_reservations()

            is_done = graph.is_completed()
            has_fail = graph.has_failures()

            if is_done:
                mission_state = MissionState.COMPLETED
            elif has_fail:
                mission_state = MissionState.ABORTED
            else:
                mission_state = MissionState.IN_PROGRESS

            span.set_attribute("mission_state", mission_state.value)
            span.set_attribute("dispatched_count", len(dispatched_nodes))

            return {
                "mission_id": graph.graph_id,
                "state": mission_state.value,
                "dispatched_nodes": dispatched_nodes,
                "replanned_events": replanned_events,
                "parallel_executed": bool(parallel_dispatch and len(dispatched_nodes) > 1),
                "is_completed": is_done,
                "has_failures": has_fail,
                "execution_batches": graph.get_execution_batches(),
                "mermaid": graph.to_mermaid(),
            }

    def run_mission_to_completion(
        self,
        graph: TaskGraph,
        max_steps: int = 20,
        available_robots: list[dict[str, Any]] | None = None,
        use_acoustic_comm: bool = False,
        acoustic_channel: Any | None = None,
        force_fail_node_id: str | None = None,
        failure_reason: str = "Subsea execution failure",
        parallel_dispatch: bool = True,
        max_workers: int = 4,
        check_4d_corridor: bool = True,
    ) -> dict[str, Any]:
        """Autonomously step through DAG task batches until the mission is either
        COMPLETED, ABORTED, or max_steps limit is reached."""
        with trace_span("seagent.mission.run_to_completion", attributes={"graph_id": graph.graph_id}) as span:
            step_records: list[dict[str, Any]] = []
            active_force_fail = force_fail_node_id

            while len(step_records) < max_steps:
                # If all nodes are already completed or graph aborted, finish loop
                if graph.is_completed():
                    break

                ready_nodes = graph.get_ready_nodes()
                if not ready_nodes:
                    # No nodes ready to execute
                    break

                # Ensure unassigned ready nodes are assigned
                unassigned = [n for n in ready_nodes if not n.assigned_robot_id]
                if unassigned:
                    self.allocate_fleet(graph, available_robots=available_robots)

                # Inject simulated fault only once on target node
                step_fail_id = active_force_fail if (active_force_fail and any(n.node_id == active_force_fail for n in ready_nodes)) else None

                step_res = self.step_mission(
                    graph=graph,
                    available_robots=available_robots,
                    force_fail_node_id=step_fail_id,
                    failure_reason=failure_reason,
                    use_acoustic_comm=use_acoustic_comm,
                    acoustic_channel=acoustic_channel,
                    parallel_dispatch=parallel_dispatch,
                    max_workers=max_workers,
                    check_4d_corridor=check_4d_corridor,
                )

                if step_fail_id:
                    active_force_fail = None

                step_records.append(step_res)

                if step_res["is_completed"]:
                    break
                if step_res["has_failures"] and not step_res["replanned_events"]:
                    # Unrecoverable failure occurred without replanning
                    break

            summary = self.get_mission_summary(graph)
            span.set_attribute("final_state", summary.state.value)
            span.set_attribute("total_steps", len(step_records))

            return {
                "mission_id": graph.graph_id,
                "state": summary.state.value,
                "steps_count": len(step_records),
                "is_completed": graph.is_completed(),
                "has_failures": graph.has_failures(),
                "step_records": step_records,
                "summary": summary.to_dict(),
                "graph": graph.to_dict(),
                "mermaid": graph.to_mermaid(),
            }

    def get_mission_summary(self, graph: TaskGraph) -> MissionSummary:
        """Export comprehensive audit summary of the orchestrated mission."""
        history = self._execution_history.get(graph.graph_id, [])
        completed = sum(1 for n in graph.nodes.values() if n.status == TaskNodeStatus.COMPLETED)
        failed = sum(1 for n in graph.nodes.values() if n.status == TaskNodeStatus.FAILED)

        if graph.is_completed():
            st = MissionState.COMPLETED
        elif graph.has_failures():
            st = MissionState.ABORTED
        elif history:
            st = MissionState.IN_PROGRESS
        else:
            st = MissionState.INITIALIZED

        return MissionSummary(
            mission_id=graph.graph_id,
            state=st,
            total_subtasks=graph.node_count,
            completed_subtasks=completed,
            failed_subtasks=failed,
            replanning_count=self._replanning_counts.get(graph.graph_id, 0),
            execution_history=history,
            mermaid_flowchart=graph.to_mermaid(),
        )
