"""Task Node representation within the SEAgent Task Graph."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskNodeStatus(str, Enum):
    """Execution status of a task node in the task graph."""
    PENDING = "PENDING"          # Dependencies not yet satisfied
    READY = "READY"              # All dependencies satisfied, waiting for dispatch
    RUNNING = "RUNNING"          # Dispatched to robot / actively executing
    COMPLETED = "COMPLETED"      # Successfully executed
    FAILED = "FAILED"            # Execution failed
    BLOCKED = "BLOCKED"          # Downstream blocked due to upstream failure
    SKIPPED = "SKIPPED"          # Intentionally skipped/bypassed


@dataclass
class TaskNode:
    """Represents an atomic subtask node in a Directed Acyclic Graph (DAG)."""
    node_id: str
    task_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    status: TaskNodeStatus = TaskNodeStatus.PENDING
    assigned_robot_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "task_type": self.task_type,
            "parameters": self.parameters,
            "status": self.status.value,
            "assigned_robot_id": self.assigned_robot_id,
            "result": self.result,
            "error": self.error,
            "metadata": self.metadata,
        }

    @property
    def water_depth(self) -> float:
        val = self.parameters.get("water_depth", self.metadata.get("water_depth", 0.0))
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    @property
    def name(self) -> str:
        return str(self.metadata.get("name") or self.parameters.get("name") or self.node_id)

    @property
    def properties(self) -> dict[str, Any]:
        return self.metadata


    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskNode:
        status_val = data.get("status", TaskNodeStatus.PENDING.value)
        if isinstance(status_val, str):
            status = TaskNodeStatus(status_val)
        else:
            status = status_val
        meta = dict(data.get("metadata") or {})
        if "properties" in data and isinstance(data["properties"], dict):
            meta.update(data["properties"])
        if "name" in data and "name" not in meta:
            meta["name"] = data["name"]
        params = dict(data.get("parameters") or {})
        if "water_depth" in data and "water_depth" not in params:
            params["water_depth"] = data["water_depth"]
        return cls(
            node_id=data["node_id"],
            task_type=data["task_type"],
            parameters=params,
            status=status,
            assigned_robot_id=data.get("assigned_robot_id"),
            result=data.get("result"),
            error=data.get("error"),
            metadata=meta,
        )
