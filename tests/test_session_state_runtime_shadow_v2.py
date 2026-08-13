"""
tests/test_session_state_runtime_shadow_v2.py

SEAgent G4.2 SessionState V2 Runtime Shadow Instrumentation Test Suite.

Verifies the 15 required runtime shadow criteria + strict log privacy sanitization:
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
12. request_id appears in Shadow audit metadata.
13. Shadow produces 0 SlotStore writes.
14. Shadow produces 0 TaskIntent files.
15. session_state_v2 remains false throughout tests.
16. Log Privacy: MISMATCH log contains zero intent/task IDs, zero result.details, zero raw snapshot data.
17. Log Privacy: Comparator exception log contains exception_type but zero exception message/err text.
"""

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.model_profile import is_session_state_v2_enabled
from src.session_state_shadow import compare_session_state_shadow
from src.slot_store import Slot
from src.validator import ValidationResult
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)


def _make_dm(tmp_dir: Path, session_id: str | None = "sess_test_shadow") -> DialogueManager:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    state_file = tmp_dir / "state.yaml"
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.state_file = state_file
    llm = ScriptedLLM(default_reply="测试回复")
    return DialogueManager(llm, kb, session_id=session_id)


def _task_state_digest(dm: DialogueManager) -> dict:
    return {
        "slot_store": copy.deepcopy(dm.slot_store.export_snapshot()),
        "task_state": copy.deepcopy(dm.task_state),
        "last_built_json": copy.deepcopy(dm._last_built_json),
        "last_missing": copy.deepcopy(dm._last_missing),
        "phase": dm.phase,
        "mode": dm.mode,
    }


def _process_scripted_read(
    dm: DialogueManager,
    user_message: str,
    *,
    request_id: str,
    query_intent: str = "KNOWLEDGE_QA",
) -> str:
    before = _task_state_digest(dm)
    classify_count = len(dm.llm.classify_calls)
    extract_count = len(dm.llm.extract_calls)
    dm.llm.queue_plan(make_plan("READ", query_intent=query_intent))

    reply = dm.process(user_message, request_id=request_id)

    assert _task_state_digest(dm) == before
    assert len(dm.llm.classify_calls) == classify_count + 1
    assert len(dm.llm.extract_calls) == extract_count
    return reply


def _queue_pipeline_write(dm: DialogueManager, *, water_depth: float = 300.0) -> None:
    dm.llm.queue_plan(make_plan("WRITE"))
    dm.llm.queue_extraction(
        extraction_result(
            slot_candidate(
                "task_type_key",
                "pipeline_inspection",
                raw_key="任务类型",
                raw_value="管缆巡检",
            )
        )
    )
    dm.llm.queue_extraction(
        extraction_result(
            slot_candidate(
                "water_depth",
                water_depth,
                raw_key="作业水深",
                raw_value=f"{water_depth:g}米",
            )
        )
    )


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
    slots["equipment_unit_id"] = Slot("equipment_unit_id", value="OBSROV-75-001", status="valid", value_type="string")
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
        "time": {
            "start": "2026-08-10T10:00:00+08:00",
            "end": "2026-08-10T11:00:00+08:00",
        },
        "location": {"oilfield": "A区", "water_depth_m": 300.0},
        "task": {"type": "pipeline_inspection", "details": {}},
        "equipment": {
            "robot_type": "observation_rov",
            "payload": [],
            "support_vessel": {"name": "Vessel1", "latitude": None, "longitude": None},
        },
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
            reply = _process_scripted_read(dm, "什么是DVL？", request_id="req_t01")
            self.assertTrue(reply)
            mock_comp.assert_not_called()

    # 2. shadow_compare=true -> Valid initial/task state returns PARITY.
    def test_02_shadow_enabled_valid_initial_state_returns_parity(self):
        dm = _make_dm(self.tmp_path / "t02")
        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True):
            snap = dm.export_snapshot()
            res = compare_session_state_shadow(snap, checkpoint="process", request_id="req_t02")
            self.assertEqual(res.classification, "PARITY")
            self.assertEqual(res.request_id, "req_t02")

    # 3. Knowledge QA with Shadow enabled -> reply and state unchanged.
    def test_03_knowledge_qa_shadow_enabled_preserves_reply_and_state(self):
        dm = _make_dm(self.tmp_path / "t03")
        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True):
            reply = _process_scripted_read(dm, "什么是DVL？", request_id="req_t03")
            self.assertTrue(isinstance(reply, str) and len(reply) > 0)

    # 4. Task creation with Shadow enabled -> exactly 0 additional LLM execution compared to disabled.
    def test_04_task_creation_shadow_enabled_no_double_execution(self):
        dm_disabled = _make_dm(self.tmp_path / "t04_disabled")
        dm_enabled = _make_dm(self.tmp_path / "t04_enabled")

        v_disabled_before = dm_disabled.slot_store.version
        v_enabled_before = dm_enabled.slot_store.version
        _queue_pipeline_write(dm_disabled)
        _queue_pipeline_write(dm_enabled)

        with patch("src.dialogue_manager.is_shadow_compare_enabled", return_value=False):
            dm_disabled.process("创建一个管缆巡检任务", request_id="req_t04_dis")
            count_disabled = (
                len(dm_disabled.llm.classify_calls),
                len(dm_disabled.llm.extract_calls),
                len(dm_disabled.llm.chat_calls),
            )

        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True):
            dm_enabled.process("创建一个管缆巡检任务", request_id="req_t04_en")
            count_enabled = (
                len(dm_enabled.llm.classify_calls),
                len(dm_enabled.llm.extract_calls),
                len(dm_enabled.llm.chat_calls),
            )

        self.assertEqual(count_disabled, (1, 2, 1))
        self.assertEqual(count_enabled, count_disabled)
        for dm, version_before in (
            (dm_disabled, v_disabled_before),
            (dm_enabled, v_enabled_before),
        ):
            state = dm.slot_store.get_task_state()
            self.assertGreater(dm.slot_store.version, version_before)
            self.assertEqual(state.get("task_type_key"), "pipeline_inspection")
            self.assertEqual(state.get("water_depth"), 300.0)

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
            slots["equipment_unit_id"] = Slot("equipment_unit_id", value="OBSROV-75-001", status="valid", value_type="string")
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
        dm.llm.queue_plan(make_plan("CONTROL", emergency_action="stop"))
        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True), \
             patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            _helper_setup_published_task(dm, task_dir, "TI202608100001")
            dm.process("停止当前任务", request_id="req_t06")
            self.assertEqual(dm.control_state, "stop_requested")
            self.assertEqual(dm.last_control_request["action"], "stop")
            self.assertEqual(dm.last_control_request["target_intent_id"], "TI202608100001")
            self.assertEqual(len(dm.llm.classify_calls), 1)
            self.assertEqual(len(dm.llm.extract_calls), 0)
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
        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True):
            dm.load_snapshot(snap)
            res = compare_session_state_shadow(dm.export_snapshot(), checkpoint="load_snapshot")
            self.assertEqual(res.classification, "PARITY")

    # 8. Post-reset Shadow returns PARITY.
    def test_08_post_reset_shadow_returns_parity(self):
        dm = _make_dm(self.tmp_path / "t08")
        dm.phase = "confirming"
        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True):
            dm.reset()
            self.assertEqual(dm.phase, "collecting")
            res = compare_session_state_shadow(dm.export_snapshot(), checkpoint="reset")
            self.assertEqual(res.classification, "PARITY")

    # 9. Manually constructed invalid Legacy state -> Shadow records STRICT_REJECTED, main business succeeds, memory unmodified.
    def test_09_invalid_legacy_state_shadow_records_strict_rejected_without_exception(self):
        dm = _make_dm(self.tmp_path / "t09")
        dm.phase = "invalid_phase_xyz"

        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True), \
             patch("src.dialogue_manager.logger.warning") as mock_warn:
            reply = _process_scripted_read(dm, "什么是DVL？", request_id="req_t09")
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
        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True), \
             patch("src.dialogue_manager.compare_session_state_shadow", side_effect=RuntimeError("Simulated shadow crash")), \
             patch("src.dialogue_manager.logger.warning") as mock_warn:
            reply = _process_scripted_read(dm, "什么是DVL？", request_id="req_t11")
            self.assertTrue(isinstance(reply, str) and len(reply) > 0)
            err_calls = [str(call) for call in mock_warn.call_args_list if "[SESSION_STATE_SHADOW_ERROR]" in str(call)]
            self.assertTrue(len(err_calls) > 0)

    # 12. request_id appears in Shadow audit metadata.
    def test_12_request_id_in_shadow_audit_metadata(self):
        dm = _make_dm(self.tmp_path / "t12")
        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True), \
             patch("src.dialogue_manager.logger.info") as mock_info:
            _process_scripted_read(dm, "什么是DVL？", request_id="req_audit_999")
            info_calls = [str(call) for call in mock_info.call_args_list if "[SESSION_STATE_SHADOW_PARITY]" in str(call)]
            self.assertTrue(len(info_calls) > 0)
            self.assertIn("req_audit_999", info_calls[0])

    # 13. Shadow produces 0 SlotStore writes.
    def test_13_shadow_produces_zero_slotstore_writes(self):
        dm = _make_dm(self.tmp_path / "t13")
        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True):
            v_before = dm.slot_store.version
            _process_scripted_read(dm, "什么是DVL？", request_id="req_t13")
            v_after = dm.slot_store.version
            self.assertEqual(v_before, v_after)

    # 14. Shadow produces 0 TaskIntent files.
    def test_14_shadow_produces_zero_task_intent_files(self):
        dm = _make_dm(self.tmp_path / "t14")
        task_dir = self.tmp_path / "t14_tasks"
        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True), \
             patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            _process_scripted_read(dm, "什么是DVL？", request_id="req_t14")
            files = list(task_dir.glob("*.json")) if task_dir.exists() else []
            self.assertEqual(len(files), 0)

    # 15. session_state_v2 remains false throughout tests.
    def test_15_session_state_v2_remains_false(self):
        self.assertFalse(is_session_state_v2_enabled())
        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True):
            dm = _make_dm(self.tmp_path / "t15")
            _process_scripted_read(dm, "什么是DVL？", request_id="req_t15")
            self.assertFalse(is_session_state_v2_enabled())

    # 16. Log Privacy: MISMATCH log contains zero intent/task IDs, zero result.details, zero raw snapshot.
    def test_16_log_privacy_mismatch_sanitization(self):
        dm = _make_dm(self.tmp_path / "t16")
        task_dir = self.tmp_path / "t16_tasks"
        _helper_setup_published_task(dm, task_dir, "TI202608109999")

        sensitive_intent_id = "TI202608109999"
        sensitive_task_id = "PI-20260810-001"

        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True), \
             patch("src.session_state_shadow.session_state_to_legacy_fields") as mock_v2_fields, \
             patch("src.dialogue_manager.logger.warning") as mock_warn:

            mock_v2_fields.return_value = {
                "snapshot_version": 2,
                "phase": "confirming",  # Mismatch with dm.phase ("done")
                "mode": "normal",
                "dialogue_mode": "task_collection",
                "control_state": "idle",
                "last_control_request": None,
            }

            dm._run_session_state_shadow_check(checkpoint="process", request_id="req_priv_01")

            rendered_calls = [call.args[0] % call.args[1:] for call in mock_warn.call_args_list if "[SESSION_STATE_SHADOW_MISMATCH]" in call.args[0]]
            self.assertTrue(len(rendered_calls) > 0, "Expected MISMATCH warning log")
            log_str = rendered_calls[0]

            # Assert required log fields present
            self.assertIn("checkpoint=process", log_str)
            self.assertIn("request_id=req_priv_01", log_str)
            self.assertIn("diff_fields=", log_str)

            # Assert NO task/intent ID actual values or un-sanitized details
            self.assertNotIn(sensitive_intent_id, log_str)
            self.assertNotIn(sensitive_task_id, log_str)
            self.assertNotIn("details=", log_str)
            self.assertNotIn("snapshot=", log_str)
            self.assertNotIn("task_state=", log_str)

    # 17. Log Privacy: Comparator exception log contains exception_type but zero exception message/err text.
    def test_17_log_privacy_exception_sanitization(self):
        dm = _make_dm(self.tmp_path / "t17")
        secret_msg = "Sensitive database credential secret_key_12345 leaked"

        with patch("src.dialogue_manager.should_run_session_state_shadow", return_value=True), \
             patch("src.dialogue_manager.compare_session_state_shadow", side_effect=RuntimeError(secret_msg)), \
             patch("src.dialogue_manager.logger.warning") as mock_warn:

            dm._run_session_state_shadow_check(checkpoint="process", request_id="req_priv_02")

            rendered_calls = [call.args[0] % call.args[1:] for call in mock_warn.call_args_list if "[SESSION_STATE_SHADOW_ERROR]" in call.args[0]]
            self.assertTrue(len(rendered_calls) > 0, "Expected ERROR warning log")
            log_str = rendered_calls[0]

            # Assert required log fields present
            self.assertIn("checkpoint=process", log_str)
            self.assertIn("request_id=req_priv_02", log_str)
            self.assertIn("exc_type=RuntimeError", log_str)

            # Assert NO exception message or err= format
            self.assertNotIn(secret_msg, log_str)
            self.assertNotIn("secret_key_12345", log_str)
            self.assertNotIn("err=", log_str)


if __name__ == "__main__":
    unittest.main()
