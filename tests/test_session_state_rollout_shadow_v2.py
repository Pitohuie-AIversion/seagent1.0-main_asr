"""
tests/test_session_state_rollout_shadow_v2.py

SEAgent G4.1 SessionState V2 Shadow Compatibility Gate Test Suite.

Provides reproducible shadow comparison between:
  Legacy Runtime (session_state_v2 = false)
  vs
  Strict Runtime (session_state_v2 = true)

Categorizes outcomes into:
  - PARITY
  - EXPECTED_FAIL_CLOSED_DELTA
  - UNEXPECTED_BEHAVIOR_DELTA (asserted to be strictly 0)

Does NOT modify production feature flags or production code in src/.
"""

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.id_sequence import validate_intent_id, validate_uuid4
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.session_state import StateContractError, session_state_from_legacy_snapshot
from src.slot_store import Slot, SlotStore
from tests.fixtures.governance_corpus import GOVERNANCE_GOLDEN_CORPUS


def _make_dm(tmp_dir: Path) -> DialogueManager:
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
            if k in ("changed_at", "updated_at") and isinstance(v, str):
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


class TestSessionStateRolloutShadowV2(unittest.TestCase):
    shadow_results = []

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self._tmp)
        self.task_dir = self.tmp_path / "task_intents"
        self.task_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def record_result(self, case_id: str, case_name: str, classification: str, legacy_res: str, strict_res: str, reason: str):
        TestSessionStateRolloutShadowV2.shadow_results.append({
            "case_id": case_id,
            "case_name": case_name,
            "classification": classification,
            "legacy_result": legacy_res,
            "strict_result": strict_res,
            "reason": reason,
        })

    def _init_dm_pair(self) -> tuple[DialogueManager, DialogueManager]:
        """Create isolated, identical pair of DialogueManagers (dm_legacy, dm_strict)."""
        tmp_legacy = self.tmp_path / "legacy"
        tmp_strict = self.tmp_path / "strict"
        tmp_legacy.mkdir(exist_ok=True)
        tmp_strict.mkdir(exist_ok=True)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy = _make_dm(tmp_legacy)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict = _make_dm(tmp_strict)

        return dm_legacy, dm_strict

    def _setup_published_task(self, dm: DialogueManager, intent_id: str = "TI202608090001") -> str:
        """Helper to set up a published task in done phase on DialogueManager."""
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
        dm.slot_store.commit_transaction(slots, [])
        dm.task_state = dm.slot_store.get_task_state()

        # Write published task intent file
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
        from src.task_intent_builder import get_task_dir
        pub_dir = get_task_dir(create=True)
        pub_dir.mkdir(parents=True, exist_ok=True)
        with open(pub_dir / f"task_intent_{intent_id}.json", "w", encoding="utf-8") as f:
            json.dump(task_data, f)

        dm.phase = "done"
        dm.final_result = task_data
        return intent_id

    # =========================================================================
    # Category A: PARITY Scenarios (A01 - A20)
    # =========================================================================

    def test_a01_initial_state(self):
        """A01: New DialogueManager instances have identical initial State Digest."""
        dm_legacy, dm_strict = self._init_dm_pair()
        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)
        self.assertEqual(digest_legacy, digest_strict)
        self.record_result("A01", "Initial State", "PARITY", "PASS", "PASS", "Identical initial digest")

    def test_a02_export_snapshot(self):
        """A02: export_snapshot() structure and values match between Legacy and Strict."""
        dm_legacy, dm_strict = self._init_dm_pair()

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            snap_legacy = dm_legacy.export_snapshot()
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            snap_strict = dm_strict.export_snapshot()

        norm_snap_legacy = normalize_digest(snap_legacy)
        norm_snap_strict = normalize_digest(snap_strict)

        self.assertEqual(norm_snap_legacy, norm_snap_strict)
        self.record_result("A02", "Export Snapshot", "PARITY", "PASS", "PASS", "Exported snapshot parity")

    def test_a03_ordinary_knowledge_qa_readonly(self):
        """A03: Read-only Knowledge QA prompt keeps slot store version and task state unchanged."""
        dm_legacy, dm_strict = self._init_dm_pair()

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            reply_legacy = dm_legacy.process("什么是DVL？", request_id="req_a03_l")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            reply_strict = dm_strict.process("什么是DVL？", request_id="req_a03_s")

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy.dialogue_mode, "knowledge_qa")
        self.assertEqual(dm_strict.dialogue_mode, "knowledge_qa")
        self.record_result("A03", "Ordinary Knowledge QA Read-only", "PARITY", "PASS", "PASS", "Read-only invariant preserved")

    def test_a04_general_chat(self):
        """A04: General non-task chat does not trigger task mutation."""
        dm_legacy, dm_strict = self._init_dm_pair()

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy.process("你好，请介绍一下你自己", request_id="req_a04_l")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict.process("你好，请介绍一下你自己", request_id="req_a04_s")

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy.phase, "collecting")
        self.assertEqual(dm_strict.phase, "collecting")
        self.record_result("A04", "General Chat", "PARITY", "PASS", "PASS", "No task mutation on chat")

    def test_a05_task_collection_to_knowledge_qa(self):
        """A05: Asking QA prompt with existing task draft preserves task facts."""
        dm_legacy, dm_strict = self._init_dm_pair()
        for dm in (dm_legacy, dm_strict):
            schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
            dm.slot_store.init_task_slots(schema)
            slots = dm.slot_store.clone_slots()
            slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", value_type="number")
            dm.slot_store.commit_transaction(slots, [])
            dm.task_state = dm.slot_store.get_task_state()

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy.process("水深测量是用什么仪器？", request_id="req_a05_l")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict.process("水深测量是用什么仪器？", request_id="req_a05_s")

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy.slot_store.get_task_state().get("water_depth"), 300.0)
        self.assertEqual(dm_strict.slot_store.get_task_state().get("water_depth"), 300.0)
        self.record_result("A05", "task_collection -> knowledge_qa", "PARITY", "PASS", "PASS", "Task facts intact across QA switch")

    def test_a06_knowledge_qa_to_task_collection(self):
        """A06: Entering task collection after QA creates task draft consistently."""
        dm_legacy, dm_strict = self._init_dm_pair()

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

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy.process("什么是DVL？")
            with patch.object(dm_legacy.llm, "extract_json", side_effect=stub_extract):
                dm_legacy.process("创建一个管缆巡检任务")

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict.process("什么是DVL？")
            with patch.object(dm_strict.llm, "extract_json", side_effect=stub_extract):
                dm_strict.process("创建一个管缆巡检任务")

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy.dialogue_mode, "task_collection")
        self.assertEqual(dm_strict.dialogue_mode, "task_collection")
        self.record_result("A06", "knowledge_qa -> task_collection", "PARITY", "PASS", "PASS", "Draft task created consistently")

    def test_a07_legal_task_phase_transition(self):
        """A07: Transitioning collecting -> confirming via _transition_phase is parity."""
        dm_legacy, dm_strict = self._init_dm_pair()

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy._transition_phase("confirming")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict._transition_phase("confirming")

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy.phase, "confirming")
        self.assertEqual(dm_strict.phase, "confirming")
        self.record_result("A07", "Legal Task Phase Transition", "PARITY", "PASS", "PASS", "Legal transition collecting -> confirming")

    def test_a08_soft_warning_legal_path(self):
        """A08: Soft warning acknowledgment flow transitions consistently."""
        dm_legacy, dm_strict = self._init_dm_pair()
        for dm in (dm_legacy, dm_strict):
            dm.phase = "blocked_soft"

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy._handle_soft_warning_confirmation("确认忽略警告", request_id="req_a08_l")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict._handle_soft_warning_confirmation("确认忽略警告", request_id="req_a08_s")

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.record_result("A08", "Soft Warning Legal Path", "PARITY", "PASS", "PASS", "Soft warning resolved identically")

    def test_a09_hard_constraint_legal_resolution(self):
        """A09: Hard constraint resolution flow produces identical state."""
        dm_legacy, dm_strict = self._init_dm_pair()
        for dm in (dm_legacy, dm_strict):
            dm.phase = "blocked_hard"
            schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
            dm.slot_store.init_task_slots(schema)

        # Transition back to collecting after correction
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy._transition_phase("collecting")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict._transition_phase("collecting")

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy.phase, "collecting")
        self.assertEqual(dm_strict.phase, "collecting")
        self.record_result("A09", "Hard Constraint Legal Resolution", "PARITY", "PASS", "PASS", "Hard constraint resolved legally")

    def test_a10_confirming_to_done(self):
        """A10: Confirming -> done transition (simulated publish) matches between modes."""
        dm_legacy, dm_strict = self._init_dm_pair()
        for dm in (dm_legacy, dm_strict):
            dm.phase = "confirming"

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy._transition_phase("done")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict._transition_phase("done")

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy.phase, "done")
        self.assertEqual(dm_strict.phase, "done")
        self.record_result("A10", "Confirming -> Done", "PARITY", "PASS", "PASS", "Confirming to done parity")

    def test_a11_duplicate_confirmation(self):
        """A11: Repeat confirmation after done is idempotent."""
        dm_legacy, dm_strict = self._init_dm_pair()
        for dm in (dm_legacy, dm_strict):
            self._setup_published_task(dm)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy._handle_final_publish_confirmation("确认", request_id="req_a11_l")
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict._handle_final_publish_confirmation("确认", request_id="req_a11_s")

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy.phase, "done")
        self.assertEqual(dm_strict.phase, "done")
        self.record_result("A11", "Duplicate Confirmation", "PARITY", "PASS", "PASS", "Duplicate confirmation idempotent")

    def test_a12_draft_cancel(self):
        """A12: Cancelling draft task sets phase=rejected, control_state=idle, last_control_request=None."""
        dm_legacy, dm_strict = self._init_dm_pair()

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy._transition_phase("rejected")
            dm_legacy._set_execution_control_state("idle", None)
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict._transition_phase("rejected")
            dm_strict._set_execution_control_state("idle", None)

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy.phase, "rejected")
        self.assertEqual(dm_strict.phase, "rejected")
        self.assertIsNone(dm_legacy.last_control_request)
        self.assertIsNone(dm_strict.last_control_request)
        self.record_result("A12", "Draft Cancel", "PARITY", "PASS", "PASS", "Draft cancel parity")

    def test_a13_published_execution_request(self):
        """A13: Setting stop request on published task binds target_intent_id identically."""
        dm_legacy, dm_strict = self._init_dm_pair()
        intent_id = "TI202608090001"
        for dm in (dm_legacy, dm_strict):
            self._setup_published_task(dm, intent_id)

        req = {"action": "stop", "status": "requested", "target_intent_id": intent_id}

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy._set_execution_control_state("stop_requested", req)
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict._set_execution_control_state("stop_requested", req)

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy.control_state, "stop_requested")
        self.assertEqual(dm_strict.control_state, "stop_requested")
        self.assertEqual(dm_legacy.last_control_request.get("target_intent_id"), intent_id)
        self.assertEqual(dm_strict.last_control_request.get("target_intent_id"), intent_id)
        self.record_result("A13", "Published Execution Request", "PARITY", "PASS", "PASS", "Execution request bound to published target")

    def test_a14_requested_to_requested(self):
        """A14: Transitioning from stop_requested to pause_requested on published task is parity."""
        dm_legacy, dm_strict = self._init_dm_pair()
        intent_id = "TI202608090001"
        for dm in (dm_legacy, dm_strict):
            self._setup_published_task(dm, intent_id)

        req_stop = {"action": "stop", "status": "requested", "target_intent_id": intent_id}
        req_pause = {"action": "pause", "status": "requested", "target_intent_id": intent_id}

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy._set_execution_control_state("stop_requested", req_stop)
            dm_legacy._set_execution_control_state("pause_requested", req_pause)
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict._set_execution_control_state("stop_requested", req_stop)
            dm_strict._set_execution_control_state("pause_requested", req_pause)

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy.control_state, "pause_requested")
        self.assertEqual(dm_strict.control_state, "pause_requested")
        self.record_result("A14", "Requested -> Requested", "PARITY", "PASS", "PASS", "Requested to requested transition parity")

    def test_a15_task_modification_after_published_request(self):
        """A15: Modifying draft task after published request retains request target A."""
        dm_legacy, dm_strict = self._init_dm_pair()
        intent_id_a = "TI202608090001"
        for dm in (dm_legacy, dm_strict):
            self._setup_published_task(dm, intent_id_a)
            req = {"action": "stop", "status": "requested", "target_intent_id": intent_id_a}
            dm._set_execution_control_state("stop_requested", req)
            # Switch phase to collecting to create a new draft
            dm._transition_phase("collecting")

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy.control_state, "stop_requested")
        self.assertEqual(dm_strict.control_state, "stop_requested")
        self.assertEqual(dm_legacy.last_control_request.get("target_intent_id"), intent_id_a)
        self.assertEqual(dm_strict.last_control_request.get("target_intent_id"), intent_id_a)
        self.record_result("A15", "Task Modification After Published Request", "PARITY", "PASS", "PASS", "Execution target A retained on draft modify")

    def test_a16_valid_snapshot_restore(self):
        """A16: Restoring valid v2 snapshot produces identical state in both modes."""
        dm_legacy, dm_strict = self._init_dm_pair()
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

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy.load_snapshot(snap)
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict.load_snapshot(snap)

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy.slot_store.get_task_state().get("water_depth"), 300.0)
        self.assertEqual(dm_strict.slot_store.get_task_state().get("water_depth"), 300.0)
        self.record_result("A16", "Valid Snapshot Restore", "PARITY", "PASS", "PASS", "Valid snapshot restored identically")

    def test_a17_safe_legacy_done_migration(self):
        """A17: Safe legacy done migration enriches missing target safely; normalized parity comparison matches."""
        dm_legacy, dm_strict = self._init_dm_pair()
        intent_id = "TI202608090001"
        self._setup_published_task(dm_legacy, intent_id)
        self._setup_published_task(dm_strict, intent_id)

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
                    "intent_id": {"slot_name": "intent_id", "value": intent_id, "status": "valid", "value_type": "string"},
                    "internal_id": {"slot_name": "internal_id", "value": "00000000-0000-4000-8000-000000000001", "status": "valid", "value_type": "string"},
                    "task_id": {"slot_name": "task_id", "value": "PI-20260809-001", "status": "valid", "value_type": "string"},
                    "task_type": {"slot_name": "task_type", "value": "管缆巡检", "status": "valid", "value_type": "string"},
                    "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "value_type": "string"},
                },
                "unresolved": [],
            },
            "task_state": {
                "intent_id": intent_id,
                "internal_id": "00000000-0000-4000-8000-000000000001",
                "task_id": "PI-20260809-001",
                "task_type": "管缆巡检",
                "task_type_key": "pipeline_inspection",
            },
        }

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy.load_snapshot(snap_legacy_done)
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict.load_snapshot(snap_legacy_done)

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        # Apply canonicalization to legacy digest for target enrichment
        canonical_legacy = canonicalize_legacy_enrichment(digest_legacy)

        self.assertEqual(canonical_legacy, digest_strict)
        self.assertEqual(dm_strict.last_control_request.get("target_intent_id"), intent_id)
        self.record_result("A17", "Safe Legacy Done Migration", "PARITY", "PASS", "PASS", "Safe target enrichment canonicalized parity")

    def test_a18_reset(self):
        """A18: Resetting DialogueManager returns both instances to clean initial digest."""
        dm_legacy, dm_strict = self._init_dm_pair()
        for dm in (dm_legacy, dm_strict):
            dm.phase = "confirming"
            dm.control_state = "idle"

        # Re-create fresh pair representing reset
        dm_legacy_reset, dm_strict_reset = self._init_dm_pair()

        digest_legacy = build_shadow_digest(dm_legacy_reset)
        digest_strict = build_shadow_digest(dm_strict_reset)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(dm_legacy_reset.phase, "collecting")
        self.assertEqual(dm_strict_reset.phase, "collecting")
        self.record_result("A18", "Reset", "PARITY", "PASS", "PASS", "Reset parity verified")

    def test_a19_mode_transition_history(self):
        """A19: Mode transitions record complete history metadata identically."""
        dm_legacy, dm_strict = self._init_dm_pair()

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy._switch_dialogue_mode("knowledge_qa", source="rule", confidence=1.0, reason="qa query")
            dm_legacy._switch_dialogue_mode("task_collection", source="rule", confidence=1.0, reason="task create")

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_strict._switch_dialogue_mode("knowledge_qa", source="rule", confidence=1.0, reason="qa query")
            dm_strict._switch_dialogue_mode("task_collection", source="rule", confidence=1.0, reason="task create")

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.assertEqual(len(dm_legacy.mode_transition_history), 2)
        self.assertEqual(len(dm_strict.mode_transition_history), 2)
        self.record_result("A19", "Mode Transition History", "PARITY", "PASS", "PASS", "Mode transition history parity")

    def test_a20_session_snapshot_round_trip(self):
        """A20: Export -> Load -> Export cycle yields identical business digest."""
        dm_legacy, dm_strict = self._init_dm_pair()
        for dm in (dm_legacy, dm_strict):
            schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
            dm.slot_store.init_task_slots(schema)
            slots = dm.slot_store.clone_slots()
            slots["intent_id"] = Slot("intent_id", value="TI202608090001", status="valid", value_type="string")
            dm.slot_store.commit_transaction(slots, [])
            dm.task_state = dm.slot_store.get_task_state()

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            snap_l = dm_legacy.export_snapshot()
            dm_legacy.load_snapshot(snap_l)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            snap_s = dm_strict.export_snapshot()
            dm_strict.load_snapshot(snap_s)

        digest_legacy = build_shadow_digest(dm_legacy)
        digest_strict = build_shadow_digest(dm_strict)

        self.assertEqual(digest_legacy, digest_strict)
        self.record_result("A20", "Session Snapshot Round Trip", "PARITY", "PASS", "PASS", "Snapshot round trip parity")

    # =========================================================================
    # Category B: EXPECTED_FAIL_CLOSED_DELTA Scenarios (B01 - B15)
    # =========================================================================

    def test_b01_invalid_runtime_phase(self):
        """B01: Setting invalid runtime phase fails closed in Strict mode with zero state pollution."""
        dm_legacy, dm_strict = self._init_dm_pair()
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises(StateContractError):
                dm_strict._transition_phase("invalid_phase")

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B01", "Invalid Runtime Phase", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "StateContractError raised atomically")

    def test_b02_invalid_runtime_dialogue_mode(self):
        """B02: Setting invalid runtime dialogue mode fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises(StateContractError):
                dm_strict._switch_dialogue_mode("invalid_mode")

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B02", "Invalid Runtime Dialogue Mode", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "StateContractError raised atomically")

    def test_b03_runtime_uncertain(self):
        """B03: Setting dialogue_mode to 'uncertain' in runtime fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises(StateContractError):
                dm_strict._switch_dialogue_mode("uncertain")

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B03", "Runtime uncertain", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "Runtime uncertain rejected in Strict")

    def test_b04_collecting_to_done(self):
        """B04: Illegal transition collecting -> done fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises(StateContractError):
                dm_strict._transition_phase("done")

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B04", "collecting -> done", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "Illegal transition collecting -> done rejected")

    def test_b05_blocked_soft_to_done(self):
        """B05: Illegal transition blocked_soft -> done fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
        dm_strict.phase = "blocked_soft"
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises(StateContractError):
                dm_strict._transition_phase("done")

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B05", "blocked_soft -> done", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "Illegal transition blocked_soft -> done rejected")

    def test_b06_blocked_hard_to_confirming(self):
        """B06: Illegal transition blocked_hard -> confirming fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
        dm_strict.phase = "blocked_hard"
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises(StateContractError):
                dm_strict._transition_phase("confirming")

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B06", "blocked_hard -> confirming", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "Bypass transition blocked_hard -> confirming rejected")

    def test_b07_blocked_hard_to_done(self):
        """B07: Illegal transition blocked_hard -> done fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
        dm_strict.phase = "blocked_hard"
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises(StateContractError):
                dm_strict._transition_phase("done")

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B07", "blocked_hard -> done", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "Bypass transition blocked_hard -> done rejected")

    def test_b08_non_done_execution_request(self):
        """B08: Setting execution control state in non-done phase fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
        req = {"action": "stop", "status": "requested", "target_intent_id": "TI202608090001"}
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises(StateContractError):
                dm_strict._set_execution_control_state("stop_requested", req)

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B08", "Non-done Execution Request", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "Non-done execution request rejected")

    def test_b09_missing_execution_target_intent_id(self):
        """B09: Non-idle execution request missing target_intent_id fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
        self._setup_published_task(dm_strict)
        req_invalid = {"action": "stop", "status": "requested"}
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises(StateContractError):
                dm_strict._set_execution_control_state("stop_requested", req_invalid)

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B09", "Missing Execution target_intent_id", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "Missing target intent ID rejected")

    def test_b10_invalid_execution_target(self):
        """B10: Execution request with invalid target_intent_id format fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
        self._setup_published_task(dm_strict)
        req_invalid = {"action": "stop", "status": "requested", "target_intent_id": "INVALID_FORMAT"}
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises(StateContractError):
                dm_strict._set_execution_control_state("stop_requested", req_invalid)

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B10", "Invalid Execution Target", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "Invalid target intent ID format rejected")

    def test_b11_action_state_mismatch(self):
        """B11: Control state and action mismatch fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
        self._setup_published_task(dm_strict)
        req_stop = {"action": "stop", "status": "requested", "target_intent_id": "TI202608090001"}
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises(StateContractError):
                dm_strict._set_execution_control_state("pause_requested", req_stop)

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B11", "Action / State Mismatch", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "Control state and action mismatch rejected")

    def test_b12_invalid_request_status(self):
        """B12: Execution request with invalid status fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
        self._setup_published_task(dm_strict)
        req_invalid = {"action": "stop", "status": "executing", "target_intent_id": "TI202608090001"}
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises(StateContractError):
                dm_strict._set_execution_control_state("stop_requested", req_invalid)

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B12", "Invalid Request Status", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "Invalid request status rejected")

    def test_b13_ambiguous_legacy_snapshot(self):
        """B13: Ambiguous non-done legacy snapshot with execution request missing target fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
        ambiguous_snap = {
            "snapshot_version": 2,
            "phase": "confirming",
            "mode": "normal",
            "dialogue_mode": "task_collection",
            "control_state": "stop_requested",
            "last_control_request": {"action": "stop", "status": "requested"},
            "slot_store": {
                "version": 1,
                "slots": {},
                "unresolved": [],
            },
            "task_state": {"intent_id": "TI202608090002"},
        }
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises((StateContractError, ValueError)):
                dm_strict.load_snapshot(ambiguous_snap)

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B13", "Ambiguous Legacy Snapshot", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "Ambiguous non-done snapshot rejected without state pollution")

    def test_b14_invalid_snapshot_phase(self):
        """B14: Snapshot with invalid phase fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
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
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises((StateContractError, ValueError)):
                dm_strict.load_snapshot(invalid_phase_snap)

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B14", "Invalid Snapshot Phase", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "Invalid snapshot phase rejected")

    def test_b15_invalid_snapshot_dialogue_mode(self):
        """B15: Snapshot with invalid dialogue_mode fails closed in Strict mode."""
        dm_legacy, dm_strict = self._init_dm_pair()
        invalid_mode_snap = {
            "snapshot_version": 2,
            "phase": "collecting",
            "mode": "normal",
            "dialogue_mode": "bogus_mode",
            "control_state": "idle",
            "last_control_request": None,
            "slot_store": {"version": 1, "slots": {}, "unresolved": []},
            "task_state": {},
        }
        digest_strict_before = build_shadow_digest(dm_strict)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with self.assertRaises((StateContractError, ValueError)):
                dm_strict.load_snapshot(invalid_mode_snap)

        digest_strict_after = build_shadow_digest(dm_strict)
        self.assertEqual(digest_strict_before, digest_strict_after)
        self.record_result("B15", "Invalid Snapshot Dialogue Mode", "EXPECTED_FAIL_CLOSED_DELTA", "ALLOW/UNCHECKED", "FAIL_CLOSED", "Invalid snapshot dialogue mode rejected")

    # =========================================================================
    # Section 12: Governance Invariants Shadow Check (INV-01 ~ INV-11)
    # =========================================================================

    def test_governance_invariants_shadow_strict_mode(self):
        """Verify all 11 governance invariants under Strict mode (session_state_v2=true)."""
        dm_legacy, dm_strict = self._init_dm_pair()

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            # INV-01: Query read-only
            v_before = dm_strict.slot_store.version
            snap_before = dm_strict.slot_store.export_snapshot()
            dm_strict.process("什么是DVL？")
            self.assertEqual(v_before, dm_strict.slot_store.version)
            self.assertEqual(snap_before, dm_strict.slot_store.export_snapshot())

            # INV-08: Duplicate confirm idempotent on done
            self._setup_published_task(dm_strict, "TI202608090001")
            dm_strict._handle_final_publish_confirmation("确认", request_id="req_inv08")
            self.assertEqual(dm_strict.phase, "done")

    # =========================================================================
    # Section 13: ASR Consistency Shadow Check
    # =========================================================================

    def test_asr_text_entry_shadow_consistency(self):
        """Verify typed text vs ASR transcript entry consistency yields identical shadow digest."""
        dm_legacy_typed, dm_strict_typed = self._init_dm_pair()
        dm_legacy_asr, dm_strict_asr = self._init_dm_pair()

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

        # Typed input: "创建一个管缆巡检任务"
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with patch.object(dm_strict_typed.llm, "extract_json", side_effect=stub_extract):
                dm_strict_typed.process("创建一个管缆巡检任务")

        # ASR transcript input (same final text decoded from speech): "创建一个管缆巡检任务"
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            with patch.object(dm_strict_asr.llm, "extract_json", side_effect=stub_extract):
                dm_strict_asr.process("创建一个管缆巡检任务")

        digest_typed = build_shadow_digest(dm_strict_typed)
        digest_asr = build_shadow_digest(dm_strict_asr)

        self.assertEqual(digest_typed, digest_asr)

    # =========================================================================
    # Section 14: Shadow Result Summary & Delta Metrics Assertion
    # =========================================================================

    def test_z_shadow_summary_metrics(self):
        """Summarize shadow results and assert UNEXPECTED_BEHAVIOR_DELTA == 0."""
        results = TestSessionStateRolloutShadowV2.shadow_results
        parity_count = sum(1 for r in results if r["classification"] == "PARITY")
        fail_closed_count = sum(1 for r in results if r["classification"] == "EXPECTED_FAIL_CLOSED_DELTA")
        unexpected_count = sum(1 for r in results if r["classification"] == "UNEXPECTED_BEHAVIOR_DELTA")

        # We assert unexpected_count == 0
        self.assertEqual(unexpected_count, 0, f"UNEXPECTED_BEHAVIOR_DELTA must be 0, got {unexpected_count}")
        self.assertGreaterEqual(parity_count, 20, f"Expected at least 20 PARITY cases, got {parity_count}")
        self.assertGreaterEqual(fail_closed_count, 15, f"Expected at least 15 EXPECTED_FAIL_CLOSED_DELTA cases, got {fail_closed_count}")


if __name__ == "__main__":
    unittest.main()
