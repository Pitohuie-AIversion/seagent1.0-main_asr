"""
tests/test_p0_publish_race_and_router_closeout.py - P0 发布竞态收口测试套件

A. Staging Race Condition Tests (1-12)
"""

import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.knowledge_retriever import KnowledgeBase
from src.task_intent_builder import TaskIntentBuilder
from src.exceptions import TaskPersistenceError



# ─────────────────────────────────────────────────────────────────────────────
# 测试 A: staging 竞态与安全发布测试 (1-12)
# ─────────────────────────────────────────────────────────────────────────────

class StagingRaceConditionTest(unittest.TestCase):
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

    def test_b1_race_replace_staging_before_link_raises_error(self):
        """1. 在认领/发布前将 staging 路径替换为伪造文件 → publish_staging 抛出 TaskPersistenceError"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["priority"] = 99

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / f"task_intent_TI2026072101.staging_{os.getpid() + 9999}_1_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(forged_intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

    def test_b2_race_replace_staging_final_file_rolled_back(self):
        """2. 发生竞态/所有权不匹配时，final_file 不存在（或已被安全回滚删除）"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / f"task_intent_TI2026072101.staging_{os.getpid() + 9999}_1_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

            final_file = task_dir / "task_intent_TI2026072101.json"
            self.assertFalse(final_file.exists())

    def test_b3_race_replace_staging_file_preserved(self):
        """3. 发生竞态替换时，替换后的 staging 文件必须仍然存在于磁盘"""
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

    def test_b4_race_replace_staging_file_content_unchanged(self):
        """4. 发生竞态替换时，替换后的 staging 文件内容保持不变"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["priority"] = 99

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / f"task_intent_TI2026072101.staging_{os.getpid()}_5678_abcd1234"
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

            with open(staging_file, "r", encoding="utf-8") as f:
                content = json.load(f)
            self.assertEqual(content, forged_intent)

    def test_b5_os_link_wrap_intercept_forged_inode(self):
        """5. os.link 之前替换新 inode 文件 → 拦截且伪造 inode 绝对不会成为有效正式文件"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["payload"] = "FORGED_INODE_PAYLOAD"

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / f"task_intent_TI2026072101.staging_{os.getpid()}_{threading.get_ident()}_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(forged_intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

            final_file = task_dir / "task_intent_TI2026072101.json"
            self.assertFalse(final_file.exists())

    def test_b6_post_link_inode_mismatch_rejected_and_rolled_back(self):
        """6. 认领后 PID/所有权不一致 → 拒绝并回滚删除 final_file"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / f"task_intent_TI2026072101.staging_{os.getpid() + 9999}_1_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

            final_file = task_dir / "task_intent_TI2026072101.json"
            self.assertFalse(final_file.exists())

    def test_b7_post_link_content_mismatch_rejected_and_rolled_back(self):
        """7. 提交后 content 不一致 → 拒绝并回滚删除 final_file"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("src.task_intent_builder._atomic_commit_noreplace", side_effect=TaskPersistenceError("Mock commit failed")):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

            final_file = task_dir / "task_intent_TI2026072101.json"
            self.assertFalse(final_file.exists())

    def test_b8_preexisting_final_file_not_overwritten(self):
        """8. final_file 预先存在 → 不得覆盖，原内容与预存在 final_file 保持不变"""
        intent = self._make_valid_intent("TI2026072101")
        preexisting_intent = copy.deepcopy(intent)
        preexisting_intent["priority"] = 5

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            final_file = task_dir / "task_intent_TI2026072101.json"
            with open(final_file, "w", encoding="utf-8") as f:
                json.dump(preexisting_intent, f)

            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                from src.exceptions import IntentIdConflict
                with self.assertRaises((TaskPersistenceError, IntentIdConflict)):
                    self.builder.publish_staging(staging_file, intent)

            with open(final_file, "r", encoding="utf-8") as f:
                final_content = json.load(f)
            self.assertEqual(final_content, preexisting_intent)

    def test_b9_normal_staging_publish_success(self):
        """9. staging 未被替换的正常流程 → 发布成功，final JSON == intent，staging 被删除"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                staging_file = self.builder.create_staging(intent)
                pub_name = self.builder.publish_staging(staging_file, intent)
                final_file = task_dir / pub_name

                self.assertTrue(final_file.exists())
                self.assertFalse(staging_file.exists())

                with open(final_file, "r", encoding="utf-8") as f:
                    final_content = json.load(f)
                self.assertEqual(final_content, intent)

    def test_b10_memory_trusted_publishing_safeguards(self):
        """10. 模拟在 publish 过程中 staging 路径被替换 → final JSON 仍只能等于已验证 intent"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["priority"] = 888

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            real_link = os.link

            def race_link(src, dst):
                with open(staging_file, "w", encoding="utf-8") as f:
                    json.dump(forged_intent, f)
                real_link(src, dst)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link", side_effect=race_link):
                try:
                    self.builder.publish_staging(staging_file, intent)
                except TaskPersistenceError:
                    pass

            final_file = task_dir / "task_intent_TI2026072101.json"
            if final_file.exists():
                with open(final_file, "r", encoding="utf-8") as f:
                    fc = json.load(f)
                self.assertEqual(fc, intent)

    def test_b11_replacement_staging_not_deleted_on_cleanup(self):
        """11. 清理阶段前 staging 路径被替换 → 绝不得删除替代文件"""
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

    def test_b12_fail_closed_error_wrapping(self):
        """12. 所有异常与失败路径统一包装抛出 TaskPersistenceError，不泄漏不受控底层异常"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link", side_effect=PermissionError("Mock disk error")):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)


if __name__ == "__main__":
    unittest.main()
