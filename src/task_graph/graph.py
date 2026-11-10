"""Directed Acyclic Graph (DAG) for subsea robot task decomposition and execution tracking."""
from __future__ import annotations

import copy
from typing import Any
import networkx as nx

from .node import TaskNode, TaskNodeStatus


class CyclicDependencyError(ValueError):
    """Raised when adding a dependency introduces a cycle in the task graph."""
    pass


class TaskGraphNotFoundError(KeyError):
    """Raised when a referenced task node does not exist in the graph."""
    pass


class TaskGraph:
    """Task Graph model backed by networkx.DiGraph for dependency management,
    topological stage computation, and parallel dispatch tracking."""

    def __init__(self, graph_id: str = "default_graph", description: str = ""):
        self.graph_id = graph_id
        self.description = description
        self._graph = nx.DiGraph()
        self._nodes: dict[str, TaskNode] = {}

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return self._graph.number_of_edges()

    def add_node(self, node: TaskNode) -> None:
        """Add a subtask node to the graph."""
        if node.node_id in self._nodes:
            raise ValueError(f"Task node '{node.node_id}' already exists in graph.")
        self._nodes[node.node_id] = copy.deepcopy(node)
        self._graph.add_node(node.node_id)
        # If node has no prerequisites, it starts in READY status if PENDING
        if self._graph.in_degree(node.node_id) == 0 and self._nodes[node.node_id].status == TaskNodeStatus.PENDING:
            self._nodes[node.node_id].status = TaskNodeStatus.READY

    def get_node(self, node_id: str) -> TaskNode:
        """Retrieve a task node by its id."""
        if node_id not in self._nodes:
            raise TaskGraphNotFoundError(f"Task node '{node_id}' not found.")
        return self._nodes[node_id]

    def add_dependency(self, prerequisite_id: str, dependent_id: str) -> None:
        """Add a directed edge: prerequisite_id -> dependent_id.
        dependent_id cannot start until prerequisite_id has COMPLETED."""
        if prerequisite_id not in self._nodes:
            raise TaskGraphNotFoundError(f"Prerequisite node '{prerequisite_id}' not found.")
        if dependent_id not in self._nodes:
            raise TaskGraphNotFoundError(f"Dependent node '{dependent_id}' not found.")
        if prerequisite_id == dependent_id:
            raise CyclicDependencyError(f"Self-dependency is invalid for '{dependent_id}'.")

        # Temporarily add edge to test for acyclicity
        self._graph.add_edge(prerequisite_id, dependent_id)
        if not nx.is_directed_acyclic_graph(self._graph):
            self._graph.remove_edge(prerequisite_id, dependent_id)
            raise CyclicDependencyError(
                f"Adding dependency {prerequisite_id} -> {dependent_id} would create a cycle."
            )

        # Re-evaluate dependent node readiness
        if self._nodes[dependent_id].status == TaskNodeStatus.READY:
            if not self._are_prerequisites_completed(dependent_id):
                self._nodes[dependent_id].status = TaskNodeStatus.PENDING

    def get_prerequisites(self, node_id: str) -> list[str]:
        """Get all immediate predecessor node IDs."""
        if node_id not in self._nodes:
            raise TaskGraphNotFoundError(f"Task node '{node_id}' not found.")
        return list(self._graph.predecessors(node_id))

    def get_dependents(self, node_id: str) -> list[str]:
        """Get all immediate successor node IDs."""
        if node_id not in self._nodes:
            raise TaskGraphNotFoundError(f"Task node '{node_id}' not found.")
        return list(self._graph.successors(node_id))

    def _are_prerequisites_completed(self, node_id: str) -> bool:
        preds = self.get_prerequisites(node_id)
        return all(self._nodes[p].status == TaskNodeStatus.COMPLETED for p in preds)

    def get_ready_nodes(self) -> list[TaskNode]:
        """Return all nodes that are currently ready for dispatch."""
        ready_nodes = []
        for node_id, node in self._nodes.items():
            if node.status == TaskNodeStatus.READY:
                ready_nodes.append(node)
            elif node.status == TaskNodeStatus.PENDING and self._are_prerequisites_completed(node_id):
                node.status = TaskNodeStatus.READY
                ready_nodes.append(node)
        return ready_nodes

    def get_execution_batches(self) -> list[list[str]]:
        """Return topological generations (parallel execution stages).
        Nodes in generation N can run simultaneously once generation N-1 completes."""
        if not nx.is_directed_acyclic_graph(self._graph):
            raise CyclicDependencyError("Graph has cyclic dependencies, cannot compute execution stages.")
        return [list(gen) for gen in nx.topological_generations(self._graph)]

    def update_node_status(
        self,
        node_id: str,
        status: TaskNodeStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Update node status and propagate readiness or blockage downstream."""
        node = self.get_node(node_id)
        node.status = status
        if result is not None:
            node.result = result
        if error is not None:
            node.error = error

        if status == TaskNodeStatus.COMPLETED:
            # Check successors and promote to READY if all their prerequisites are met
            for succ_id in self.get_dependents(node_id):
                succ = self._nodes[succ_id]
                if succ.status == TaskNodeStatus.PENDING and self._are_prerequisites_completed(succ_id):
                    succ.status = TaskNodeStatus.READY
        elif status == TaskNodeStatus.FAILED:
            # Propagate BLOCKED to all downstream reachable nodes
            downstream = nx.descendants(self._graph, node_id)
            for d_id in downstream:
                d_node = self._nodes[d_id]
                if d_node.status in (TaskNodeStatus.PENDING, TaskNodeStatus.READY):
                    d_node.status = TaskNodeStatus.BLOCKED
                    d_node.error = f"Blocked by upstream failure in '{node_id}'"

    def is_completed(self) -> bool:
        """Check if all nodes in graph are COMPLETED or SKIPPED."""
        if not self._nodes:
            return False
        return all(n.status in (TaskNodeStatus.COMPLETED, TaskNodeStatus.SKIPPED) for n in self._nodes.values())

    def has_failures(self) -> bool:
        """Check if any node in graph is FAILED or BLOCKED."""
        return any(n.status in (TaskNodeStatus.FAILED, TaskNodeStatus.BLOCKED) for n in self._nodes.values())

    def to_dict(self) -> dict[str, Any]:
        """Serialize the TaskGraph to a JSON-serializable dictionary."""
        return {
            "graph_id": self.graph_id,
            "description": self.description,
            "nodes": [node.to_dict() for node in self._nodes.values()],
            "edges": [{"from": u, "to": v} for u, v in self._graph.edges()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskGraph:
        """Deserialize a TaskGraph from a dictionary with strict validation."""
        graph = cls(
            graph_id=data.get("graph_id", "default_graph"),
            description=data.get("description", ""),
        )
        for n_data in data.get("nodes", []):
            graph.add_node(TaskNode.from_dict(n_data))
        for edge in data.get("edges", []):
            graph.add_dependency(edge["from"], edge["to"])
        return graph
