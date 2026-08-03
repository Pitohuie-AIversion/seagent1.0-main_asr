"""tests/test_issue_11_deterministic_task_id.py — Issue #11 确定性任务编号全闭环与合规性单元测试套件

本文件全面兼容 python -m unittest discover tests -v 命令（0 pytest 依赖）。
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
    validate_task_id_for_task_type,
    validate_task_prefix,
)
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.output_builder import OutputBuilder
from src.result_paths import get_history_dir, get_task_dir
from src.simulated_time import (
    get_business_date,
    get_business_datetime,
    get_business_timezone,
    get_current_datetime,
    get_simulated_time,
)
from src.slot_store import Slot
from src.task_intent_builder import TaskIntentBuilder, validate_task_intent


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
            allowed_prefixes=["PI", "PB", "CT"],
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
        task_id = dm.task_state.get("task_id")
        self.assertEqual(task_id, "PI-20260803-001")

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

    def test_19_category_modification_is_rejected_with_validation_error(self):
        dm = create_dialogue_manager()
        dm.process("新建管缆巡检任务")
        tid1 = dm.task_state.get("task_id")
        self.assertEqual(tid1, "PI-20260803-001")

        dm.process("修改任务类型为管缆埋设")
        # Category remains pipeline_inspection and task_id remains locked
        self.assertEqual(dm.task_state.get("task_type_key"), "pipeline_inspection")
        self.assertEqual(dm.task_state.get("task_id"), tid1)
        err = dm.slot_store.slots["task_type_key"].validation_error
        self.assertIsNotNone(err)
        self.assertIn("任务编号已锁定", err)

    def test_20_publish_retry_preserves_task_id(self):
        dm = create_dialogue_manager()
        dm.process("新建管缆巡检任务")
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
        dm = create_dialogue_manager()
        dm.process("你好，请问你是谁？")
        self.assertIsNone(dm.task_state.get("task_id"))

        dm.process("深海勇士号的最大作业水深是多少？")
        self.assertIsNone(dm.task_state.get("task_id"))

    def test_24_asr_input_task_id_behavior(self):
        import io
        import web_backend
        client = web_backend.app.test_client()

        kb = KnowledgeBase()
        llm = FakeLLM()
        mock_asr = MagicMock()
        mock_asr.transcribe_file.return_value = {
            "text": "新建管缆埋设任务",
            "raw_text": "新建管缆埋设任务",
            "corrected_text": "新建管缆埋设任务",
            "language_hint": "Chinese",
            "device": "cpu",
            "elapsed_ms": 10,
            "segments": [],
            "entities": []
        }

        with patch("web_backend._shared_asr", mock_asr), \
             patch("web_backend._shared_kb", kb), \
             patch("web_backend._shared_llm", llm):
            data_file = (io.BytesIO(b"dummy audio"), "test.wav")
            res_asr = client.post("/api/asr", data={"audio": data_file}, content_type="multipart/form-data")
            self.assertEqual(res_asr.status_code, 200)
            corrected_text = res_asr.get_json().get("corrected_text")
            self.assertEqual(corrected_text, "新建管缆埋设任务")

            res_chat = client.post("/api/chat", json={"session_id": "sess_asr_issue11", "message": corrected_text})
            self.assertEqual(res_chat.status_code, 200)
            data = res_chat.get_json()
            self.assertIn("collected", data)
            self.assertIn("task_id", data["collected"])
            self.assertTrue(data["collected"]["task_id"].startswith("PB-"))

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
        dm = create_dialogue_manager()
        dm.process("我要做管缆巡检")
        internal_id = dm.task_state.get("internal_id")
        self.assertIsNotNone(internal_id)
        self.assertTrue(validate_uuid4(internal_id))

        # Re-confirming same category preserves exact same internal_id
        dm.process("任务类型还是管缆巡检")
        self.assertEqual(dm.task_state.get("internal_id"), internal_id)

        # Confirm internal_id is preserved on update and distinct from task_id
        dm.process("水深设置为 300 米")
        self.assertEqual(dm.task_state.get("internal_id"), internal_id)
        self.assertNotEqual(dm.task_state.get("internal_id"), dm.task_state.get("task_id"))

        # User input attempting to set internal_id cannot overwrite it
        dm.process("把 internal_id 改成 12345678-1234-4234-8234-1234567890ab")
        self.assertEqual(dm.task_state.get("internal_id"), internal_id)

        # prepare without internal_id must fail closed
        kb = KnowledgeBase()
        ti_builder = TaskIntentBuilder(kb)
        with self.assertRaises(TaskPersistenceError):
            ti_builder.prepare({"task_id": "PI-20260803-001"}, {"task_id": "PI-20260803-001"}, "normal", "pipeline_inspection")

        # Non-UUIDv4 (e.g. UUIDv1) must be rejected
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
                "task_state": {"internal_id": "b1ffcd88-8b0a-4fe7-aa5c-5aa8ac270a22", "task_id": "PI-20260803-001", "intent_id": "TI2026080301"},
                "slot_store": {
                    "store_version": 1,
                    "slots": {
                        "internal_id": {"slot_name": "internal_id", "value": "b1ffcd88-8b0a-4fe7-aa5c-5aa8ac270a22", "status": "valid", "version": 1},
                        "task_id": {"slot_name": "task_id", "value": "PI-20260803-001", "status": "valid", "version": 1},
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


if __name__ == "__main__":
    unittest.main()
