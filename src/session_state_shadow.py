"""src/session_state_shadow.py

SEAgent G4.2 SessionState V2 Runtime Shadow Instrumentation Module.

Provides pure, read-only, non-interfering compatibility observation comparing:
  Legacy Runtime State Snapshot
  vs
  SessionState V2 Adapted Projection

Classifications:
  - PARITY: Legacy current state is valid and matches SessionState V2 core projection.
  - STRICT_REJECTED: Legacy state snapshot fails SessionState V2 Contract validation.
  - MISMATCH: Both models construct successfully, but core projection fields differ.

Zero side-effects, zero state mutation, zero double-execution of business logic.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal

from .id_sequence import validate_intent_id, validate_task_id, validate_uuid4
from .session_state import (
    StateContractError,
    session_state_from_legacy_snapshot,
    session_state_to_legacy_fields,
)

SESSION_STATE_MANAGED_KEYS: tuple[str, ...] = (
    "phase",
    "mode",
    "awaiting_final_confirm",
    "dialogue_mode",
    "last_mode_transition",
    "mode_transition_history",
    "control_state",
    "last_control_request",
)


@dataclass(frozen=True)
class ShadowComparisonResult:
    classification: Literal["PARITY", "STRICT_REJECTED", "MISMATCH"]
    checkpoint: str
    request_id: str | None
    exception_type: str | None
    exception_message: str | None
    diff_fields: tuple[str, ...]
    details: str


def _canonicalize_legacy_enrichment(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Apply safe legacy done enrichment canonicalization to Legacy raw snapshot projection.

    When phase is 'done' and control_state is non-idle, if legacy snapshot retains
    last_control_request without target_intent_id, SessionState V2 safely populates
    target_intent_id from task_state. This is a known safe contract enrichment.
    """
    res = {k: copy.deepcopy(snapshot.get(k)) for k in SESSION_STATE_MANAGED_KEYS}

    if res.get("phase") is None:
        res["phase"] = "collecting"
    if res.get("mode") is None:
        res["mode"] = "normal"
    if res.get("awaiting_final_confirm") is None:
        res["awaiting_final_confirm"] = False
    if res.get("dialogue_mode") is None or res.get("dialogue_mode") == "uncertain":
        raw_dm = snapshot.get("dialogue_mode", "task_collection")
        res["dialogue_mode"] = "knowledge_qa" if raw_dm == "uncertain" else raw_dm
    if res.get("mode_transition_history") is None:
        res["mode_transition_history"] = []
    if res.get("control_state") is None:
        res["control_state"] = "idle"

    phase = res.get("phase")
    ctrl_st = res.get("control_state")
    last_req = res.get("last_control_request")
    task_st = snapshot.get("task_state") or {}

    if phase == "done" and ctrl_st != "idle" and isinstance(last_req, dict) and not last_req.get("target_intent_id"):
        if isinstance(task_st, dict):
            cand_id = task_st.get("intent_id")
            if cand_id and validate_intent_id(cand_id):
                last_req["target_intent_id"] = cand_id
            cand_tid = task_st.get("task_id")
            if cand_tid and validate_task_id(cand_tid) and "target_task_id" not in last_req:
                last_req["target_task_id"] = cand_tid
            cand_iid = task_st.get("internal_id")
            if cand_iid and validate_uuid4(cand_iid) and "target_internal_id" not in last_req:
                last_req["target_internal_id"] = cand_iid

    return res


def compare_session_state_shadow(
    snapshot: dict[str, Any],
    checkpoint: str = "unknown",
    request_id: str | None = None,
) -> ShadowComparisonResult:
    """Perform a pure, read-only shadow comparison on a legacy state snapshot dictionary."""
    if not isinstance(snapshot, dict):
        return ShadowComparisonResult(
            classification="STRICT_REJECTED",
            checkpoint=checkpoint,
            request_id=request_id,
            exception_type="TypeError",
            exception_message=f"Snapshot must be dict, got {type(snapshot).__name__}",
            diff_fields=(),
            details=f"Invalid snapshot type: {type(snapshot).__name__}",
        )

    # 1. Attempt adaptation via session_state_from_legacy_snapshot
    try:
        session_state = session_state_from_legacy_snapshot(snapshot)
    except Exception as exc:
        return ShadowComparisonResult(
            classification="STRICT_REJECTED",
            checkpoint=checkpoint,
            request_id=request_id,
            exception_type=type(exc).__name__,
            exception_message=type(exc).__name__,
            diff_fields=(),
            details="Snapshot rejected by SessionState V2 Contract",
        )

    # 2. Extract V2 fields and canonicalized Legacy projection
    v2_fields = session_state_to_legacy_fields(session_state)
    v2_projection = {k: v2_fields.get(k) for k in SESSION_STATE_MANAGED_KEYS}
    legacy_projection = _canonicalize_legacy_enrichment(snapshot)

    # 3. Compare core fields
    diffs = []
    for key in SESSION_STATE_MANAGED_KEYS:
        leg_val = legacy_projection.get(key)
        v2_val = v2_projection.get(key)
        if leg_val != v2_val:
            diffs.append(key)

    if not diffs:
        return ShadowComparisonResult(
            classification="PARITY",
            checkpoint=checkpoint,
            request_id=request_id,
            exception_type=None,
            exception_message=None,
            diff_fields=(),
            details="Legacy state matches SessionState V2 projection",
        )

    diff_tuple = tuple(diffs)
    return ShadowComparisonResult(
        classification="MISMATCH",
        checkpoint=checkpoint,
        request_id=request_id,
        exception_type=None,
        exception_message=None,
        diff_fields=diff_tuple,
        details=f"Field projection mismatch on fields: {diff_tuple}",
    )
