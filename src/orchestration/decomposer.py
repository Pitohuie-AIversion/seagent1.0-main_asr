"""Mission Decomposer transforming high-level TaskIntents into DAG TaskGraphs."""
from __future__ import annotations

import uuid
from typing import Any

from src.task_graph import TaskGraph, TaskNode, TaskNodeStatus


class MissionDecomposer:
    """Decomposes composite oilfield subsea task intents into structured DAG task graphs."""

    @classmethod
    def decompose(cls, intent: dict[str, Any]) -> TaskGraph:
        intent_id = intent.get("intent_id") or f"intent_{uuid.uuid4().hex[:8]}"
        task_type = str(intent.get("task_type") or "generic_mission").lower()
        graph_id = f"graph_{intent_id}"

        description = intent.get("task_name") or f"Subsea Mission DAG for {intent_id}"
        graph = TaskGraph(graph_id=graph_id, description=description)

        water_depth = float(intent.get("water_depth") or intent.get("depth_m") or 150.0)
        assigned_robot = intent.get("equipment_unit_id") or intent.get("assigned_robot_id")

        if "valve" in task_type or "tree" in task_type or "采油树" in task_type or "阀" in task_type:
            cls._build_valve_operation_dag(graph, intent_id, water_depth, assigned_robot, intent)
        elif "pipe" in task_type or "pipeline" in task_type or "管道" in task_type:
            cls._build_pipeline_inspection_dag(graph, intent_id, water_depth, assigned_robot, intent)
        elif "cable" in task_type or "burial" in task_type or "海缆" in task_type:
            cls._build_cable_burial_dag(graph, intent_id, water_depth, assigned_robot, intent)
        else:
            cls._build_generic_dag(graph, intent_id, task_type, water_depth, assigned_robot, intent)

        return graph

    @classmethod
    def _build_valve_operation_dag(
        cls,
        graph: TaskGraph,
        intent_id: str,
        water_depth: float,
        assigned_robot: str | None,
        intent: dict[str, Any],
    ) -> None:
        turns = intent.get("turns", 5.0)
        direction = intent.get("direction", "close")

        n1 = TaskNode(
            node_id=f"{intent_id}_approach",
            task_type="visual_inspection",
            parameters={"water_depth": water_depth, "target": "valve_structure"},
            assigned_robot_id=assigned_robot,
            metadata={"name": "进近与采油树结构目视勘验"},
        )
        n2 = TaskNode(
            node_id=f"{intent_id}_clean",
            task_type="marine_cleaning",
            parameters={"water_depth": water_depth, "nozzle_pressure_bar": 180.0},
            assigned_robot_id=assigned_robot,
            metadata={"name": "阀门外圈生物附着高压冲洗"},
        )
        n3 = TaskNode(
            node_id=f"{intent_id}_turn_valve",
            task_type="valve_control",
            parameters={"water_depth": water_depth, "turns": turns, "direction": direction, "max_torque_nm": 350.0},
            assigned_robot_id=assigned_robot,
            metadata={"name": f"机械臂对接并回转阀门 ({direction}, {turns}圈)"},
        )
        n4 = TaskNode(
            node_id=f"{intent_id}_verify_seal",
            task_type="leak_detection",
            parameters={"water_depth": water_depth, "duration_s": 60.0},
            assigned_robot_id=assigned_robot,
            metadata={"name": "气密性与密封界面检漏复核"},
        )

        graph.add_node(n1)
        graph.add_node(n2)
        graph.add_node(n3)
        graph.add_node(n4)

        graph.add_dependency(n1.node_id, n2.node_id)
        graph.add_dependency(n2.node_id, n3.node_id)
        graph.add_dependency(n3.node_id, n4.node_id)

    @classmethod
    def _build_pipeline_inspection_dag(
        cls,
        graph: TaskGraph,
        intent_id: str,
        water_depth: float,
        assigned_robot: str | None,
        intent: dict[str, Any],
    ) -> None:
        pipe_id = intent.get("pipe_id") or "PIPE-MAIN-01"

        n1 = TaskNode(
            node_id=f"{intent_id}_descent",
            task_type="positioning_descent",
            parameters={"water_depth": water_depth, "target": pipe_id},
            assigned_robot_id=assigned_robot,
            metadata={"name": f"下潜定位至管道起始段 ({pipe_id})"},
        )
        n2 = TaskNode(
            node_id=f"{intent_id}_sonar_scan",
            task_type="pipeline_inspection",
            parameters={"water_depth": water_depth, "mode": "side_sonar"},
            assigned_robot_id=assigned_robot,
            metadata={"name": "高频侧扫声呐悬空与裸露巡检"},
        )
        n3 = TaskNode(
            node_id=f"{intent_id}_cp_audit",
            task_type="cathodic_inspection",
            parameters={"water_depth": water_depth, "contact_probe": True},
            assigned_robot_id=assigned_robot,
            metadata={"name": "管道阳极阴极保护电位 (CP) 触探测量"},
        )
        n4 = TaskNode(
            node_id=f"{intent_id}_report",
            task_type="report_generation",
            parameters={"water_depth": water_depth},
            assigned_robot_id=assigned_robot,
            metadata={"name": "生成缺陷图谱与完工归档"},
        )

        graph.add_node(n1)
        graph.add_node(n2)
        graph.add_node(n3)
        graph.add_node(n4)

        graph.add_dependency(n1.node_id, n2.node_id)
        graph.add_dependency(n2.node_id, n3.node_id)
        graph.add_dependency(n3.node_id, n4.node_id)

    @classmethod
    def _build_cable_burial_dag(
        cls,
        graph: TaskGraph,
        intent_id: str,
        water_depth: float,
        assigned_robot: str | None,
        intent: dict[str, Any],
    ) -> None:
        cable_id = intent.get("cable_id") or "CABLE-01"
        target_dob = float(intent.get("target_dob_m", 1.5))

        n1 = TaskNode(
            node_id=f"{intent_id}_pre_survey",
            task_type="visual_inspection",
            parameters={"water_depth": water_depth, "target": cable_id},
            assigned_robot_id=assigned_robot,
            metadata={"name": f"海缆路由与海底底质前置探测 ({cable_id})"},
        )
        n2 = TaskNode(
            node_id=f"{intent_id}_jetting_burial",
            task_type="cable_burial",
            parameters={"water_depth": water_depth, "target_dob_m": target_dob, "water_jet_flow": "high"},
            assigned_robot_id=assigned_robot,
            metadata={"name": f"高压水冲式海缆埋设作业 (目标深度 {target_dob}m)"},
        )
        n3 = TaskNode(
            node_id=f"{intent_id}_dob_audit",
            task_type="dob_verification",
            parameters={"water_depth": water_depth, "target_dob_m": target_dob},
            assigned_robot_id=assigned_robot,
            metadata={"name": "声学埋深仪 DOB 完工复核与回填检测"},
        )

        graph.add_node(n1)
        graph.add_node(n2)
        graph.add_node(n3)

        graph.add_dependency(n1.node_id, n2.node_id)
        graph.add_dependency(n2.node_id, n3.node_id)

    @classmethod
    def _build_generic_dag(
        cls,
        graph: TaskGraph,
        intent_id: str,
        task_type: str,
        water_depth: float,
        assigned_robot: str | None,
        intent: dict[str, Any],
    ) -> None:
        n1 = TaskNode(
            node_id=f"{intent_id}_survey",
            task_type="visual_inspection",
            parameters={"water_depth": water_depth},
            assigned_robot_id=assigned_robot,
            metadata={"name": "作业区域环境与状态勘验"},
        )
        n2 = TaskNode(
            node_id=f"{intent_id}_exec",
            task_type=task_type,
            parameters=dict(intent.get("parameters") or {"water_depth": water_depth}),
            assigned_robot_id=assigned_robot,
            metadata={"name": f"主任务执行: {task_type}"},
        )
        graph.add_node(n1)
        graph.add_node(n2)
        graph.add_dependency(n1.node_id, n2.node_id)
