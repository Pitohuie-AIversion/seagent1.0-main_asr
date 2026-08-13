"""tests/test_issue_11_deterministic_task_id.py — Issue #11 确定性任务编号全闭环与合规性单元测试套件

本文件全面兼容 python -m unittest discover tests -v 命令（0 pytest 依赖）。
"""

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime
from multiprocessing import Process, Queue
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from src.dialogue_manager import DialogueManager
from src.exceptions import IdReservationError, TaskPersistenceError
from src.slot_store import SnapshotValidationError
from src import id_sequence
from src.id_sequence import (
    next_daily_task_id,
    validate_intent_id,
    validate_task_id,
    validate_task_id_for_task_type,
    validate_task_prefix,
)
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.output_builder import OutputBuilder
from src.result_paths import get_history_dir, get_task_dir
from src.simulated_time import (
    SimulatedTime,
    get_business_date,
    get_business_datetime,
    get_business_timezone,
    get_current_datetime,
    get_simulated_time,
)
from src.slot_store import Slot
from src.task_intent_builder import TaskIntentBuilder, validate_task_intent
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)


class FakeLLM(LLMClient):
    """测试调用方显式指定语义；替身不读取用户原句。"""

    _TASK_NAMES = {
        "pipeline_inspection": "管缆巡检",
        "pipeline_burial": "管缆埋设",
        "tree_valve_operation": "采油树控制面板插入",
    }

    def __init__(
        self,
        task_type_key: str = "pipeline_inspection",
        operation: str = "WRITE",
    ):
        self.llm = None
        self.tokenizer = None
        self.task_type_key = task_type_key
        self.operation = operation

    def chat(self, messages, **kwargs):
        return "已接收到您的任务请求"

    def filter_reply(self, text, *args, **kwargs):
        return text if isinstance(text, str) else "已接收到您的任务请求"

    def classify_interaction(self, messages, max_tokens=480, role=None):
        return make_plan(
            self.operation,
            query_intent="GENERAL_CHAT" if self.operation == "READ" else None,
        )

    def extract_json(self, prompt, schema=None, **kwargs):
        if self.operation != "WRITE":
            return {"slot_candidates": [], "list_mutations": [], "unresolved": []}
        task_name = self._TASK_NAMES[self.task_type_key]
        return {
            "slot_candidates": [
                {
                    "raw_key": "任务类型",
                    "canonical_key": "task_type_key",
                    "raw_value": task_name,
                    "normalized_value": self.task_type_key,
                    "confidence": 0.95,
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }


def create_dialogue_manager(
    task_type_key: str = "pipeline_inspection",
    operation: str = "WRITE",
):
    kb = KnowledgeBase()
    llm = FakeLLM(task_type_key=task_type_key, operation=operation)
    return DialogueManager(llm, kb)


def _task_type_candidate(task_type_key: str) -> dict:
    task_name = FakeLLM._TASK_NAMES[task_type_key]
    return slot_candidate(
        "task_type_key",
        task_type_key,
        raw_key="任务类型",
        raw_value=task_name,
    )


def _worker_reserve_task_id(result_queue, prefix, date_text, width, tmp_dir):
    try:
        os.environ["SEAGENT_RESULT_DIR"] = str(tmp_dir)
        task_dir = Path(tmp_dir) / "task"
        hist_dir = Path(tmp_dir) / "history"
        task_dir.mkdir(parents=True, exist_ok=True)
        hist_dir.mkdir(parents=True, exist_ok=True)

        tid = next_daily_task_id(
            prefix,
            date_text,
            width,
            [(task_dir, "task_id"), (hist_dir, "task_id")],
            allowed_prefixes=["PI", "PB", "CT"],
        )
        result_queue.put(tid)
    except Exception as exc:
        result_queue.put(exc)


class Issue11DeterministicTaskIdTest(unittest.TestCase):
    def setUp(self):
        self._sim_time_patcher = patch(
            "src.simulated_time._simulated_time",
            SimulatedTime(),
        )
        self._sim_time_patcher.start()
        self.addCleanup(self._sim_time_patcher.stop)
        self.tmp_dir_obj = tempfile.TemporaryDirectory()
        self.tmp_dir = self.tmp_dir_obj.name
        self.env_patcher = patch.dict(os.environ, {"SEAGENT_RESULT_DIR": self.tmp_dir})
        self.env_patcher.start()
        id_sequence._COUNTERS.clear()
        get_simulated_time().set_current_time(
            datetime(2026, 8, 3, 10, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )

    def tearDown(self):
        self.env_patcher.stop()
        self.tmp_dir_obj.cleanup()
        id_sequence._COUNTERS.clear()

    def test_01_all_task_templates_preserve_code(self):
        kb = KnowledgeBase()
        templates = kb.task_schemas.get("task_templates", {})
        self.assertIn("pipeline_inspection", templates)
        self.assertEqual(templates["pipeline_inspection"].get("code"), "PI")
        self.assertEqual(templates["pipeline_burial"].get("code"), "PB")
        self.assertEqual(templates["tree_valve_operation"].get("code"), "CT")

    def test_02_single_source_of_truth_for_prefixes(self):
        kb = KnowledgeBase()
        builder = OutputBuilder(kb)

        tid_pi = builder.reserve_task_id("pipeline_inspection")
        self.assertTrue(tid_pi.startswith("PI-"))

        tid_pb = builder.reserve_task_id("pipeline_burial")
        self.assertTrue(tid_pb.startswith("PB-"))

        tid_ct = builder.reserve_task_id("tree_valve_operation")
        self.assertTrue(tid_ct.startswith("CT-"))

    def test_03_sequential_ids_on_same_day(self):
        kb = KnowledgeBase()
        builder = OutputBuilder(kb)

        tid1 = builder.reserve_task_id("pipeline_inspection")
        tid2 = builder.reserve_task_id("pipeline_inspection")
        tid3 = builder.reserve_task_id("pipeline_inspection")

        self.assertEqual(tid1, "PI-20260803-001")
        self.assertEqual(tid2, "PI-20260803-002")
        self.assertEqual(tid3, "PI-20260803-003")

    def test_04_first_task_must_be_001(self):
        dm = create_dialogue_manager()
        dm.process("我要做管缆巡检")
        # 草稿阶段 task_id 存在于 dm.task_id_preview（UI 展示预览），不在 _last_built_json 或 task_state
        preview_id = dm.task_id_preview
        self.assertEqual(preview_id, "PI-20260803-001")
        self.assertNotIn("task_id", dm._last_built_json)
        self.assertIsNone(dm.task_state.get("task_id"))
        # SlotStore 中 task_id 状态为 candidate
        slot = dm.slot_store.slots.get("task_id")
        self.assertIsNotNone(slot)
        self.assertEqual(slot.status, "candidate")
        self.assertEqual(slot.candidate_value, "PI-20260803-001")

    def test_05_shared_global_daily_sequence_across_categories(self):
        kb = KnowledgeBase()
        builder = OutputBuilder(kb)

        tid1 = builder.reserve_task_id("pipeline_inspection")
        tid2 = builder.reserve_task_id("pipeline_burial")
        tid3 = builder.reserve_task_id("tree_valve_operation")

        self.assertEqual(tid1, "PI-20260803-001")
        self.assertEqual(tid2, "PB-20260803-002")
        self.assertEqual(tid3, "CT-20260803-003")

    def test_06_date_change_resets_sequence(self):
        kb = KnowledgeBase()
        builder = OutputBuilder(kb)

        tid1 = builder.reserve_task_id("pipeline_inspection")
        self.assertEqual(tid1, "PI-20260803-001")

        get_simulated_time().set_current_time(
            datetime(2026, 8, 4, 9, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )

        tid2 = builder.reserve_task_id("pipeline_inspection")
        self.assertEqual(tid2, "PI-20260804-001")

    def test_07_timezone_decoupling_between_business_date_and_intent_id(self):
        # Setting SEAGENT_TIMEZONE to Tokyo (+9h)
        with patch.dict(os.environ, {"SEAGENT_TIMEZONE": "Asia/Tokyo"}):
            self.assertEqual(get_business_timezone().key, "Asia/Tokyo")
            sys_dt = get_current_datetime()
            biz_dt = get_business_datetime()
            # System simulated datetime retains Asia/Shanghai timezone
            self.assertEqual(sys_dt.tzinfo.key, "Asia/Shanghai")
            # Business datetime uses Asia/Tokyo
            self.assertEqual(biz_dt.tzinfo.key, "Asia/Tokyo")

    def test_08_invalid_timezone_configuration(self):
        with patch.dict(os.environ, {"SEAGENT_TIMEZONE": "Invalid/Timezone_Name"}):
            with self.assertRaises(ValueError):
                get_business_timezone()

    def test_09_restart_recovery_from_persistence(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)

            dummy_file = tmp_task_dir / "task_PI-20260803-005.json"
            with open(dummy_file, "w", encoding="utf-8") as f:
                json.dump({"task_id": "PI-20260803-005"}, f)

            with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}):
                id_sequence._COUNTERS.clear()
                kb = KnowledgeBase()
                builder = OutputBuilder(kb)
                tid = builder.reserve_task_id("pipeline_inspection")
                self.assertEqual(tid, "PI-20260803-006")

    def test_10_multiprocessing_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            num_processes = 10
            queue = Queue()
            processes = []

            for _ in range(num_processes):
                p = Process(
                    target=_worker_reserve_task_id,
                    args=(queue, "PI", "20260803", 3, tmp_dir),
                )
                processes.append(p)
                p.start()

            for p in processes:
                p.join()

            results = []
            while not queue.empty():
                results.append(queue.get())

            self.assertEqual(len(results), num_processes)
            for res in results:
                self.assertIsInstance(res, str)

            self.assertEqual(len(set(results)), num_processes)

    def test_11a_counter_file_exists_deleting_file_does_not_reuse_seq(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)

            counter_file = Path(tmp_dir) / ".id_sequences.json"
            with open(counter_file, "w", encoding="utf-8") as f:
                json.dump({"TASK:20260803": 2}, f)

            file1 = tmp_task_dir / "task_PI-20260803-001.json"
            file2 = tmp_task_dir / "task_PI-20260803-002.json"
            with open(file1, "w", encoding="utf-8") as f:
                json.dump({"task_id": "PI-20260803-001"}, f)
            with open(file2, "w", encoding="utf-8") as f:
                json.dump({"task_id": "PI-20260803-002"}, f)

            # Delete file 001 from disk
            file1.unlink()

            with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}):
                id_sequence._COUNTERS.clear()
                kb = KnowledgeBase()
                builder = OutputBuilder(kb)
                tid = builder.reserve_task_id("pipeline_inspection")
                # Counter file specifies 2, so next ID is 003
                self.assertEqual(tid, "PI-20260803-003")

    def test_11b_counter_file_missing_disk_max_scan_recovers_sequence(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)

            file2 = tmp_task_dir / "task_PI-20260803-002.json"
            with open(file2, "w", encoding="utf-8") as f:
                json.dump({"task_id": "PI-20260803-002"}, f)

            # Counter file is missing
            counter_file = Path(tmp_dir) / ".id_sequences.json"
            if counter_file.exists():
                counter_file.unlink()

            with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}):
                id_sequence._COUNTERS.clear()
                kb = KnowledgeBase()
                builder = OutputBuilder(kb)
                tid = builder.reserve_task_id("pipeline_inspection")
                # Disk scan finds 002, so next ID recovers to 003
                self.assertEqual(tid, "PI-20260803-003")

    def test_12_corrupted_counter_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            counter_file = Path(tmp_dir) / ".id_sequences.json"
            with open(counter_file, "w", encoding="utf-8") as f:
                f.write("{corrupted_json_content:")

            with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}):
                id_sequence._COUNTERS.clear()
                with self.assertRaises(IdReservationError):
                    next_daily_task_id(
                        "PI",
                        "20260803",
                        3,
                        [(Path(tmp_dir), "task_id")],
                        allowed_prefixes=["PI"],
                    )

    def test_13_invalid_task_prefix_fails_closed(self):
        self.assertFalse(validate_task_prefix(""))
        self.assertFalse(validate_task_prefix("../invalid"))
        self.assertFalse(validate_task_prefix("PI/2026"))

        kb = KnowledgeBase()
        builder = OutputBuilder(kb)

        with self.assertRaises(IdReservationError):
            builder.reserve_task_id("non_existent_template")

    def test_14_prepare_without_task_id_fails_closed(self):
        kb = KnowledgeBase()
        ti_builder = TaskIntentBuilder(kb)

        task_state = {"water_depth": 500.0}
        built_json = {"start_time": "2026-08-03T10:00:00Z"}

        # prepare without valid task_id must raise TaskPersistenceError and NOT auto-reserve ID
        with self.assertRaises(TaskPersistenceError):
            ti_builder.prepare(task_state, built_json, "normal", "pipeline_inspection")

    def test_15_validate_task_id_for_task_type_prefix_consistency(self):
        kb = KnowledgeBase()
        schemas = kb.task_schemas

        self.assertTrue(validate_task_id_for_task_type("PI-20260803-001", "pipeline_inspection", schemas))
        self.assertTrue(validate_task_id_for_task_type("PB-20260803-001", "pipeline_burial", schemas))
        self.assertTrue(validate_task_id_for_task_type("CT-20260803-001", "tree_valve_operation", schemas))

        # Similar prefix hijacking attempts must be rejected
        self.assertFalse(validate_task_id_for_task_type("PIZ-20260803-001", "pipeline_inspection", schemas))
        self.assertFalse(validate_task_id_for_task_type("PI_FAKE-20260803-001", "pipeline_inspection", schemas))
        self.assertFalse(validate_task_id_for_task_type("PI-20260803-001", "pipeline_burial", schemas))
        self.assertFalse(validate_task_id_for_task_type("TI2026080301", "pipeline_inspection", schemas))
        self.assertFalse(validate_task_id_for_task_type("FAKE-20260803-001", "pipeline_inspection", schemas))

    def test_16_persist_and_publish_staging_verify_task_id_and_write_file(self):
        kb = KnowledgeBase()
        ti_builder = TaskIntentBuilder(kb)

        task_state = {
            "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "task_id": "PI-20260803-001",
            "task_type_key": "pipeline_inspection",
            "water_depth": 500.0,
            "oilfield_name": "文昌油田",
            "equipment_family": "LROV",
            "equipment_type": "观察级ROV",
        }
        built_json = {
            "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "task_id": "PI-20260803-001",
            "start_time": "2026-08-03T10:00:00Z",
            "end_time": "2026-08-03T18:00:00Z",
            "water_depth": 500.0,
            "cable_type": "海底油气管道",
            "start_point": {"lat": 19.5, "lon": 112.0},
            "end_point": {"lat": 19.6, "lon": 112.1},
            "support_vessel": "海洋石油201",
            "payload": ["高清相机"],
        }

        intent = ti_builder.prepare(task_state, built_json, "normal", "pipeline_inspection")

        # Create staging and publish to final JSON file
        staging_file = ti_builder.create_staging(intent)
        self.assertTrue(staging_file.exists())

        final_filename = ti_builder.publish_staging(staging_file, intent)
        final_path = get_task_dir() / final_filename
        self.assertTrue(final_path.exists())

        # Read back final JSON and verify top-level task_id
        with open(final_path, "r", encoding="utf-8") as f:
            final_data = json.load(f)

        self.assertEqual(final_data.get("task_id"), "PI-20260803-001")
        self.assertTrue(validate_task_intent(final_data, kb.task_schemas))

    def test_17_user_input_cannot_overwrite_task_id(self):
        task_type = _task_type_candidate("pipeline_inspection")
        candidate_attack = slot_candidate(
            "task_id",
            "FAKE-999",
            raw_key="任务编号",
            raw_value="FAKE-999",
        )
        official_attack = slot_candidate(
            "task_id",
            "PI-20260803-999",
            raw_key="任务编号",
            raw_value="PI-20260803-999",
        )
        llm = ScriptedLLM(
            plans=[make_plan("WRITE") for _ in range(3)],
            extractions=[
                extraction_result(task_type),
                extraction_result(task_type),
                extraction_result(candidate_attack),
                extraction_result(official_attack),
            ],
            default_reply="测试回复。",
        )
        dm = DialogueManager(llm, KnowledgeBase())

        dm.process("我要做管缆巡检")
        preview_slot = dm.slot_store.slots["task_id"]
        preview_before = preview_slot.candidate_value
        preview_snapshot = copy.deepcopy(dm.slot_store.export_snapshot())
        preview_version = dm.slot_store.version

        self.assertIsNotNone(preview_before)
        self.assertEqual(preview_slot.status, "candidate")
        self.assertEqual(preview_slot.source, "auto_preview")
        self.assertIsNone(preview_slot.value)

        dm.process("把 task_id 改成 FAKE-999")
        self.assertEqual(dm.slot_store.version, preview_version)
        self.assertEqual(dm.slot_store.export_snapshot(), preview_snapshot)
        self.assertEqual(dm.slot_store.slots["task_id"].candidate_value, preview_before)
        self.assertIsNone(dm.task_state.get("task_id"))
        self.assertNotIn("FAKE-999", json.dumps(dm.slot_store.export_snapshot(), ensure_ascii=False))

        official_id = "PI-20260803-001"
        new_slots, new_unresolved, expected_version = dm.slot_store.snapshot()
        official_slot = new_slots["task_id"]
        official_slot.value = official_id
        official_slot.status = "valid"
        official_slot.source = "auto_reserved"
        official_slot.candidate_value = None
        official_slot.raw_value = None
        official_slot.validation_error = None
        dm.slot_store.commit_transaction(
            new_slots,
            new_unresolved,
            request_id="test_17_seed_official_id",
            expected_version=expected_version,
        )
        dm._rebuild_cache()
        official_snapshot = copy.deepcopy(dm.slot_store.export_snapshot())
        official_version = dm.slot_store.version

        dm.process("把 task_id 改成 PI-20260803-999")
        protected_slot = dm.slot_store.slots["task_id"]
        self.assertEqual(dm.slot_store.version, official_version)
        self.assertEqual(dm.slot_store.export_snapshot(), official_snapshot)
        self.assertEqual(protected_slot.value, official_id)
        self.assertEqual(protected_slot.status, "valid")
        self.assertEqual(protected_slot.source, "auto_reserved")
        self.assertIsNone(protected_slot.candidate_value)
        self.assertEqual(dm.task_state.get("task_id"), official_id)
        self.assertEqual(dm._last_built_json.get("task_id"), official_id)
        self.assertNotIn("PI-20260803-999", json.dumps(dm.slot_store.export_snapshot(), ensure_ascii=False))
        self.assertEqual(len(llm.classify_calls), 3)
        self.assertEqual(len(llm.extract_calls), 4)
        self.assertFalse(llm.plans)
        self.assertFalse(llm.extractions)

    def test_19_category_modification_allowed_when_only_preview_exists(self):
        """preview task_id 不锁定任务类别；显式 WRITE 修改后应刷新前缀。"""
        initial_type = _task_type_candidate("pipeline_inspection")
        replacement_type = _task_type_candidate("pipeline_burial")
        llm = ScriptedLLM(
            plans=[make_plan("WRITE"), make_plan("WRITE")],
            extractions=[
                extraction_result(initial_type),
                extraction_result(initial_type),
                extraction_result(replacement_type),
            ],
            default_reply="测试回复。",
        )
        dm = DialogueManager(llm, KnowledgeBase())

        dm.process("新建管缆巡检任务")
        slot = dm.slot_store.slots["task_id"]
        preview_before = dm.task_id_preview
        self.assertEqual(slot.status, "candidate")
        self.assertEqual(slot.source, "auto_preview")
        self.assertIsNotNone(preview_before)
        self.assertTrue(preview_before.startswith("PI-"))
        self.assertNotIn("task_id", dm._last_built_json)

        version_before = dm.slot_store.version
        dm.process("改成管缆埋设任务")

        updated_type = dm.slot_store.slots["task_type_key"]
        updated_preview = dm.slot_store.slots["task_id"]
        self.assertGreater(dm.slot_store.version, version_before)
        self.assertEqual(updated_type.value, "pipeline_burial")
        self.assertEqual(updated_type.status, "valid")
        self.assertIsNone(updated_type.validation_error)
        self.assertEqual(updated_preview.status, "candidate")
        self.assertEqual(updated_preview.source, "auto_preview")
        self.assertIsNone(updated_preview.value)
        self.assertTrue(updated_preview.candidate_value.startswith("PB-"))
        self.assertEqual(dm.task_id_preview, updated_preview.candidate_value)
        self.assertNotEqual(dm.task_id_preview, preview_before)
        self.assertIsNone(dm.task_state.get("task_id"))
        self.assertEqual(dm.task_state.get("task_type_key"), "pipeline_burial")
        self.assertEqual(dm.task_state, dm.slot_store.get_task_state())
        self.assertEqual(len(llm.classify_calls), 2)
        self.assertEqual(len(llm.extract_calls), 3)
        self.assertFalse(llm.plans)
        self.assertFalse(llm.extractions)

    def test_21_whitelist_disk_scan_excludes_unregistered_filenames(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)

            # File with unregistered prefix FAKE and large sequence number 999
            fake_file = tmp_task_dir / "task_FAKE-20260803-999.json"
            with open(fake_file, "w", encoding="utf-8") as f:
                json.dump({"task_id": "FAKE-20260803-999"}, f)

            with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}):
                id_sequence._COUNTERS.clear()
                kb = KnowledgeBase()
                builder = OutputBuilder(kb)
                # Next daily task ID for pipeline_inspection should be 001 because FAKE is whitelisted out
                tid = builder.reserve_task_id("pipeline_inspection")
                self.assertEqual(tid, "PI-20260803-001")

    def test_22_intent_id_filenames_do_not_affect_task_sequence_scan(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)

            intent_file = tmp_task_dir / "task_intent_TI2026080399.json"
            with open(intent_file, "w", encoding="utf-8") as f:
                json.dump({"intent_id": "TI2026080399", "task_id": "PI-20260803-002"}, f)

            with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}):
                id_sequence._COUNTERS.clear()
                kb = KnowledgeBase()
                builder = OutputBuilder(kb)
                tid = builder.reserve_task_id("pipeline_inspection")
                self.assertEqual(tid, "PI-20260803-003")

    def test_23_general_chat_does_not_generate_task_id(self):
        llm = ScriptedLLM(
            plans=[
                make_plan("READ", query_intent="GENERAL_CHAT"),
                make_plan(
                    "READ",
                    query_intent="DEVICE_CAPABILITY",
                    subject_type="device",
                    subject_text="深海勇士号",
                    relation="capabilities",
                    source_policy="project_kb",
                ),
            ],
            replies=["今天天气确实不错。", "测试设备能力回复。"],
        )
        dm = DialogueManager(llm, KnowledgeBase())

        for message in ("今天天气不错。", "深海勇士号的最大作业水深是多少？"):
            version_before = dm.slot_store.version
            snapshot_before = copy.deepcopy(dm.slot_store.export_snapshot())
            unresolved_before = copy.deepcopy(dm.slot_store.unresolved)
            state_before = copy.deepcopy(dm.task_state)
            built_before = copy.deepcopy(dm._last_built_json)
            missing_before = copy.deepcopy(dm._last_missing)
            phase_before = dm.phase
            mode_before = dm.mode
            rov_before = copy.deepcopy(dm._pending_rov_candidates)
            extract_calls_before = len(llm.extract_calls)

            reply = dm.process(message)

            self.assertTrue(reply)
            self.assertEqual(dm.slot_store.version, version_before)
            self.assertEqual(dm.slot_store.export_snapshot(), snapshot_before)
            self.assertEqual(dm.slot_store.unresolved, unresolved_before)
            self.assertEqual(dm.task_state, state_before)
            self.assertEqual(dm._last_built_json, built_before)
            self.assertEqual(dm._last_missing, missing_before)
            self.assertEqual(dm.phase, phase_before)
            self.assertEqual(dm.mode, mode_before)
            self.assertEqual(dm._pending_rov_candidates, rov_before)
            self.assertEqual(len(llm.extract_calls), extract_calls_before)
            self.assertIsNone(dm.task_state.get("task_id"))

        self.assertEqual(len(llm.classify_calls), 2)
        self.assertEqual(len(llm.extract_calls), 0)
        self.assertFalse(llm.plans)

    def test_24_asr_input_task_id_behavior(self):
        import io
        import web_backend
        client = web_backend.app.test_client()

        kb = KnowledgeBase()
        task_type = _task_type_candidate("pipeline_burial")
        llm = ScriptedLLM(
            plans=[make_plan("WRITE")],
            extractions=[
                extraction_result(task_type),
                extraction_result(task_type),
            ],
            default_reply="已接收到您的任务请求。",
        )
        mock_asr = MagicMock()
        mock_asr.transcribe_file.return_value = {
            "text": "新建管缆埋设任务",
            "raw_text": "新建管缆埋设任务",
            "corrected_text": "新建管缆埋设任务",
            "language_hint": "Chinese",
            "device": "cpu",
            "elapsed_ms": 10,
            "segments": [],
            "entities": [],
        }
        session_id = "sess_asr_issue11"

        def clear_test_session():
            with web_backend._sessions_lock:
                web_backend._sessions_manager.pop(session_id, None)

        clear_test_session()
        try:
            with patch("web_backend._shared_asr", mock_asr), \
                 patch("web_backend._shared_kb", kb), \
                 patch("web_backend._shared_llm", llm):
                data_file = (io.BytesIO(b"dummy audio"), "test.wav")
                res_asr = client.post("/api/asr", data={"audio": data_file}, content_type="multipart/form-data")
                self.assertEqual(res_asr.status_code, 200)
                corrected_text = res_asr.get_json().get("corrected_text")
                self.assertEqual(corrected_text, "新建管缆埋设任务")
                mock_asr.transcribe_file.assert_called_once()

                res_chat = client.post("/api/chat", json={"session_id": session_id, "message": corrected_text})
                self.assertEqual(res_chat.status_code, 200)
                data = res_chat.get_json()
                self.assertIn("task_id_preview", data)
                self.assertIsNotNone(data["task_id_preview"])
                self.assertTrue(data["task_id_preview"].startswith("PB-"))
                self.assertIsNone(data.get("task_id"))

                with web_backend._sessions_lock:
                    session_dm = web_backend._sessions_manager[session_id]
                task_type_slot = session_dm.slot_store.slots["task_type_key"]
                task_id_slot = session_dm.slot_store.slots["task_id"]
                self.assertGreater(session_dm.slot_store.version, 0)
                self.assertEqual(task_type_slot.value, "pipeline_burial")
                self.assertEqual(task_type_slot.status, "valid")
                self.assertEqual(task_id_slot.status, "candidate")
                self.assertEqual(task_id_slot.source, "auto_preview")
                self.assertIsNone(task_id_slot.value)
                self.assertEqual(task_id_slot.candidate_value, data["task_id_preview"])
                self.assertEqual(session_dm.task_state, session_dm.slot_store.get_task_state())
                self.assertEqual(len(llm.classify_calls), 1)
                self.assertEqual(len(llm.extract_calls), 2)
                self.assertFalse(llm.plans)
                self.assertFalse(llm.extractions)
        finally:
            clear_test_session()

    def test_25_directory_fsync_failure_raises_id_reservation_error(self):
        real_fsync = os.fsync
        def mock_fsync(fd):
            try:
                st = os.fstat(fd)
                import stat
                if stat.S_ISDIR(st.st_mode):
                    raise OSError("Directory sync not supported")
            except Exception as e:
                if "Directory sync not supported" in str(e):
                    raise
            return real_fsync(fd)

        with patch("os.fsync", side_effect=mock_fsync):
            with self.assertRaises(IdReservationError):
                id_sequence.next_daily_task_id(
                    "PI",
                    "20260803",
                    3,
                    [],
                    allowed_prefixes=["PI"],
                )

    def test_26_internal_id_is_uuid_and_immutable(self):
        import uuid
        from src.task_intent_builder import validate_uuid4

        task_type = _task_type_candidate("pipeline_inspection")
        llm = ScriptedLLM(
            plans=[make_plan("WRITE") for _ in range(4)],
            extractions=[
                extraction_result(task_type),
                extraction_result(task_type),
                extraction_result(task_type),
                extraction_result(
                    slot_candidate(
                        "water_depth",
                        300.0,
                        raw_key="水深",
                        raw_value="300米",
                    )
                ),
                extraction_result(
                    slot_candidate(
                        "internal_id",
                        "12345678-1234-4234-8234-1234567890ab",
                        raw_key="内部编号",
                        raw_value="12345678-1234-4234-8234-1234567890ab",
                    )
                ),
            ],
            default_reply="测试回复。",
        )
        dm = DialogueManager(llm, KnowledgeBase())

        dm.process("我要做管缆巡检")
        internal_id = dm.task_state.get("internal_id")
        self.assertIsNotNone(internal_id)
        self.assertTrue(validate_uuid4(internal_id))

        dm.process("任务类型还是管缆巡检")
        self.assertEqual(dm.task_state.get("internal_id"), internal_id)

        version_before_depth = dm.slot_store.version
        dm.process("水深设置为 300 米")
        self.assertGreater(dm.slot_store.version, version_before_depth)
        self.assertEqual(dm.task_state.get("water_depth"), 300.0)
        self.assertEqual(dm.task_state.get("internal_id"), internal_id)
        self.assertNotEqual(dm.task_state.get("internal_id"), dm.task_state.get("task_id"))
        self.assertEqual(dm.task_state, dm.slot_store.get_task_state())

        state_before_attack = copy.deepcopy(dm.task_state)
        snapshot_before_attack = copy.deepcopy(dm.slot_store.export_snapshot())
        version_before_attack = dm.slot_store.version
        dm.process("把 internal_id 改成 12345678-1234-4234-8234-1234567890ab")
        self.assertEqual(dm.slot_store.version, version_before_attack)
        self.assertEqual(dm.slot_store.export_snapshot(), snapshot_before_attack)
        self.assertEqual(dm.task_state, state_before_attack)
        self.assertEqual(dm.task_state.get("internal_id"), internal_id)
        self.assertNotIn(
            "12345678-1234-4234-8234-1234567890ab",
            json.dumps(dm.slot_store.export_snapshot(), ensure_ascii=False),
        )
        self.assertEqual(len(llm.classify_calls), 4)
        self.assertEqual(len(llm.extract_calls), 5)
        self.assertFalse(llm.plans)
        self.assertFalse(llm.extractions)

        kb = KnowledgeBase()
        ti_builder = TaskIntentBuilder(kb)
        with self.assertRaises(TaskPersistenceError):
            ti_builder.prepare({"task_id": "PI-20260803-001"}, {"task_id": "PI-20260803-001"}, "normal", "pipeline_inspection")

        v1_uuid = str(uuid.uuid1())
        self.assertFalse(validate_uuid4(v1_uuid))
        with self.assertRaises(TaskPersistenceError):
            ti_builder.prepare({"task_id": "PI-20260803-001", "internal_id": v1_uuid}, {"task_id": "PI-20260803-001", "internal_id": v1_uuid}, "normal", "pipeline_inspection")

    def test_27_next_daily_task_id_requires_allowed_prefixes_whitelist(self):
        # allowed_prefixes is None -> raise IdReservationError
        with self.assertRaises(IdReservationError):
            next_daily_task_id("PI", "20260803", 3, allowed_prefixes=None)

        # allowed_prefixes is empty -> raise IdReservationError
        with self.assertRaises(IdReservationError):
            next_daily_task_id("PI", "20260803", 3, allowed_prefixes=[])

        # requested prefix not in allowed_prefixes whitelist -> raise IdReservationError
        with self.assertRaises(IdReservationError):
            next_daily_task_id("FAKE", "20260803", 3, allowed_prefixes=["PI", "PB", "CT"])

    def test_28_legacy_v1_task_intent_read_compatibility(self):
        """Historical v1 TaskIntent without internal_id/task_id is accepted by validate_task_intent and restores done phase without downgrade."""
        kb = KnowledgeBase()
        legacy_v1_intent = {
            "intent_id": "TI2026063001",
            "task_type": "pipeline_inspection",
            "priority": 7,
            "time": {"start": "2026-06-30T10:00:00+08:00", "end": "2026-06-30T12:00:00+08:00"},
            "location": {"oilfield": "南海一号", "water_depth_m": 300.0},
            "task": {"type": "pipeline_inspection", "details": {}},
            "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": {"name": None}},
            "conditions": {}
        }
        self.assertTrue(validate_task_intent(legacy_v1_intent, kb.task_schemas))

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)
            pub_file = tmp_task_dir / "task_intent_TI2026063001.json"
            with open(pub_file, "w", encoding="utf-8") as f:
                json.dump(legacy_v1_intent, f)

            snap = {
                "phase": "done",
                "mode": "normal",
                "task_state": {"task_type_key": "pipeline_inspection", "intent_id": "TI2026063001"},
                "built_json": {"task_type_key": "pipeline_inspection", "intent_id": "TI2026063001"},
                "slot_store": {
                    "store_version": 1,
                    "slots": {
                        "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "version": 1},
                        "intent_id": {"slot_name": "intent_id", "value": "TI2026063001", "status": "valid", "version": 1}
                    },
                    "unresolved": []
                }
            }

            dm = create_dialogue_manager()
            with patch("src.dialogue_manager.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                dm.load_snapshot(snap)

            self.assertEqual(dm.phase, "done")
            self.assertIsNotNone(dm.final_result)
            self.assertEqual(dm.final_result.get("intent_id"), "TI2026063001")

    def test_29_snapshot_restore_identifier_validation_and_3id_match(self):
        """Invalid internal_id UUIDv4 in snapshot candidate raises SnapshotValidationError without mutating memory state.
        Mismatched internal_id or task_id between snapshot and final rejects done phase."""
        from src.slot_store import SnapshotValidationError
        dm = create_dialogue_manager()
        dm.process("我要做管缆巡检")
        orig_phase = dm.phase
        orig_internal_id = dm.task_state.get("internal_id")

        # 1. Candidate snapshot with invalid internal_id -> raise SnapshotValidationError and keep state untouched
        bad_uuid_snap = {
            "phase": "collecting",
            "mode": "normal",
            "task_state": {"internal_id": "invalid-not-a-uuid"},
            "slot_store": {
                "store_version": 1,
                "slots": {
                    "internal_id": {"slot_name": "internal_id", "value": "invalid-not-a-uuid", "status": "valid", "version": 1}
                },
                "unresolved": []
            }
        }
        with self.assertRaises(SnapshotValidationError):
            dm.load_snapshot(bad_uuid_snap)

        # Memory state remains untouched
        self.assertEqual(dm.phase, orig_phase)
        self.assertEqual(dm.task_state.get("internal_id"), orig_internal_id)

        # 2. Done snapshot internal_id mismatch against disk final file -> fails entering done
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)
            pub_file = tmp_task_dir / "task_intent_TI2026080301.json"
            valid_file_intent = {
                "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
                "task_id": "PI-20260803-001",
                "intent_id": "TI2026080301",
                "task_type": "pipeline_inspection",
                "priority": 7,
                "time": {"start": None, "end": None},
                "location": {"oilfield": None, "water_depth_m": 300.0},
                "task": {"type": "pipeline_inspection", "details": {}},
                "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": {"name": None}},
                "conditions": {}
            }
            with open(pub_file, "w", encoding="utf-8") as f:
                json.dump(valid_file_intent, f)

            mismatched_snap = {
                "phase": "done",
                "mode": "normal",
                "task_state": {"internal_id": "b1ffcd88-8b0a-4fe7-aa5c-5aa8ac270a22", "task_id": "PI-20260803-001", "task_type_key": "pipeline_inspection", "intent_id": "TI2026080301"},
                "slot_store": {
                    "store_version": 1,
                    "slots": {
                        "internal_id": {"slot_name": "internal_id", "value": "b1ffcd88-8b0a-4fe7-aa5c-5aa8ac270a22", "status": "valid", "version": 1},
                        "task_id": {"slot_name": "task_id", "value": "PI-20260803-001", "status": "valid", "version": 1},
                        "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "version": 1},
                        "intent_id": {"slot_name": "intent_id", "value": "TI2026080301", "status": "valid", "version": 1}
                    },
                    "unresolved": []
                }
            }

            with patch("src.dialogue_manager.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                dm.load_snapshot(mismatched_snap)

            self.assertNotEqual(dm.phase, "done")

    def test_30_explicit_schema_version_and_anti_tampering_closeout(self):
        """Test explicit schema_version dispatching, mandatory schema_version: 2 on write, and anti-tampering during restoration."""
        kb = KnowledgeBase()
        ti_builder = TaskIntentBuilder(kb)

        # 1. validator version dispatch tests
        base_v2_intent = {
            "schema_version": 2,
            "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "task_id": "PI-20260803-001",
            "intent_id": "TI2026080301",
            "task_type": "pipeline_inspection",
            "priority": 7,
            "time": {"start": "2026-08-03T10:00:00+08:00", "end": "2026-08-03T12:00:00+08:00"},
            "location": {"oilfield": "南海一号", "water_depth_m": 300.0},
            "task": {"type": "pipeline_inspection", "details": {}},
            "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": {"name": None}},
            "conditions": {}
        }
        self.assertTrue(validate_task_intent(base_v2_intent, kb.task_schemas))

        # schema_version = 2 but internal_id deleted -> False
        no_internal = copy.deepcopy(base_v2_intent)
        del no_internal["internal_id"]
        self.assertFalse(validate_task_intent(no_internal, kb.task_schemas))

        # schema_version = 2 but task_id deleted -> False
        no_task_id = copy.deepcopy(base_v2_intent)
        del no_task_id["task_id"]
        self.assertFalse(validate_task_intent(no_task_id, kb.task_schemas))

        # schema_version = 999 -> False
        unsupported_ver = copy.deepcopy(base_v2_intent)
        unsupported_ver["schema_version"] = 999
        self.assertFalse(validate_task_intent(unsupported_ver, kb.task_schemas))

        # schema_version = True (bool) -> False
        bool_ver = copy.deepcopy(base_v2_intent)
        bool_ver["schema_version"] = True
        self.assertFalse(validate_task_intent(bool_ver, kb.task_schemas))

        # schema_version = 2.0 (float) -> False
        float_ver = copy.deepcopy(base_v2_intent)
        float_ver["schema_version"] = 2.0
        self.assertFalse(validate_task_intent(float_ver, kb.task_schemas))

        # schema_version = None (explicit null) -> False
        null_ver = copy.deepcopy(base_v2_intent)
        null_ver["schema_version"] = None
        self.assertFalse(validate_task_intent(null_ver, kb.task_schemas))

        # Unversioned dict with internal_id/task_id -> False (Option A strict versioning)
        unversioned_v2 = copy.deepcopy(base_v2_intent)
        del unversioned_v2["schema_version"]
        self.assertFalse(validate_task_intent(unversioned_v2, kb.task_schemas))

        # v2 validation without task_schemas -> False
        self.assertFalse(validate_task_intent(base_v2_intent, task_schemas=None))

        # v1 structure with internal_id attached -> False
        v1_with_internal = {
            "schema_version": 1,
            "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "intent_id": "TI2026080301",
            "task_type": "pipeline_inspection",
            "priority": 7,
            "time": {"start": "2026-08-03T10:00:00+08:00", "end": "2026-08-03T12:00:00+08:00"},
            "location": {"oilfield": "南海一号", "water_depth_m": 300.0},
            "task": {"type": "pipeline_inspection", "details": {}},
            "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": {"name": None}},
            "conditions": {}
        }
        self.assertFalse(validate_task_intent(v1_with_internal, kb.task_schemas))

        # 2. _validate_intent requires schema_version == 2
        no_ver_intent = copy.deepcopy(base_v2_intent)
        del no_ver_intent["schema_version"]
        with self.assertRaises(TaskPersistenceError):
            ti_builder._validate_intent(no_ver_intent)

        # 3. Done restoration: final deleted internal_id/task_id or v1/v2 mismatch rejects done phase
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)
            pub_file = tmp_task_dir / "task_intent_TI2026080301.json"

            # Final file has internal_id deleted
            file_deleted_internal = copy.deepcopy(base_v2_intent)
            del file_deleted_internal["internal_id"]
            with open(pub_file, "w", encoding="utf-8") as f:
                json.dump(file_deleted_internal, f)

            snap_v2 = {
                "phase": "done",
                "mode": "normal",
                "task_state": {
                    "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
                    "task_id": "PI-20260803-001",
                    "task_type_key": "pipeline_inspection",
                    "intent_id": "TI2026080301"
                },
                "slot_store": {
                    "store_version": 1,
                    "slots": {
                        "internal_id": {"slot_name": "internal_id", "value": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "status": "valid", "version": 1},
                        "task_id": {"slot_name": "task_id", "value": "PI-20260803-001", "status": "valid", "version": 1},
                        "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "version": 1},
                        "intent_id": {"slot_name": "intent_id", "value": "TI2026080301", "status": "valid", "version": 1}
                    },
                    "unresolved": []
                }
            }

            dm = create_dialogue_manager()
            with patch("src.dialogue_manager.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                dm.load_snapshot(snap_v2)

            self.assertNotEqual(dm.phase, "done")

        # 4. Snapshot with task_id but missing task_type_key or internal_id raises SnapshotValidationError
        incomplete_cand_snap = {
            "phase": "collecting",
            "mode": "normal",
            "task_state": {"task_id": "PI-20260803-001"},
            "slot_store": {
                "store_version": 1,
                "slots": {
                    "task_id": {"slot_name": "task_id", "value": "PI-20260803-001", "status": "valid", "version": 1}
                },
                "unresolved": []
            }
        }
        with self.assertRaises(SnapshotValidationError):
            dm.load_snapshot(incomplete_cand_snap)


class TestPreviewReserve(unittest.TestCase):
    """Tests 31-43: preview/reserve 生命周期、SSOT 事务、双 DM 并发与发布失败三重回滚真实端到端测试套件。"""

    def setUp(self):
        from src.id_sequence import peek_daily_task_id
        self.peek = peek_daily_task_id
        self._tmp = tempfile.mkdtemp()
        self._patcher_result = patch("src.id_sequence.get_result_dir", return_value=Path(self._tmp))
        self._patcher_result.start()
        # 隔离内存计数器
        import src.id_sequence as _idseq
        self._orig_counters = dict(_idseq._COUNTERS)
        _idseq._COUNTERS.clear()

    def tearDown(self):
        self._patcher_result.stop()
        import src.id_sequence as _idseq
        _idseq._COUNTERS.clear()
        _idseq._COUNTERS.update(self._orig_counters)
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ──────────────────────────────────────────────────────
    # Test 31: preview 不消耗正式编号
    # ──────────────────────────────────────────────────────
    def test_31_preview_does_not_consume_counter(self):
        """第一次 preview → 001；第二次 preview → 001（计数器未变化）。"""
        date = get_business_date().strftime("%Y%m%d")
        prefixes = ["PI", "PB", "CT"]
        p1 = self.peek("PI", date, 3, (), prefixes)
        p2 = self.peek("PI", date, 3, (), prefixes)

        self.assertEqual(p1, p2, "两次 preview 应返回相同估算值")
        self.assertRegex(p1, fr"^PI-{date}-\d{{3}}$")

        # counter 文件不应存在（preview 不写磁盘）
        counter_file = Path(self._tmp) / ".id_sequences.json"
        if counter_file.exists():
            data = json.loads(counter_file.read_text())
            key = f"TASK:{date}"
            self.assertNotIn(key, data, "preview 不得写入 counter 文件")

    # ──────────────────────────────────────────────────────
    # Test 32: reserve 后 preview 应返回下一个编号
    # ──────────────────────────────────────────────────────
    def test_32_reserve_advances_counter_then_preview_reflects_next(self):
        """preview→001，reserve→001，再次 preview→002。"""
        date = get_business_date().strftime("%Y%m%d")
        prefixes = ["PI", "PB", "CT"]
        specs = ()

        p_before = self.peek("PI", date, 3, specs, prefixes)
        r = next_daily_task_id("PI", date, 3, specs, prefixes)
        p_after = self.peek("PI", date, 3, specs, prefixes)

        self.assertEqual(p_before, r, "第一次 preview 与第一次 reserve 编号应相同")
        before_num = int(p_before.split("-")[-1])
        after_num = int(p_after.split("-")[-1])
        self.assertEqual(after_num, before_num + 1, "reserve 后 preview 应递增 1")

    # ──────────────────────────────────────────────────────
    # Test 33: 两个草稿可以显示相同 preview（允许行为）
    # ──────────────────────────────────────────────────────
    def test_33_two_drafts_can_share_same_preview(self):
        """两个独立草稿在尚未 reserve 前 preview 相同——这是允许行为。"""
        date = get_business_date().strftime("%Y%m%d")
        prefixes = ["PI", "PB", "CT"]
        pa = self.peek("PI", date, 3, (), prefixes)
        pb = self.peek("PI", date, 3, (), prefixes)
        self.assertEqual(pa, pb, "两个草稿 preview 可以相同，因为都未消耗编号")

    # ──────────────────────────────────────────────────────
    # Test 34: 真实双 DialogueManager 并发发布端到端测试 (P0 回归核心)
    # ──────────────────────────────────────────────────────
    def test_34_concurrent_dialogue_managers_publish_uniqueness(self):
        """
        创建两个真实的 DialogueManager 实例 dm_a 和 dm_b：
        1. 两个会话在草稿阶段 preview 编号相同（均估算 001）；
        2. dm_a 先完成确认发布，正式生成 001 号 TaskIntent artifact；
        3. dm_b 后完成确认发布，正式生成 002 号 TaskIntent artifact；
        4. 两个会话落盘的 final artifact 文件 task_id 不同且各自状态严格一致！
        """
        from tests.test_slot_consistency import seed_complete_valid_pipeline_task

        tmp_task_dir = Path(self._tmp) / "task"
        tmp_task_dir.mkdir(parents=True, exist_ok=True)

        with patch("src.dialogue_manager.get_task_dir", return_value=tmp_task_dir), \
             patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
             patch("src.id_sequence.get_result_dir", return_value=Path(self._tmp)):

            dm_a = create_dialogue_manager()
            dm_b = create_dialogue_manager()

            dm_a.process("新建管缆巡检任务")
            dm_b.process("新建管缆巡检任务")

            # 验证草稿阶段只读 preview 均估算 001
            self.assertEqual(dm_a.task_id_preview, dm_b.task_id_preview, "草稿预览值应相同")

            # 填充全量可发布槽位
            seed_complete_valid_pipeline_task(dm_a, dm_a.kb)
            seed_complete_valid_pipeline_task(dm_b, dm_b.kb)

            # 设置唯一的 intent_id 并重置 task_id 为 candidate
            dm_a.slot_store.slots["intent_id"] = Slot("intent_id", "TI2026080601", status="valid")
            dm_a.slot_store.slots["task_id"] = Slot("task_id", value=None, status="candidate", source="auto_preview", candidate_value=dm_a.task_id_preview)
            dm_a.task_state = dm_a.slot_store.get_task_state()
            dm_a._last_built_json = dm_a.slot_store.get_built_json()
            dm_a.phase = "confirming"

            dm_b.slot_store.slots["intent_id"] = Slot("intent_id", "TI2026080602", status="valid")
            dm_b.slot_store.slots["task_id"] = Slot("task_id", value=None, status="candidate", source="auto_preview", candidate_value=dm_b.task_id_preview)
            dm_b.task_state = dm_b.slot_store.get_task_state()
            dm_b._last_built_json = dm_b.slot_store.get_built_json()
            dm_b.phase = "confirming"

            # 触发 dm_a 确认发布
            res_a = dm_a._handle_final_publish_confirmation("确认发布", "req_a")
            self.assertEqual(dm_a.phase, "done", f"dm_a 发布失败: {res_a}")
            tid_a = dm_a.final_result.get("task_id")

            # 触发 dm_b 确认发布
            res_b = dm_b._handle_final_publish_confirmation("确认发布", "req_b")
            self.assertEqual(dm_b.phase, "done", f"dm_b 发布失败: {res_b}")
            tid_b = dm_b.final_result.get("task_id")

            # 断言 A 与 B 正式编号必须不同！
            self.assertNotEqual(tid_a, tid_b, "并发发布的正式任务编号必须绝对不同")
            self.assertTrue(tid_a.endswith("-001"), f"dm_a 应为 001: {tid_a}")
            self.assertTrue(tid_b.endswith("-002"), f"dm_b 应为 002: {tid_b}")

            # 检查两份落盘的 TaskIntent JSON 文件
            file_a = tmp_task_dir / "task_intent_TI2026080601.json"
            file_b = tmp_task_dir / "task_intent_TI2026080602.json"
            self.assertTrue(file_a.exists(), "dm_a final artifact 应建立")
            self.assertTrue(file_b.exists(), "dm_b final artifact 应建立")

            content_a = json.loads(file_a.read_text(encoding="utf-8"))
            content_b = json.loads(file_b.read_text(encoding="utf-8"))
            self.assertEqual(content_a["task_id"], tid_a)
            self.assertEqual(content_b["task_id"], tid_b)

    # ──────────────────────────────────────────────────────
    # Test 35: 真实多进程正式预约唯一性
    # ──────────────────────────────────────────────────────
    def test_35_multiprocess_reserve_uniqueness(self):
        """4 个进程同时 reserve，返回 4 个不同编号，无重复。"""
        tmp_dir = self._tmp
        date = get_business_date().strftime("%Y%m%d")
        prefixes_list = ["PI", "PB", "CT"]
        n_proc = 4

        def _worker(q, tmp):
            try:
                with patch("src.id_sequence.get_result_dir", return_value=Path(tmp)):
                    import src.id_sequence as _idseq
                    _idseq._COUNTERS.clear()
                    result = next_daily_task_id("PI", date, 3, (), prefixes_list)
                    q.put(("ok", result))
            except Exception as e:
                q.put(("err", str(e)))

        from multiprocessing import Process, Queue
        q = Queue()
        procs = [Process(target=_worker, args=(q, tmp_dir)) for _ in range(n_proc)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)

        results = []
        for _ in range(n_proc):
            status, val = q.get(timeout=5)
            self.assertEqual(status, "ok", f"进程失败: {val}")
            results.append(val)

        self.assertEqual(len(set(results)), n_proc, f"存在重复编号: {results}")

    # ──────────────────────────────────────────────────────
    # Test 36: 不同前缀共享当日全局序号
    # ──────────────────────────────────────────────────────
    def test_36_different_prefixes_share_global_daily_sequence(self):
        """PI→001，PB→002，CT→003；跨前缀序号连续。"""
        date = get_business_date().strftime("%Y%m%d")
        prefixes = ["PI", "PB", "CT"]
        specs = ()

        r1 = next_daily_task_id("PI", date, 3, specs, prefixes)
        r2 = next_daily_task_id("PB", date, 3, specs, prefixes)
        r3 = next_daily_task_id("CT", date, 3, specs, prefixes)

        nums = [int(r.split("-")[-1]) for r in [r1, r2, r3]]
        self.assertEqual(nums, [1, 2, 3], f"序号应全局共享且连续: {r1} {r2} {r3}")

    # ──────────────────────────────────────────────────────
    # Test 37: 精确断言 preview 不能直接进入 TaskIntent
    # ──────────────────────────────────────────────────────
    def test_37_preview_not_in_final_artifact(self):
        """当 task_state 缺少正式 task_id（仅有 preview）时，
        TaskIntentBuilder.prepare() 必须抛出 TaskPersistenceError，并明确指出 missing task_id。
        """
        kb = KnowledgeBase()
        cand_state = {
            "task_type_key": "pipeline_inspection",
            "internal_id": "12345678-1234-4234-8234-1234567890ab",
            "equipment_class": "observation_rov",
            "equipment_type": "观察级ROV",
            "equipment_unit_id": "OBSROV-75-001",
            # 故意不传 task_id（草稿 preview 不在 task_state 中）
        }
        cand_built = dict(cand_state)

        builder = TaskIntentBuilder(kb)
        with self.assertRaises(TaskPersistenceError) as cm:
            builder.prepare(
                task_state=cand_state,
                built_json=cand_built,
                mode="normal",
                task_type_key="pipeline_inspection",
                intent_id="TI2026080601",
            )
        self.assertIn("task_id", str(cm.exception), "应明确说明缺少正式 task_id")

    # ──────────────────────────────────────────────────────
    # Test 38: 连续正式预约必须消耗并递增序号
    # ──────────────────────────────────────────────────────
    def test_38_consecutive_reservations_advance_sequence_without_reuse(self):
        """每次正式 reserve 都消耗序号；连续预约必须返回 001、002。"""
        date = get_business_date().strftime("%Y%m%d")
        prefixes = ["PI", "PB", "CT"]
        specs = ()

        r1 = next_daily_task_id("PI", date, 3, specs, prefixes)
        self.assertEqual(r1, f"PI-{date}-001")

        r2 = next_daily_task_id("PI", date, 3, specs, prefixes)
        self.assertEqual(r2, f"PI-{date}-002")
        self.assertNotEqual(r1, r2)

    # ──────────────────────────────────────────────────────
    # Test 39: 真实全链 5 路 SSOT 零误差一致性测试
    # ──────────────────────────────────────────────────────
    def test_39_official_task_id_full_chain_ssot_consistency(self):
        """真实 DialogueManager 发布完成后，断言以下 5 处 task_id 完全一致：
        1. dm.slot_store.slots["task_id"].value
        2. dm.task_state["task_id"]
        3. dm._last_built_json["task_id"]
        4. dm.final_result["task_id"]
        5. 落盘 TaskIntent artifact 中的 task_id
        并且 slot.status == "valid", source == "auto_reserved"，store_version 已递增！
        """
        from tests.test_slot_consistency import seed_complete_valid_pipeline_task

        tmp_task_dir = Path(self._tmp) / "task"
        tmp_task_dir.mkdir(parents=True, exist_ok=True)

        with patch("src.dialogue_manager.get_task_dir", return_value=tmp_task_dir), \
             patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
             patch("src.id_sequence.get_result_dir", return_value=Path(self._tmp)):

            dm = create_dialogue_manager()
            dm.process("新建管缆巡检任务")
            seed_complete_valid_pipeline_task(dm, dm.kb)

            dm.slot_store.slots["intent_id"] = Slot("intent_id", "TI2026080699", status="valid")
            dm.slot_store.slots["task_id"] = Slot("task_id", value=None, status="candidate", source="auto_preview", candidate_value=dm.task_id_preview)
            dm.task_state = dm.slot_store.get_task_state()
            dm._last_built_json = dm.slot_store.get_built_json()
            dm.phase = "confirming"

            init_version = dm.slot_store.version

            res = dm._handle_final_publish_confirmation("确认发布", "req_39")
            self.assertEqual(dm.phase, "done", f"发布应成功: {res}")

            # 1. 提取 5 路状态
            slot_task_id = dm.slot_store.slots["task_id"].value
            state_task_id = dm.task_state.get("task_id")
            built_task_id = dm._last_built_json.get("task_id")
            final_task_id = dm.final_result.get("task_id")

            artifact_file = tmp_task_dir / "task_intent_TI2026080699.json"
            self.assertTrue(artifact_file.exists())
            file_task_id = json.loads(artifact_file.read_text(encoding="utf-8")).get("task_id")

            # 断言 5 路 100% 绝对一致
            self.assertEqual(slot_task_id, state_task_id)
            self.assertEqual(state_task_id, built_task_id)
            self.assertEqual(built_task_id, final_task_id)
            self.assertEqual(final_task_id, file_task_id)

            # 断言事务属性
            self.assertEqual(dm.slot_store.slots["task_id"].status, "valid")
            self.assertEqual(dm.slot_store.slots["task_id"].source, "auto_reserved")
            self.assertGreater(dm.slot_store.version, init_version, "SlotStore version 必须因事务提交而增加")

    # ──────────────────────────────────────────────────────
    # Test 40: DialogueManager Snapshot 包含 Candidate 还原测试
    # ──────────────────────────────────────────────────────
    def test_40_snapshot_candidate_restoration_via_dialogue_manager(self):
        """测试通过 DialogueManager.export_snapshot() 与 load_snapshot()，
        能够精准还原 candidate 预览状态与 task_id_preview 属性。
        """
        dm = create_dialogue_manager()
        dm.process("新建管缆巡检任务")
        dm.slot_store.slots["internal_id"] = Slot("internal_id", "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", status="valid")
        preview_before = dm.task_id_preview
        self.assertIsNotNone(preview_before)

        # 导出并还原到新的 DM
        snap = dm.export_snapshot()
        dm2 = create_dialogue_manager()
        dm2.load_snapshot(snap)

        self.assertEqual(dm2.task_id_preview, preview_before)
        self.assertIsNone(dm2.task_state.get("task_id"), "还原后 task_state 仍不包含 candidate task_id")
        self.assertEqual(dm2.slot_store.slots["task_id"].status, "candidate")

    # ──────────────────────────────────────────────────────
    # Test 41: prepare 失败时的三回滚断言
    # ──────────────────────────────────────────────────────
    def test_41_prepare_failure_rollback(self):
        """当 TaskIntentBuilder.prepare 抛出异常时：
        1. phase 恢复为原状态（ confirming ）；
        2. final_result 为 None；
        3. SlotStore, task_state, _last_built_json 恢复快照，无有效 task_id；
        4. 磁盘无 final artifact。
        """
        from tests.test_slot_consistency import seed_complete_valid_pipeline_task

        tmp_task_dir = Path(self._tmp) / "task"
        tmp_task_dir.mkdir(parents=True, exist_ok=True)

        with patch("src.dialogue_manager.get_task_dir", return_value=tmp_task_dir), \
             patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
             patch("src.id_sequence.get_result_dir", return_value=Path(self._tmp)):

            dm = create_dialogue_manager()
            dm.process("新建管缆巡检任务")
            seed_complete_valid_pipeline_task(dm, dm.kb)

            dm.slot_store.slots["intent_id"] = Slot("intent_id", "TI2026080641", status="valid")
            dm.slot_store.slots["task_id"] = Slot("task_id", value=None, status="candidate", source="auto_preview", candidate_value=dm.task_id_preview)
            dm.task_state = dm.slot_store.get_task_state()
            dm._last_built_json = dm.slot_store.get_built_json()
            dm.phase = "confirming"

            snap_before = dm.slot_store.export_snapshot()
            state_before = copy.deepcopy(dm.task_state)
            built_before = copy.deepcopy(dm._last_built_json)
            missing_before = copy.deepcopy(dm._last_missing)
            hist_before = copy.deepcopy(dm.conversation_history)

            with patch(
                "src.task_intent_builder.TaskIntentBuilder.prepare",
                side_effect=TaskPersistenceError("Mock prepare error"),
            ) as mock_prepare:
                with self.assertRaises(TaskPersistenceError):
                    dm._handle_final_publish_confirmation("确认发布", "req_41")
                mock_prepare.assert_called_once()

            self.assertEqual(dm.phase, "confirming", "phase 应完全回滚")
            self.assertIsNone(dm.final_result, "final_result 应为 None")
            self.assertEqual(dm.slot_store.export_snapshot(), snap_before, "SlotStore 快照必须 100% 完全回滚")
            self.assertEqual(dm.task_state, state_before, "task_state 必须 100% 完全回滚")
            self.assertEqual(dm._last_built_json, built_before, "_last_built_json 必须 100% 完全回滚")
            self.assertEqual(dm._last_missing, missing_before, "_last_missing 必须 100% 完全回滚")
            self.assertEqual(dm.conversation_history, hist_before, "conversation_history 必须 100% 完全回滚")
            self.assertFalse((tmp_task_dir / "task_intent_TI2026080641.json").exists())
            self.assertEqual(len(list(tmp_task_dir.glob("*"))), 0, "磁盘无任何残留临时/staging/final文件")

    # ──────────────────────────────────────────────────────
    # Test 42: create_staging 失败时的三回滚断言
    # ──────────────────────────────────────────────────────
    def test_42_create_staging_failure_rollback(self):
        """当 create_staging 失败时，SlotStore 与内存状态 100% 完全回滚。"""
        from tests.test_slot_consistency import seed_complete_valid_pipeline_task

        tmp_task_dir = Path(self._tmp) / "task"
        tmp_task_dir.mkdir(parents=True, exist_ok=True)

        with patch("src.dialogue_manager.get_task_dir", return_value=tmp_task_dir), \
             patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
             patch("src.id_sequence.get_result_dir", return_value=Path(self._tmp)):

            dm = create_dialogue_manager()
            dm.process("新建管缆巡检任务")
            seed_complete_valid_pipeline_task(dm, dm.kb)

            dm.slot_store.slots["intent_id"] = Slot("intent_id", "TI2026080642", status="valid")
            dm.slot_store.slots["task_id"] = Slot("task_id", value=None, status="candidate", source="auto_preview", candidate_value=dm.task_id_preview)
            dm.task_state = dm.slot_store.get_task_state()
            dm._last_built_json = dm.slot_store.get_built_json()
            dm.phase = "confirming"

            snap_before = dm.slot_store.export_snapshot()
            state_before = copy.deepcopy(dm.task_state)
            built_before = copy.deepcopy(dm._last_built_json)
            missing_before = copy.deepcopy(dm._last_missing)
            hist_before = copy.deepcopy(dm.conversation_history)

            with patch(
                "src.task_intent_builder.TaskIntentBuilder.create_staging",
                side_effect=TaskPersistenceError("Mock staging error"),
            ) as mock_create_staging:
                with self.assertRaises(TaskPersistenceError):
                    dm._handle_final_publish_confirmation("确认发布", "req_42")
                mock_create_staging.assert_called_once()

            self.assertEqual(dm.phase, "confirming")
            self.assertIsNone(dm.final_result)
            self.assertEqual(dm.slot_store.export_snapshot(), snap_before)
            self.assertEqual(dm.task_state, state_before)
            self.assertEqual(dm._last_built_json, built_before)
            self.assertEqual(dm._last_missing, missing_before)
            self.assertEqual(dm.conversation_history, hist_before)
            self.assertEqual(len(list(tmp_task_dir.glob("*"))), 0)

    # ──────────────────────────────────────────────────────
    # Test 43: publish_staging 失败时的三回滚断言
    # ──────────────────────────────────────────────────────
    def test_43_publish_staging_failure_rollback(self):
        """当 publish_staging 失败时，SlotStore 与内存状态 100% 完全回滚。"""
        from tests.test_slot_consistency import seed_complete_valid_pipeline_task

        tmp_task_dir = Path(self._tmp) / "task"
        tmp_task_dir.mkdir(parents=True, exist_ok=True)

        with patch("src.dialogue_manager.get_task_dir", return_value=tmp_task_dir), \
             patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
             patch("src.id_sequence.get_result_dir", return_value=Path(self._tmp)):

            dm = create_dialogue_manager()
            dm.process("新建管缆巡检任务")
            seed_complete_valid_pipeline_task(dm, dm.kb)

            dm.slot_store.slots["intent_id"] = Slot("intent_id", "TI2026080643", status="valid")
            dm.slot_store.slots["task_id"] = Slot("task_id", value=None, status="candidate", source="auto_preview", candidate_value=dm.task_id_preview)
            dm.task_state = dm.slot_store.get_task_state()
            dm._last_built_json = dm.slot_store.get_built_json()
            dm.phase = "confirming"

            snap_before = dm.slot_store.export_snapshot()
            state_before = copy.deepcopy(dm.task_state)
            built_before = copy.deepcopy(dm._last_built_json)
            missing_before = copy.deepcopy(dm._last_missing)
            hist_before = copy.deepcopy(dm.conversation_history)

            with patch(
                "src.task_intent_builder.TaskIntentBuilder.publish_staging",
                side_effect=TaskPersistenceError("Mock publish error"),
            ) as mock_publish_staging:
                with self.assertRaises(TaskPersistenceError):
                    dm._handle_final_publish_confirmation("确认发布", "req_43")
                mock_publish_staging.assert_called_once()

            self.assertEqual(dm.phase, "confirming")
            self.assertIsNone(dm.final_result)
            self.assertEqual(dm.slot_store.export_snapshot(), snap_before)
            self.assertEqual(dm.task_state, state_before)
            self.assertEqual(dm._last_built_json, built_before)
            self.assertEqual(dm._last_missing, missing_before)
            self.assertEqual(dm.conversation_history, hist_before)
            self.assertEqual(len(list(tmp_task_dir.glob("*.json"))), 0)


if __name__ == "__main__":
    unittest.main()
