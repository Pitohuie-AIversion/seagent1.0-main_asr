"""Multi-robot allocation engine for Task Graph nodes."""
from __future__ import annotations

from typing import Any
from .graph import TaskGraph
from .node import TaskNode


class TaskGraphAllocator:
    """Allocates available robots to ready subtasks in a TaskGraph based on capability requirements."""

    def __init__(self, available_robots: list[dict[str, Any]] | None = None):
        """
        available_robots: list of robot dicts, e.g.:
        [
            {"unit_id": "OBSROV-75-001", "family": "observation_rov", "max_depth_m": 1000, "capabilities": ["visual_inspection"]},
            {"unit_id": "WROV-150-001", "family": "heavy_work_class_rov", "max_depth_m": 3000, "capabilities": ["cable_burial", "manipulator_operation"]},
        ]
        """
        self.robots = available_robots or []

    def allocate_ready_nodes(self, graph: TaskGraph) -> dict[str, str]:
        """
        Match ready unassigned nodes in graph to compatible available robots.
        Returns a mapping of {node_id: assigned_robot_id}.
        """
        ready_nodes = [n for n in graph.get_ready_nodes() if not n.assigned_robot_id]
        assigned_map: dict[str, str] = {}
        used_robots: set[str] = {
            n.assigned_robot_id for n in graph._nodes.values() if n.assigned_robot_id and n.status.value in ("RUNNING",)
        }

        for node in ready_nodes:
            allocated_robot = self._find_best_robot(node, used_robots)
            if allocated_robot:
                node.assigned_robot_id = allocated_robot["unit_id"]
                used_robots.add(allocated_robot["unit_id"])
                assigned_map[node.node_id] = allocated_robot["unit_id"]

        return assigned_map

    def _find_best_robot(self, node: TaskNode, used_robots: set[str]) -> dict[str, Any] | None:
        water_depth = node.parameters.get("water_depth", 0)

        for robot in self.robots:
            unit_id = robot.get("unit_id")
            if unit_id in used_robots:
                continue

            max_depth = robot.get("max_depth_m", 0)
            if water_depth and max_depth and water_depth > max_depth:
                continue

            # Check task type compatibility
            req_caps = robot.get("capabilities", [])
            if node.task_type == "pipeline_inspection" and not any("inspection" in c for c in req_caps):
                continue
            if node.task_type == "pipeline_burial" and not any("burial" in c for c in req_caps):
                continue
            if node.task_type == "tree_valve_operation" and not any("manipulator" in c or "valve" in c for c in req_caps):
                continue

            return robot
        return None
