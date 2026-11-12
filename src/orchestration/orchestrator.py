"""Mission Orchestrator executing end-to-end DAG task batches, gateway dispatch, and replanning."""
from __future__ import annotations

import logging
import time
from typing import Any

from src.observability import trace_span
from src.replanning import DynamicReplanningEngine, ReplanningTrigger, ReplanningTriggerType
from src.robot_gateway import RobotGateway
from src.task_graph import TaskGraph, TaskGraphAllocator, TaskNodeStatus
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

    def step_mission(
        self,
        graph: TaskGraph,
        available_robots: list[dict[str, Any]] | None = None,
        force_fail_node_id: str | None = None,
        failure_reason: str = "Subsea execution failure",
        use_acoustic_comm: bool = False,
        acoustic_channel: Any | None = None,
    ) -> dict[str, Any]:
        """Execute one topological stage of the mission DAG.
        Dispatches READY nodes to the Robot Gateway and triggers Dynamic Replanning upon failure."""
        with trace_span("seagent.mission.step", attributes={"graph_id": graph.graph_id}) as span:
            ready_nodes = graph.get_ready_nodes()
            history = self._execution_history.setdefault(graph.graph_id, [])

            dispatched_nodes = []
            replanned_events = []

            for node in ready_nodes:
                target_unit = node.assigned_robot_id
                start_t = time.time()

                # Simulate / test fault injection trigger
                is_injected_fail = (force_fail_node_id == node.node_id)
                is_acoustic_dropped = False
                raw_acoustic_frame: bytes | None = None
                seq_num = len(history) + 1

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

                rec = SubtaskExecutionRecord(
                    node_id=node.node_id,
                    task_type=node.task_type,
                    robot_id=target_unit,
                    status=dispatch_status,
                    dispatched_at=start_t,
                    finished_at=time.time(),
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
                    # Trigger Dynamic Replanning
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
