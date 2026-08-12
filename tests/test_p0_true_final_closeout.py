"""
tests/test_p0_true_final_closeout.py - P0/P1 真正最终收口测试套件

B. staging 内容与正则匹配校验 (9-18):
   9. 同目录、合法前缀但内容与 intent 不一致 → 拒绝发布
   10. staging 内部 intent_id 与参数不一致 → 拒绝发布
   11. staging JSON 顶层为 list → 拒绝发布
   12. staging 内容不是合法 JSON → 拒绝发布
   13. .staging_forged 文件名 → 拒绝发布
   14. .staging_ 空后缀 → 拒绝发布
   15. 后缀格式错误 → 拒绝发布
   16. 内容校验失败副作用断言 (os.link 未调用，staging 原存，final 不存在)
   17. 正常 create_staging → publish_staging 成功，内容一致，staging 清理
   18. 校验期间 staging 发生变化 → fail closed
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
from src.exceptions import TaskPersistenceError


# ─────────────────────────────────────────────────────────────────────────────
# 测试 B: staging 内容与正则匹配校验 (9-18)
# ─────────────────────────────────────────────────────────────────────────────

class StagingContentAndSuffixValidationTest(unittest.TestCase):
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

    def test_b9_mismatched_payload_content_rejected(self):
        """9. 同目录、合法前缀但内容与 intent 不一致 (如 payload 伪造) → 拒绝发布"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["payload"] = "FORGED_MALICIOUS_PAYLOAD"

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(forged_intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)
                mock_link.assert_not_called()

            self.assertTrue(staging_file.exists())
            final_file = task_dir / "task_intent_TI2026072101.json"
            self.assertFalse(final_file.exists())

    def test_b10_mismatched_intent_id_inside_json_rejected(self):
        """10. staging 内部 intent_id 与参数不一致 → 拒绝发布"""
        intent = self._make_valid_intent("TI2026072101")
        mismatched_json = copy.deepcopy(intent)
        mismatched_json["intent_id"] = "TI2026072199"

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(mismatched_json, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)
                mock_link.assert_not_called()

    def test_b11_top_level_list_json_rejected(self):
        """11. staging JSON 顶层为 list → 拒绝发布"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump([intent], f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)
                mock_link.assert_not_called()

    def test_b12_corrupted_json_content_rejected(self):
        """12. staging 内容不是合法 JSON → 拒绝发布"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                f.write("{corrupted json content...")

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)
                mock_link.assert_not_called()

    def test_b13_staging_forged_suffix_rejected(self):
        """13. .staging_forged 文件名 → 拒绝发布"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_forged"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)
                mock_link.assert_not_called()

    def test_b14_staging_empty_suffix_rejected(self):
        """14. .staging_ 空后缀 → 拒绝发布"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)
                mock_link.assert_not_called()

    def test_b15_staging_invalid_suffix_format_rejected(self):
        """15. 后缀格式错误 (如含非十六进制字符、段数错误) → 拒绝发布"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_123_456_nonhex"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)
                mock_link.assert_not_called()

    def test_b16_validation_failure_side_effects(self):
        """16. 内容校验失败时副作用断言 (os.link 未调用，staging 原存，final 不存在)"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["water_depth"] = 9999.0

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(forged_intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

                mock_link.assert_not_called()

            self.assertTrue(staging_file.exists())
            final_file = task_dir / "task_intent_TI2026072101.json"
            self.assertFalse(final_file.exists())

    def test_b17_normal_create_to_publish_flow_succeeds(self):
        """17. 正常 create_staging → publish_staging 成功，内容与 intent 完全一致，staging 删除"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                staging_file = self.builder.create_staging(intent)
                self.assertTrue(staging_file.exists())

                final_name = self.builder.publish_staging(staging_file, intent)
                final_file = task_dir / final_name
                self.assertTrue(final_file.exists())
                self.assertFalse(staging_file.exists())

                with open(final_file, "r", encoding="utf-8") as f:
                    published_content = json.load(f)
                self.assertEqual(published_content, intent)

    def test_b18_file_modified_during_verification_fails_closed(self):
        """18. 模拟 staging 文件在校验期间发生变化 (stat mtime/size 改变) → fail closed"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:
                original_open = open

                def open_and_tamper(*args, **kwargs):
                    f_obj = original_open(*args, **kwargs)
                    with original_open(staging_file, "a", encoding="utf-8") as tf:
                        tf.write(" ")
                    return f_obj

                with patch("builtins.open", side_effect=open_and_tamper):
                    with self.assertRaises(TaskPersistenceError):
                        self.builder.publish_staging(staging_file, intent)
                mock_link.assert_not_called()


if __name__ == "__main__":
    unittest.main()
