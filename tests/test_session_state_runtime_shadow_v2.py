"""
tests/test_session_state_runtime_shadow_v2.py

SEAgent G4.2 SessionState V2 Runtime Shadow Instrumentation Test Suite.

Verifies the 15 required runtime shadow criteria:
 1. shadow_compare=false -> Shadow does not execute at all.
 2. shadow_compare=true -> Valid initial/task state returns PARITY.
 3. Ordinary Knowledge QA with Shadow -> reply and state unchanged.
 4. Task creation with Shadow -> single business/LLM execution (0 double execution).
 5. Post-publish -> Shadow returns PARITY.
 6. Post-execution request -> Shadow returns PARITY.
 7. Post-snapshot restore -> Shadow returns PARITY.
 8. Post-reset -> Shadow returns PARITY.
 9. Manually constructed invalid Legacy state -> Shadow records STRICT_REJECTED, main business succeeds, memory unmodified.
10. Manually constructed projection mismatch -> MISMATCH recorded.
11. Shadow comparator exception -> main business completes successfully (fail-safe).
12. request_id appears in Shadow audit metadata logs.
13. Shadow produces 0 SlotStore writes.
14. Shadow produces 0 TaskIntent files.
15. session_state_v2 remains false throughout tests.
"""

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.model_profile import is_session_state_v2_enabled, is_shadow_compare_enabled
from src.session_state_shadow import compare_session_state_shadow, ShadowComparisonResult
from src.slot_store import Slot
from src.validator import ValidationResult


def _make_dm(tmp_dir: Path) -> DialogueManager:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    state_file = tmp_dir / "state.yaml"
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.state_file = state_file
    llm = LLMClient(None, None)
    return DialogueManager(llm, kb)


def _helper_setup_published_task(dm: DialogueManager, task_dir: Path, intent_id: str = "TI202608100001"):
    schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
    dm.slot_store.init_task_slots(schema)
    slots = dm.slot_store.clone_slots()
    slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid", value_type="string")
    slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid", value_type="string")
    slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", value_type="number")
    slots["location"] = Slot("location", value="A区", status="valid", value_type="string")
    slots["intent_id"] = Slot("intent_id", value=intent_id, status="valid", value_type="string")
    slots["internal_id"] = Slot("internal_id", value="00000000-0000-4000-8000-000000000001", status="valid", value_type="string")
    slots["task_id"] = Slot("task_id", value="PI-20260810-001", status="valid", value_type="string")
    slots["equipment_class"] = Slot("equipment_class", value="观察级ROV", status="valid", value_type="string")
    slots["equipment_type"] = Slot("equipment_type", value="观察级深海机器人", status="valid", value_type="string")
    slots["equipment_unit_id"] = Slot("equipment_unit_id", value="OBSROV--001", status="valid", value_type="string")
    dm.slot_store.commit_transaction(slots, [])
    dm.task_state = dm.slot_store.get_task_state()

    task_data = {
        "schema_version": 2,
        "intent_id": intent_id,
        "internal_id": "00000000-0000-4000-8000-000000000001",
        "task_id": "PI-20260810-001",
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


class TestSessionStateRuntimeShadowV2(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self._tmp)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    # 1. shadow_compare=false -> Shadow does not execute at all.
    def test_01_shadow_disabled_does_not_execute(self):
        dm = _make_dm(self.tmp_path / "t01")
        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=False), \
             patch("src.dialogue_manager.compare_session_state_shadow") as mock_comp:
            dm.process("什么是DVL？", request_id="req_t01")
            mock_comp.assert_not_called()

    # 2. shadow_compare=true -> Valid initial/task state returns PARITY.
    def test_02_shadow_enabled_valid_initial_state_returns_parity(self):
        dm = _make_dm(self.tmp_path / "t02")
        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=True):
            snap = dm.export_snapshot()
            res = compare_session_state_shadow(snap, checkpoint="process", request_id="req_t02")
            self.assertEqual(res.classification, "PARITY")
            self.assertEqual(res.request_id, "req_t02")

    # 3. Knowledge QA with Shadow enabled -> reply and state unchanged.
    def test_03_knowledge_qa_shadow_enabled_preserves_reply_and_state(self):
        dm = _make_dm(self.tmp_path / "t03")
        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=True):
            snap_before = dm.export_snapshot()
            reply = dm.process("什么是DVL？", request_id="req_t03")
            snap_after = dm.export_snapshot()
            self.assertTrue(isinstance(reply, str) and len(reply) > 0)
            self.assertEqual(snap_before["phase"], snap_after["phase"])
            self.assertEqual(snap_before["task_state"], snap_after["task_state"])

    # 4. Task creation with Shadow enabled -> exactly 0 additional LLM execution compared to disabled.
    def test_04_task_creation_shadow_enabled_no_double_execution(self):
        dm_disabled = _make_dm(self.tmp_path / "t04_disabled")
        dm_enabled = _make_dm(self.tmp_path / "t04_enabled")

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

        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=False):
            with patch.object(dm_disabled.llm, "extract_json", side_effect=stub_extract) as mock_llm_dis:
                dm_disabled.process("创建一个管缆巡检任务", request_id="req_t04_dis")
                count_disabled = mock_llm_dis.call_count

        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=True):
            with patch.object(dm_enabled.llm, "extract_json", side_effect=stub_extract) as mock_llm_en:
                dm_enabled.process("创建一个管缆巡检任务", request_id="req_t04_en")
                count_enabled = mock_llm_en.call_count

        self.assertEqual(count_enabled, count_disabled, f"Shadow enabled LLM calls ({count_enabled}) must equal disabled ({count_disabled})")
        self.assertEqual(dm_enabled.slot_store.get_task_state().get("task_type"), "管缆巡检")

    # 5. Post-publish Shadow returns PARITY.
    def test_05_post_publish_shadow_returns_parity(self):
        dm = _make_dm(self.tmp_path / "t05")
        task_dir = self.tmp_path / "t05_tasks"
        task_dir.mkdir(parents=True, exist_ok=True)
        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=True), \
             patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
            dm.slot_store.init_task_slots(schema)
            slots = dm.slot_store.clone_slots()
            slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid", value_type="string")
            slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid", value_type="string")
            slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", value_type="number")
            slots["location"] = Slot("location", value="A区", status="valid", value_type="string")
            slots["intent_id"] = Slot("intent_id", value="TI202608100001", status="valid", value_type="string")
            slots["internal_id"] = Slot("internal_id", value="00000000-0000-4000-8000-000000000001", status="valid", value_type="string")
            slots["task_id"] = Slot("task_id", value="PI-20260810-001", status="valid", value_type="string")
            slots["equipment_class"] = Slot("equipment_class", value="观察级ROV", status="valid", value_type="string")
            slots["equipment_type"] = Slot("equipment_type", value="观察级深海机器人", status="valid", value_type="string")
            slots["equipment_unit_id"] = Slot("equipment_unit_id", value="OBSROV--001", status="valid", value_type="string")
            dm.slot_store.commit_transaction(slots, [])
            dm.task_state = dm.slot_store.get_task_state()
            dm.phase = "confirming"

            mock_val_res = ValidationResult(
                overall_status="valid",
                validated_at="2026-08-10 12:00:00",
                task_version=1,
                validation_version=1,
                validation_fingerprint="fp_test",
                state_snapshot=None,
                violations=[],
            )

            with patch.object(dm, "_refresh_validation", return_value=mock_val_res), \
                 patch.object(dm.kb.state_info, "check_runtime_availability", return_value={"available": True}), \
                 patch.object(dm.slot_store, "get_missing_slots", return_value=[]):
                dm.process("确认发布", request_id="req_t05")
                self.assertEqual(dm.phase, "done")

            res = compare_session_state_shadow(dm.export_snapshot(), checkpoint="process", request_id="req_t05")
            self.assertEqual(res.classification, "PARITY")

    # 6. Post-execution request Shadow returns PARITY.
    def test_06_post_execution_request_shadow_returns_parity(self):
        dm = _make_dm(self.tmp_path / "t06")
        task_dir = self.tmp_path / "t06_tasks"
        task_dir.mkdir(parents=True, exist_ok=True)
        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=True), \
             patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            _helper_setup_published_task(dm, task_dir, "TI202608100001")
            dm.process("停止当前任务", request_id="req_t06")
            res = compare_session_state_shadow(dm.export_snapshot(), checkpoint="process", request_id="req_t06")
            self.assertEqual(res.classification, "PARITY")

    # 7. Post-snapshot restore Shadow returns PARITY.
    def test_07_post_snapshot_restore_shadow_returns_parity(self):
        dm = _make_dm(self.tmp_path / "t07")
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
                    "intent_id": {"slot_name": "intent_id", "value": "TI202608100001", "status": "valid", "value_type": "string"},
                    "task_type": {"slot_name": "task_type", "value": "管缆巡检", "status": "valid", "value_type": "string"},
                    "water_depth": {"slot_name": "water_depth", "value": 300.0, "status": "valid", "value_type": "number"},
                },
                "unresolved": [],
            },
            "task_state": {"intent_id": "TI202608100001", "task_type": "管缆巡检", "water_depth": 300.0},
        }
        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=True):
            dm.load_snapshot(snap)
            res = compare_session_state_shadow(dm.export_snapshot(), checkpoint="load_snapshot")
            self.assertEqual(res.classification, "PARITY")

    # 8. Post-reset Shadow returns PARITY.
    def test_08_post_reset_shadow_returns_parity(self):
        dm = _make_dm(self.tmp_path / "t08")
        dm.phase = "confirming"
        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=True):
            dm.reset()
            self.assertEqual(dm.phase, "collecting")
            res = compare_session_state_shadow(dm.export_snapshot(), checkpoint="reset")
            self.assertEqual(res.classification, "PARITY")

    # 9. Manually constructed invalid Legacy state -> Shadow records STRICT_REJECTED, main business succeeds, memory unmodified.
    def test_09_invalid_legacy_state_shadow_records_strict_rejected_without_exception(self):
        dm = _make_dm(self.tmp_path / "t09")
        dm.phase = "invalid_phase_xyz"

        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=True), \
             patch("src.dialogue_manager.logger.warning") as mock_warn:
            reply = dm.process("什么是DVL？", request_id="req_t09")
            self.assertTrue(isinstance(reply, str) and len(reply) > 0)
            self.assertEqual(dm.phase, "invalid_phase_xyz")

            warn_calls = [str(call) for call in mock_warn.call_args_list if "[SESSION_STATE_SHADOW_STRICT_REJECTED]" in str(call)]
            self.assertTrue(len(warn_calls) > 0, f"Expected STRICT_REJECTED warning, got {mock_warn.call_args_list}")

    # 10. Manually constructed projection mismatch -> MISMATCH recorded.
    def test_10_projection_mismatch_shadow_records_mismatch(self):
        snap_mismatch = {
            "snapshot_version": 2,
            "phase": "collecting",
            "mode": "normal",
            "dialogue_mode": "task_collection",
            "control_state": "idle",
            "last_control_request": None,
        }
        with patch("src.session_state_shadow.session_state_to_legacy_fields") as mock_v2_fields:
            mock_v2_fields.return_value = {
                "snapshot_version": 2,
                "phase": "confirming",
                "mode": "normal",
                "dialogue_mode": "task_collection",
                "control_state": "idle",
                "last_control_request": None,
            }
            res = compare_session_state_shadow(snap_mismatch, checkpoint="process", request_id="req_t10")
            self.assertEqual(res.classification, "MISMATCH")
            self.assertIn("phase", res.diff_fields)

    # 11. Shadow comparator exception -> main business completes successfully (fail-safe).
    def test_11_shadow_comparator_exception_fail_safe(self):
        dm = _make_dm(self.tmp_path / "t11")
        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=True), \
             patch("src.dialogue_manager.compare_session_state_shadow", side_effect=RuntimeError("Simulated shadow crash")), \
             patch("src.dialogue_manager.logger.warning") as mock_warn:
            reply = dm.process("什么是DVL？", request_id="req_t11")
            self.assertTrue(isinstance(reply, str) and len(reply) > 0)
            err_calls = [str(call) for call in mock_warn.call_args_list if "[SESSION_STATE_SHADOW_ERROR]" in str(call)]
            self.assertTrue(len(err_calls) > 0)

    # 12. request_id appears in Shadow audit metadata.
    def test_12_request_id_in_shadow_audit_metadata(self):
        dm = _make_dm(self.tmp_path / "t12")
        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=True), \
             patch("src.dialogue_manager.logger.info") as mock_info:
            dm.process("什么是DVL？", request_id="req_audit_999")
            info_calls = [str(call) for call in mock_info.call_args_list if "[SESSION_STATE_SHADOW_PARITY]" in str(call)]
            self.assertTrue(len(info_calls) > 0)
            self.assertIn("req_audit_999", info_calls[0])

    # 13. Shadow produces 0 SlotStore writes.
    def test_13_shadow_produces_zero_slotstore_writes(self):
        dm = _make_dm(self.tmp_path / "t13")
        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=True):
            v_before = dm.slot_store.version
            dm.process("什么是DVL？", request_id="req_t13")
            v_after = dm.slot_store.version
            self.assertEqual(v_before, v_after)

    # 14. Shadow produces 0 TaskIntent files.
    def test_14_shadow_produces_zero_task_intent_files(self):
        dm = _make_dm(self.tmp_path / "t14")
        task_dir = self.tmp_path / "t14_tasks"
        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=True), \
             patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            dm.process("什么是DVL？", request_id="req_t14")
            files = list(task_dir.glob("*.json")) if task_dir.exists() else []
            self.assertEqual(len(files), 0)

    # 15. session_state_v2 remains false throughout tests.
    def test_15_session_state_v2_remains_false(self):
        self.assertFalse(is_session_state_v2_enabled())
        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=True):
            dm = _make_dm(self.tmp_path / "t15")
            dm.process("什么是DVL？", request_id="req_t15")
            self.assertFalse(is_session_state_v2_enabled())


if __name__ == "__main__":
    unittest.main()
