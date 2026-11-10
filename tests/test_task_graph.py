"""Unit tests for SEAgent Task Graph and Multi-Robot Allocator."""

import pytest
from src.task_graph import (
    TaskNode,
    TaskNodeStatus,
    TaskGraph,
    CyclicDependencyError,
    TaskGraphNotFoundError,
    TaskGraphAllocator,
)


def test_task_node_lifecycle_and_serialization():
    node = TaskNode(
        node_id="subtask_01",
        task_type="pipeline_inspection",
        parameters={"water_depth": 350.0, "pipe_id": "P-101"},
        status=TaskNodeStatus.PENDING,
    )
    assert node.node_id == "subtask_01"
    assert node.status == TaskNodeStatus.PENDING

    data = node.to_dict()
    assert data["node_id"] == "subtask_01"
    assert data["status"] == "PENDING"
    assert data["parameters"]["water_depth"] == 350.0

    restored = TaskNode.from_dict(data)
    assert restored.node_id == node.node_id
    assert restored.task_type == node.task_type
    assert restored.status == TaskNodeStatus.PENDING
    assert restored.parameters == node.parameters


def test_graph_node_and_edge_addition():
    graph = TaskGraph(graph_id="subsea_survey_01", description="Subsea inspection and burial pipeline")

    n1 = TaskNode("inspect_head", "pipeline_inspection", {"water_depth": 300.0})
    n2 = TaskNode("inspect_tail", "pipeline_inspection", {"water_depth": 400.0})
    n3 = TaskNode("bury_pipe", "pipeline_burial", {"water_depth": 350.0})

    graph.add_node(n1)
    graph.add_node(n2)
    graph.add_node(n3)

    assert graph.node_count == 3
    assert graph.edge_count == 0

    # n1 and n2 have no prerequisites, so they start READY
    assert graph.get_node("inspect_head").status == TaskNodeStatus.READY
    assert graph.get_node("inspect_tail").status == TaskNodeStatus.READY

    # Burying requires inspecting both head and tail
    graph.add_dependency("inspect_head", "bury_pipe")
    graph.add_dependency("inspect_tail", "bury_pipe")

    assert graph.edge_count == 2
    # bury_pipe now has prerequisites, so it is PENDING
    assert graph.get_node("bury_pipe").status == TaskNodeStatus.PENDING
    assert set(graph.get_prerequisites("bury_pipe")) == {"inspect_head", "inspect_tail"}
    assert graph.get_dependents("inspect_head") == ["bury_pipe"]


def test_cyclic_dependency_rejected():
    graph = TaskGraph("cycle_test")
    n1 = TaskNode("node_A", "pipeline_inspection")
    n2 = TaskNode("node_B", "pipeline_inspection")
    n3 = TaskNode("node_C", "pipeline_burial")

    graph.add_node(n1)
    graph.add_node(n2)
    graph.add_node(n3)

    # Self-dependency
    with pytest.raises(CyclicDependencyError):
        graph.add_dependency("node_A", "node_A")

    graph.add_dependency("node_A", "node_B")
    graph.add_dependency("node_B", "node_C")

    # Closing the loop (C -> A) must raise CyclicDependencyError
    with pytest.raises(CyclicDependencyError, match="create a cycle"):
        graph.add_dependency("node_C", "node_A")

    # Edge count should remain 2
    assert graph.edge_count == 2


def test_missing_node_raises_error():
    graph = TaskGraph("missing_test")
    n1 = TaskNode("node_A", "pipeline_inspection")
    graph.add_node(n1)

    with pytest.raises(TaskGraphNotFoundError):
        graph.get_node("non_existent")

    with pytest.raises(TaskGraphNotFoundError):
        graph.add_dependency("node_A", "non_existent")


def test_topological_execution_batches():
    # Diamond graph:
    #      start
    #     /     \
    #    branchA branchB
    #     \     /
    #       end
    graph = TaskGraph("diamond_graph")
    graph.add_node(TaskNode("start", "pipeline_inspection"))
    graph.add_node(TaskNode("branchA", "pipeline_inspection"))
    graph.add_node(TaskNode("branchB", "pipeline_inspection"))
    graph.add_node(TaskNode("end", "pipeline_burial"))

    graph.add_dependency("start", "branchA")
    graph.add_dependency("start", "branchB")
    graph.add_dependency("branchA", "end")
    graph.add_dependency("branchB", "end")

    batches = graph.get_execution_batches()
    assert len(batches) == 3
    assert batches[0] == ["start"]
    assert set(batches[1]) == {"branchA", "branchB"}
    assert batches[2] == ["end"]


def test_execution_readiness_progression():
    graph = TaskGraph("execution_flow")
    graph.add_node(TaskNode("step1", "pipeline_inspection"))
    graph.add_node(TaskNode("step2", "pipeline_burial"))
    graph.add_dependency("step1", "step2")

    # Initially step1 is READY, step2 is PENDING
    ready_initial = graph.get_ready_nodes()
    assert [n.node_id for n in ready_initial] == ["step1"]
    assert graph.get_node("step2").status == TaskNodeStatus.PENDING

    # step1 starts running
    graph.update_node_status("step1", TaskNodeStatus.RUNNING)
    assert graph.get_ready_nodes() == []

    # step1 completes
    graph.update_node_status("step1", TaskNodeStatus.COMPLETED, result={"coverage_pct": 100})
    assert graph.get_node("step1").status == TaskNodeStatus.COMPLETED

    # step2 now becomes READY
    ready_next = graph.get_ready_nodes()
    assert [n.node_id for n in ready_next] == ["step2"]
    assert graph.get_node("step2").status == TaskNodeStatus.READY

    # Complete step2
    graph.update_node_status("step2", TaskNodeStatus.COMPLETED)
    assert graph.is_completed() is True
    assert graph.has_failures() is False


def test_failure_propagation_blocks_downstream():
    graph = TaskGraph("failure_graph")
    graph.add_node(TaskNode("survey", "pipeline_inspection"))
    graph.add_node(TaskNode("burial", "pipeline_burial"))
    graph.add_node(TaskNode("valve_turn", "tree_valve_operation"))
    graph.add_dependency("survey", "burial")
    graph.add_dependency("burial", "valve_turn")

    # Survey fails
    graph.update_node_status("survey", TaskNodeStatus.FAILED, error="Turbidity exceeded threshold")

    assert graph.get_node("survey").status == TaskNodeStatus.FAILED
    assert graph.get_node("burial").status == TaskNodeStatus.BLOCKED
    assert "Blocked by upstream failure" in graph.get_node("burial").error
    assert graph.get_node("valve_turn").status == TaskNodeStatus.BLOCKED

    assert graph.has_failures() is True
    assert graph.is_completed() is False


def test_symmetric_serialization():
    graph = TaskGraph("ser_test", description="Test serialization")
    graph.add_node(TaskNode("task_A", "pipeline_inspection", {"water_depth": 500.0}))
    graph.add_node(TaskNode("task_B", "tree_valve_operation", {"valve_id": "V-02"}))
    graph.add_dependency("task_A", "task_B")

    data = graph.to_dict()
    assert data["graph_id"] == "ser_test"
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1
    assert data["edges"][0] == {"from": "task_A", "to": "task_B"}

    restored = TaskGraph.from_dict(data)
    assert restored.graph_id == "ser_test"
    assert restored.node_count == 2
    assert restored.edge_count == 1
    assert restored.get_dependents("task_A") == ["task_B"]
    assert restored.get_node("task_A").parameters["water_depth"] == 500.0


def test_task_graph_allocator():
    robots = [
        {
            "unit_id": "OBSROV-75-001",
            "family": "observation_rov",
            "max_depth_m": 1000,
            "capabilities": ["visual_inspection"],
        },
        {
            "unit_id": "WROV-150-001",
            "family": "heavy_work_class_rov",
            "max_depth_m": 3000,
            "capabilities": ["cable_burial", "manipulator_operation"],
        },
    ]
    allocator = TaskGraphAllocator(robots)

    graph = TaskGraph("alloc_test")
    graph.add_node(TaskNode("node_survey", "pipeline_inspection", {"water_depth": 400.0}))
    graph.add_node(TaskNode("node_deep_burial", "pipeline_burial", {"water_depth": 1500.0}))
    # Independent nodes in parallel
    assignments = allocator.allocate_ready_nodes(graph)

    assert assignments.get("node_survey") == "OBSROV-75-001"
    # OBSROV cannot do deep burial (depth 1500 > 1000 and missing burial capability)
    # WROV should be assigned to node_deep_burial
    assert assignments.get("node_deep_burial") == "WROV-150-001"


def test_api_dev_validate_task_graph():
    from web_backend import app
    client = app.test_client()

    valid_payload = {
        "graph_id": "api_test_dag",
        "description": "Subsea inspection then burial",
        "nodes": [
            {"node_id": "step1", "task_type": "pipeline_inspection", "parameters": {"water_depth": 300.0}},
            {"node_id": "step2", "task_type": "pipeline_burial", "parameters": {"water_depth": 300.0}},
        ],
        "edges": [{"from": "step1", "to": "step2"}],
    }

    resp = client.post("/api/dev/task-graph/validate", json=valid_payload)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["node_count"] == 2
    assert data["edge_count"] == 1
    assert data["execution_batches"] == [["step1"], ["step2"]]
    assert len(data["ready_nodes"]) == 1
    assert data["ready_nodes"][0]["node_id"] == "step1"


def test_api_dev_validate_task_graph_cycle_rejected():
    from web_backend import app
    client = app.test_client()

    cycle_payload = {
        "graph_id": "cyclic_dag",
        "nodes": [
            {"node_id": "A", "task_type": "pipeline_inspection"},
            {"node_id": "B", "task_type": "pipeline_burial"},
        ],
        "edges": [
            {"from": "A", "to": "B"},
            {"from": "B", "to": "A"},
        ],
    }

    resp = client.post("/api/dev/task-graph/validate", json=cycle_payload)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error_type"] == "CyclicDependencyError"

