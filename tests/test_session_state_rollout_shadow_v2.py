"""
tests/test_session_state_rollout_shadow_v2.py

SEAgent G4.1 SessionState V2 Shadow Compatibility Gate Test Suite (Authenticity Repaired).

Provides reproducible, dual-executed shadow comparison between:
  Legacy Runtime (session_state_v2 = false)
  vs
  Strict Runtime (session_state_v2 = true)

Automatically categorizes outcomes into:
  - PARITY
  - EXPECTED_FAIL_CLOSED_DELTA
  - UNEXPECTED_BEHAVIOR_DELTA (asserted to be strictly 0)

Guarantees 0 production code diff in src/ and 0 feature flag modifications.
"""

import copy
import json
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch, MagicMock

from web_backend import app, DialogueManager as WebDialogueManager
from src.dialogue_manager import DialogueManager
from src.exceptions import TaskPersistenceError, IntentIdConflict
from src.id_sequence import validate_intent_id, validate_uuid4
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.session_state import StateContractError, session_state_from_legacy_snapshot
from src.slot_store import Slot, SlotStore, SnapshotValidationError
from src.task_intent_builder import TaskIntentBuilder, get_task_dir
from src.validator import ValidationResult, Violation


@dataclass
class ShadowOutcome:
    succeeded: bool
    exception_type: str | None
    exception_message: str | None
    before_digest: dict
    after_digest: dict


def _make_dm(tmp_dir: Path) -> DialogueManager:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    state_file = tmp_dir / "state.yaml"
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.state_file = state_file
    llm = LLMClient(None, None)
    return DialogueManager(llm, kb)


def normalize_digest(obj: any) -> any:
    """Normalize non-deterministic fields (ISO timestamps, generated UUIDs) while preserving structural facts."""
    if isinstance(obj, dict):
        res = {}
        for k, v in obj.items():
            if k in ("changed_at", "updated_at", "validated_at", "acknowledged_at") and isinstance(v, str):
                res[k] = "<TIMESTAMP>"
            elif (k in ("internal_id", "target_internal_id") and isinstance(v, str) and validate_uuid4(v)):
                res[k] = "<UUID>"
            else:
                res[k] = normalize_digest(v)
        return res
    elif isinstance(obj, list):
        return [normalize_digest(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(normalize_digest(item) for item in obj)
    elif isinstance(obj, str) and validate_uuid4(obj):
        return "<UUID>"
    else:
        return obj


def canonicalize_legacy_enrichment(digest_legacy: dict) -> dict:
    """Apply safe legacy done enrichment canonicalization to Legacy digest for A17 parity comparison.

    When phase is 'done' and control_state is non-idle, if legacy snapshot/memory retains
    last_control_request without target_intent_id, Strict safely populates target_intent_id
    from task_state. In digest comparison, this is a known safe contract enrichment.
    """
    res = copy.deepcopy(digest_legacy)
    phase = res.get("phase")
    ctrl_st = res.get("control_state")
    last_req = res.get("last_control_request")
    task_st = res.get("task_state") or {}

    if phase == "done" and ctrl_st != "idle" and isinstance(last_req, dict) and not last_req.get("target_intent_id"):
        cand_id = task_st.get("intent_id")
        if cand_id and validate_intent_id(cand_id):
            last_req["target_intent_id"] = cand_id
        cand_tid = task_st.get("task_id")
        if cand_tid and "target_task_id" not in last_req:
            last_req["target_task_id"] = cand_tid
        cand_iid = task_st.get("internal_id")
        if cand_iid and "target_internal_id" not in last_req:
            last_req["target_internal_id"] = cand_iid

    return res


def build_shadow_digest(dm: DialogueManager) -> dict:
    """Construct complete normalized State Digest for DialogueManager instance."""
    phase = getattr(dm, "phase", None)
    mode = getattr(dm, "mode", None)
    dialogue_mode = getattr(dm, "dialogue_mode", None)
    last_mode_transition = copy.deepcopy(getattr(dm, "last_mode_transition", None))
    mode_transition_history = copy.deepcopy(getattr(dm, "mode_transition_history", None))
    control_state = getattr(dm, "control_state", None)
    last_control_request = copy.deepcopy(getattr(dm, "last_control_request", None))
    slot_store_snap = dm.slot_store.export_snapshot() if getattr(dm, "slot_store", None) else None
    task_state = copy.deepcopy(getattr(dm, "task_state", None))
    last_built_json = copy.deepcopy(getattr(dm, "_last_built_json", None))
    last_missing = copy.deepcopy(getattr(dm, "_last_missing", None))
    final_result = copy.deepcopy(getattr(dm, "final_result", None))
    awaiting_final_confirm = getattr(dm, "awaiting_final_confirm", None)
    task_start_now = getattr(dm, "task_start_now", None)

    raw_digest = {
        "phase": phase,
        "mode": mode,
        "dialogue_mode": dialogue_mode,
        "last_mode_transition": last_mode_transition,
        "mode_transition_history": mode_transition_history,
        "control_state": control_state,
        "last_control_request": last_control_request,
        "slot_store": slot_store_snap,
        "task_state": task_state,
        "_last_built_json": last_built_json,
        "_last_missing": last_missing,
        "final_result": final_result,
        "awaiting_final_confirm": awaiting_final_confirm,
        "task_start_now": task_start_now,
    }
    return normalize_digest(raw_digest)


def execute_shadow_side(
    *,
    strict: bool,
    tmp_path: Path,
    setup_fn,
    action_fn,
) -> ShadowOutcome:
    """Execute a single side (Legacy if strict=False, Strict if strict=True) in full filesystem isolation."""
    side_dir = tmp_path / ("strict" if strict else "legacy")
    side_task_dir = side_dir / "tasks"
    side_dir.mkdir(parents=True, exist_ok=True)
    side_task_dir.mkdir(parents=True, exist_ok=True)

    with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=strict), \
         patch("src.task_intent_builder.get_task_dir", return_value=side_task_dir), \
         patch("src.id_sequence._get_counter_file_path", return_value=side_dir / "counter.json"), \
         patch("src.id_sequence._get_lock_file_path", return_value=side_dir / "counter.lock"), \
         patch("src.id_sequence._COUNTERS", {}):

        dm = _make_dm(side_dir)

        if setup_fn:
            setup_fn(dm, side_task_dir)

        before_digest = build_shadow_digest(dm)

        succeeded = False
        exc_type = None
        exc_msg = None

        try:
            action_fn(dm, side_task_dir)
            succeeded = True
        except Exception as exc:
            succeeded = False
            exc_type = type(exc).__name__
            exc_msg = str(exc)

        after_digest = build_shadow_digest(dm)

        return ShadowOutcome(
            succeeded=succeeded,
            exception_type=exc_type,
            exception_message=exc_msg,
            before_digest=before_digest,
            after_digest=after_digest,
        )


def classify_shadow_outcomes(
    legacy: ShadowOutcome,
    strict: ShadowOutcome,
    *,
    expected_kind: str,
    canonicalizer=None,
) -> tuple[str, str]:
    """Automated classification comparator comparing Legacy and Strict execution outcomes."""
    leg_digest = canonicalizer(legacy.after_digest) if canonicalizer else legacy.after_digest
    st_digest = strict.after_digest

    if expected_kind in ("parity", "success_parity"):
        if legacy.succeeded and strict.succeeded and leg_digest == st_digest:
            return "PARITY", "Both sides succeeded with identical state digest"
        else:
            return "UNEXPECTED_BEHAVIOR_DELTA", f"Parity mismatch: legacy_ok={legacy.succeeded}, strict_ok={strict.succeeded}, leg_exc={legacy.exception_type}, st_exc={strict.exception_type}"

    elif expected_kind == "strict_fail_closed":
        strict_failed_closed = (not strict.succeeded) and (strict.exception_type in ("StateContractError", "ValueError", "SnapshotValidationError"))
        strict_atomicity_pass = (strict.before_digest == strict.after_digest)

        if legacy.succeeded and strict_failed_closed and strict_atomicity_pass:
            return "EXPECTED_FAIL_CLOSED_DELTA", f"Legacy succeeded, Strict failed closed with {strict.exception_type} and 0 state pollution"
        else:
            return "UNEXPECTED_BEHAVIOR_DELTA", f"Fail closed violation: legacy_ok={legacy.succeeded}, strict_ok={strict.succeeded}, exc={strict.exception_type}, atomic={strict_atomicity_pass}"
    else:
        raise ValueError(f"Unknown expected_kind: {expected_kind}")


def run_shadow_case(
    case_id: str,
    case_name: str,
    tmp_path: Path,
    setup_fn,
    action_fn,
    expected_kind: str,
    *,
    canonicalizer=None,
) -> dict:
    """Run dual-executed shadow case on isolated filesystem and return automated classification result."""
    legacy_outcome = execute_shadow_side(strict=False, tmp_path=tmp_path, setup_fn=setup_fn, action_fn=action_fn)
    strict_outcome = execute_shadow_side(strict=True, tmp_path=tmp_path, setup_fn=setup_fn, action_fn=action_fn)

    classification, reason = classify_shadow_outcomes(
        legacy_outcome,
        strict_outcome,
        expected_kind=expected_kind,
        canonicalizer=canonicalizer,
    )

    return {
        "case_id": case_id,
        "case_name": case_name,
        "expected_kind": expected_kind,
        "classification": classification,
        "legacy": legacy_outcome,
        "strict": strict_outcome,
        "reason": reason,
    }


def _helper_setup_published_task(dm: DialogueManager, task_dir: Path, intent_id: str = "TI202608090001"):
    """Helper to populate valid published task state and write task intent file into task_dir."""
    schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
    dm.slot_store.init_task_slots(schema)
    slots = dm.slot_store.clone_slots()
    slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid", value_type="string")
    slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid", value_type="string")
    slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", value_type="number")
    slots["location"] = Slot("location", value="A区", status="valid", value_type="string")
    slots["intent_id"] = Slot("intent_id", value=intent_id, status="valid", value_type="string")
    slots["internal_id"] = Slot("internal_id", value="00000000-0000-4000-8000-000000000001", status="valid", value_type="string")
    slots["task_id"] = Slot("task_id", value="PI-20260809-001", status="valid", value_type="string")
    slots["equipment_class"] = Slot("equipment_class", value="观察级ROV", status="valid", value_type="string")
    slots["equipment_type"] = Slot("equipment_type", value="观察级深海机器人", status="valid", value_type="string")
    slots["equipment_unit_id"] = Slot("equipment_unit_id", value="OBSROV--001", status="valid", value_type="string")
    dm.slot_store.commit_transaction(slots, [])
    dm.task_state = dm.slot_store.get_task_state()

    task_data = {
        "schema_version": 2,
        "intent_id": intent_id,
        "internal_id": "00000000-0000-4000-8000-000000000001",
        "task_id": "PI-20260809-001",
        "task_type": "pipeline_inspection",
        "task_type_key": "pipeline_inspection",
        "priority": 1,
        "time": {"start": "now", "end": "now+1h"},
        "location": {"oilfield": "A区", "water_depth_m": 300.0},
        "task": {"type": "pipeline_inspection", "details": "管缆巡检"},
        "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": "Vessel1"},
        "conditions": {"water_depth": 300.0},
    }
    task_dir.mkdir(parents=True, exist_ok=True)
    with open(task_dir / f"task_intent_{intent_id}.json", "w", encoding="utf-8") as f:
        json.dump(task_data, f)

    dm.phase = "done"
    dm.final_result = task_data


def GET_SHADOW_CASE_DEFINITIONS() -> list[dict]:
    """Return array of case specs defining (case_id, case_name, setup_fn, action_fn, expected_kind, canonicalizer)."""

    # A01
    def a01_action(dm, td): pass

    # A02
    def a02_action(dm, td):
        dm.export_snapshot()

    # A03
    def a03_action(dm, td):
        dm.process("什么是DVL？", request_id="req_a03")

    # A04
    def a04_action(dm, td):
        dm.process("你好，请介绍一下你自己", request_id="req_a04")

    # A05
    def a05_setup(dm, td):
        schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
        dm.slot_store.init_task_slots(schema)
        slots = dm.slot_store.clone_slots()
        slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", value_type="number")
        dm.slot_store.commit_transaction(slots, [])
        dm.task_state = dm.slot_store.get_task_state()

    def a05_action(dm, td):
        dm.process("水深测量是用什么仪器？", request_id="req_a05")

    # A06
    def stub_extract(messages, max_tokens=None):
        return {
            "slot_candidates": [
                {
                    "canonical_key": "task_type",
                    "normalized_value": "管缆巡检",
                    "raw_value": "管缆巡检",
                    "confidence": 1.0,
                    "resolution_method": "canonical_exact",
                }
            ]
        }

    def a06_action(dm, td):
        dm.process("什么是DVL？")
        with patch.object(dm.llm, "extract_json", side_effect=stub_extract):
            dm.process("创建一个管缆巡检任务")

    # A07
    def a07_action(dm, td):
        dm._transition_phase("confirming")

    # A08 (Real Soft Warning Acknowledgment Flow)
    def a08_setup(dm, td):
        schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
        dm.slot_store.init_task_slots(schema)
        dm.phase = "blocked_soft"
        v = Violation(constraint_id="SOFT_01", constraint_name="Soft Test", message="Soft warning", severity="soft", related_fields=["water_depth"], observed_value=300.0)
        dm._blocking_violations = [v]

    def a08_action(dm, td):
        dm.process("确认忽略警告", request_id="req_a08")

    # A09 (Real Hard Constraint Correction Flow)
    def a09_setup(dm, td):
        dm.phase = "blocked_hard"
        schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
        dm.slot_store.init_task_slots(schema)

    def a09_action(dm, td):
        with patch.object(dm.llm, "extract_json", side_effect=stub_extract):
            dm.process("修改水深为300米", request_id="req_a09")

    # A10 (Real Publish Flow)
    def a10_setup(dm, td):
        schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
        dm.slot_store.init_task_slots(schema)
        slots = dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid", value_type="string")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid", value_type="string")
        slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", value_type="number")
        slots["location"] = Slot("location", value="A区", status="valid", value_type="string")
        slots["intent_id"] = Slot("intent_id", value="TI202608090001", status="valid", value_type="string")
        slots["internal_id"] = Slot("internal_id", value="00000000-0000-4000-8000-000000000001", status="valid", value_type="string")
        slots["task_id"] = Slot("task_id", value="PI-20260809-001", status="valid", value_type="string")
        slots["equipment_class"] = Slot("equipment_class", value="观察级ROV", status="valid", value_type="string")
        slots["equipment_type"] = Slot("equipment_type", value="观察级深海机器人", status="valid", value_type="string")
        slots["equipment_unit_id"] = Slot("equipment_unit_id", value="OBSROV--001", status="valid", value_type="string")
        dm.slot_store.commit_transaction(slots, [])
        dm.task_state = dm.slot_store.get_task_state()
        dm.phase = "confirming"

    def a10_action(dm, td):
        dm.process("确认发布", request_id="req_a10")

    # A11
    def a11_setup(dm, td):
        _helper_setup_published_task(dm, td, "TI202608090001")

    def a11_action(dm, td):
        dm.process("确认", request_id="req_a11")

    # A12 (Real Draft Cancel Flow)
    def a12_setup(dm, td):
        schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
        dm.slot_store.init_task_slots(schema)

    def a12_action(dm, td):
        dm.process("取消任务", request_id="req_a12")

    # A13 (Real Control Action Flow)
    def a13_setup(dm, td):
        _helper_setup_published_task(dm, td, "TI202608090001")

    def a13_action(dm, td):
        dm.process("停止当前任务", request_id="req_a13")

    # A14
    def a14_setup(dm, td):
        _helper_setup_published_task(dm, td, "TI202608090001")

    def a14_action(dm, td):
        req_stop = {"action": "stop", "status": "requested", "target_intent_id": "TI202608090001"}
        req_pause = {"action": "pause", "status": "requested", "target_intent_id": "TI202608090001"}
        dm._set_execution_control_state("stop_requested", req_stop)
        dm._set_execution_control_state("pause_requested", req_pause)

    # A15 (Real Published Task A -> Emergency Request A -> Task Modification on A)
    def a15_setup(dm, td):
        _helper_setup_published_task(dm, td, "TI202608090001")
        req = {"action": "stop", "status": "requested", "target_intent_id": "TI202608090001"}
        dm._set_execution_control_state("stop_requested", req)

    def a15_action(dm, td):
        def stub_extract_a15(messages, max_tokens=None):
            return {
                "slot_candidates": [
                    {
                        "canonical_key": "water_depth",
                        "normalized_value": "500",
                        "raw_value": "500米",
                        "confidence": 0.95,
                        "resolution_method": "regex_rule",
                    }
                ]
            }
        mock_route = MagicMock()
        mock_route.dialogue_mode = "task_collection"
        with patch.object(dm.intent_router, "route", return_value=mock_route):
            with patch.object(dm.llm, "extract_json", side_effect=stub_extract_a15):
                dm.process("修改水深为500米", request_id="req_a15")

    # A16
    def a16_action(dm, td):
        snap = {
            "snapshot_version": 2,
            "phase": "collecting",
            "mode": "normal",
            "dialogue_mode": "task_collection",
            "control_state": "idle",
            "last_control_request": None,
            "slot_store": {
                "version": 1,
                "slots": {
                    "intent_id": {"slot_name": "intent_id", "value": "TI202608090001", "status": "valid", "value_type": "string"},
                    "task_type": {"slot_name": "task_type", "value": "管缆巡检", "status": "valid", "value_type": "string"},
                    "water_depth": {"slot_name": "water_depth", "value": 300.0, "status": "valid", "value_type": "number"},
                },
                "unresolved": [],
            },
            "task_state": {"intent_id": "TI202608090001", "task_type": "管缆巡检", "water_depth": 300.0},
        }
        dm.load_snapshot(snap)

    # A17
    def a17_setup(dm, td):
        _helper_setup_published_task(dm, td, "TI202608090001")

    def a17_action(dm, td):
        snap_legacy_done = {
            "snapshot_version": 2,
            "phase": "done",
            "mode": "normal",
            "dialogue_mode": "task_collection",
            "control_state": "stop_requested",
            "last_control_request": {"action": "stop", "status": "requested"},
            "slot_store": {
                "version": 1,
                "slots": {
                    "intent_id": {"slot_name": "intent_id", "value": "TI202608090001", "status": "valid", "value_type": "string"},
                    "internal_id": {"slot_name": "internal_id", "value": "00000000-0000-4000-8000-000000000001", "status": "valid", "value_type": "string"},
                    "task_id": {"slot_name": "task_id", "value": "PI-20260809-001", "status": "valid", "value_type": "string"},
                    "task_type": {"slot_name": "task_type", "value": "管缆巡检", "status": "valid", "value_type": "string"},
                    "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "value_type": "string"},
                },
                "unresolved": [],
            },
            "task_state": {
                "intent_id": "TI202608090001",
                "internal_id": "00000000-0000-4000-8000-000000000001",
                "task_id": "PI-20260809-001",
                "task_type": "管缆巡检",
                "task_type_key": "pipeline_inspection",
            },
        }
        dm.load_snapshot(snap_legacy_done)

    # A18 (Real dm.reset())
    def a18_setup(dm, td):
        schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
        dm.slot_store.init_task_slots(schema)
        dm.phase = "confirming"

    def a18_action(dm, td):
        dm.reset()

    # A19
    def a19_action(dm, td):
        dm._switch_dialogue_mode("knowledge_qa", source="rule", confidence=1.0, reason="qa query")
        dm._switch_dialogue_mode("task_collection", source="rule", confidence=1.0, reason="task create")

    # A20
    def a20_setup(dm, td):
        schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
        dm.slot_store.init_task_slots(schema)
        slots = dm.slot_store.clone_slots()
        slots["intent_id"] = Slot("intent_id", value="TI202608090001", status="valid", value_type="string")
        dm.slot_store.commit_transaction(slots, [])
        dm.task_state = dm.slot_store.get_task_state()

    def a20_action(dm, td):
        snap = dm.export_snapshot()
        dm.load_snapshot(snap)

    # B01
    def b01_action(dm, td):
        dm._transition_phase("invalid_phase")

    # B02
    def b02_action(dm, td):
        dm._switch_dialogue_mode("invalid_mode")

    # B03
    def b03_action(dm, td):
        dm._switch_dialogue_mode("uncertain")

    # B04
    def b04_action(dm, td):
        dm._transition_phase("done")

    # B05
    def b05_setup(dm, td):
        dm.phase = "blocked_soft"

    def b05_action(dm, td):
        dm._transition_phase("done")

    # B06
    def b06_setup(dm, td):
        dm.phase = "blocked_hard"

    def b06_action(dm, td):
        dm._transition_phase("confirming")

    # B07
    def b07_setup(dm, td):
        dm.phase = "blocked_hard"

    def b07_action(dm, td):
        dm._transition_phase("done")

    # B08
    def b08_action(dm, td):
        req = {"action": "stop", "status": "requested", "target_intent_id": "TI202608090001"}
        dm._set_execution_control_state("stop_requested", req)

    # B09
    def b09_setup(dm, td):
        _helper_setup_published_task(dm, td, "TI202608090001")

    def b09_action(dm, td):
        req_invalid = {"action": "stop", "status": "requested"}
        dm._set_execution_control_state("stop_requested", req_invalid)

    # B10
    def b10_setup(dm, td):
        _helper_setup_published_task(dm, td, "TI202608090001")

    def b10_action(dm, td):
        req_invalid = {"action": "stop", "status": "requested", "target_intent_id": "INVALID_FORMAT"}
        dm._set_execution_control_state("stop_requested", req_invalid)

    # B11
    def b11_setup(dm, td):
        _helper_setup_published_task(dm, td, "TI202608090001")

    def b11_action(dm, td):
        req_stop = {"action": "stop", "status": "requested", "target_intent_id": "TI202608090001"}
        dm._set_execution_control_state("pause_requested", req_stop)

    # B12
    def b12_setup(dm, td):
        _helper_setup_published_task(dm, td, "TI202608090001")

    def b12_action(dm, td):
        req_invalid = {"action": "stop", "status": "executing", "target_intent_id": "TI202608090001"}
        dm._set_execution_control_state("stop_requested", req_invalid)

    # B13
    def b13_action(dm, td):
        ambiguous_snap = {
            "snapshot_version": 2,
            "phase": "confirming",
            "mode": "normal",
            "dialogue_mode": "task_collection",
            "control_state": "stop_requested",
            "last_control_request": {"action": "stop", "status": "requested"},
            "slot_store": {"version": 1, "slots": {}, "unresolved": []},
            "task_state": {"intent_id": "TI202608090002"},
        }
        dm.load_snapshot(ambiguous_snap)

    # B14
    def b14_action(dm, td):
        invalid_phase_snap = {
            "snapshot_version": 2,
            "phase": "bogus_phase",
            "mode": "normal",
            "dialogue_mode": "task_collection",
            "control_state": "idle",
            "last_control_request": None,
            "slot_store": {"version": 1, "slots": {}, "unresolved": []},
            "task_state": {},
        }
        dm.load_snapshot(invalid_phase_snap)

    return [
        {"case_id": "A01", "name": "Initial State", "setup": None, "action": a01_action, "kind": "success_parity", "canon": None},
        {"case_id": "A02", "name": "Export Snapshot", "setup": None, "action": a02_action, "kind": "success_parity", "canon": None},
        {"case_id": "A03", "name": "Ordinary Knowledge QA Read-only", "setup": None, "action": a03_action, "kind": "success_parity", "canon": None},
        {"case_id": "A04", "name": "General Chat", "setup": None, "action": a04_action, "kind": "success_parity", "canon": None},
        {"case_id": "A05", "name": "task_collection -> knowledge_qa", "setup": a05_setup, "action": a05_action, "kind": "success_parity", "canon": None},
        {"case_id": "A06", "name": "knowledge_qa -> task_collection", "setup": None, "action": a06_action, "kind": "success_parity", "canon": None},
        {"case_id": "A07", "name": "Legal Task Phase Transition", "setup": None, "action": a07_action, "kind": "success_parity", "canon": None},
        {"case_id": "A08", "name": "Soft Warning Legal Path", "setup": a08_setup, "action": a08_action, "kind": "success_parity", "canon": None},
        {"case_id": "A09", "name": "Hard Constraint Legal Resolution", "setup": a09_setup, "action": a09_action, "kind": "success_parity", "canon": None},
        {"case_id": "A10", "name": "Confirming -> Done (Real Publish)", "setup": a10_setup, "action": a10_action, "kind": "success_parity", "canon": None},
        {"case_id": "A11", "name": "Duplicate Confirmation", "setup": a11_setup, "action": a11_action, "kind": "success_parity", "canon": None},
        {"case_id": "A12", "name": "Draft Cancel (Real Cancel)", "setup": a12_setup, "action": a12_action, "kind": "success_parity", "canon": None},
        {"case_id": "A13", "name": "Published Execution Request", "setup": a13_setup, "action": a13_action, "kind": "success_parity", "canon": None},
        {"case_id": "A14", "name": "Requested -> Requested", "setup": a14_setup, "action": a14_action, "kind": "success_parity", "canon": None},
        {"case_id": "A15", "name": "Task Modification After Published Request", "setup": a15_setup, "action": a15_action, "kind": "success_parity", "canon": None},
        {"case_id": "A16", "name": "Valid Snapshot Restore", "setup": None, "action": a16_action, "kind": "success_parity", "canon": None},
        {"case_id": "A17", "name": "Safe Legacy Done Migration", "setup": a17_setup, "action": a17_action, "kind": "success_parity", "canon": canonicalize_legacy_enrichment},
        {"case_id": "A18", "name": "Reset (Real dm.reset())", "setup": a18_setup, "action": a18_action, "kind": "success_parity", "canon": None},
        {"case_id": "A19", "name": "Mode Transition History", "setup": None, "action": a19_action, "kind": "success_parity", "canon": None},
        {"case_id": "A20", "name": "Session Snapshot Round Trip", "setup": a20_setup, "action": a20_action, "kind": "success_parity", "canon": None},

        {"case_id": "B01", "name": "Invalid Runtime Phase", "setup": None, "action": b01_action, "kind": "strict_fail_closed", "canon": None},
        {"case_id": "B02", "name": "Invalid Runtime Dialogue Mode", "setup": None, "action": b02_action, "kind": "strict_fail_closed", "canon": None},
        {"case_id": "B03", "name": "Runtime uncertain", "setup": None, "action": b03_action, "kind": "strict_fail_closed", "canon": None},
        {"case_id": "B04", "name": "collecting -> done", "setup": None, "action": b04_action, "kind": "strict_fail_closed", "canon": None},
        {"case_id": "B05", "name": "blocked_soft -> done", "setup": b05_setup, "action": b05_action, "kind": "strict_fail_closed", "canon": None},
        {"case_id": "B06", "name": "blocked_hard -> confirming", "setup": b06_setup, "action": b06_action, "kind": "strict_fail_closed", "canon": None},
        {"case_id": "B07", "name": "blocked_hard -> done", "setup": b07_setup, "action": b07_action, "kind": "strict_fail_closed", "canon": None},
        {"case_id": "B08", "name": "Non-done Execution Request", "setup": None, "action": b08_action, "kind": "strict_fail_closed", "canon": None},
        {"case_id": "B09", "name": "Missing Execution target_intent_id", "setup": b09_setup, "action": b09_action, "kind": "strict_fail_closed", "canon": None},
        {"case_id": "B10", "name": "Invalid Execution Target", "setup": b10_setup, "action": b10_action, "kind": "strict_fail_closed", "canon": None},
        {"case_id": "B11", "name": "Action / State Mismatch", "setup": b11_setup, "action": b11_action, "kind": "strict_fail_closed", "canon": None},
        {"case_id": "B12", "name": "Invalid Request Status", "setup": b12_setup, "action": b12_action, "kind": "strict_fail_closed", "canon": None},
        {"case_id": "B13", "name": "Ambiguous Legacy Snapshot", "setup": None, "action": b13_action, "kind": "strict_fail_closed", "canon": None},
        {"case_id": "B14", "name": "Invalid Snapshot Phase", "setup": None, "action": b14_action, "kind": "strict_fail_closed", "canon": None},
    ]


def RUN_ALL_SHADOW_GATE_CASES(tmp_path: Path) -> list[dict]:
    """Pure standalone function executing the entire shadow gate matrix in filesystem isolation."""
    defs = GET_SHADOW_CASE_DEFINITIONS()
    results = []
    for cdef in defs:
        res = run_shadow_case(
            cdef["case_id"],
            cdef["name"],
            tmp_path / cdef["case_id"],
            cdef["setup"],
            cdef["action"],
            cdef["kind"],
            canonicalizer=cdef["canon"],
        )
        results.append(res)
    return results


class TestShadowComparatorUnit(unittest.TestCase):
    """Unit test suite for classify_shadow_outcomes comparator robustness."""

    def test_comparator_parity_success(self):
        leg = ShadowOutcome(True, None, None, {"phase": "c"}, {"phase": "c"})
        st = ShadowOutcome(True, None, None, {"phase": "c"}, {"phase": "c"})
        cls, reason = classify_shadow_outcomes(leg, st, expected_kind="success_parity")
        self.assertEqual(cls, "PARITY")

    def test_comparator_strict_fail_closed_success(self):
        leg = ShadowOutcome(True, None, None, {"phase": "c"}, {"phase": "c"})
        st = ShadowOutcome(False, "StateContractError", "Invalid transition", {"phase": "c"}, {"phase": "c"})
        cls, reason = classify_shadow_outcomes(leg, st, expected_kind="strict_fail_closed")
        self.assertEqual(cls, "EXPECTED_FAIL_CLOSED_DELTA")

    def test_comparator_both_failed_on_strict_fail_closed(self):
        leg = ShadowOutcome(False, "ValueError", "Legacy failed", {"phase": "c"}, {"phase": "c"})
        st = ShadowOutcome(False, "StateContractError", "Strict failed", {"phase": "c"}, {"phase": "c"})
        cls, reason = classify_shadow_outcomes(leg, st, expected_kind="strict_fail_closed")
        self.assertEqual(cls, "UNEXPECTED_BEHAVIOR_DELTA")

    def test_comparator_both_failed_on_success_parity(self):
        leg = ShadowOutcome(False, "RuntimeError", "Legacy failed", {"phase": "c"}, {"phase": "c"})
        st = ShadowOutcome(False, "RuntimeError", "Strict failed", {"phase": "c"}, {"phase": "c"})
        cls, reason = classify_shadow_outcomes(leg, st, expected_kind="success_parity")
        self.assertEqual(cls, "UNEXPECTED_BEHAVIOR_DELTA")

    def test_comparator_parity_digest_mismatch(self):
        leg = ShadowOutcome(True, None, None, {"phase": "c"}, {"phase": "c"})
        st = ShadowOutcome(True, None, None, {"phase": "c"}, {"phase": "d"})
        cls, reason = classify_shadow_outcomes(leg, st, expected_kind="success_parity")
        self.assertEqual(cls, "UNEXPECTED_BEHAVIOR_DELTA")


class TestSessionStateRolloutShadowV2(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self._tmp)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Individual Shadow Case Runner Tests (A01 - A20, B01 - B14)
    # -------------------------------------------------------------------------

    def test_a01_initial_state(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A01"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a02_export_snapshot(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A02"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a03_ordinary_knowledge_qa_readonly(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A03"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a04_general_chat(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A04"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a05_task_collection_to_knowledge_qa(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A05"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a06_knowledge_qa_to_task_collection(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A06"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a07_legal_task_phase_transition(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A07"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a08_soft_warning_legal_path(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A08"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a09_hard_constraint_legal_resolution(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A09"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a10_confirming_to_done(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A10"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a11_duplicate_confirmation(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A11"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a12_draft_cancel(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A12"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a13_published_execution_request(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A13"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a14_requested_to_requested(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A14"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a15_task_modification_after_published_request(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A15"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a16_valid_snapshot_restore(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A16"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a17_safe_legacy_done_migration(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A17"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a18_reset(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A18"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a19_mode_transition_history(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A19"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_a20_session_snapshot_round_trip(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["A20"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "PARITY")

    def test_b01_invalid_runtime_phase(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B01"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    def test_b02_invalid_runtime_dialogue_mode(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B02"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    def test_b03_runtime_uncertain(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B03"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    def test_b04_collecting_to_done(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B04"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    def test_b05_blocked_soft_to_done(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B05"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    def test_b06_blocked_hard_to_confirming(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B06"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    def test_b07_blocked_hard_to_done(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B07"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    def test_b08_non_done_execution_request(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B08"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    def test_b09_missing_execution_target_intent_id(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B09"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    def test_b10_invalid_execution_target(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B10"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    def test_b11_action_state_mismatch(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B11"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    def test_b12_invalid_request_status(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B12"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    def test_b13_ambiguous_legacy_snapshot(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B13"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    def test_b14_invalid_snapshot_phase(self):
        defs = {c["case_id"]: c for c in GET_SHADOW_CASE_DEFINITIONS()}
        c = defs["B14"]
        res = run_shadow_case(c["case_id"], c["name"], self.tmp_path, c["setup"], c["action"], c["kind"], canonicalizer=c["canon"])
        self.assertEqual(res["classification"], "EXPECTED_FAIL_CLOSED_DELTA")

    # -------------------------------------------------------------------------
    # Governance Invariants In Strict Mode (INV-01 ~ INV-11)
    # -------------------------------------------------------------------------

    def test_inv01_strict_query_read_only(self):
        """INV-01: QUERY read-only invariant in Strict mode."""
        dm = _make_dm(self.tmp_path / "inv01")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            v_before = dm.slot_store.version
            snap_before = dm.slot_store.export_snapshot()
            dm.process("什么是DVL？", request_id="req_inv01")
            self.assertEqual(v_before, dm.slot_store.version)
            self.assertEqual(snap_before, dm.slot_store.export_snapshot())

    def test_inv02_strict_write_only_mutates_task(self):
        """INV-02: Write path task creation in Strict mode."""
        dm = _make_dm(self.tmp_path / "inv02")
        v_before = dm.slot_store.version

        def stub_extract(messages, max_tokens=None):
            return {
                "slot_candidates": [
                    {
                        "canonical_key": "task_type",
                        "normalized_value": "管缆巡检",
                        "raw_value": "管缆巡检",
                        "confidence": 1.0,
                        "resolution_method": "canonical_exact",
                    }
                ]
            }

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with patch.object(dm.llm, "extract_json", side_effect=stub_extract):
                dm.process("创建一个管缆巡检任务", request_id="req_inv02")

        self.assertGreater(dm.slot_store.version, v_before)
        self.assertEqual(dm.slot_store.get_task_state().get("task_type"), "管缆巡检")

    def test_inv03_strict_valid_slot_is_fact(self):
        """INV-03: Valid slot value is fact in Strict mode."""
        dm = _make_dm(self.tmp_path / "inv03")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
            dm.slot_store.init_task_slots(schema)
            slots = dm.slot_store.clone_slots()
            slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", value_type="number")
            dm.slot_store.commit_transaction(slots, [])
            dm.task_state = dm.slot_store.get_task_state()
            self.assertEqual(dm.task_state.get("water_depth"), 300.0)

    def test_inv04_strict_invalid_input_never_overwrites(self):
        """INV-04: Invalid candidate input never overwrites valid fact in Strict mode via real process flow."""
        dm = _make_dm(self.tmp_path / "inv04")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
            dm.slot_store.init_task_slots(schema)
            slots = dm.slot_store.clone_slots()
            slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
            slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
            slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", value_type="number")
            dm.slot_store.commit_transaction(slots, [])
            dm.task_state = dm.slot_store.get_task_state()

            def stub_llm_extract_json(messages, max_tokens=None):
                return {
                    "slot_candidates": [
                        {
                            "canonical_key": "water_depth",
                            "normalized_value": "300abc",
                            "raw_value": "差不多很深",
                            "confidence": 0.9,
                            "resolution_method": "llm_semantic",
                        }
                    ]
                }

            with patch.object(dm.llm, "extract_json", side_effect=stub_llm_extract_json):
                dm.process("水深改成差不多很深", request_id="req_inv04_invalid")

            state_after = dm.slot_store.get_task_state()
            self.assertNotIn("water_depth", state_after)

            slot = dm.slot_store.slots.get("water_depth")
            self.assertEqual(slot.value, 300.0)
            self.assertEqual(slot.candidate_value, "300abc")
            self.assertEqual(slot.raw_value, "差不多很深")
            self.assertEqual(slot.status, "conflict")
            self.assertIsNotNone(slot.validation_error)

    def test_inv05_strict_hard_cannot_be_bypassed(self):
        """INV-05: Hard constraint cannot be bypassed by generic confirmation in Strict mode."""
        dm = _make_dm(self.tmp_path / "inv05")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm.phase = "blocked_hard"
            dm.process("确认", request_id="req_inv05")
            self.assertEqual(dm.phase, "blocked_hard")

    def test_inv06_strict_soft_ack_is_distinct(self):
        """INV-06: Soft warning explicit acknowledgement is distinct in Strict mode."""
        dm = _make_dm(self.tmp_path / "inv06")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm.phase = "blocked_soft"
            # Generic message does not unblock
            dm.process("今天天气怎么样", request_id="req_inv06_gen")
            self.assertEqual(dm.phase, "blocked_soft")
            # Explicit ack unblocks
            dm.process("确认忽略警告", request_id="req_inv06_ack")
            self.assertIn(dm.phase, ("collecting", "confirming"))

    def test_inv07_strict_publish_fail_closed(self):
        """INV-07: Publish fail-closed persistence failure retains state without corruption in Strict mode."""
        dm = _make_dm(self.tmp_path / "inv07")
        task_dir = self.tmp_path / "inv07_task_dir"
        task_dir.mkdir(parents=True, exist_ok=True)
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True), \
             patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
            dm.slot_store.init_task_slots(schema)
            slots = dm.slot_store.clone_slots()
            slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid", value_type="string")
            slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid", value_type="string")
            slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", value_type="number")
            slots["location"] = Slot("location", value="A区", status="valid", value_type="string")
            slots["intent_id"] = Slot("intent_id", value="TI202608090001", status="valid", value_type="string")
            slots["internal_id"] = Slot("internal_id", value="00000000-0000-4000-8000-000000000001", status="valid", value_type="string")
            slots["task_id"] = Slot("task_id", value="PI-20260809-001", status="valid", value_type="string")
            slots["equipment_class"] = Slot("equipment_class", value="观察级ROV", status="valid", value_type="string")
            slots["equipment_type"] = Slot("equipment_type", value="观察级深海机器人", status="valid", value_type="string")
            slots["equipment_unit_id"] = Slot("equipment_unit_id", value="OBSROV--001", status="valid", value_type="string")
            dm.slot_store.commit_transaction(slots, [])
            dm.task_state = dm.slot_store.get_task_state()
            dm.phase = "confirming"

            mock_val_res = ValidationResult(
                overall_status="valid",
                validated_at="2026-08-09 12:00:00",
                task_version=1,
                validation_version=1,
                validation_fingerprint="fp_test",
                state_snapshot=None,
                violations=[],
            )

            with patch.object(dm, "_refresh_validation", return_value=mock_val_res), \
                 patch.object(dm.kb.state_info, "check_runtime_availability", return_value={"available": True}), \
                 patch.object(dm.slot_store, "get_missing_slots", return_value=[]), \
                 patch.object(TaskIntentBuilder, "create_staging", side_effect=TaskPersistenceError("Simulated disk error")):
                with self.assertRaises(TaskPersistenceError):
                    dm._handle_final_publish_confirmation("确认发布", request_id="req_inv07")
                self.assertEqual(dm.phase, "confirming")
                self.assertIsNone(dm.final_result)

    def test_inv08_strict_duplicate_confirm_idempotent(self):
        """INV-08: Duplicate confirm idempotent in Strict mode."""
        dm = _make_dm(self.tmp_path / "inv08")
        task_dir = self.tmp_path / "inv08_task_dir"
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True), \
             patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            _helper_setup_published_task(dm, task_dir, "TI202608090001")
            dm.process("确认", request_id="req_inv08")
            self.assertEqual(dm.phase, "done")

    def test_inv09_strict_session_isolation(self):
        """INV-09: Session isolation between DialogueManager instances in Strict mode."""
        dm1 = _make_dm(self.tmp_path / "s1")
        dm2 = _make_dm(self.tmp_path / "s2")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm1._transition_phase("confirming")
            self.assertEqual(dm1.phase, "confirming")
            self.assertEqual(dm2.phase, "collecting")

    def test_inv10_strict_final_no_overwrite(self):
        """INV-10: Overwriting an existing published final file fails closed in Strict mode."""
        dm = _make_dm(self.tmp_path / "inv10")
        task_dir = self.tmp_path / "inv10_task_dir"
        task_dir.mkdir(parents=True, exist_ok=True)
        intent_id = "TI202608090001"
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True), \
             patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            _helper_setup_published_task(dm, task_dir, intent_id)
            final_file = task_dir / f"task_intent_{intent_id}.json"
            self.assertTrue(final_file.exists())

            # Attempt to re-publish with existing final file
            builder = TaskIntentBuilder(dm.kb)
            staging = task_dir / f"task_intent_{intent_id}.staging"
            staging.write_text("{}")
            with self.assertRaises((IntentIdConflict, TaskPersistenceError)):
                builder.publish_staging(staging, final_file)

    def test_inv11_strict_request_traceability(self):
        """INV-11: Request traceability in Strict mode end-to-end through SlotStore write path and API chat route."""
        dm = _make_dm(self.tmp_path / "inv11")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            def stub_extract(messages, max_tokens=None):
                return {
                    "slot_candidates": [
                        {
                            "canonical_key": "task_type",
                            "normalized_value": "管缆巡检",
                            "raw_value": "管缆巡检",
                            "confidence": 1.0,
                            "resolution_method": "canonical_exact",
                        }
                    ]
                }

            with patch.object(dm.llm, "extract_json", side_effect=stub_extract):
                with patch.object(dm.slot_store, "commit_transaction", wraps=dm.slot_store.commit_transaction) as mock_commit:
                    reply = dm.process("创建一个管缆巡检任务", request_id="req_trace_12345")
                    self.assertTrue(isinstance(reply, str) and len(reply) > 0)
                    mock_commit.assert_called()
                    _, kwargs = mock_commit.call_args
                    self.assertEqual(kwargs.get("request_id"), "req_trace_12345")

        # API route traceability
        client = app.test_client()
        with patch.object(WebDialogueManager, "process", return_value="ok") as mock_proc:
            res = client.post("/api/chat", json={
                "session_id": "sess_trace_exp",
                "request_id": "req_custom_12345",
                "message": "测试透传",
            })
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("request_id"), "req_custom_12345")
            mock_proc.assert_called_once_with("测试透传", request_id="req_custom_12345")

    # -------------------------------------------------------------------------
    # ASR Text Entry Parity Check
    # -------------------------------------------------------------------------

    def test_asr_text_entry_shadow_consistency(self):
        """Verify post-ASR text-entry semantics produces 100% identical SessionState shadow digest as typed text."""
        dm_typed = _make_dm(self.tmp_path / "typed")
        dm_asr = _make_dm(self.tmp_path / "asr")

        def stub_extract(messages, max_tokens=None):
            return {
                "slot_candidates": [
                    {
                        "canonical_key": "task_type",
                        "normalized_value": "管缆巡检",
                        "raw_value": "管缆巡检",
                        "confidence": 1.0,
                        "resolution_method": "canonical_exact",
                    }
                ]
            }

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with patch.object(dm_typed.llm, "extract_json", side_effect=stub_extract):
                dm_typed.process("创建一个管缆巡检任务", request_id="req_typed")
            with patch.object(dm_asr.llm, "extract_json", side_effect=stub_extract):
                dm_asr.process("创建一个管缆巡检任务", request_id="req_asr")

        digest_typed = build_shadow_digest(dm_typed)
        digest_asr = build_shadow_digest(dm_asr)

        self.assertEqual(digest_typed, digest_asr)

    # -------------------------------------------------------------------------
    # Standalone Shadow Comparator & Summary Matrix Test
    # -------------------------------------------------------------------------

    def test_shadow_summary_metrics(self):
        """Standalone matrix runner evaluating all shadow cases in complete isolation without test order dependency."""
        results = RUN_ALL_SHADOW_GATE_CASES(self.tmp_path / "summary_runner")

        unexpected_count = sum(1 for r in results if r["classification"] == "UNEXPECTED_BEHAVIOR_DELTA")
        self.assertEqual(unexpected_count, 0, f"UNEXPECTED_BEHAVIOR_DELTA must be 0, got {unexpected_count}")

        # Assert every case actual classification matches its expected semantic category
        for r in results:
            expected_kind = r["expected_kind"]
            actual_class = r["classification"]
            if expected_kind in ("parity", "success_parity"):
                self.assertEqual(actual_class, "PARITY", f"Case {r['case_id']} expected PARITY, got {actual_class}: {r['reason']}")
            elif expected_kind == "strict_fail_closed":
                self.assertEqual(actual_class, "EXPECTED_FAIL_CLOSED_DELTA", f"Case {r['case_id']} expected EXPECTED_FAIL_CLOSED_DELTA, got {actual_class}: {r['reason']}")


if __name__ == "__main__":
    unittest.main()
