"""
tests/test_p0_true_final_closeout.py - P0/P1 真正最终收口测试套件

A. 路由测试 (1-8):
   1. 活动任务下“把设备改成金牛座一号机” → TASK_UPDATE, should_update_slots=True
   2. “将机器人更换为CRAWLER-1600-001” → TASK_UPDATE
   3. “设备换成天鹰座一号机” → TASK_UPDATE
   4. “使用金牛座一号机执行巡检任务” → TASK_CREATE 或 TASK_UPDATE
   5. expected_slot=equipment 时 “金牛座一号机” → 槽位填报
   6. 无任务上下文独立输入 “金牛座一号机” → 不得自动写槽位 (CLARIFICATION)
   7. “金牛座一号机最大水深是多少？” → DEVICE_CAPABILITY, SlotStore 不变
   8. DialogueManager.process() 端到端设备槽位修改测试

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
from unittest.mock import patch, MagicMock

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouter
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.task_intent_builder import TaskIntentBuilder
from src.exceptions import TaskPersistenceError
from src.slot_store import Slot
from tests.test_slot_consistency import seed_complete_valid_pipeline_task


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
# 测试 A: 路由测试 (1-8)
# ─────────────────────────────────────────────────────────────────────────────

class DeviceAliasRoutingPriorityTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_a1_update_device_to_jinniuzuo_yihaoji_routes_to_task_update(self):
        """1. 活动任务下'把设备改成金牛座一号机' → TASK_UPDATE, should_update_slots=True"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        res = self.dm.intent_router.route("把设备改成金牛座一号机", [], self.dm.task_state, phase=self.dm.phase)
        self.assertEqual(res.intent, "TASK_UPDATE")
        self.assertTrue(res.should_update_slots)

    def test_a2_replace_robot_to_crawler_routes_to_task_update(self):
        """2. '将机器人更换为CRAWLER-1600-001' → TASK_UPDATE"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        res = self.dm.intent_router.route("将机器人更换为CRAWLER-1600-001", [], self.dm.task_state, phase=self.dm.phase)
        self.assertEqual(res.intent, "TASK_UPDATE")
        self.assertTrue(res.should_update_slots)

    def test_a3_change_device_to_tianyingzuo_routes_to_task_update(self):
        """3. '设备换成天鹰座一号机' → TASK_UPDATE"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        res = self.dm.intent_router.route("设备换成天鹰座一号机", [], self.dm.task_state, phase=self.dm.phase)
        self.assertEqual(res.intent, "TASK_UPDATE")
        self.assertTrue(res.should_update_slots)

    def test_a4_use_device_for_inspection_routes_to_create_or_update(self):
        """4. '使用金牛座一号机执行巡检任务' → TASK_CREATE 或 TASK_UPDATE，不能是 DEVICE_CAPABILITY"""
        res = self.dm.intent_router.route("使用金牛座一号机执行巡检任务", [], {})
        self.assertIn(res.intent, ("TASK_CREATE", "TASK_UPDATE"))
        self.assertTrue(res.should_update_slots)

    def test_a5_expected_slot_equipment_allows_slot_filling(self):
        """5. expected_slot=equipment/equipment_type 时，输入'金牛座一号机'允许进入槽位填报"""
        res = self.dm.intent_router.route("金牛座一号机", [], {}, phase="collecting", expected_slots=["equipment_type"])
        self.assertEqual(res.intent, "TASK_UPDATE")
        self.assertTrue(res.should_update_slots)

    def test_a6_standalone_device_alias_without_context_no_auto_slot_filling(self):
        """6. 无任务上下文时单独输入'金牛座一号机' → 不得自动写槽位 (CLARIFICATION)"""
        res = self.dm.intent_router.route("金牛座一号机", [], {}, phase="collecting", expected_slots=None)
        self.assertEqual(res.intent, "CLARIFICATION")
        self.assertFalse(res.should_update_slots)

    def test_a7_device_cap_query_preserves_slot_store_state(self):
        """7. '金牛座一号机最大水深是多少？' → DEVICE_CAPABILITY，且 SlotStore 快照完全不变"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        snap_before = copy.deepcopy(self.dm.slot_store.export_snapshot())

        res = self.dm.intent_router.route("金牛座一号机最大水深是多少？", [], self.dm.task_state, phase=self.dm.phase)
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")
        self.assertFalse(res.should_update_slots)

        reply = self.dm.process("金牛座一号机最大水深是多少？")
        snap_after = copy.deepcopy(self.dm.slot_store.export_snapshot())
        self.assertEqual(snap_before, snap_after)

    def test_a8_end_to_end_device_slot_update_flow(self):
        """8. DialogueManager.process() 端到端设备槽位修改测试：真正到达统一槽位流水线"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        with patch.object(self.dm.extractor, "extract_updates", return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "equipment_unit_id", "normalized_value": "AUV-HP-001", "raw_value": "AUV一号机", "confidence": 1.0}
            ]
        }) as mock_ext:
            reply = self.dm.process("把设备改成AUV一号机")
            mock_ext.assert_called_once()
            slot = self.dm.slot_store.slots.get("equipment_type")
            self.assertIsNotNone(slot)
            self.assertEqual(slot.value, "水下无人自主航行器 HP")


# ─────────────────────────────────────────────────────────────────────────────
# 测试 B: staging 内容与正则匹配校验 (9-18)
# ─────────────────────────────────────────────────────────────────────────────

class StagingContentAndSuffixValidationTest(unittest.TestCase):
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
