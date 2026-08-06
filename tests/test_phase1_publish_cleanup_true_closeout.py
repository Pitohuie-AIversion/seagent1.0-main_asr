"""
tests/test_phase1_publish_cleanup_true_closeout.py
第一阶段清理竞态、回滚误删、残缺 final 拒绝与真实跨进程锁阻塞红测套件
"""

import copy
import json
import multiprocessing as mp
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.exceptions import IntentIdConflict, TaskPersistenceError
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.slot_store import Slot
from src.task_intent_builder import TaskIntentBuilder, TaskPublishLock


class DummyLLM(LLMClient):
    def __init__(self, default_reply="默认LLM回复"):
        self.llm = None
        self.default_reply = default_reply

    def chat(self, messages, temperature=0.7, max_tokens=800, **kwargs):
        return self.default_reply

    def generate(self, messages, temperature=0.7, max_tokens=800, **kwargs):
        return self.chat(messages, temperature, max_tokens)

    def filter_reply(self, text):
        return text


def _mp_lock_holder_create_staging(tmp_dir_str, hold_event, ready_event):
    """持锁 worker，用于测试 create_staging 被阻塞"""
    task_dir = Path(tmp_dir_str) / "task"
    with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
        lock = TaskPublishLock(task_dir)
        with lock:
            ready_event.set()
            hold_event.wait(timeout=15)


def _mp_contender_create_staging(tmp_dir_str, intent, res_queue, attempting_event=None):
    """争锁 worker: create_staging"""
    kb = KnowledgeBase()
    builder = TaskIntentBuilder(kb)
    task_dir = Path(tmp_dir_str) / "task"
    if attempting_event is not None:
        original_enter = TaskPublishLock.__enter__
        def observed_enter(self_lock):
            attempting_event.set()
            return original_enter(self_lock)
        patch_enter = patch.object(TaskPublishLock, "__enter__", observed_enter)
    else:
        patch_enter = patch("sys.path", sys.path)

    with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), patch_enter:
        try:
            st = builder.create_staging(intent)
            res_queue.put(("acquired", st.name, os.getpid()))
        except Exception as e:
            res_queue.put(("error", type(e).__name__, os.getpid()))


def _mp_contender_publish_staging(tmp_dir_str, intent, res_queue, attempting_event=None):
    """争锁 worker: publish_staging"""
    kb = KnowledgeBase()
    builder = TaskIntentBuilder(kb)
    task_dir = Path(tmp_dir_str) / "task"
    if attempting_event is not None:
        original_enter = TaskPublishLock.__enter__
        def observed_enter(self_lock):
            attempting_event.set()
            return original_enter(self_lock)
        patch_enter = patch.object(TaskPublishLock, "__enter__", observed_enter)
    else:
        patch_enter = patch("sys.path", sys.path)

    with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), patch_enter:
        try:
            st = builder.create_staging(intent)
            pub_name = builder.publish_staging(st, intent)
            res_queue.put(("acquired", pub_name, os.getpid()))
        except Exception as e:
            res_queue.put(("error", type(e).__name__, os.getpid()))


def _mp_contender_load_snapshot(tmp_dir_str, snap_dict, res_queue, attempting_event=None):
    """争锁 worker: load_snapshot"""
    kb = KnowledgeBase()
    llm = DummyLLM()
    dm = DialogueManager(llm, kb)
    task_dir = Path(tmp_dir_str) / "task"
    if attempting_event is not None:
        original_enter = TaskPublishLock.__enter__
        def observed_enter(self_lock):
            attempting_event.set()
            return original_enter(self_lock)
        patch_enter = patch.object(TaskPublishLock, "__enter__", observed_enter)
    else:
        patch_enter = patch("sys.path", sys.path)

    with patch("src.dialogue_manager.get_task_dir", return_value=task_dir), \
         patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
         patch("src.result_paths.get_task_dir", return_value=task_dir), \
         patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir_str)), \
         patch_enter:
        try:
            dm.load_snapshot(snap_dict)
            res_queue.put(("acquired", dm.phase, os.getpid()))
        except Exception as e:
            res_queue.put(("error", type(e).__name__, os.getpid()))


def _mp_load_snapshot_holder_pause_read(tmp_dir_str, snap_dict, in_read_event, resume_read_event, res_queue):
    """持锁并暂停读取 worker: 用于验证 load_snapshot 读取期间持锁"""
    import builtins
    kb = KnowledgeBase()
    llm = DummyLLM()
    dm = DialogueManager(llm, kb)
    task_dir = Path(tmp_dir_str) / "task"
    intent_id_val = snap_dict.get("slot_store", {}).get("slots", {}).get("intent_id", {}).get("value")
    target_pub_file = str(task_dir / f"task_intent_{intent_id_val}.json")

    real_open = builtins.open
    def hooked_open(file, *args, **kwargs):
        if str(file) == target_pub_file:
            in_read_event.set()
            resume_read_event.wait(timeout=15)
        return real_open(file, *args, **kwargs)

    with patch("src.dialogue_manager.get_task_dir", return_value=task_dir), \
         patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
         patch("src.result_paths.get_task_dir", return_value=task_dir), \
         patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir_str)), \
         patch("builtins.open", side_effect=hooked_open):
        try:
            dm.load_snapshot(snap_dict)
            res_queue.put(("holder_done", dm.phase, os.getpid()))
        except Exception as e:
            res_queue.put(("holder_error", type(e).__name__, os.getpid()))


def _mp_lock_contender(tmp_dir_str, res_queue, attempting_event=None):
    """争锁 worker: 直接争夺 TaskPublishLock"""
    task_dir = Path(tmp_dir_str) / "task"
    if attempting_event is not None:
        original_enter = TaskPublishLock.__enter__
        def observed_enter(self_lock):
            attempting_event.set()
            return original_enter(self_lock)
        patch_enter = patch.object(TaskPublishLock, "__enter__", observed_enter)
    else:
        patch_enter = patch("sys.path", sys.path)

    with patch_enter:
        lock = TaskPublishLock(task_dir)
        try:
            with lock:
                res_queue.put(("acquired", "lock_acquired", os.getpid()))
        except Exception as e:
            res_queue.put(("error", type(e).__name__, os.getpid()))


class PublishCleanupTrueCloseoutTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.builder = TaskIntentBuilder(self.kb)

    def _make_valid_intent(self, intent_id="TI2026063001"):
        return {
            "schema_version": 2,
            "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "task_id": "PI-20260630-001",
            "intent_id": intent_id,
            "task_type": "pipeline_inspection",
            "priority": 7,
            "time": {"start": "2026-06-30T10:00:00+08:00", "end": "2026-06-30T12:00:00+08:00"},
            "location": {"oilfield": "南海一号", "water_depth_m": 300.0},
            "task": {
                "type": "pipeline_inspection",
                "details": {
                    "pipeline_type": "subsea_oil_gas",
                    "start_point": {"latitude": 20.0, "longitude": 110.0},
                    "end_point": {"latitude": 20.1, "longitude": 110.1},
                },
            },
            "equipment": {
                "robot_type": "observation_rov",
                "payload": ["camera"],
                "support_vessel": {"name": "海洋石油201", "latitude": None, "longitude": None},
            },
            "conditions": {},
        }

    def test_01_claim_cleanup_true_replacement_window(self):
        """1. claim 清理真实替换窗口：完成 inode/所有权检查 -> 替换 claim 路径 -> 恢复执行删除逻辑"""
        intent = self._make_valid_intent("TI2026063001")
        forged = {"forged": True, "secret": "replacement_claim"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st = self.builder.create_staging(intent)

                real_rename = os.rename

                def race_claim_replace(src, dst):
                    real_rename(src, dst)
                    if ".claimed_" in str(dst):
                        with open(dst, "w", encoding="utf-8") as f:
                            json.dump(forged, f)

                with patch("os.rename", side_effect=race_claim_replace):
                    pub_name = self.builder.publish_staging(st, intent)

                final_file = task_dir / pub_name
                self.assertTrue(final_file.exists())

                claims = list(task_dir.glob(".claimed_*"))
                self.assertGreater(len(claims), 0, "Replaced claim file must survive deletion")
                with open(claims[0], "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data.get("secret"), "replacement_claim", "Replaced claim content must be preserved unchanged")

    def test_02_temp_rollback_true_replacement_window(self):
        """2. temp 回滚真实替换窗口：temp 已创建 -> 触发提交失败 -> 回滚删除前替换 temp 路径 -> 恢复回滚删除"""
        intent = self._make_valid_intent("TI2026063001")
        forged = {"forged": True, "secret": "replacement_temp"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st = self.builder.create_staging(intent)

                def hook_commit_fail_and_replace_temp(temp_file, final_file):
                    with open(temp_file, "w", encoding="utf-8") as f:
                        json.dump(forged, f)
                    raise OSError("Disk failure during commit")

                with patch("src.task_intent_builder._atomic_commit_noreplace", side_effect=hook_commit_fail_and_replace_temp):
                    with self.assertRaises(TaskPersistenceError):
                        self.builder.publish_staging(st, intent)

                final_file = task_dir / "task_intent_TI2026063001.json"
                self.assertFalse(final_file.exists())

                tmps = list(task_dir.glob(".tmp_publish_*"))
                self.assertGreater(len(tmps), 0, "Replaced temp file must survive rollback deletion")
                with open(tmps[0], "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data.get("secret"), "replacement_temp", "Replaced temp content must be preserved unchanged")

    def test_03_final_exists_staging_replacement_window(self):
        """3. final 已存在时的 staging 替换：检测到 final 已存在，在 staging 清理前替换 staging 路径，旧 final 不变，抛出 IntentIdConflict，替换 staging 存活且内容不变"""
        intent = self._make_valid_intent("TI2026063001")
        old_final_content = copy.deepcopy(intent)
        old_final_content["priority"] = 1
        forged_staging = {"forged": True, "secret": "replacement_staging"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            final_file = task_dir / "task_intent_TI2026063001.json"
            with open(final_file, "w", encoding="utf-8") as f:
                json.dump(old_final_content, f)

            staging_file = task_dir / f"task_intent_TI2026063001.staging_{os.getpid()}_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                with open(staging_file, "w", encoding="utf-8") as f:
                    json.dump(forged_staging, f)

                with self.assertRaises(IntentIdConflict):
                    self.builder.publish_staging(staging_file, intent)

            with open(final_file, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f)["priority"], 1)

            self.assertTrue(staging_file.exists(), "Replaced staging file must survive when target final exists")
            with open(staging_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("secret"), "replacement_staging", "Replaced staging content must be preserved unchanged")

    def test_04_dialogue_manager_rollback_does_not_delete_replaced_staging(self):
        """4. dialogue_manager 异常回滚：制造 publish_staging() 失败，同时在异常回滚前替换 staging，dialogue_manager.py 不得删除替换文件，会话状态正确回滚"""
        from tests.test_slot_consistency import seed_complete_valid_pipeline_task
        kb = KnowledgeBase()
        llm = DummyLLM()
        dm = DialogueManager(llm, kb)
        seed_complete_valid_pipeline_task(dm, kb)

        forged_staging = {"forged": True, "secret": "replaced_by_attacker_in_dm_rollback"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)

            dm.phase = "confirming"
            dm.slot_store.slots["intent_id"].value = "TI2026063001"
            dm.slot_store.slots["intent_id"].status = "valid"
            dm.task_state["intent_id"] = "TI2026063001"

            def fake_publish_fail_and_replace_staging(staging_path, intent_dict):
                p_path = Path(staging_path)
                if p_path.exists():
                    os.unlink(p_path)
                with open(p_path, "w", encoding="utf-8") as f:
                    json.dump(forged_staging, f)
                raise TaskPersistenceError("Simulated publish failure")

            with patch("src.dialogue_manager.get_task_dir", return_value=task_dir), \
                 patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("src.task_intent_builder.TaskIntentBuilder.publish_staging", side_effect=fake_publish_fail_and_replace_staging):

                with self.assertRaises(TaskPersistenceError):
                    dm.process("确认发布")

            self.assertNotEqual(dm.phase, "done")

            stagings = list(task_dir.glob("task_intent_TI2026063001.staging_*"))
            self.assertGreater(len(stagings), 0, "DialogueManager must not delete replaced staging file outside lock")
            with open(stagings[0], "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("secret"), "replaced_by_attacker_in_dm_rollback", "Replaced staging content must remain untouched")

    def test_05_consumer_rejects_incomplete_final_structures(self):
        """5. 消费者残缺结构校验：至少 test {"intent_id": "TI2026063001"} 和 {"intent_id": "TI2026063001", "task_type": "pipeline_inspection"} 均被拒绝，Phase 不得进入 done"""
        kb = KnowledgeBase()
        llm = DummyLLM()

        incomplete_cases = [
            {"intent_id": "TI2026063001"},
            {"intent_id": "TI2026063001", "task_type": "pipeline_inspection"},
            {"intent_id": "TI2026063001", "priority": 7},
            {"intent_id": "TI2026063001", "task_type": "invalid_type", "priority": 7, "time": {}, "location": {}, "task": {}, "equipment": {}, "conditions": {}},
            {"intent_id": "TI2026063001_mismatch", "task_type": "pipeline_inspection", "priority": 7, "time": {}, "location": {}, "task": {}, "equipment": {}, "conditions": {}},
        ]

        for idx, bad_final in enumerate(incomplete_cases):
            dm = DialogueManager(llm, kb)
            with tempfile.TemporaryDirectory() as tmp_dir:
                task_dir = Path(tmp_dir) / "task"
                task_dir.mkdir(parents=True, exist_ok=True)
                pub_file = task_dir / "task_intent_TI2026063001.json"
                with open(pub_file, "w", encoding="utf-8") as f:
                    json.dump(bad_final, f)

                snap = {
                    "phase": "done",
                    "mode": "normal",
                    "task_state": {"task_type_key": "pipeline_inspection", "water_depth": 300.0, "intent_id": "TI2026063001"},
                    "built_json": {"task_type_key": "pipeline_inspection", "water_depth": 300.0, "intent_id": "TI2026063001"},
                    "slot_store": {
                        "store_version": 1,
                        "slots": {
                            "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "version": 1},
                            "water_depth": {"slot_name": "water_depth", "value": 300.0, "status": "valid", "version": 1},
                            "intent_id": {"slot_name": "intent_id", "value": "TI2026063001", "status": "valid", "version": 1},
                        },
                        "unresolved": [],
                    },
                }

                with patch("src.dialogue_manager.get_task_dir", return_value=task_dir), \
                     patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                     patch("src.result_paths.get_task_dir", return_value=task_dir), \
                     patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                    dm.load_snapshot(snap)

                self.assertNotEqual(
                    dm.phase,
                    "done",
                    f"Case {idx} with malformed/incomplete final {bad_final} must be rejected by consumer and NOT enter done phase",
                )

    def test_06_real_lock_blocking_proof(self):
        """6. 真实锁阻塞证明：进程 A 持锁时，进程 B 的 create_staging / publish_staging / load_snapshot 均被真实阻塞"""
        import time
        intent = self._make_valid_intent("TI2026063001")
        ctx = mp.get_context("spawn")

        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)

            st = self.builder.create_staging(intent)

            # Test 6.1: create_staging blocked
            res_q1 = ctx.Queue()
            ready_e1 = ctx.Event()
            hold_e1 = ctx.Event()
            attempt_e1 = ctx.Event()

            p_holder1 = ctx.Process(target=_mp_lock_holder_create_staging, args=(tmp_dir, hold_e1, ready_e1))
            p_holder1.start()
            ready_e1.wait(timeout=15)

            p_contender1 = ctx.Process(target=_mp_contender_create_staging, args=(tmp_dir, intent, res_q1, attempt_e1))
            p_contender1.start()
            attempt_e1.wait(timeout=15)
            time.sleep(0.1)

            self.assertTrue(p_contender1.is_alive(), "Process B create_staging must be blocked while Process A holds lock")
            self.assertTrue(res_q1.empty(), "Process B must not have acquired lock yet")

            hold_e1.set()
            p_holder1.join(timeout=15)
            p_contender1.join(timeout=15)
            res1 = res_q1.get(timeout=15)
            self.assertEqual(res1[0], "acquired")

            # Test 6.2: publish_staging blocked
            res_q2 = ctx.Queue()
            ready_e2 = ctx.Event()
            hold_e2 = ctx.Event()
            attempt_e2 = ctx.Event()

            p_holder2 = ctx.Process(target=_mp_lock_holder_create_staging, args=(tmp_dir, hold_e2, ready_e2))
            p_holder2.start()
            ready_e2.wait(timeout=15)

            p_contender2 = ctx.Process(target=_mp_contender_publish_staging, args=(tmp_dir, intent, res_q2, attempt_e2))
            p_contender2.start()
            attempt_e2.wait(timeout=15)
            time.sleep(0.1)

            self.assertTrue(p_contender2.is_alive(), "Process B publish_staging must be blocked while Process A holds lock")
            self.assertTrue(res_q2.empty(), "Process B must not have acquired lock yet")

            hold_e2.set()
            p_holder2.join(timeout=15)
            p_contender2.join(timeout=15)
            res2 = res_q2.get(timeout=15)
            self.assertEqual(res2[0], "acquired")

            # Test 6.3: load_snapshot blocked
            pub_file = task_dir / "task_intent_TI2026063001.json"
            with open(pub_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            snap_full = {
                "phase": "done",
                "mode": "normal",
                "task_state": {"task_type_key": "pipeline_inspection", "water_depth": 300.0, "intent_id": "TI2026063001"},
                "built_json": intent,
                "slot_store": {
                    "store_version": 1,
                    "slots": {
                        "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "version": 1},
                        "water_depth": {"slot_name": "water_depth", "value": 300.0, "status": "valid", "version": 1},
                        "intent_id": {"slot_name": "intent_id", "value": "TI2026063001", "status": "valid", "version": 1},
                    },
                    "unresolved": [],
                },
            }

            res_q3 = ctx.Queue()
            ready_e3 = ctx.Event()
            hold_e3 = ctx.Event()
            attempt_e3 = ctx.Event()

            p_holder3 = ctx.Process(target=_mp_lock_holder_create_staging, args=(tmp_dir, hold_e3, ready_e3))
            p_holder3.start()
            ready_e3.wait(timeout=15)

            p_contender3 = ctx.Process(target=_mp_contender_load_snapshot, args=(tmp_dir, snap_full, res_q3, attempt_e3))
            p_contender3.start()
            attempt_e3.wait(timeout=15)
            time.sleep(0.1)

            self.assertTrue(p_contender3.is_alive(), "Process B load_snapshot must be blocked while Process A holds lock")
            self.assertTrue(res_q3.empty(), "Process B must not have acquired lock yet")

            hold_e3.set()
            p_holder3.join(timeout=15)
            p_contender3.join(timeout=15)
            res3 = res_q3.get(timeout=15)
            self.assertEqual(res3[0], "acquired")

    def test_load_snapshot_holds_lock_during_file_read(self):
        """测试 B：读取期间仍持锁证明，验证 load_snapshot 在打开/读取文件期间持续持有 TaskPublishLock"""
        import time
        intent = self._make_valid_intent("TI2026063002")
        ctx = mp.get_context("spawn")

        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)

            pub_file = task_dir / "task_intent_TI2026063002.json"
            with open(pub_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            snap_full = {
                "phase": "done",
                "mode": "normal",
                "task_state": {"internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "task_id": "PI-20260630-001", "task_type_key": "pipeline_inspection", "water_depth": 300.0, "intent_id": "TI2026063002"},
                "built_json": intent,
                "slot_store": {
                    "store_version": 1,
                    "slots": {
                        "internal_id": {"slot_name": "internal_id", "value": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "status": "valid", "version": 1},
                        "task_id": {"slot_name": "task_id", "value": "PI-20260630-001", "status": "valid", "version": 1},
                        "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "version": 1},
                        "water_depth": {"slot_name": "water_depth", "value": 300.0, "status": "valid", "version": 1},
                        "intent_id": {"slot_name": "intent_id", "value": "TI2026063002", "status": "valid", "version": 1},
                    },
                    "unresolved": [],
                },
            }

            res_q_holder = ctx.Queue()
            res_q_contender = ctx.Queue()
            in_read_e = ctx.Event()
            resume_read_e = ctx.Event()
            attempt_e = ctx.Event()

            # 1. 启动 Process A: 执行 load_snapshot，会在 open(pub_file) 时暂停
            p_holder = ctx.Process(target=_mp_load_snapshot_holder_pause_read, args=(tmp_dir, snap_full, in_read_e, resume_read_e, res_q_holder))
            p_holder.start()

            # 2. 等待 Process A 获得锁并进入 open(pub_file) 阶段
            self.assertTrue(in_read_e.wait(timeout=15), "Process A load_snapshot must enter open(pub_file)")
            self.assertTrue(p_holder.is_alive())

            # 3. 启动 Process B: 尝试获取同一 TaskPublishLock
            p_contender = ctx.Process(target=_mp_lock_contender, args=(tmp_dir, res_q_contender, attempt_e))
            p_contender.start()
            self.assertTrue(attempt_e.wait(timeout=15), "Process B must reach TaskPublishLock.__enter__")
            time.sleep(0.1)

            # 4. 验证 Process B 到达 __enter__ 但被真正阻塞在 flock 锁上，且尚未获取锁
            self.assertTrue(p_contender.is_alive(), "Process B must be blocked while Process A is reading file inside load_snapshot")
            self.assertTrue(res_q_contender.empty(), "Process B must not acquire lock while Process A is reading file")

            # 5. 允许 Process A 继续完成文件读取并释放锁
            resume_read_e.set()
            p_holder.join(timeout=15)
            p_contender.join(timeout=15)

            res_holder = res_q_holder.get(timeout=15)
            self.assertEqual(res_holder[0], "holder_done")
            self.assertEqual(res_holder[1], "done")

            res_contender = res_q_contender.get(timeout=15)
            self.assertEqual(res_contender[0], "acquired")

    def test_pipeline_burial_done_snapshot_restores_successfully(self):
        """验证 pipeline_burial 任务在完成发布后，保存的 done 状态 snapshot 能成功被恢复。
        恢复后 phase 为 done，final_result.task_type 为 pipeline_burial，且 intent_id 保持不变。
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            res_dir = Path(tmp_dir)
            res_dir.mkdir(parents=True, exist_ok=True)

            kb = KnowledgeBase()
            llm = DummyLLM()

            with patch("src.dialogue_manager.get_task_dir", return_value=task_dir), \
                 patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("src.result_paths.get_task_dir", return_value=task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=res_dir):
                dm = DialogueManager(llm, kb)

                # 1. 种子灌入合法的 pipeline_burial 任务槽位
                task_type_key = "pipeline_burial"
                task_type = kb.task_schemas.get("task_templates", {}).get(task_type_key, {}).get("task_type_values", ["海管埋设"])[0]
                allowed_rovs = kb.get_task_allowed_robot_variants(task_type_key)
                selected_rov = allowed_rovs[0] if allowed_rovs else kb.get_all_rovs()[0]

                from src.simulated_time import get_current_datetime
                from datetime import timedelta
                now_dt = get_current_datetime()
                slots_to_seed = {
                    "internal_id": ("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "string"),
                    "task_id": ("PB-20260718-001", "string"),
                    "task_type_key": (task_type_key, "string"),
                    "task_type": (task_type, "string"),
                    "cable_type": ("海底油气管道", "string"),
                    "water_depth": (150.0, "number"),
                    "buried_depth": (2.0, "number"),
                    "start_time": (now_dt.strftime("%Y-%m-%dT%H:%M:%S"), "datetime"),
                    "end_time": ((now_dt + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S"), "datetime"),
                    "start_point": ({"lat": 20.0, "lon": 110.0}, "coord"),
                    "end_point": ({"lat": 20.1, "lon": 110.1}, "coord"),
                    "equipment_class": (selected_rov.get("robot_class") or "cable_burial_robot", "string"),
                    "equipment_family": (selected_rov.get("family_full_name") or selected_rov.get("family") or "Work Class ROV", "string"),
                    "equipment_specification": ({"type": "power_hp", "value": 1600, "unit": "hp", "display_value": "1600HP", "variant_id": selected_rov.get("variant_id", "crawler_heavy_seabed_robot_1600hp")}, "object"),
                    "equipment_type": (selected_rov["full_name"], "string"),
                    "equipment_unit_id": (selected_rov.get("unit_ids", ["WCROV-STD-001"])[0], "string"),
                    "payload": (["高清水下摄像机"], "list"),
                    "support_vessel": ("DSV-Oceanic", "string"),
                    "intent_id": ("TI2026063002", "string"),
                }

                for key, (val, vtype) in slots_to_seed.items():
                    dm.slot_store.slots[key] = Slot(slot_name=key, value=val, value_type=vtype, status="valid", source="user_input")

                unit_id = selected_rov.get("unit_ids", ["WCROV-STD-001"])[0]
                if str(kb.state_info.state_file).endswith("config/state.yaml"):
                    temp_state_file = Path(tempfile.gettempdir()) / f"state_test_closeout_{os.getpid()}_{id(kb)}.yaml"
                    if not temp_state_file.exists() and Path(kb.state_info.state_file).exists():
                        import shutil
                        shutil.copy(kb.state_info.state_file, temp_state_file)
                    kb.state_info.state_file = temp_state_file

                kb.state_info.set_status(unit_id, {"overall_status": "available"})

                dm._rebuild_cache()

                # Whitelist any soft constraint warnings
                all_v = dm.validator.validate(dm.task_state)
                for v in all_v:
                    if v.severity == "soft":
                        for f in v.related_fields:
                            val = dm.task_state.get(f)
                            if val is not None:
                                dm._soft_whitelist.add((f, str(val), v.constraint_id))

                # 2. 正式生成 staging 文件并发布发布件
                dm.phase = "confirming"
                dm._handle_final_publish_confirmation("确认发布", "req_test")
                self.assertEqual(dm.phase, "done")
                orig_intent_id = dm.task_state.get("intent_id")
                self.assertEqual(orig_intent_id, "TI2026063002")

                # 3. 导出 snapshot 字典
                snap = {
                    "snapshot_version": 2,
                    "phase": "done",
                    "mode": dm.mode,
                    "task_state": copy.deepcopy(dm.task_state),
                    "slot_store": dm.slot_store.export_snapshot(),
                }
                self.assertEqual(snap.get("phase"), "done")

                # 4. 新建 DialogueManager 实例并恢复 snapshot
                dm_new = DialogueManager(llm, kb)
                dm_new.load_snapshot(snap)

                # 5. 断言恢复成功，状态为 done，task_type 为 pipeline_burial，且 intent_id 保持一致
                self.assertEqual(dm_new.phase, "done")
                self.assertIsNotNone(dm_new.final_result)
                self.assertEqual(dm_new.final_result.get("task_type"), "pipeline_burial")
                self.assertEqual(dm_new.task_state.get("intent_id"), orig_intent_id)
                self.assertEqual(dm_new.slot_store.slots["intent_id"].value, orig_intent_id)


if __name__ == "__main__":
    unittest.main()
