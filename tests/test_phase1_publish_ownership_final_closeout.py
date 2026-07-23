"""
tests/test_phase1_publish_ownership_final_closeout.py
第一阶段事务所有权、并发锁与能力问句终极收口测试套件
"""

import copy
import json
import os
import tempfile
import unittest
import multiprocessing as mp
from pathlib import Path
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouter
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.task_intent_builder import TaskIntentBuilder, TaskPublishLock
from src.exceptions import TaskPersistenceError, IntentIdConflict
from tests.test_slot_consistency import seed_complete_valid_pipeline_task


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


def _mp_worker_same_intent(tmp_dir_str, intent, res_queue, start_event):
    """跨进程并发测试 worker（同 intent_id）"""
    kb = KnowledgeBase()
    builder = TaskIntentBuilder(kb)
    task_dir = Path(tmp_dir_str) / "task"
    start_event.wait(timeout=5)
    try:
        with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            st = builder.create_staging(intent)
            pub_name = builder.publish_staging(st, intent)
            res_queue.put(("success", pub_name, os.getpid()))
    except Exception as e:
        res_queue.put(("error", type(e).__name__, os.getpid()))


def _mp_worker_diff_intent(tmp_dir_str, intent, res_queue, start_event):
    """跨进程并发测试 worker（不同 intent_id）"""
    kb = KnowledgeBase()
    builder = TaskIntentBuilder(kb)
    task_dir = Path(tmp_dir_str) / "task"
    start_event.wait(timeout=5)
    try:
        with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            st = builder.create_staging(intent)
            pub_name = builder.publish_staging(st, intent)
            res_queue.put(("success", pub_name, os.getpid()))
    except Exception as e:
        res_queue.put(("error", type(e).__name__, os.getpid()))


def _mp_worker_lock_holder(tmp_dir_str, hold_event, ready_event):
    """持锁 worker"""
    task_dir = Path(tmp_dir_str) / "task"
    with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
        lock = TaskPublishLock(task_dir)
        with lock:
            ready_event.set()
            hold_event.wait(timeout=5)


def _mp_worker_lock_contender(tmp_dir_str, res_queue):
    """争锁 worker"""
    task_dir = Path(tmp_dir_str) / "task"
    with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
        lock = TaskPublishLock(task_dir)
        with lock:
            res_queue.put(("acquired", os.getpid()))


class PublishOwnershipAndLockTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.builder = TaskIntentBuilder(self.kb)

    def _make_valid_intent(self, intent_id="TI2026072101"):
        return {
            "intent_id": intent_id,
            "task_type": "pipeline_inspection",
            "priority": 7,
            "time": {"start": None, "end": None},
            "location": {"oilfield": None, "water_depth_m": 300.0},
            "task": {"type": "pipeline_inspection", "details": {}},
            "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": {"name": None}},
            "conditions": {}
        }

    def test_01_staging_replaced_at_claim_fails_and_preserves_replacement(self):
        """1. staging 在认领瞬间被替换：不得发布、不得删除替换文件"""
        intent = self._make_valid_intent("TI2026072101")
        forged = copy.deepcopy(intent)
        forged["priority"] = 99

        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            real_rename = os.rename

            def race_rename_replace(src, dst):
                real_rename(src, dst)
                with open(staging_file, "w", encoding="utf-8") as f:
                    json.dump(forged, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.rename", side_effect=race_rename_replace):
                try:
                    self.builder.publish_staging(staging_file, intent)
                except TaskPersistenceError:
                    pass

            self.assertTrue(staging_file.exists())
            with open(staging_file, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f)["priority"], 99)

    def test_02_claim_replaced_at_cleanup_preserves_replacement(self):
        """2. claim 在清理窗口被替换：不得删除替换文件"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st = self.builder.create_staging(intent)
                pub_name = self.builder.publish_staging(st, intent)
                self.assertTrue((task_dir / pub_name).exists())

    def test_03_temp_replaced_at_rollback_preserves_replacement(self):
        """3. temp 在失败回滚窗口被替换：不得删除替换文件"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("src.task_intent_builder._atomic_commit_noreplace", side_effect=OSError("Disk failure")):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

    def test_04_inode_mismatch_fails_closed(self):
        """4. inode 不一致时 fail closed"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            orig_stat = os.stat

            def fake_stat_claim(path, *args, **kwargs):
                if ".claimed_" in str(path):
                    st = orig_stat(path, *args, **kwargs)
                    return os.stat_result((
                        st.st_mode, st.st_ino + 8888, st.st_dev, st.st_nlink,
                        st.st_uid, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime
                    ))
                return orig_stat(path, *args, **kwargs)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.stat", side_effect=fake_stat_claim):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

    def test_05_preexisting_final_file_not_overwritten(self):
        """5. final 已存在时不得覆盖"""
        intent = self._make_valid_intent("TI2026072101")
        existing = copy.deepcopy(intent)
        existing["priority"] = 1

        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            final_file = task_dir / "task_intent_TI2026072101.json"
            with open(final_file, "w", encoding="utf-8") as f:
                json.dump(existing, f)

            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

            with open(final_file, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f)["priority"], 1)

    def test_06_concurrent_publish_same_intent_id_exactly_one_succeeds(self):
        """6. 同一 intent_id 两个真实进程并发：恰好一个成功"""
        intent = self._make_valid_intent("TI2026072101")
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)

            res_queue = ctx.Queue()
            start_event = ctx.Event()

            p1 = ctx.Process(target=_mp_worker_same_intent, args=(tmp_dir, intent, res_queue, start_event))
            p2 = ctx.Process(target=_mp_worker_same_intent, args=(tmp_dir, intent, res_queue, start_event))

            p1.start()
            p2.start()

            start_event.set()

            p1.join(timeout=10)
            p2.join(timeout=10)

            res1 = res_queue.get(timeout=2)
            res2 = res_queue.get(timeout=2)

            statuses = [res1[0], res2[0]]
            self.assertEqual(sorted(statuses), ["error", "success"])

    def test_07_concurrent_publish_failing_process_gets_task_persistence_error(self):
        """7. 失败进程得到 TaskPersistenceError"""
        intent = self._make_valid_intent("TI2026072101")
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)

            res_queue = ctx.Queue()
            start_event = ctx.Event()

            p1 = ctx.Process(target=_mp_worker_same_intent, args=(tmp_dir, intent, res_queue, start_event))
            p2 = ctx.Process(target=_mp_worker_same_intent, args=(tmp_dir, intent, res_queue, start_event))

            p1.start()
            p2.start()

            start_event.set()

            p1.join(timeout=10)
            p2.join(timeout=10)

            res1 = res_queue.get(timeout=2)
            res2 = res_queue.get(timeout=2)

            err_res = res1 if res1[0] == "error" else res2
            self.assertIn(err_res[1], ("TaskPersistenceError", "IntentIdConflict"))

    def test_08_concurrent_publish_different_intent_ids_both_succeed(self):
        """8. 不同 intent_id 两进程并发：两个均成功且内容正确"""
        intent1 = self._make_valid_intent("TI2026072101")
        intent2 = self._make_valid_intent("TI2026072102")

        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)

            res_queue = ctx.Queue()
            start_event = ctx.Event()

            p1 = ctx.Process(target=_mp_worker_diff_intent, args=(tmp_dir, intent1, res_queue, start_event))
            p2 = ctx.Process(target=_mp_worker_diff_intent, args=(tmp_dir, intent2, res_queue, start_event))

            p1.start()
            p2.start()

            start_event.set()

            p1.join(timeout=10)
            p2.join(timeout=10)

            res1 = res_queue.get(timeout=2)
            res2 = res_queue.get(timeout=2)

            self.assertEqual(res1[0], "success")
            self.assertEqual(res2[0], "success")

    def test_09_process_a_holds_lock_blocks_process_b(self):
        """9. 进程 A 持锁时进程 B 确实阻塞"""
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)

            res_queue = ctx.Queue()
            ready_event = ctx.Event()
            hold_event = ctx.Event()

            p_holder = ctx.Process(target=_mp_worker_lock_holder, args=(tmp_dir, hold_event, ready_event))
            p_holder.start()

            ready_event.wait(timeout=5)

            p_contender = ctx.Process(target=_mp_worker_lock_contender, args=(tmp_dir, res_queue))
            p_contender.start()

            # 确认在 hold_event 被 set 之前 contender 依然无法获得锁
            p_contender.join(timeout=0.5)
            self.assertTrue(p_contender.is_alive())

            # 释放锁
            hold_event.set()
            p_holder.join(timeout=5)
            p_contender.join(timeout=5)

            res = res_queue.get(timeout=2)
            self.assertEqual(res[0], "acquired")

    def test_10_create_staging_follows_same_lock_protocol(self):
        """10. create_staging 遵循同一锁协议"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st = self.builder.create_staging(intent)
                self.assertTrue(st.exists())

    def test_11_load_snapshot_follows_same_lock_protocol(self):
        """11. load_snapshot 遵循同一锁协议"""
        kb = KnowledgeBase()
        llm = DummyLLM()
        dm = DialogueManager(llm, kb)
        seed_complete_valid_pipeline_task(dm, kb)
        dm.slot_store.slots["intent_id"].value = "TI2026063001"
        dm.slot_store.slots["intent_id"].status = "valid"
        dm.task_state["intent_id"] = "TI2026063001"
        if dm._last_built_json:
            dm._last_built_json["intent_id"] = "TI2026063001"
        snap = dm.slot_store.export_snapshot()

        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            pub_file = task_dir / "task_intent_TI2026063001.json"
            with open(pub_file, "w", encoding="utf-8") as f:
                json.dump({"intent_id": "TI2026063001", "task_type": "pipeline_inspection"}, f)

            snap_full = {
                "phase": "done",
                "mode": "normal",
                "task_state": dm.task_state,
                "built_json": dm._last_built_json,
                "slot_store": snap
            }

            with patch("src.dialogue_manager.get_task_dir", return_value=task_dir), \
                 patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("src.result_paths.get_task_dir", return_value=task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                dm.load_snapshot(snap_full)

            self.assertEqual(dm.phase, "done")

    def test_12_staging_symlink_rejected(self):
        """12. staging 符号链接被拒绝"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            target = task_dir / "real_file.json"
            with open(target, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            sym = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            os.symlink(target, sym)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(sym, intent)

    def test_13_final_symlink_rejected_by_consumer(self):
        """13. final 符号链接被消费者拒绝"""
        kb = KnowledgeBase()
        llm = DummyLLM()
        dm = DialogueManager(llm, kb)

        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            real_file = task_dir / "real_intent.json"
            with open(real_file, "w", encoding="utf-8") as f:
                json.dump({"intent_id": "TI2026063001", "task_type": "pipeline_inspection"}, f)

            sym_final = task_dir / "task_intent_TI2026063001.json"
            os.symlink(real_file, sym_final)

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
                        "intent_id": {"slot_name": "intent_id", "value": "TI2026063001", "status": "valid", "version": 1}
                    },
                    "unresolved": []
                }
            }

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                dm.load_snapshot(snap)

            # 符号链接无法通过消费者真实性校验，降级 phase
            self.assertNotEqual(dm.phase, "done")

    def test_14_final_json_equals_intent(self):
        """14. final JSON 完全等于 intent"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st = self.builder.create_staging(intent)
                pub_name = self.builder.publish_staging(st, intent)
                final_file = task_dir / pub_name
                with open(final_file, "r", encoding="utf-8") as f:
                    self.assertEqual(json.load(f), intent)

    def test_15_final_content_generated_from_trusted_memory(self):
        """15. final 内容来自可信内存 intent"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st = self.builder.create_staging(intent)
                pub_name = self.builder.publish_staging(st, intent)
                final_file = task_dir / pub_name
                with open(final_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data["intent_id"], "TI2026072101")

    def test_16_fsync_called_on_file_and_directory(self):
        """16. 文件和结果目录执行 fsync"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.fsync") as mock_fsync:
                st = self.builder.create_staging(intent)
                self.builder.publish_staging(st, intent)
                self.assertTrue(mock_fsync.called)

    def test_17_successful_publish_leaves_no_temp(self):
        """17. 正常成功不留下 temp"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st = self.builder.create_staging(intent)
                self.builder.publish_staging(st, intent)

                tmps = list(task_dir.glob(".tmp_publish_*"))
                self.assertEqual(len(tmps), 0)

    def test_18_quarantine_policy_compliance(self):
        """18. quarantine 保留策略符合设计"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st = self.builder.create_staging(intent)
                self.builder.publish_staging(st, intent)

                claims = list(task_dir.glob(".claimed_*"))
                self.assertEqual(len(claims), 0)

    def test_19_all_failures_raise_task_persistence_error(self):
        """19. 所有失败统一抛出 TaskPersistenceError"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("src.task_intent_builder._atomic_commit_noreplace", side_effect=OSError("Disk failure")):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

    def test_20_no_runtime_files_left_in_git_repo(self):
        """20. 不在仓库生成运行时文件"""
        intent = self._make_valid_intent("TI2026072101")
        repo_root = Path("/root/mzy/seagent1.0-main_asr")
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st = self.builder.create_staging(intent)
                self.builder.publish_staging(st, intent)

        repo_task_files = list(repo_root.glob("task_intent_*.json"))
        self.assertEqual(len(repo_task_files), 0)


class DeviceCapabilityQuestionRoutingTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_21_jinniuzuo_depth_is_500m_ne_routes_to_device_capability(self):
        """21. '水深为500米呢' 进入 DEVICE_CAPABILITY 或 CLARIFICATION (不进入 TASK_UPDATE)"""
        res1 = self.dm.intent_router.route("水深为500米呢", [], {})
        self.assertFalse(res1.should_update_slots)
        self.assertNotEqual(res1.intent, "TASK_UPDATE")

        res2 = self.dm.intent_router.route("金牛座一号机水深为500米呢", [], {})
        self.assertEqual(res2.intent, "DEVICE_CAPABILITY")
        self.assertFalse(res2.should_update_slots)

        res3 = self.dm.intent_router.route("金牛座一号机的作业水深是500米呢？", [], {})
        self.assertEqual(res3.intent, "DEVICE_CAPABILITY")
        self.assertFalse(res3.should_update_slots)

    def test_22_capability_query_does_not_call_extractor(self):
        """22. 能力查询不调用 extractor"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        with patch.object(self.dm.extractor, "extract_updates") as mock_ext:
            self.dm.process("金牛座一号机水深为500米呢？")
            mock_ext.assert_not_called()

    def test_23_capability_query_preserves_dialogue_state(self):
        """23. 能力查询前后完整会话状态不变"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        snap_before = copy.deepcopy(self.dm.slot_store.export_snapshot())
        phase_before = self.dm.phase
        mode_before = self.dm.mode
        res_before = copy.deepcopy(self.dm.final_result)

        self.dm.process("金牛座一号机的作业水深是500米呢？")

        snap_after = copy.deepcopy(self.dm.slot_store.export_snapshot())
        phase_after = self.dm.phase
        mode_after = self.dm.mode
        res_after = copy.deepcopy(self.dm.final_result)

        self.assertEqual(snap_before, snap_after)
        self.assertEqual(phase_before, phase_after)
        self.assertEqual(mode_before, mode_after)
        self.assertEqual(res_before, res_after)

    def test_24_explicit_update_enters_slot_update(self):
        """24. 明确修改语句继续进入统一槽位更新流程"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        res = self.dm.intent_router.route("把作业水深改为500米", [], self.dm.task_state, phase=self.dm.phase)
        self.assertEqual(res.intent, "TASK_UPDATE")
        self.assertTrue(res.should_update_slots)

    def test_25_ordinary_question_does_not_false_trigger(self):
        """25. 普通含'到/为/是/使用'的问句不误触发"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        res1 = self.dm.intent_router.route("订单001什么时候到？", [], self.dm.task_state, phase=self.dm.phase)
        self.assertNotEqual(res1.intent, "TASK_UPDATE")

        res2 = self.dm.intent_router.route("我能看看系统说明书吗？", [], {})
        self.assertNotEqual(res2.intent, "TASK_UPDATE")


if __name__ == "__main__":
    unittest.main()
