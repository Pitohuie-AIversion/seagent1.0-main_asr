"""Unit tests for Execution-Feedback-Driven Dynamic Replanning Engine."""

import pytest
from src.replanning import (
    DynamicReplanningEngine,
    ReplanningAction,
    ReplanningActionType,
    ReplanningPlan,
    ReplanningTrigger,
    ReplanningTriggerType,
)
from src.task_graph import TaskGraph, TaskNode, TaskNodeStatus


def test_replanning_models_serialization():
    trigger = ReplanningTrigger(
        trigger_type=ReplanningTriggerType.SUBTASK_FAILED,
        node_id="subtask_01",
        unit_id="ROV-ALPHA",
        reason="Hydraulic pressure drop",
        context={"pressure_bar": 12.5},
    )
    t_dict = trigger.to_dict()
    assert t_dict["trigger_type"] == "subtask_failed"
    assert t_dict["node_id"] == "subtask_01"
    assert t_dict["unit_id"] == "ROV-ALPHA"
    assert t_dict["context"]["pressure_bar"] == 12.5

    action = ReplanningAction(
        action_type=ReplanningActionType.REALLOCATE_ROBOT,
        target_node_id="subtask_01",
        new_robot_id="ROV-BETA",
        reason="Reallocated to backup ROV-BETA",
    )
    a_dict = action.to_dict()
    assert a_dict["action_type"] == "reallocate_robot"
    assert a_dict["new_robot_id"] == "ROV-BETA"

    plan = ReplanningPlan(
        plan_id="plan_test_01",
        trigger=trigger,
        actions=[action],
        is_recoverable=True,
        summary="Recovery successful",
    )
    p_dict = plan.to_dict()
    assert p_dict["plan_id"] == "plan_test_01"
    assert len(p_dict["actions"]) == 1
    assert p_dict["is_recoverable"] is True


def test_replanning_robot_hardware_fault_reallocation():
    graph = TaskGraph("survey_mission")
    n1 = TaskNode("n1", "pipeline_inspection", assigned_robot_id="ROV-001", status=TaskNodeStatus.COMPLETED)
    n2 = TaskNode("n2", "pipeline_inspection", assigned_robot_id="ROV-001", status=TaskNodeStatus.READY)
    n3 = TaskNode("n3", "cable_tracking", assigned_robot_id="ROV-002", status=TaskNodeStatus.PENDING)

    graph.add_node(n1)
    graph.add_node(n2)
    graph.add_node(n3)
    graph.add_dependency("n1", "n2")
    graph.add_dependency("n2", "n3")

    available_robots = [
        {"unit_id": "ROV-001", "status": "FAULT", "max_depth_m": 3000, "capabilities": ["all"]},
        {"unit_id": "ROV-003", "status": "IDLE", "max_depth_m": 3000, "capabilities": ["all"]},
    ]

    engine = DynamicReplanningEngine()
    trigger = ReplanningTrigger(
        trigger_type=ReplanningTriggerType.ROBOT_HARDWARE_FAULT,
        unit_id="ROV-001",
        reason="Thruster controller short circuit",
    )

    plan = engine.evaluate_failure(trigger, graph, available_robots)
    assert plan.is_recoverable is True
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ReplanningActionType.REALLOCATE_ROBOT
    assert plan.actions[0].target_node_id == "n2"
    assert plan.actions[0].new_robot_id == "ROV-003"

    # Apply replanning
    engine.apply_replanning_plan(plan, graph)
    assert graph.get_node("n2").assigned_robot_id == "ROV-003"
    assert graph.get_node("n2").status == TaskNodeStatus.READY


def test_replanning_robot_hardware_fault_no_backup_aborts():
    graph = TaskGraph("deepsea_mission")
    node = TaskNode("deep_task", "deep_sampling", parameters={"water_depth": 2500.0}, assigned_robot_id="ROV-DEEP")
    graph.add_node(node)

    # Only shallow robot available
    available_robots = [
        {"unit_id": "ROV-SHALLOW", "status": "IDLE", "max_depth_m": 500.0, "capabilities": ["deep_sampling"]},
    ]

    engine = DynamicReplanningEngine()
    trigger = ReplanningTrigger(
        trigger_type=ReplanningTriggerType.ROBOT_HARDWARE_FAULT,
        unit_id="ROV-DEEP",
        reason="Power umbilical severed",
    )

    plan = engine.evaluate_failure(trigger, graph, available_robots)
    assert plan.is_recoverable is False
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ReplanningActionType.SAFE_ABORT_DOWNSTREAM


def test_replanning_valve_stuck_mitigation_splicing():
    graph = TaskGraph("valve_operation_pipeline")
    n_survey = TaskNode("survey_head", "visual_inspection", status=TaskNodeStatus.COMPLETED)
    n_valve = TaskNode("turn_valve", "valve_control", status=TaskNodeStatus.READY, assigned_robot_id="ROV-001")
    n_verify = TaskNode("verify_seal", "leak_detection", status=TaskNodeStatus.PENDING)

    graph.add_node(n_survey)
    graph.add_node(n_valve)
    graph.add_node(n_verify)
    graph.add_dependency("survey_head", "turn_valve")
    graph.add_dependency("turn_valve", "verify_seal")

    engine = DynamicReplanningEngine()
    trigger = ReplanningTrigger(
        trigger_type=ReplanningTriggerType.SUBTASK_FAILED,
        node_id="turn_valve",
        reason="Hydraulic torque exceeded maximum limit: valve stuck with marine growth",
    )

    plan = engine.evaluate_failure(trigger, graph)
    assert plan.is_recoverable is True
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.action_type == ReplanningActionType.INSERT_MITIGATION_NODE
    assert action.mitigation_node is not None
    assert action.mitigation_node.node_id == "turn_valve_assist"
    assert action.mitigation_node.task_type == "torque_assist"

    # Apply replanning
    engine.apply_replanning_plan(plan, graph)

    # Check DAG structure:
    # survey_head -> turn_valve_assist -> turn_valve -> verify_seal
    assert "turn_valve_assist" in graph.nodes
    assist_node = graph.get_node("turn_valve_assist")
    assert assist_node.status == TaskNodeStatus.READY  # survey_head was already COMPLETED

    preds_of_assist = graph.get_prerequisites("turn_valve_assist")
    assert "survey_head" in preds_of_assist

    preds_of_valve = graph.get_prerequisites("turn_valve")
    assert "turn_valve_assist" in preds_of_valve
    assert "survey_head" not in preds_of_valve  # Was rewired

    batches = graph.get_execution_batches()
    assert len(batches) >= 3


def test_replanning_optional_subtask_skip():
    graph = TaskGraph("cleaning_pipeline")
    n_scan = TaskNode("scan_hull", "visual_inspection", status=TaskNodeStatus.COMPLETED)
    n_opt = TaskNode("extra_photo", "photo_record", status=TaskNodeStatus.READY, metadata={"optional": True})
    n_final = TaskNode("undock", "undock_carrier", status=TaskNodeStatus.PENDING)

    graph.add_node(n_scan)
    graph.add_node(n_opt)
    graph.add_node(n_final)
    graph.add_dependency("scan_hull", "extra_photo")
    graph.add_dependency("extra_photo", "undock")

    engine = DynamicReplanningEngine()
    trigger = ReplanningTrigger(
        trigger_type=ReplanningTriggerType.SUBTASK_FAILED,
        node_id="extra_photo",
        reason="Turbidity camera focus timeout",
    )

    plan = engine.evaluate_failure(trigger, graph)
    assert plan.is_recoverable is True
    assert plan.actions[0].action_type == ReplanningActionType.SKIP_OPTIONAL_NODE

    engine.apply_replanning_plan(plan, graph)
    assert graph.get_node("extra_photo").status == TaskNodeStatus.SKIPPED


def test_replanning_critical_subtask_abort():
    graph = TaskGraph("critical_repair")
    n_repair = TaskNode("weld_crack", "underwater_welding", status=TaskNodeStatus.READY)
    graph.add_node(n_repair)

    engine = DynamicReplanningEngine()
    trigger = ReplanningTrigger(
        trigger_type=ReplanningTriggerType.SUBTASK_FAILED,
        node_id="weld_crack",
        reason="Electrode containment lost, irreversible thermal breakdown",
    )

    plan = engine.evaluate_failure(trigger, graph)
    assert plan.is_recoverable is False
    assert plan.actions[0].action_type == ReplanningActionType.SAFE_ABORT_DOWNSTREAM

    engine.apply_replanning_plan(plan, graph)
    assert graph.get_node("weld_crack").status == TaskNodeStatus.FAILED


def test_api_dev_replanning_evaluate_endpoint():
    from web_backend import app
    client = app.test_client()

    payload = {
        "graph": {
            "graph_id": "test_replanning_dag",
            "nodes": [
                {"node_id": "step1", "task_type": "visual_inspection", "status": "COMPLETED"},
                {"node_id": "step2", "task_type": "valve_control", "status": "READY"},
            ],
            "edges": [
                {"from": "step1", "to": "step2"},
            ],
        },
        "trigger": {
            "trigger_type": "subtask_failed",
            "node_id": "step2",
            "reason": "valve jammed with marine growth, torque limit exceeded",
        },
        "apply": True,
    }

    resp = client.post("/api/dev/replanning/evaluate", json=payload)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "plan" in data
    assert data["plan"]["is_recoverable"] is True
    assert len(data["plan"]["actions"]) == 1
    assert data["plan"]["actions"][0]["action_type"] == "insert_mitigation_node"
    assert "mermaid" in data
    assert "step2_assist" in data["mermaid"]
    assert len(data["execution_batches"]) >= 2


def test_api_dev_replanning_evaluate_invalid_trigger_rejected():
    from web_backend import app
    client = app.test_client()

    payload = {
        "graph": {
            "graph_id": "test_replanning_dag",
            "nodes": [{"node_id": "step1", "task_type": "visual_inspection"}],
        },
        "trigger": {
            "trigger_type": "invalid_trigger_type_xyz",
        },
    }

    resp = client.post("/api/dev/replanning/evaluate", json=payload)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert "trigger_type" in data["msg"]
