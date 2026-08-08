"""src/session_state.py

SEAgent G3.1 State Contract Definition (Hardened).
Establishes explicit, deeply immutable, serializable, and fail-closed data contracts
for Conversation State, Task Lifecycle State, and Execution Control State.
Does NOT alter runtime behavior or DialogueManager state transition logic.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any


class StateContractError(ValueError):
    """Raised when session state validation or conversion fails."""
    pass


# Supported schema version for SessionState
SUPPORTED_SESSION_STATE_SCHEMA_VERSION: int = 2


# Valid value sets based on current runtime code
VALID_DIALOGUE_MODES: frozenset[str] = frozenset({
    "task_collection",
    "knowledge_qa",
    "emergency_intervention",
})

# Note: In mode_transition_history transitions, legacy snapshots might contain "uncertain" mode before mapping
VALID_TRANSITION_MODES: frozenset[str] = frozenset({
    "task_collection",
    "knowledge_qa",
    "emergency_intervention",
    "uncertain",
})

VALID_PHASES: frozenset[str] = frozenset({
    "collecting",
    "blocked_hard",
    "blocked_soft",
    "confirming",
    "done",
    "rejected",
})

VALID_TASK_MODES: frozenset[str] = frozenset({
    "normal",
    "emergency",
})

VALID_CONTROL_STATES: frozenset[str] = frozenset({
    "idle",
    "stop_requested",
    "pause_requested",
    "abort_requested",
    "cancel_requested",
})

VALID_CONTROL_ACTIONS: frozenset[str] = frozenset({
    "stop",
    "pause",
    "abort",
    "cancel",
})


def _validate_mode_transition(transition: Any) -> MappingProxyType[str, Any]:
    """Validate a single mode transition dict and return a read-only MappingProxyType."""
    if not isinstance(transition, (dict, MappingProxyType)):
        raise StateContractError(f"Transition item must be a dictionary or MappingProxyType, got {type(transition).__name__}")

    from_m = transition.get("from")
    to_m = transition.get("to")
    if not isinstance(from_m, str) or type(from_m) is not str or from_m not in VALID_TRANSITION_MODES:
        raise StateContractError(f"Invalid 'from' mode in transition: {from_m!r}")
    if not isinstance(to_m, str) or type(to_m) is not str or to_m not in VALID_TRANSITION_MODES:
        raise StateContractError(f"Invalid 'to' mode in transition: {to_m!r}")

    conf = transition.get("confidence", 1.0)
    if type(conf) is bool:
        raise StateContractError("confidence cannot be boolean")
    if not isinstance(conf, (int, float)) or not math.isfinite(float(conf)) or not (0.0 <= float(conf) <= 1.0):
        raise StateContractError(f"Invalid confidence in transition: {conf!r}")

    changed_at = transition.get("changed_at")
    if not isinstance(changed_at, str) or type(changed_at) is not str or not changed_at.strip():
        raise StateContractError(f"Invalid changed_at in transition: {changed_at!r}")

    try:
        parsed = datetime.fromisoformat(changed_at)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise StateContractError(f"changed_at must be timezone-aware ISO timestamp: {changed_at!r}")
    except StateContractError:
        raise
    except Exception as exc:
        raise StateContractError(f"Invalid ISO timestamp format for changed_at: {changed_at!r} ({exc})") from exc

    plain_copy = dict(copy.deepcopy(dict(transition)))
    return MappingProxyType(plain_copy)


@dataclass(frozen=True)
class ConversationState:
    dialogue_mode: str
    last_mode_transition: MappingProxyType[str, Any] | None = None
    mode_transition_history: tuple[MappingProxyType[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if type(self.dialogue_mode) is not str or not self.dialogue_mode:
            raise StateContractError(f"dialogue_mode must be non-empty string, got {self.dialogue_mode!r}")
        if self.dialogue_mode not in VALID_DIALOGUE_MODES:
            raise StateContractError(f"Invalid dialogue_mode: {self.dialogue_mode!r}")

        if self.last_mode_transition is not None:
            validated_last = _validate_mode_transition(self.last_mode_transition)
            object.__setattr__(self, "last_mode_transition", validated_last)

        if not isinstance(self.mode_transition_history, (tuple, list)):
            raise StateContractError(f"mode_transition_history must be tuple or list, got {type(self.mode_transition_history).__name__}")

        validated_history = tuple(_validate_mode_transition(item) for item in self.mode_transition_history)
        object.__setattr__(self, "mode_transition_history", validated_history)


@dataclass(frozen=True)
class TaskLifecycleState:
    phase: str
    mode: str
    awaiting_final_confirm: bool

    def __post_init__(self) -> None:
        if type(self.phase) is not str or not self.phase:
            raise StateContractError(f"phase must be non-empty string, got {self.phase!r}")
        if self.phase not in VALID_PHASES:
            raise StateContractError(f"Invalid phase: {self.phase!r}")

        if type(self.mode) is not str or not self.mode:
            raise StateContractError(f"mode must be non-empty string, got {self.mode!r}")
        if self.mode not in VALID_TASK_MODES:
            raise StateContractError(f"Invalid task mode: {self.mode!r}")

        if type(self.awaiting_final_confirm) is not bool:
            raise StateContractError(f"awaiting_final_confirm must be strictly boolean, got {type(self.awaiting_final_confirm).__name__}: {self.awaiting_final_confirm!r}")


@dataclass(frozen=True)
class ExecutionControlState:
    control_state: str
    last_control_request: MappingProxyType[str, Any] | None = None

    def __post_init__(self) -> None:
        if type(self.control_state) is not str or not self.control_state:
            raise StateContractError(f"control_state must be non-empty string, got {self.control_state!r}")
        if self.control_state not in VALID_CONTROL_STATES:
            raise StateContractError(f"Invalid control_state: {self.control_state!r}")

        if self.last_control_request is not None:
            if not isinstance(self.last_control_request, (dict, MappingProxyType)):
                raise StateContractError(f"last_control_request must be dict, MappingProxyType or None, got {type(self.last_control_request).__name__}")
            act = self.last_control_request.get("action")
            if not isinstance(act, str) or type(act) is not str or act not in VALID_CONTROL_ACTIONS:
                raise StateContractError(f"Invalid action in last_control_request: {act!r}")
            st = self.last_control_request.get("status")
            if not isinstance(st, str) or type(st) is not str or st != "requested":
                raise StateContractError(f"Invalid status in last_control_request: {st!r}")
            validated_req = MappingProxyType(dict(copy.deepcopy(dict(self.last_control_request))))
            object.__setattr__(self, "last_control_request", validated_req)

        # Cross-field consistency validation between control_state and last_control_request
        if self.last_control_request is None:
            if self.control_state != "idle":
                raise StateContractError(f"control_state must be 'idle' when last_control_request is None, got {self.control_state!r}")
        else:
            act = self.last_control_request["action"]
            expected_state = f"{act}_requested"
            if self.control_state != expected_state:
                raise StateContractError(
                    f"Mismatched control_state {self.control_state!r} for action {act!r} (expected {expected_state!r})"
                )


@dataclass(frozen=True)
class SessionState:
    schema_version: int
    conversation: ConversationState
    task: TaskLifecycleState
    execution: ExecutionControlState

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise StateContractError(f"schema_version must be int, got {type(self.schema_version).__name__}: {self.schema_version!r}")
        if self.schema_version != SUPPORTED_SESSION_STATE_SCHEMA_VERSION:
            raise StateContractError(f"Unsupported schema_version: {self.schema_version} (supported version: {SUPPORTED_SESSION_STATE_SCHEMA_VERSION})")

        if not isinstance(self.conversation, ConversationState):
            raise StateContractError(f"conversation must be ConversationState instance, got {type(self.conversation).__name__}")
        if not isinstance(self.task, TaskLifecycleState):
            raise StateContractError(f"task must be TaskLifecycleState instance, got {type(self.task).__name__}")
        if not isinstance(self.execution, ExecutionControlState):
            raise StateContractError(f"execution must be ExecutionControlState instance, got {type(self.execution).__name__}")


def session_state_from_legacy_snapshot(snapshot: dict[str, Any]) -> SessionState:
    """Convert a legacy snapshot dictionary into a validated SessionState object. Fail closed on invalid schema_version."""
    if not isinstance(snapshot, dict):
        raise StateContractError(f"Legacy snapshot must be a dictionary, got {type(snapshot).__name__}")

    try:
        if "snapshot_version" in snapshot:
            ver = snapshot["snapshot_version"]
            if type(ver) is not int:
                raise StateContractError(f"Invalid snapshot_version type: {type(ver).__name__} (expected int)")
            if ver != SUPPORTED_SESSION_STATE_SCHEMA_VERSION:
                raise StateContractError(f"Unsupported snapshot_version: {ver} (expected {SUPPORTED_SESSION_STATE_SCHEMA_VERSION})")
            schema_version = ver
        else:
            schema_version = SUPPORTED_SESSION_STATE_SCHEMA_VERSION

        raw_dm = snapshot.get("dialogue_mode", "task_collection")
        if raw_dm == "uncertain":
            raw_dm = "knowledge_qa"

        conversation = ConversationState(
            dialogue_mode=raw_dm,
            last_mode_transition=snapshot.get("last_mode_transition"),
            mode_transition_history=snapshot.get("mode_transition_history", []),
        )

        task = TaskLifecycleState(
            phase=snapshot.get("phase", "collecting"),
            mode=snapshot.get("mode", "normal"),
            awaiting_final_confirm=snapshot.get("awaiting_final_confirm", False),
        )

        execution = ExecutionControlState(
            control_state=snapshot.get("control_state", "idle"),
            last_control_request=snapshot.get("last_control_request"),
        )

        return SessionState(
            schema_version=schema_version,
            conversation=conversation,
            task=task,
            execution=execution,
        )
    except StateContractError:
        raise
    except Exception as exc:
        raise StateContractError(f"Failed to adapt legacy snapshot to SessionState: {exc}") from exc


def session_state_to_legacy_fields(state: SessionState) -> dict[str, Any]:
    """Convert a SessionState object into a dictionary of legacy snapshot state fields. Outputs plain dict/list."""
    if not isinstance(state, SessionState):
        raise StateContractError(f"state must be SessionState instance, got {type(state).__name__}")

    last_trans = dict(state.conversation.last_mode_transition) if state.conversation.last_mode_transition is not None else None
    trans_hist = [dict(t) for t in state.conversation.mode_transition_history]
    last_ctrl = dict(state.execution.last_control_request) if state.execution.last_control_request is not None else None

    return {
        "snapshot_version": state.schema_version,
        "phase": state.task.phase,
        "mode": state.task.mode,
        "awaiting_final_confirm": state.task.awaiting_final_confirm,
        "dialogue_mode": state.conversation.dialogue_mode,
        "last_mode_transition": last_trans,
        "mode_transition_history": trans_hist,
        "control_state": state.execution.control_state,
        "last_control_request": last_ctrl,
    }
