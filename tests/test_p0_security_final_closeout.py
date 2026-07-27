"""
tests/test_p0_security_final_closeout.py - P0/P1 最终安全与逻辑边界收口测试套件

A. intent_id Unicode 数字与严格正则校验
B. 纯数字设备别名精准语境限制
C. publish_staging 来源路径与安全防冒充校验
"""

import copy
import json
import os
import tempfile
import typing
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.id_sequence import validate_intent_id
from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouter, IntentRouteResult
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.task_intent_builder import TaskIntentBuilder
from src.exceptions import TaskPersistenceError


class DummyLLM(LLMClient):
    def __init__(self, default_reply="默认LLM测试回复"):
        self.llm = None
        self.default_reply = default_reply

    def chat(self, messages, temperature=0.7, max_tokens=800):
        return self.default_reply

    def generate(self, messages, temperature=0.7, max_tokens=800):
        return self.chat(messages, temperature, max_tokens)

    def filter_reply(self, text):
        return text


# ─────────────────────────────────────────────────────────────────────────────
# 测试 A: intent_id Unicode 数字与严格正则校验 (1-8)
# ─────────────────────────────────────────────────────────────────────────────

class IntentIdUnicodeSecurityTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)
        self.builder = TaskIntentBuilder(self.kb)

    def test_a1_ascii_valid_id_returns_true(self):
        """1. ASCII 合法 ID 返回 True"""
        self.assertTrue(validate_intent_id("TI2026072001"))
        self.assertTrue(validate_intent_id("TI20260720100"))

    def test_a2_fullwidth_digits_return_false(self):
        """2. 全角数字 ID 返回 False"""
        self.assertFalse(validate_intent_id("TI１２３４５６７８９０"))
        self.assertFalse(validate_intent_id("TI20260720０1"))

    def test_a3_arabic_indic_digits_return_false(self):
        """3. 阿拉伯数字字符 ID (Arabic-Indic digits) 返回 False"""
        self.assertFalse(validate_intent_id("TI٢٠٢٦٠٧٢٠٠١"))

    def test_a4_mixed_and_invalid_formats_return_false(self):
        """4. ASCII 与 Unicode 混合 / 换行 / 浮点 / 小写 / 非字符串均返回 False"""
        self.assertFalse(validate_intent_id("TI2026072001\n"))
        self.assertFalse(validate_intent_id("TI2026072001/"))
        self.assertFalse(validate_intent_id("TI20260720.01"))
        self.assertFalse(validate_intent_id("ti2026072001"))
        self.assertFalse(validate_intent_id(123456789012))
        self.assertFalse(validate_intent_id(True))
        self.assertFalse(validate_intent_id(["TI2026072001"]))

    def test_a5_type_hints_inspection_no_name_error(self):
        """5. typing.get_type_hints(validate_intent_id) 不抛出 NameError (Any 正确导入)"""
        try:
            hints = typing.get_type_hints(validate_intent_id)
            self.assertIn("intent_id", hints)
        except NameError as e:
            self.fail(f"typing.get_type_hints(validate_intent_id) raised NameError: {e}")

    def test_a6_confirming_snapshot_unicode_id_generates_new_id(self):
        """6. confirming 快照为 Unicode 全角数字 ID 时生成新合法 ID"""
        snap = {
            "phase": "confirming",
            "mode": "normal",
            "slot_store": {
                "store_version": 2,
                "slots": {
                    "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "version": 1},
                    "intent_id": {"slot_name": "intent_id", "value": "TI１２３４５６７８９０", "status": "valid", "version": 1},
                },
                "unresolved": []
            }
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.load_snapshot(snap)

        new_id = self.dm.slot_store.slots["intent_id"].value
        self.assertNotEqual(new_id, "TI１２３４５６７８９０")
        self.assertTrue(validate_intent_id(new_id))

    def test_a7_done_snapshot_unicode_id_downgrades_without_file_construction(self):
        """7. done 快照为 Unicode ID 时不得用其构造路径，直接降级并生成新 ID"""
        snap = {
            "phase": "done",
            "mode": "normal",
            "slot_store": {
                "store_version": 2,
                "slots": {
                    "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "version": 1},
                    "intent_id": {"slot_name": "intent_id", "value": "TI１２３４５６７８９０", "status": "valid", "version": 1},
                },
                "unresolved": []
            }
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.load_snapshot(snap)

        self.assertNotEqual(self.dm.phase, "done")
        self.assertIsNone(self.dm.final_result)
        new_id = self.dm.slot_store.slots["intent_id"].value
        self.assertNotEqual(new_id, "TI１２３４５６７８９０")
        self.assertTrue(validate_intent_id(new_id))

    def test_a8_task_intent_builder_rejects_unicode_id(self):
        """8. TaskIntentBuilder 拒绝 Unicode 数字 ID 且不创建文件"""
        unicode_id = "TI１２３４５６７８９０"
        intent = {"intent_id": unicode_id, "task_type": "pipeline_inspection"}
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.prepare({}, {"intent_id": unicode_id}, "normal", "pipeline_inspection", intent_id=unicode_id)
                with self.assertRaises(TaskPersistenceError):
                    self.builder.create_staging(intent)
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(tmp_task_dir / "dummy.staging", intent)
                files = list(tmp_task_dir.iterdir())
                self.assertEqual(len(files), 0)


# ─────────────────────────────────────────────────────────────────────────────
# 测试 B: 数字设备别名精准语境限制 (9-13)
# ─────────────────────────────────────────────────────────────────────────────

class NumericDeviceAliasContextTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_b9_apple_query_no_device_reason(self):
        """9. '我有001个苹果吗？' 不得包含任何设备别名相关的 reason，不误解释 001"""
        res = self.dm.intent_router.route("我有001个苹果吗？", [], {})
        reason = res.reason or ""
        self.assertNotIn("设备别名", reason)
        self.assertNotIn("多个设备", reason)
        self.assertNotIn("设备型号歧义", reason)
        self.assertNotEqual(res.intent, "DEVICE_CAPABILITY")

    def test_b10_order_query_no_device_reason(self):
        """10. '订单001什么时候到？' 不得包含任何设备别名相关的 reason"""
        res = self.dm.intent_router.route("订单001什么时候到？", [], {})
        reason = res.reason or ""
        self.assertNotIn("设备别名", reason)
        self.assertNotIn("多个设备", reason)
        self.assertNotIn("设备型号歧义", reason)
        self.assertNotEqual(res.intent, "DEVICE_CAPABILITY")

    def test_b11_001_in_explicit_device_cap_context_is_clarification(self):
        """11. '001最大水深是多少？' 在明确设备能力语境下仍为 CLARIFICATION"""
        res = self.dm.intent_router.route("001最大水深是多少？", [], {})
        self.assertEqual(res.intent, "CLARIFICATION")
        self.assertIn("001", res.reason)

    def test_b12_jinniuzuo_001_in_explicit_device_cap_context_is_device_cap(self):
        """12. '金牛座001最大水深是多少？' 仍为 DEVICE_CAPABILITY"""
        res = self.dm.intent_router.route("金牛座001最大水深是多少？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")

    def test_b13_non_device_numeric_queries_preserve_slot_store_state(self):
        """13. 普通数字问题 ('第001题答案是什么？', '房间001在哪里？') 不影响 SlotStore 状态"""
        queries = [
            "我有001个苹果吗？",
            "订单001什么时候到？",
            "第001题答案是什么？",
            "编号001是否正确？",
            "房间001在哪里？",
            "今天是001号吗？",
        ]
        snap_before = copy.deepcopy(self.dm.slot_store.export_snapshot())
        v_before = self.dm.slot_store.version
        phase_before = self.dm.phase

        for q in queries:
            with self.subTest(query=q):
                res = self.dm.intent_router.route(q, [], self.dm.task_state)
                reason = res.reason or ""
                self.assertNotIn("设备别名", reason)
                self.assertNotIn("多个设备", reason)
                self.assertNotIn("设备型号歧义", reason)

                reply = self.dm.process(q)

                self.assertEqual(self.dm.slot_store.version, v_before)
                self.assertEqual(self.dm.phase, phase_before)
                self.assertEqual(self.dm.slot_store.export_snapshot(), snap_before)


# ─────────────────────────────────────────────────────────────────────────────
# 测试 C: publish_staging 来源路径与安全防冒充校验 (14-20)
# ─────────────────────────────────────────────────────────────────────────────

class StagingSourceValidationTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.builder = TaskIntentBuilder(self.kb)

    def test_c14_publish_staging_rejects_external_file_and_preserves_original(self):
        """14. publish_staging 拒绝 task_dir 外部文件，保留原外部文件，不创建 final_file"""
        intent = {"intent_id": "TI2026072001", "task_type": "pipeline_inspection"}
        with tempfile.TemporaryDirectory() as tmp_task_dir_str, \
             tempfile.TemporaryDirectory() as tmp_outside_dir_str:

            task_dir = Path(tmp_task_dir_str)
            outside_dir = Path(tmp_outside_dir_str)
            outside_file = outside_dir / "task_intent_TI2026072001.staging_1234"
            with open(outside_file, "w", encoding="utf-8") as f:
                f.write("external secret content")

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:

                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(outside_file, intent)

                mock_link.assert_not_called()

            # 外部原文件必须完好存在，内容未变
            self.assertTrue(outside_file.exists())
            with open(outside_file, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "external secret content")

            # 最终文件绝对不能创建
            final_file = task_dir / "task_intent_TI2026072001.json"
            self.assertFalse(final_file.exists())

    def test_c15_publish_staging_rejects_symlink_inside_task_dir_pointing_outside(self):
        """15. 拒绝 task_dir 内指向外部文件的符号链接"""
        intent = {"intent_id": "TI2026072001", "task_type": "pipeline_inspection"}
        with tempfile.TemporaryDirectory() as tmp_task_dir_str, \
             tempfile.TemporaryDirectory() as tmp_outside_dir_str:

            task_dir = Path(tmp_task_dir_str)
            outside_dir = Path(tmp_outside_dir_str)
            outside_file = outside_dir / "secret.txt"
            with open(outside_file, "w", encoding="utf-8") as f:
                f.write("secret")

            symlink_in_task = task_dir / "task_intent_TI2026072001.staging_link"
            try:
                os.symlink(outside_file, symlink_in_task)
            except OSError:
                self.skipTest("Symlinks not supported on this platform")

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:

                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(symlink_in_task, intent)

                mock_link.assert_not_called()

            self.assertTrue(outside_file.exists())
            final_file = task_dir / "task_intent_TI2026072001.json"
            self.assertFalse(final_file.exists())

    def test_c16_publish_staging_rejects_mismatched_intent_id_in_filename(self):
        """16. 拒绝文件名中的 intent_id 与 intent['intent_id'] 不一致的 staging 文件"""
        intent = {"intent_id": "TI2026072001", "task_type": "pipeline_inspection"}
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            mismatched_staging = task_dir / "task_intent_TI2026072099.staging_1234"
            with open(mismatched_staging, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:

                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(mismatched_staging, intent)

                mock_link.assert_not_called()

            self.assertTrue(mismatched_staging.exists())
            final_file = task_dir / "task_intent_TI2026072001.json"
            self.assertFalse(final_file.exists())

    def test_c17_publish_staging_rejects_regular_json_impersonating_staging(self):
        """17. 拒绝普通 JSON 文件冒充 staging 文件 (例如文件名不含 .staging_)"""
        intent = {"intent_id": "TI2026072001", "task_type": "pipeline_inspection"}
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            impostor_file = task_dir / "task_intent_TI2026072001.json"
            with open(impostor_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:

                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(impostor_file, intent)

                mock_link.assert_not_called()

    def test_c18_legitimate_create_staging_to_publish_staging_succeeds(self):
        """18. 正常的 create_staging → publish_staging 流程成功发布"""
        intent = {
            "intent_id": "TI2026072001",
            "task_type": "pipeline_inspection",
            "priority": 7,
            "time": {"start": None, "end": None},
            "location": {"oilfield": None, "water_depth_m": 300.0},
            "task": {"type": "pipeline_inspection", "details": {}},
            "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": {"name": None}},
            "conditions": {}
        }
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                staging_file = self.builder.create_staging(intent)
                self.assertTrue(staging_file.exists())
                self.assertEqual(staging_file.parent.resolve(), task_dir.resolve())

                final_name = self.builder.publish_staging(staging_file, intent)
                self.assertEqual(final_name, "task_intent_TI2026072001.json")

                final_file = task_dir / final_name
                self.assertTrue(final_file.exists())
                self.assertFalse(staging_file.exists())

    def test_c19_validation_failure_mock_os_link_never_called(self):
        """19. 校验失败时断言 os.link 从未调用"""
        intent = {"intent_id": "TI2026072001"}
        with tempfile.TemporaryDirectory() as tmp_dir:
            bad_staging = Path(tmp_dir) / "non_existent.staging_123"
            with patch("os.link") as mock_link:
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(bad_staging, intent)
                mock_link.assert_not_called()

    def test_c20_validation_failure_final_file_does_not_exist(self):
        """20. 校验失败时 final_file 绝对不存在"""
        intent = {"intent_id": "TI2026072001"}
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            invalid_staging = task_dir / "directory_as_staging.staging_123"
            invalid_staging.mkdir(parents=True, exist_ok=True)  # 目录而非文件

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link") as mock_link:
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(invalid_staging, intent)
                mock_link.assert_not_called()

            final_file = task_dir / "task_intent_TI2026072001.json"
            self.assertFalse(final_file.exists())


if __name__ == "__main__":
    unittest.main()
