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
            hold_event.wait(timeout=5)


def _mp_contender_create_staging(tmp_dir_str, intent, res_queue):
    """争锁 worker: create_staging"""
    kb = KnowledgeBase()
    builder = TaskIntentBuilder(kb)
    task_dir = Path(tmp_dir_str) / "task"
    with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
        try:
            st = builder.create_staging(intent)
            res_queue.put(("acquired", st.name, os.getpid()))
        except Exception as e:
            res_queue.put(("error", type(e).__name__, os.getpid()))


def _mp_contender_publish_staging(tmp_dir_str, intent, res_queue):
    """争锁 worker: publish_staging"""
    kb = KnowledgeBase()
    builder = TaskIntentBuilder(kb)
    task_dir = Path(tmp_dir_str) / "task"
    with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
        try:
            st = builder.create_staging(intent)
            pub_name = builder.publish_staging(st, intent)
            res_queue.put(("acquired", pub_name, os.getpid()))
        except Exception as e:
            res_queue.put(("error", type(e).__name__, os.getpid()))


def _mp_contender_load_snapshot(tmp_dir_str, snap_dict, res_queue):
    """争锁 worker: load_snapshot"""
    kb = KnowledgeBase()
    llm = DummyLLM()
    dm = DialogueManager(llm, kb)
    task_dir = Path(tmp_dir_str) / "task"
    with patch("src.dialogue_manager.get_task_dir", return_value=task_dir), \
         patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
         patch("src.result_paths.get_task_dir", return_value=task_dir), \
         patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir_str)):
        try:
            dm.load_snapshot(snap_dict)
            res_queue.put(("acquired", dm.phase, os.getpid()))
        except Exception as e:
            res_queue.put(("error", type(e).__name__, os.getpid()))


class PublishCleanupTrueCloseoutTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.builder = TaskIntentBuilder(self.kb)

    def _make_valid_intent(self, intent_id="TI2026063001"):
        return {
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

            p_holder1 = ctx.Process(target=_mp_lock_holder_create_staging, args=(tmp_dir, hold_e1, ready_e1))
            p_holder1.start()
            ready_e1.wait(timeout=5)

            p_contender1 = ctx.Process(target=_mp_contender_create_staging, args=(tmp_dir, intent, res_q1))
            p_contender1.start()

            p_contender1.join(timeout=0.4)
            self.assertTrue(p_contender1.is_alive(), "Process B create_staging must be blocked while Process A holds lock")

            hold_e1.set()
            p_holder1.join(timeout=5)
            p_contender1.join(timeout=5)
            res1 = res_q1.get(timeout=2)
            self.assertEqual(res1[0], "acquired")

            # Test 6.2: publish_staging blocked
            res_q2 = ctx.Queue()
            ready_e2 = ctx.Event()
            hold_e2 = ctx.Event()

            p_holder2 = ctx.Process(target=_mp_lock_holder_create_staging, args=(tmp_dir, hold_e2, ready_e2))
            p_holder2.start()
            ready_e2.wait(timeout=5)

            p_contender2 = ctx.Process(target=_mp_contender_publish_staging, args=(tmp_dir, intent, res_q2))
            p_contender2.start()

            p_contender2.join(timeout=0.4)
            self.assertTrue(p_contender2.is_alive(), "Process B publish_staging must be blocked while Process A holds lock")

            hold_e2.set()
            p_holder2.join(timeout=5)
            p_contender2.join(timeout=5)
            res2 = res_q2.get(timeout=2)
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

            p_holder3 = ctx.Process(target=_mp_lock_holder_create_staging, args=(tmp_dir, hold_e3, ready_e3))
            p_holder3.start()
            ready_e3.wait(timeout=5)

            p_contender3 = ctx.Process(target=_mp_contender_load_snapshot, args=(tmp_dir, snap_full, res_q3))
            p_contender3.start()

            p_contender3.join(timeout=0.4)
            self.assertTrue(p_contender3.is_alive(), "Process B load_snapshot must be blocked while Process A holds lock")

            hold_e3.set()
            p_holder3.join(timeout=5)
            p_contender3.join(timeout=5)
            res3 = res_q3.get(timeout=2)
            self.assertEqual(res3[0], "acquired")


if __name__ == "__main__":
    unittest.main()
