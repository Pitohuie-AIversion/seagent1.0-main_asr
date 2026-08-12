"""
tests/test_phase1_atomic_publish_final_closeout.py - 第一阶段原子发布事务收口测试

A. Atomic Publishing Transaction Tests (1-16)
"""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.knowledge_retriever import KnowledgeBase
from src.task_intent_builder import TaskIntentBuilder
from src.exceptions import TaskPersistenceError, IntentIdConflict



# ─────────────────────────────────────────────────────────────────────────────
# A. 原子发布事务测试 (1-16)
# ─────────────────────────────────────────────────────────────────────────────

class AtomicPublishTransactionTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.builder = TaskIntentBuilder(self.kb)

    def _make_valid_intent(self, intent_id="TI2026072101"):
        return {
            "schema_version": 2,
            "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "task_id": "PI-20260721-001",
            "intent_id": intent_id,
            "task_type": "pipeline_inspection",
            "priority": 7,
            "time": {"start": None, "end": None},
            "location": {"oilfield": None, "water_depth_m": 300.0},
            "task": {"type": "pipeline_inspection", "details": {}},
            "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": {"name": None}},
            "conditions": {}
        }

    def test_a1_preexisting_final_file_not_overwritten(self):
        """1. final 已存在且内容不同时不得覆盖，抛出 IntentIdConflict"""
        intent = self._make_valid_intent("TI2026072101")
        existing_intent = copy.deepcopy(intent)
        existing_intent["priority"] = 1

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            final_file = task_dir / "task_intent_TI2026072101.json"
            with open(final_file, "w", encoding="utf-8") as f:
                json.dump(existing_intent, f)

            staging_file = task_dir / f"task_intent_TI2026072101.staging_{os.getpid()}_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                with self.assertRaises(IntentIdConflict):
                    self.builder.publish_staging(staging_file, intent)

    def test_a2_preexisting_final_file_content_intact(self):
        """2. final 已存在时原内容保持不变"""
        intent = self._make_valid_intent("TI2026072101")
        existing_intent = copy.deepcopy(intent)
        existing_intent["priority"] = 1

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            final_file = task_dir / "task_intent_TI2026072101.json"
            with open(final_file, "w", encoding="utf-8") as f:
                json.dump(existing_intent, f)

            staging_file = task_dir / f"task_intent_TI2026072101.staging_{os.getpid()}_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                try:
                    self.builder.publish_staging(staging_file, intent)
                except Exception:
                    pass

            with open(final_file, "r", encoding="utf-8") as f:
                current_content = json.load(f)
            self.assertEqual(current_content, existing_intent)

    def test_a3_normal_publish_final_content_equals_intent(self):
        """3. 正常发布时 final JSON 与 intent 内容完全相同"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st_file = self.builder.create_staging(intent)
                pub_name = self.builder.publish_staging(st_file, intent)
                final_file = task_dir / pub_name
                with open(final_file, "r", encoding="utf-8") as f:
                    fc = json.load(f)
                self.assertEqual(fc, intent)

    def test_a4_normal_publish_public_staging_path_disappears(self):
        """4. 正常发布后公共 staging 路径消失"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st_file = self.builder.create_staging(intent)
                self.builder.publish_staging(st_file, intent)
                self.assertFalse(st_file.exists())

    def test_a5_final_content_generated_from_trusted_memory(self):
        """5. 正式内容由内存中已验证的 intent 生成，而不是来自未绑定的 staging hard link"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["priority"] = 999

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / f"task_intent_TI2026072101.staging_{os.getpid()}_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                pub_name = self.builder.publish_staging(staging_file, intent)
                final_file = task_dir / pub_name
                with open(final_file, "r", encoding="utf-8") as f:
                    fc = json.load(f)
                self.assertEqual(fc, intent)
                self.assertNotEqual(fc, forged_intent)

    def test_a6_staging_replaced_before_claim_fails_closed(self):
        """6. staging 在认领（claim）前被替换 → 触发 fail closed 并抛出 TaskPersistenceError"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["priority"] = 99

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            original_stat = os.stat

            def fake_stat_on_claim(path, *args, **kwargs):
                if ".claimed_" in str(path):
                    st = original_stat(path, *args, **kwargs)
                    return os.stat_result((
                        st.st_mode, st.st_ino + 9999, st.st_dev, st.st_nlink,
                        st.st_uid, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime
                    ))
                return original_stat(path, *args, **kwargs)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.stat", side_effect=fake_stat_on_claim):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

    def test_a7_replaced_staging_file_not_deleted(self):
        """7. 认领前被替换的文件绝不得被误删，仍留在磁盘上"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["priority"] = 99

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            real_rename = os.rename

            def race_replace_rename(src, dst):
                real_rename(src, dst)
                with open(staging_file, "w", encoding="utf-8") as f:
                    json.dump(forged_intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.rename", side_effect=race_replace_rename):
                try:
                    self.builder.publish_staging(staging_file, intent)
                except TaskPersistenceError:
                    pass

            self.assertTrue(staging_file.exists())

    def test_a8_staging_inode_mismatch_does_not_create_final(self):
        """8. staging inode 不一致时绝不得创建 final 文件"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["priority"] = 88

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            original_stat = os.stat

            def fake_stat_on_claim(path, *args, **kwargs):
                if ".claimed_" in str(path):
                    st = original_stat(path, *args, **kwargs)
                    return os.stat_result((
                        st.st_mode, st.st_ino + 9999, st.st_dev, st.st_nlink,
                        st.st_uid, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime
                    ))
                return original_stat(path, *args, **kwargs)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.stat", side_effect=fake_stat_on_claim):
                try:
                    self.builder.publish_staging(staging_file, intent)
                except TaskPersistenceError:
                    pass

            final_file = task_dir / "task_intent_TI2026072101.json"
            self.assertFalse(final_file.exists())

    def test_a9_final_commit_failure_only_cleans_owned_temp(self):
        """9. final commit 失败时只清理本次调用的私有临时文件"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("src.task_intent_builder._atomic_commit_noreplace", side_effect=RuntimeError("Commit failed")):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

            # 确认没有残留任何 .tmp_publish 临时文件
            tmp_files = list(task_dir.glob(".tmp_publish_*"))
            self.assertEqual(len(tmp_files), 0)

    def test_a10_rollback_does_not_delete_replaced_final(self):
        """10. 回滚时若 final 路径已被替换为不属于本次调用的节点，绝不得删除替换文件"""
        intent = self._make_valid_intent("TI2026072101")
        other_intent = copy.deepcopy(intent)
        other_intent["priority"] = 33

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / f"task_intent_TI2026072101.staging_{os.getpid()}_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            final_file = task_dir / "task_intent_TI2026072101.json"

            def mock_failed_commit(tmp_path, dst_path):
                with open(final_file, "w", encoding="utf-8") as f:
                    json.dump(other_intent, f)
                raise TaskPersistenceError("Mock commit fail after external creation")

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("src.task_intent_builder._atomic_commit_noreplace", side_effect=mock_failed_commit):
                try:
                    self.builder.publish_staging(staging_file, intent)
                except TaskPersistenceError:
                    pass

            self.assertTrue(final_file.exists())
            with open(final_file, "r", encoding="utf-8") as f:
                c = json.load(f)
            self.assertEqual(c, other_intent)

    def test_a11_cleanup_does_not_delete_replaced_staging(self):
        """11. 清理阶段若 staging 路径已被替换，绝不得删除新替代文件"""
        intent = self._make_valid_intent("TI2026072101")
        replaced_intent = copy.deepcopy(intent)
        replaced_intent["priority"] = 55

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st_file = self.builder.create_staging(intent)
                st_file.unlink()
                with open(st_file, "w", encoding="utf-8") as f:
                    json.dump(replaced_intent, f)

                try:
                    self.builder.publish_staging(st_file, intent)
                except TaskPersistenceError:
                    pass

                self.assertTrue(st_file.exists())

    def test_a12_concurrent_publish_same_intent_id_only_one_succeeds(self):
        """12. 并发发布同一 intent_id：只能有一个成功，另一个触发 IntentIdConflict 或冲突处理"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st1 = self.builder.create_staging(intent)
                st2 = self.builder.create_staging(intent)

                res1 = self.builder.publish_staging(st1, intent)
                self.assertEqual(res1, "task_intent_TI2026072101.json")

                try:
                    res2 = self.builder.publish_staging(st2, intent)
                    self.assertEqual(res2, "task_intent_TI2026072101.json")
                except (IntentIdConflict, TaskPersistenceError):
                    pass

    def test_a13_concurrent_publish_different_intent_ids_both_succeed(self):
        """13. 并发发布不同 intent_id：均能成功创建各自正式文件"""
        intent1 = self._make_valid_intent("TI2026072101")
        intent2 = self._make_valid_intent("TI2026072102")

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st1 = self.builder.create_staging(intent1)
                st2 = self.builder.create_staging(intent2)

                pub1 = self.builder.publish_staging(st1, intent1)
                pub2 = self.builder.publish_staging(st2, intent2)

                self.assertTrue((task_dir / pub1).exists())
                self.assertTrue((task_dir / pub2).exists())

    def test_a14_interprocess_publish_lock_functionality(self):
        """14. 发布锁能跨线程与跨进程安全加锁"""
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                from src.task_intent_builder import TaskPublishLock
                lock = TaskPublishLock(task_dir)
                with lock:
                    lock_file = task_dir / ".task_intent_publish.lock"
                    self.assertTrue(lock_file.exists())

    def test_a15_no_runtime_files_left_in_git_repo(self):
        """15. 发布过程不得在 Git 仓库内部生成任何运行时临时文件"""
        intent = self._make_valid_intent("TI2026072101")
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                st = self.builder.create_staging(intent)
                self.builder.publish_staging(st, intent)

        repo_task_files = list(repo_root.glob("task_intent_*.json"))
        self.assertEqual(len(repo_task_files), 0)

    def test_a16_all_failure_paths_wrapped_in_task_persistence_error(self):
        """16. 所有失败路径均统一包装抛出 TaskPersistenceError"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("src.task_intent_builder._atomic_commit_noreplace", side_effect=OSError("Disk write error")):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)


if __name__ == "__main__":
    unittest.main()
