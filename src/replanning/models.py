"""Data models for Execution-Feedback-Driven Dynamic Replanning."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.task_graph.node import TaskNode


class ReplanningTriggerType(str, Enum):
    SUBTASK_FAILED = "subtask_failed"
    ROBOT_HARDWARE_FAULT = "robot_hardware_fault"
    ENVIRONMENT_CURRENT_EXCEEDED = "environment_current_exceeded"
    GEOFENCE_PROXIMITY_WARNING = "geofence_proximity_warning"
    EXECUTION_TIMEOUT = "execution_timeout"


class ReplanningActionType(str, Enum):
    REALLOCATE_ROBOT = "reallocate_robot"
    INSERT_MITIGATION_NODE = "insert_mitigation_node"
    RETRY_NODE = "retry_node"
    SKIP_OPTIONAL_NODE = "skip_optional_node"
    SAFE_ABORT_DOWNSTREAM = "safe_abort_downstream"


@dataclass
class ReplanningTrigger:
    trigger_type: ReplanningTriggerType
    node_id: str | None = None
    unit_id: str | None = None
    reason: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_type": self.trigger_type.value,
            "node_id": self.node_id,
            "unit_id": self.unit_id,
            "reason": self.reason,
            "context": self.context,
        }


@dataclass
class ReplanningAction:
    action_type: ReplanningActionType
    target_node_id: str
    new_robot_id: str | None = None
    mitigation_node: TaskNode | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type.value,
            "target_node_id": self.target_node_id,
            "new_robot_id": self.new_robot_id,
            "mitigation_node": self.mitigation_node.to_dict() if self.mitigation_node else None,
            "reason": self.reason,
        }


@dataclass
class ReplanningPlan:
    plan_id: str
    trigger: ReplanningTrigger
    actions: list[ReplanningAction] = field(default_factory=list)
    is_recoverable: bool = True
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "trigger": self.trigger.to_dict(),
            "actions": [a.to_dict() for a in self.actions],
            "is_recoverable": self.is_recoverable,
            "summary": self.summary,
        }
