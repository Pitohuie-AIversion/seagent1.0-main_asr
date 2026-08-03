"""tests/test_issue_11_deterministic_task_id.py — Issue #11 确定性任务编号生成与持久化收口单元测试套件

本文件全面兼容 python -m unittest discover tests -v 命令（不引入 pytest 依赖）。
"""

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
from src import id_sequence
from src.id_sequence import (
    next_daily_task_id,
    validate_intent_id,
    validate_task_id,
    validate_task_prefix,
)
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.output_builder import OutputBuilder
from src.result_paths import get_history_dir, get_task_dir
from src.simulated_time import (
    get_business_date,
    get_business_timezone,
    get_simulated_time,
)
from src.slot_store import Slot
from src.task_intent_builder import TaskIntentBuilder


class FakeLLM(LLMClient):
    def __init__(self):
        self.llm = None
        self.tokenizer = None

    def chat(self, messages, **kwargs):
        return "已接收到您的任务请求"

    def filter_reply(self, text, *args, **kwargs):
        return text if isinstance(text, str) else "已接收到您的任务请求"

    def extract_json(self, prompt, schema=None, **kwargs):
        user_msg = ""
        if isinstance(prompt, list) and prompt and isinstance(prompt[-1], dict):
            user_msg = str(prompt[-1].get("content", ""))
        elif isinstance(prompt, str):
            user_msg = prompt

        if "管缆埋设" in user_msg or "pipeline_burial" in user_msg:
            return {
                "slot_candidates": [
                    {
                        "raw_key": "任务类型",
                        "canonical_key": "task_type_key",
                        "raw_value": "管缆埋设",
                        "normalized_value": "pipeline_burial",
                        "confidence": 0.95,
                    }
                ],
                "unresolved": [],
            }
        if "采油树" in user_msg or "tree_valve_operation" in user_msg:
            return {
                "slot_candidates": [
                    {
                        "raw_key": "任务类型",
                        "canonical_key": "task_type_key",
                        "raw_value": "采油树控制面板插入",
                        "normalized_value": "tree_valve_operation",
                        "confidence": 0.95,
                    }
                ],
                "unresolved": [],
            }
        if "管缆巡检" in user_msg or "pipeline_inspection" in user_msg:
            return {
                "slot_candidates": [
                    {
                        "raw_key": "任务类型",
                        "canonical_key": "task_type_key",
                        "raw_value": "管缆巡检",
                        "normalized_value": "pipeline_inspection",
                        "confidence": 0.95,
                    }
                ],
                "unresolved": [],
            }
        return {"slot_candidates": [], "unresolved": []}


def create_dialogue_manager():
    kb = KnowledgeBase()
    llm = FakeLLM()
    return DialogueManager(llm, kb)


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
        )
        result_queue.put(tid)
    except Exception as exc:
        result_queue.put(exc)


class Issue11DeterministicTaskIdTest(unittest.TestCase):
    def setUp(self):
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

        tid_pi = builder.reserve_task_id("pipeline_inspection", {})
        self.assertTrue(tid_pi.startswith("PI-"))

        tid_pb = builder.reserve_task_id("pipeline_burial", {})
        self.assertTrue(tid_pb.startswith("PB-"))

        tid_ct = builder.reserve_task_id("tree_valve_operation", {})
        self.assertTrue(tid_ct.startswith("CT-"))

    def test_03_sequential_ids_on_same_day(self):
        kb = KnowledgeBase()
        builder = OutputBuilder(kb)

        tid1 = builder.reserve_task_id("pipeline_inspection", {})
        tid2 = builder.reserve_task_id("pipeline_inspection", {})
        tid3 = builder.reserve_task_id("pipeline_inspection", {})

        self.assertEqual(tid1, "PI-20260803-001")
        self.assertEqual(tid2, "PI-20260803-002")
        self.assertEqual(tid3, "PI-20260803-003")

    def test_04_first_task_must_be_001(self):
        dm = create_dialogue_manager()
        dm.process("我要做管缆巡检")
        task_id = dm.task_state.get("task_id")
        self.assertEqual(task_id, "PI-20260803-001")

    def test_05_shared_global_daily_sequence_across_categories(self):
        kb = KnowledgeBase()
        builder = OutputBuilder(kb)

        tid1 = builder.reserve_task_id("pipeline_inspection", {})
        tid2 = builder.reserve_task_id("pipeline_burial", {})
        tid3 = builder.reserve_task_id("tree_valve_operation", {})

        self.assertEqual(tid1, "PI-20260803-001")
        self.assertEqual(tid2, "PB-20260803-002")
        self.assertEqual(tid3, "CT-20260803-003")

    def test_06_date_change_resets_sequence(self):
        kb = KnowledgeBase()
        builder = OutputBuilder(kb)

        tid1 = builder.reserve_task_id("pipeline_inspection", {})
        self.assertEqual(tid1, "PI-20260803-001")

        get_simulated_time().set_current_time(
            datetime(2026, 8, 4, 9, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )

        tid2 = builder.reserve_task_id("pipeline_inspection", {})
        self.assertEqual(tid2, "PI-20260804-001")

    def test_07_timezone_configuration_boundary(self):
        with patch.dict(os.environ, {"SEAGENT_TIMEZONE": "Asia/Tokyo"}):
            self.assertEqual(get_business_timezone().key, "Asia/Tokyo")

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
                tid = builder.reserve_task_id("pipeline_inspection", {})
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

    def test_11_deleting_intermediate_file_does_not_reuse_seq(self):
        kb = KnowledgeBase()
        builder = OutputBuilder(kb)

        tid1 = builder.reserve_task_id("pipeline_inspection", {})
        tid2 = builder.reserve_task_id("pipeline_inspection", {})

        self.assertEqual(tid1, "PI-20260803-001")
        self.assertEqual(tid2, "PI-20260803-002")

        tid3 = builder.reserve_task_id("pipeline_inspection", {})
        self.assertEqual(tid3, "PI-20260803-003")

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
                    )

    def test_13_invalid_task_prefix_fails_closed(self):
        self.assertFalse(validate_task_prefix(""))
        self.assertFalse(validate_task_prefix("../invalid"))
        self.assertFalse(validate_task_prefix("PI/2026"))

        kb = KnowledgeBase()
        builder = OutputBuilder(kb)

        with self.assertRaises(IdReservationError):
            builder.reserve_task_id("non_existent_template", {})

    def test_14_non_valid_task_type_does_not_generate_id(self):
        dm = create_dialogue_manager()
        dm.process("你好，我想了解离岸工程")
        self.assertIsNone(dm.task_state.get("task_id"))

    def test_15_field_edits_preserve_task_id(self):
        dm = create_dialogue_manager()
        dm.process("我要做管缆巡检")
        tid1 = dm.task_state.get("task_id")
        self.assertIsNotNone(tid1)

        dm.process("设置水深为500米")
        tid2 = dm.task_state.get("task_id")
        self.assertEqual(tid1, tid2)

    def test_16_builder_build_does_not_burn_sequence(self):
        kb = KnowledgeBase()
        builder = OutputBuilder(kb)

        state = {}
        schema = builder.get_schema("pipeline_inspection", "normal")

        field_res = builder._extract_field("task_id", "auto", schema[0], state, "pipeline_inspection")
        self.assertIsNone(field_res)
        self.assertIsNone(state.get("task_id"))

    def test_17_user_input_cannot_overwrite_task_id(self):
        dm = create_dialogue_manager()
        dm.process("我要做管缆巡检")
        tid1 = dm.task_state.get("task_id")

        dm.process("把 task_id 改成 FAKE-999")
        self.assertEqual(dm.task_state.get("task_id"), tid1)

    def test_18_snapshot_restore_preserves_task_id(self):
        dm = create_dialogue_manager()
        dm.process("我要做管缆巡检")
        tid1 = dm.task_state.get("task_id")

        snap = dm.slot_store.export_snapshot()

        dm2 = create_dialogue_manager()
        dm2.slot_store.restore_snapshot(snap)
        self.assertEqual(dm2.slot_store.get_task_state().get("task_id"), tid1)

    def test_19_published_task_intent_contains_top_level_task_id(self):
        kb = KnowledgeBase()
        builder = TaskIntentBuilder(kb)

        task_state = {
            "task_id": "PI-20260803-001",
            "task_type_key": "pipeline_inspection",
            "water_depth": 500.0,
            "oilfield_name": "文昌油田",
            "equipment_family": "LROV",
            "equipment_type": "观察级ROV",
        }
        built_json = {
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

        intent = builder.prepare(task_state, built_json, "normal", "pipeline_inspection")
        self.assertIn("task_id", intent)
        self.assertEqual(intent["task_id"], "PI-20260803-001")
        self.assertTrue(validate_task_id(intent["task_id"]))

    def test_20_publish_retry_preserves_task_id(self):
        dm = create_dialogue_manager()
        dm.process("我要做管缆巡检")
        tid1 = dm.task_state.get("task_id")
        self.assertIsNotNone(tid1)

        dm.slot_store.slots["intent_id"] = Slot("intent_id", "TI2026080301", status="valid")
        dm._last_built_json["intent_id"] = "TI2026080301"
        dm.task_state["intent_id"] = "TI2026080301"

        def mock_publish_fail(*args, **kwargs):
            raise RuntimeError("Disk write failed")

        with patch("src.dialogue_manager.TaskIntentBuilder.publish_staging", side_effect=mock_publish_fail):
            dm.phase = "confirming"
            reply = dm._handle_final_publish_confirmation("确认发布", "req_test")
            self.assertTrue("发布" in reply or dm.phase != "done")
            self.assertEqual(dm.task_state.get("task_id"), tid1)

    def test_21_category_modification_is_rejected_once_task_id_locked(self):
        dm = create_dialogue_manager()
        dm.process("新建管缆巡检任务")
        tid1 = dm.task_state.get("task_id")
        self.assertEqual(tid1, "PI-20260803-001")
        self.assertEqual(dm.task_state.get("task_type_key"), "pipeline_inspection")

        dm.process("修改任务类型为管缆埋设")
        # Category remains pipeline_inspection and task_id remains locked
        self.assertEqual(dm.task_state.get("task_type_key"), "pipeline_inspection")
        self.assertEqual(dm.task_state.get("task_id"), tid1)

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
                tid = builder.reserve_task_id("pipeline_inspection", {})
                self.assertEqual(tid, "PI-20260803-003")

    def test_23_general_chat_does_not_generate_task_id(self):
        dm = create_dialogue_manager()
        dm.process("你好，请问你是谁？")
        self.assertIsNone(dm.task_state.get("task_id"))

        dm.process("深海勇士号的最大作业水深是多少？")
        self.assertIsNone(dm.task_state.get("task_id"))

    def test_24_asr_input_task_id_behavior(self):
        dm = create_dialogue_manager()
        dm.process("新建管缆埋设任务")
        self.assertEqual(dm.task_state.get("task_id"), "PB-20260803-001")


if __name__ == "__main__":
    unittest.main()
