"""Dynamic Replanning Engine executing feedback-driven DAG adaptation and mitigation splicing."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from src.task_graph import CyclicDependencyError, TaskGraph, TaskNode, TaskNodeStatus
from .models import (
    ReplanningAction,
    ReplanningActionType,
    ReplanningPlan,
    ReplanningTrigger,
    ReplanningTriggerType,
)

logger = logging.getLogger(__name__)


class DynamicReplanningEngine:
    """Evaluates runtime execution failures and dynamically restructures TaskGraph DAGs."""

    def evaluate_failure(
        self,
        trigger: ReplanningTrigger,
        graph: TaskGraph,
        available_robots: list[dict[str, Any]] | None = None,
    ) -> ReplanningPlan:
        """Synthesize a mitigation and recovery plan from an execution trigger."""
        plan_id = f"plan_{uuid.uuid4().hex[:8]}"
        actions: list[ReplanningAction] = []
        is_recoverable = True
        summary = ""

        # Case 1: Robot hardware fault or sudden disconnect
        if trigger.trigger_type == ReplanningTriggerType.ROBOT_HARDWARE_FAULT:
            failed_unit = trigger.unit_id
            uncompleted_nodes = [
                n
                for n in graph.nodes.values()
                if (n.assigned_robot_id == failed_unit or n.node_id == trigger.node_id)
                and n.status not in (TaskNodeStatus.COMPLETED, TaskNodeStatus.SKIPPED)
            ]

            reallocated_count = 0
            for node in uncompleted_nodes:
                backup_robot = self._find_backup_robot(node, available_robots or [], exclude_unit_id=failed_unit)
                if backup_robot:
                    actions.append(
                        ReplanningAction(
                            action_type=ReplanningActionType.REALLOCATE_ROBOT,
                            target_node_id=node.node_id,
                            new_robot_id=backup_robot["unit_id"],
                            reason=f"设备 {failed_unit} 发生硬件故障，重新分配至可用备用设备 {backup_robot['unit_id']}",
                        )
                    )
                    reallocated_count += 1
                else:
                    actions.append(
                        ReplanningAction(
                            action_type=ReplanningActionType.SAFE_ABORT_DOWNSTREAM,
                            target_node_id=node.node_id,
                            reason=f"未找到可替代设备执行子任务 {node.node_id}，触发安全中止",
                        )
                    )
                    is_recoverable = False

            summary = f"设备 {failed_unit} 故障，成功重分配 {reallocated_count} 个任务，终止 {len(uncompleted_nodes) - reallocated_count} 个任务"

        # Case 2: Subtask failed during execution
        elif trigger.trigger_type == ReplanningTriggerType.SUBTASK_FAILED and trigger.node_id:
            node = graph.get_node(trigger.node_id)
            if not node:
                return ReplanningPlan(plan_id=plan_id, trigger=trigger, is_recoverable=False, summary="Target node not found")

            reason_lower = trigger.reason.lower()

            # Valve stuck / torque limit exceeded -> splice in mitigation node
            if any(k in reason_lower for k in ("torque", "stuck", "jammed", "卡阻", "超扭", "过载")):
                mitigation_id = f"{node.node_id}_assist"
                mitigation_node = TaskNode(
                    node_id=mitigation_id,
                    task_type="torque_assist",
                    parameters={"water_depth": node.water_depth, "parent_task": node.node_id, "mitigation": True},
                    assigned_robot_id=node.assigned_robot_id,
                    metadata={"name": f"{node.name} · 液压反冲与增扭辅助", "parent_task": node.node_id, "mitigation": True},
                )
                actions.append(
                    ReplanningAction(
                        action_type=ReplanningActionType.INSERT_MITIGATION_NODE,
                        target_node_id=node.node_id,
                        mitigation_node=mitigation_node,
                        reason=f"针对子任务 {node.node_id} 阀门卡阻/超扭故障，动态插入前置增扭与反冲解卡作业",
                    )
                )
                summary = f"检测到阀门卡阻故障，动态插入解卡子任务 {mitigation_id} 并重置原任务"

            # Optional task failed -> skip without blocking downstream
            elif node.properties.get("optional") is True:
                actions.append(
                    ReplanningAction(
                        action_type=ReplanningActionType.SKIP_OPTIONAL_NODE,
                        target_node_id=node.node_id,
                        reason=f"非关键可选任务 {node.node_id} 执行失败，自动跳过以保障主航程",
                    )
                )
                summary = f"可选子任务 {node.node_id} 失败，已自动跳过以保障主作业航程"

            # Critical failure with no mitigation -> abort downstream
            else:
                actions.append(
                    ReplanningAction(
                        action_type=ReplanningActionType.SAFE_ABORT_DOWNSTREAM,
                        target_node_id=node.node_id,
                        reason=f"关键子任务 {node.node_id} 执行不可逆失败: {trigger.reason}",
                    )
                )
                is_recoverable = False
                summary = f"关键子任务 {node.node_id} 失败且无备选解，已安全中止下游依赖任务"

        # Case 3: Environmental ocean current exceeded
        elif trigger.trigger_type == ReplanningTriggerType.ENVIRONMENT_CURRENT_EXCEEDED and trigger.node_id:
            node = graph.get_node(trigger.node_id)
            if not node:
                return ReplanningPlan(plan_id=plan_id, trigger=trigger, is_recoverable=False, summary="Target node not found")

            # Try to reallocate to a heavy work-class robot with higher current limit
            backup_robot = None
            for robot in (available_robots or []):
                if robot.get("unit_id") == node.assigned_robot_id:
                    continue
                if robot.get("status") in ("BUSY", "FAULT", "OFFLINE"):
                    continue
                if float(robot.get("current_limit_mps", 1.5)) >= 2.0:
                    backup_robot = robot
                    break

            if backup_robot:
                actions.append(
                    ReplanningAction(
                        action_type=ReplanningActionType.REALLOCATE_ROBOT,
                        target_node_id=node.node_id,
                        new_robot_id=backup_robot["unit_id"],
                        reason=f"环境海流超限 ({trigger.reason})，重新分配至高抗流重型作业设备 {backup_robot['unit_id']}",
                    )
                )
                summary = f"海流超限，成功重分配任务至高抗流设备 {backup_robot['unit_id']}"
            else:
                mitigation_id = f"{node.node_id}_await_current"
                mitigation_node = TaskNode(
                    node_id=mitigation_id,
                    task_type="await_current_window",
                    parameters={"water_depth": node.water_depth, "parent_task": node.node_id, "hold_duration_s": 300},
                    assigned_robot_id=node.assigned_robot_id,
                    metadata={"name": f"{node.name} · 海流窗口观测与定深驻留", "parent_task": node.node_id, "mitigation": True},
                )
                actions.append(
                    ReplanningAction(
                        action_type=ReplanningActionType.INSERT_MITIGATION_NODE,
                        target_node_id=node.node_id,
                        mitigation_node=mitigation_node,
                        reason=f"海流流速超限 ({trigger.reason})，动态插入定深驻留等待海流平缓窗口子任务",
                    )
                )
                summary = f"海流超限，动态插入定深驻留子任务 {mitigation_id} 并延后执行原任务"

        # Case 4: Geofence proximity or dynamic obstacle encroaching
        elif trigger.trigger_type == ReplanningTriggerType.GEOFENCE_PROXIMITY_WARNING and trigger.node_id:
            node = graph.get_node(trigger.node_id)
            if not node:
                return ReplanningPlan(plan_id=plan_id, trigger=trigger, is_recoverable=False, summary="Target node not found")

            detour_id = f"{node.node_id}_detour"
            mitigation_node = TaskNode(
                node_id=detour_id,
                task_type="obstacle_avoidance_detour",
                parameters={"water_depth": node.water_depth, "parent_task": node.node_id, "clearance_m": 25.0},
                assigned_robot_id=node.assigned_robot_id,
                metadata={"name": f"{node.name} · 3D 避障旁通航段", "parent_task": node.node_id, "mitigation": True},
            )
            actions.append(
                ReplanningAction(
                    action_type=ReplanningActionType.INSERT_MITIGATION_NODE,
                    target_node_id=node.node_id,
                    mitigation_node=mitigation_node,
                    reason=f"地理围栏或动态障碍物预警 ({trigger.reason})，动态插入 3D 旁通避障航线节点",
                )
            )
            summary = f"检测到空间障碍物阻挡，动态插入 3D 旁通避障子任务 {detour_id}"

        # Case 5: Execution timeout
        elif trigger.trigger_type == ReplanningTriggerType.EXECUTION_TIMEOUT and trigger.node_id:
            node = graph.get_node(trigger.node_id)
            if not node:
                return ReplanningPlan(plan_id=plan_id, trigger=trigger, is_recoverable=False, summary="Target node not found")

            retries = getattr(node, "retry_count", 0)
            max_retries = int(node.properties.get("max_retries", 2))

            if retries < max_retries:
                actions.append(
                    ReplanningAction(
                        action_type=ReplanningActionType.RETRY_NODE,
                        target_node_id=node.node_id,
                        reason=f"子任务 {node.node_id} 执行超时 (重试次数: {retries + 1}/{max_retries})",
                    )
                )
                summary = f"子任务 {node.node_id} 超时，触发第 {retries + 1} 次就地重试"
            else:
                backup = self._find_backup_robot(node, available_robots or [], exclude_unit_id=node.assigned_robot_id)
                if backup:
                    actions.append(
                        ReplanningAction(
                            action_type=ReplanningActionType.REALLOCATE_ROBOT,
                            target_node_id=node.node_id,
                            new_robot_id=backup["unit_id"],
                            reason=f"子任务 {node.node_id} 达到最大重试次数，转派至备选设备 {backup['unit_id']}",
                        )
                    )
                    summary = f"子任务 {node.node_id} 超时达到上限，转派至设备 {backup['unit_id']}"
                elif node.properties.get("optional") is True:
                    actions.append(
                        ReplanningAction(
                            action_type=ReplanningActionType.SKIP_OPTIONAL_NODE,
                            target_node_id=node.node_id,
                            reason=f"可选子任务 {node.node_id} 超时且无重试额度，跳过以保护主流程",
                        )
                    )
                    summary = f"可选子任务 {node.node_id} 超时，已自动跳过"
                else:
                    actions.append(
                        ReplanningAction(
                            action_type=ReplanningActionType.SAFE_ABORT_DOWNSTREAM,
                            target_node_id=node.node_id,
                            reason=f"关键子任务 {node.node_id} 超时且耗尽重试配额，安全中止下游",
                        )
                    )
                    is_recoverable = False
                    summary = f"关键子任务 {node.node_id} 严重超时，安全中止下游"

        else:
            is_recoverable = False
            summary = f"未处理的重规划触发类型: {trigger.trigger_type.value}"

        return ReplanningPlan(
            plan_id=plan_id,
            trigger=trigger,
            actions=actions,
            is_recoverable=is_recoverable,
            summary=summary,
        )

    def apply_replanning_plan(self, plan: ReplanningPlan, graph: TaskGraph) -> TaskGraph:
        """Apply replanning actions directly onto a TaskGraph DAG and ensure acyclic integrity."""
        for action in plan.actions:
            target_node = graph.get_node(action.target_node_id)
            if not target_node:
                continue

            if action.action_type == ReplanningActionType.REALLOCATE_ROBOT:
                target_node.assigned_robot_id = action.new_robot_id
                # Reset to READY if all prerequisites are already completed
                preds = graph.get_prerequisites(target_node.node_id)
                if all(graph.get_node(p).status == TaskNodeStatus.COMPLETED for p in preds):
                    target_node.status = TaskNodeStatus.READY
                else:
                    target_node.status = TaskNodeStatus.PENDING

            elif action.action_type == ReplanningActionType.INSERT_MITIGATION_NODE and action.mitigation_node:
                mit_node = action.mitigation_node
                graph.add_node(mit_node)

                # Transfer predecessors of target_node to mit_node
                old_preds = list(graph.get_prerequisites(target_node.node_id))
                for pred in old_preds:
                    graph._graph.remove_edge(pred, target_node.node_id)
                    graph.add_dependency(pred, mit_node.node_id)

                # Connect mit_node -> target_node
                graph.add_dependency(mit_node.node_id, target_node.node_id)

                # Update statuses
                target_node.status = TaskNodeStatus.PENDING
                mit_preds = graph.get_prerequisites(mit_node.node_id)
                if all(graph.get_node(p).status == TaskNodeStatus.COMPLETED for p in mit_preds):
                    mit_node.status = TaskNodeStatus.READY
                else:
                    mit_node.status = TaskNodeStatus.PENDING

            elif action.action_type == ReplanningActionType.RETRY_NODE:
                target_node.retry_count = getattr(target_node, "retry_count", 0) + 1
                target_node.status = TaskNodeStatus.READY

            elif action.action_type == ReplanningActionType.SKIP_OPTIONAL_NODE:
                target_node.status = TaskNodeStatus.SKIPPED
                # Re-evaluate ready nodes for successors
                graph.get_ready_nodes()

            elif action.action_type == ReplanningActionType.SAFE_ABORT_DOWNSTREAM:
                target_node.status = TaskNodeStatus.FAILED
                graph.update_node_status(target_node.node_id, TaskNodeStatus.FAILED)

        # Validate DAG acyclic property
        batches = graph.get_execution_batches()
        logger.info("[DynamicReplanning] Successfully applied replanning plan %s. DAG has %d execution batches.", plan.plan_id, len(batches))
        return graph

    def _find_backup_robot(
        self,
        node: TaskNode,
        available_robots: list[dict[str, Any]],
        exclude_unit_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find an available robot matching capabilities and water depth."""
        for robot in available_robots:
            uid = robot.get("unit_id")
            if uid == exclude_unit_id:
                continue
            if robot.get("status") in ("BUSY", "FAULT", "OFFLINE"):
                continue

            max_depth = float(robot.get("max_depth_m", 3000.0))
            if node.water_depth > max_depth:
                continue

            capabilities = robot.get("capabilities", [])
            if capabilities and node.task_type not in capabilities and "all" not in capabilities:
                continue

            return robot
        return None
