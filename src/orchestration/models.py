"""Data models for Subsea Mission Orchestration and Pipeline Execution."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.task_graph.node import TaskNodeStatus


class MissionState(str, Enum):
    INITIALIZED = "INITIALIZED"
    PREFLIGHT_PASS = "PREFLIGHT_PASS"
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


@dataclass
class SubtaskExecutionRecord:
    node_id: str
    task_type: str
    robot_id: str | None
    status: TaskNodeStatus
    dispatched_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    retries: int = 0
    error: str | None = None
    telemetry_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "task_type": self.task_type,
            "robot_id": self.robot_id,
            "status": self.status.value,
            "dispatched_at": self.dispatched_at,
            "finished_at": self.finished_at,
            "retries": self.retries,
            "error": self.error,
            "telemetry_snapshot": self.telemetry_snapshot,
        }


@dataclass
class MissionSummary:
    mission_id: str
    state: MissionState
    intent_id: str | None = None
    total_subtasks: int = 0
    completed_subtasks: int = 0
    failed_subtasks: int = 0
    replanning_count: int = 0
    execution_history: list[SubtaskExecutionRecord] = field(default_factory=list)
    mermaid_flowchart: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "state": self.state.value,
            "intent_id": self.intent_id,
            "total_subtasks": self.total_subtasks,
            "completed_subtasks": self.completed_subtasks,
            "failed_subtasks": self.failed_subtasks,
            "replanning_count": self.replanning_count,
            "execution_history": [r.to_dict() for r in self.execution_history],
            "mermaid_flowchart": self.mermaid_flowchart,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
