"""Unit tests for Subsea Mission Orchestration, Decomposer, and Feedback Execution."""

import pytest
from src.orchestration import (
    MissionDecomposer,
    MissionOrchestrator,
    MissionState,
)
from src.robot_gateway import RobotGateway, SimulatedRobotDriver
from src.task_graph import TaskNodeStatus
from src.world_model import GeofenceZone, Point3D, SubseaWorldModel


def test_mission_decomposer_valve_operation():
    intent = {
        "intent_id": "TI_VALVE_01",
        "task_type": "valve_operation",
        "task_name": "采油树 2 号水下主阀闭合",
        "water_depth": 320.0,
        "turns": 6.5,
        "direction": "close",
        "equipment_unit_id": "ROV-001",
    }
    graph = MissionDecomposer.decompose(intent)
    assert graph.node_count == 4
    assert graph.edge_count == 3
    assert f"TI_VALVE_01_approach" in graph.nodes
    assert f"TI_VALVE_01_clean" in graph.nodes
    assert f"TI_VALVE_01_turn_valve" in graph.nodes
    assert f"TI_VALVE_01_verify_seal" in graph.nodes

    batches = graph.get_execution_batches()
    assert len(batches) == 4
    assert batches[0] == ["TI_VALVE_01_approach"]


def test_mission_decomposer_pipeline_inspection():
    intent = {
        "intent_id": "TI_PIPE_01",
        "task_type": "pipeline_inspection",
        "pipe_id": "PL-WEST-200",
        "water_depth": 450.0,
    }
    graph = MissionDecomposer.decompose(intent)
    assert graph.node_count == 4
    assert f"TI_PIPE_01_sonar_scan" in graph.nodes
    assert f"TI_PIPE_01_cp_audit" in graph.nodes


def test_mission_decomposer_cable_burial():
    intent = {
        "intent_id": "TI_CABLE_01",
        "task_type": "cable_burial",
        "cable_id": "EXP-CABLE-88",
        "target_dob_m": 2.0,
        "water_depth": 180.0,
    }
    graph = MissionDecomposer.decompose(intent)
    assert graph.node_count == 3
    assert f"TI_CABLE_01_jetting_burial" in graph.nodes


def test_mission_preflight_safety_pass_and_block():
    SubseaWorldModel.reset_instance()
    wm = SubseaWorldModel.get_instance()

    # Create safe open point
    intent_safe = {
        "intent_id": "TI_SAFE",
        "task_type": "valve_operation",
        "water_depth": 200.0,
        "parameters": {"lat": 18.250, "lon": 115.100},
    }
    orchestrator = MissionOrchestrator(world_model=wm)
    graph_safe = orchestrator.create_mission(intent_safe)

    passed, msg, violations = orchestrator.run_preflight_safety(graph_safe)
    assert passed is True

    from src.world_model.models import GeofenceZoneType, GeofenceSeverity
    wm.add_dynamic_geofence(
        GeofenceZone(
            zone_id="FORBIDDEN_TEST",
            name="Restricted Zone",
            zone_type=GeofenceZoneType.FORBIDDEN,
            severity=GeofenceSeverity.HARD,
            lat_range=(18.900, 19.100),
            lon_range=(114.900, 115.100),
        )
    )


    intent_unsafe = {
        "intent_id": "TI_UNSAFE",
        "task_type": "generic_mission",
        "water_depth": 250.0,
        "parameters": {"lat": 19.000, "lon": 115.000},
    }
    graph_unsafe = orchestrator.create_mission(intent_unsafe)
    # Assign coordinates to the node parameters
    for node in graph_unsafe.nodes.values():
        node.parameters["lat"] = 19.000
        node.parameters["lon"] = 115.000

    passed_un, msg_un, violations_un = orchestrator.run_preflight_safety(graph_unsafe)
    assert passed_un is False
    assert len(violations_un) >= 1
    assert any(v["severity"] == "hard" for v in violations_un)


def test_mission_step_success_progression_to_completion():
    gw = RobotGateway.get_instance()
    driver = SimulatedRobotDriver(unit_id="TEST-SIM-ROV")
    gw.register_driver(driver, is_default=True)

    orchestrator = MissionOrchestrator(gateway=gw)
    intent = {
        "intent_id": "TI_AUTO_FLOW",
        "task_type": "valve_operation",
        "water_depth": 300.0,
        "assigned_robot_id": "TEST-SIM-ROV",
    }
    graph = orchestrator.create_mission(intent)
    assert graph.node_count == 4

    # Run step 1: approach
    res1 = orchestrator.step_mission(graph)
    assert res1["state"] == MissionState.IN_PROGRESS.value
    assert len(res1["dispatched_nodes"]) == 1
    assert res1["is_completed"] is False

    # Run step 2: clean
    res2 = orchestrator.step_mission(graph)
    assert res2["state"] == MissionState.IN_PROGRESS.value
    assert len(res2["dispatched_nodes"]) == 1

    # Run step 3: turn valve
    res3 = orchestrator.step_mission(graph)
    assert res3["state"] == MissionState.IN_PROGRESS.value

    # Run step 4: verify seal
    res4 = orchestrator.step_mission(graph)
    assert res4["state"] == MissionState.COMPLETED.value
    assert res4["is_completed"] is True
    assert graph.is_completed() is True

    summary = orchestrator.get_mission_summary(graph)
    assert summary.state == MissionState.COMPLETED
    assert summary.completed_subtasks == 4
    assert len(summary.execution_history) == 4


def test_mission_step_dynamic_replanning_on_valve_stuck():
    gw = RobotGateway.get_instance()
    driver = SimulatedRobotDriver(unit_id="TEST-SIM-ROV")
    gw.register_driver(driver, is_default=True)

    orchestrator = MissionOrchestrator(gateway=gw)
    intent = {
        "intent_id": "TI_JAMMED_FLOW",
        "task_type": "valve_operation",
        "water_depth": 280.0,
        "assigned_robot_id": "TEST-SIM-ROV",
    }
    graph = orchestrator.create_mission(intent)

    # Step 1: approach (succeeds)
    orchestrator.step_mission(graph)
    # Step 2: clean (succeeds)
    orchestrator.step_mission(graph)

    # Step 3: turn valve -> simulate hydraulic torque limit exceeded / valve stuck
    valve_node_id = f"TI_JAMMED_FLOW_turn_valve"
    res3 = orchestrator.step_mission(
        graph,
        force_fail_node_id=valve_node_id,
        failure_reason="Hydraulic torque exceeded maximum: valve stuck with marine growth",
    )

    # Replanning should have spliced mitigation node `TI_JAMMED_FLOW_turn_valve_assist`
    assert len(res3["replanned_events"]) == 1
    assist_node_id = f"{valve_node_id}_assist"
    assert assist_node_id in graph.nodes
    assert graph.get_node(assist_node_id).status == TaskNodeStatus.READY

    # Next step: executes the mitigation assist node!
    res4 = orchestrator.step_mission(graph)
    assert assist_node_id in res4["dispatched_nodes"]
    assert graph.get_node(assist_node_id).status == TaskNodeStatus.COMPLETED

    # Next step: retries turning the valve after assist!
    res5 = orchestrator.step_mission(graph)
    assert valve_node_id in res5["dispatched_nodes"]
    assert graph.get_node(valve_node_id).status == TaskNodeStatus.COMPLETED

    # Final step: verify seal
    res6 = orchestrator.step_mission(graph)
    assert res6["is_completed"] is True
    assert graph.is_completed() is True


def test_api_dev_mission_endpoints():
    from web_backend import app
    client = app.test_client()

    decompose_payload = {
        "intent": {
            "intent_id": "TI_API_01",
            "task_type": "valve_operation",
            "water_depth": 300.0,
        }
    }
    resp = client.post("/api/dev/mission/decompose", json=decompose_payload)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "graph" in data
    assert len(data["execution_batches"]) == 4
    assert data["preflight"]["passed"] is True
    assert "mermaid" in data

    # Step mission endpoint
    step_payload = {
        "graph": data["graph"],
    }
    resp_step = client.post("/api/dev/mission/step", json=step_payload)
    assert resp_step.status_code == 200
    step_data = resp_step.get_json()
    assert step_data["ok"] is True
    assert "step_result" in step_data
    assert "summary" in step_data

    # Run to completion endpoint
    run_payload = {
        "graph": data["graph"],
        "use_acoustic_comm": True,
    }
    resp_run = client.post("/api/dev/mission/run", json=run_payload)
    assert resp_run.status_code == 200
    run_data = resp_run.get_json()
    assert run_data["ok"] is True
    assert run_data["run_result"]["is_completed"] is True
    assert run_data["run_result"]["state"] == MissionState.COMPLETED.value


def test_mission_run_to_completion_autonomous_success():
    gw = RobotGateway.get_instance()
    driver = SimulatedRobotDriver(unit_id="TEST-SIM-ROV")
    gw.register_driver(driver, is_default=True)

    orchestrator = MissionOrchestrator(gateway=gw)
    intent = {
        "intent_id": "TI_RUN_COMPLETION",
        "task_type": "pipeline_inspection",
        "pipe_id": "PIPE-999",
        "water_depth": 350.0,
        "assigned_robot_id": "TEST-SIM-ROV",
    }
    graph = orchestrator.create_mission(intent)
    assert graph.node_count == 4

    run_res = orchestrator.run_mission_to_completion(graph, max_steps=10)
    assert run_res["is_completed"] is True
    assert run_res["has_failures"] is False
    assert run_res["state"] == MissionState.COMPLETED.value
    assert run_res["steps_count"] == 4
    assert graph.is_completed() is True


def test_mission_run_to_completion_with_self_healing_recovery():
    gw = RobotGateway.get_instance()
    driver = SimulatedRobotDriver(unit_id="TEST-SIM-ROV")
    gw.register_driver(driver, is_default=True)

    orchestrator = MissionOrchestrator(gateway=gw)
    intent = {
        "intent_id": "TI_RUN_HEAL",
        "task_type": "valve_operation",
        "water_depth": 200.0,
        "assigned_robot_id": "TEST-SIM-ROV",
    }
    graph = orchestrator.create_mission(intent)
    valve_node_id = "TI_RUN_HEAL_turn_valve"

    # Inject failure on turn_valve
    run_res = orchestrator.run_mission_to_completion(
        graph,
        max_steps=10,
        force_fail_node_id=valve_node_id,
        failure_reason="Hydraulic torque exceeded maximum: valve stuck with marine growth",
    )

    # Autonomous loop should have triggered replanning, inserted assist node, retried, and completed!
    assert run_res["is_completed"] is True
    assert run_res["state"] == MissionState.COMPLETED.value
    summary = run_res["summary"]
    assert summary["replanning_count"] >= 1
    assert graph.is_completed() is True


def test_mission_execution_with_acoustic_weak_comm():
    from src.weak_comm.channel_simulator import AcousticChannelSimulator

    gw = RobotGateway.get_instance()
    driver = SimulatedRobotDriver(unit_id="TEST-SIM-ROV")
    gw.register_driver(driver, is_default=True)

    orchestrator = MissionOrchestrator(gateway=gw)
    intent = {
        "intent_id": "TI_ACOUSTIC_01",
        "task_type": "cable_burial",
        "water_depth": 120.0,
        "assigned_robot_id": "TEST-SIM-ROV",
    }
    graph = orchestrator.create_mission(intent)

    # Test successful acoustic run
    run_res = orchestrator.run_mission_to_completion(graph, use_acoustic_comm=True)
    assert run_res["is_completed"] is True
    summary = orchestrator.get_mission_summary(graph)
    assert len(summary.execution_history) > 0
    first_rec = summary.execution_history[0]
    assert "acoustic_comm" in first_rec.telemetry_snapshot
    assert first_rec.telemetry_snapshot["acoustic_comm"]["delivered"] is True

    # Test acoustic dropout packet loss triggers failure and replanning
    graph2 = orchestrator.create_mission({
        "intent_id": "TI_ACOUSTIC_DROP",
        "task_type": "valve_operation",
        "water_depth": 150.0,
        "assigned_robot_id": "TEST-SIM-ROV",
    })
    loss_sim = AcousticChannelSimulator(packet_loss_rate=1.0)
    step_res = orchestrator.step_mission(
        graph2,
        use_acoustic_comm=True,
        acoustic_channel=loss_sim,
    )
    assert step_res["has_failures"] is True
    assert any("水声弱网通信链路中断" in str(rec.error) for rec in orchestrator._execution_history[graph2.graph_id])
