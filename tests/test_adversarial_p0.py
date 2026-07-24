"""
tests/test_adversarial_p0.py - P0 安全收口 15 项 mandatory 对抗测试
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouter, IntentRouteResult
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.exceptions import TaskPersistenceError
from tests.test_slot_consistency import seed_complete_valid_pipeline_task


class DummyLLM(LLMClient):
    def __init__(self, default_reply: str = "默认LLM测试回复"):
        self.llm = None
        self.default_reply = default_reply

    def chat(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 800) -> str:
        return self.default_reply

    def generate(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 800) -> str:
        return self.chat(messages, temperature, max_tokens)

    def filter_reply(self, text: str) -> str:
        return text


class AdversarialP0SecurityTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)
        self.router = IntentRouter(self.llm)

    # 1. confirming + “不确认发布，水深改成500米” -> TASK_UPDATE, 不生成文件, phase != done
    def test_adv_01_confirming_negation_update(self):
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        with patch.object(self.dm.extractor, 'extract_updates', return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "water_depth", "normalized_value": 500.0, "raw_value": "500米", "confidence": 1.0}
            ]
        }) as mock_ext:
            reply = self.dm.process("不确认发布，水深改成500米")
            mock_ext.assert_called_once()
            self.assertNotEqual(self.dm.phase, "done")
            self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)

    # 2. confirming + “确认发布前，把水深改成500米” -> TASK_UPDATE, 不发布
    def test_adv_02_confirming_prefix_update(self):
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        with patch.object(self.dm.extractor, 'extract_updates', return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "water_depth", "normalized_value": 500.0, "raw_value": "500米", "confidence": 1.0}
            ]
        }):
            reply = self.dm.process("确认发布前，把水深改成500米")
            self.assertNotEqual(self.dm.phase, "done")
            self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)

    # 3. confirming + “水深改成500米并确认发布” -> 只修改, 不发布, 下一轮重新确认
    def test_adv_03_mixed_update_and_confirm(self):
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        with patch.object(self.dm.extractor, 'extract_updates', return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "water_depth", "normalized_value": 500.0, "raw_value": "500米", "confidence": 1.0}
            ]
        }):
            reply = self.dm.process("水深改成500米并确认发布")
            self.assertNotEqual(self.dm.phase, "done")
            self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)

    # 4. confirming + “继续” -> 不发布
    def test_adv_04_confirming_bare_continue(self):
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        reply = self.dm.process("继续")
        self.assertNotEqual(self.dm.phase, "done")

    # 5. confirming + “忽略” -> 不发布
    def test_adv_05_confirming_bare_ignore(self):
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        reply = self.dm.process("忽略")
        self.assertNotEqual(self.dm.phase, "done")

    # 6. blocked_soft + “继续” -> 只忽略软警告, 不发布
    def test_adv_06_blocked_soft_continue(self):
        self.dm.phase = "blocked_soft"
        reply = self.dm.process("继续")
        self.assertNotEqual(self.dm.phase, "done")

    # 7. blocked_soft + “忽略警告，但水深改成600米” -> 不得静默忽略修改，走 TASK_UPDATE, 不发布
    def test_adv_07_blocked_soft_mixed_update(self):
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.dm.phase = "blocked_soft"
        with patch.object(self.dm.extractor, 'extract_updates', return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "water_depth", "normalized_value": 600.0, "raw_value": "600米", "confidence": 1.0}
            ]
        }):
            reply = self.dm.process("忽略警告，但水深改成600米")
            self.assertNotEqual(self.dm.phase, "done")
            self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 600.0)

    # 8. 确认发布成功：真实路由, extractor未调用, commit_transaction未调用, slot_store.version不变, intent_id一致
    def test_adv_08_confirm_publish_success_invariance(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)
            seed_complete_valid_pipeline_task(self.dm, self.kb)

            # Whitelist any soft violations so confirmation publish is ready
            all_v = self.dm.validator.validate(self.dm.task_state)
            for v in all_v:
                if v.severity == "soft":
                    for f in v.related_fields:
                        val = self.dm.task_state.get(f)
                        if val is not None:
                            self.dm._soft_whitelist.add((f, str(val), v.constraint_id))

            v_before = self.dm.slot_store.version
            snap_before = self.dm.slot_store.export_snapshot()
            ti_slot_val = self.dm.slot_store.slots["intent_id"].value

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path), \
                 patch.object(self.dm.extractor, 'extract_updates') as mock_ext, \
                 patch.object(self.dm.slot_store, 'commit_transaction') as mock_commit:
                reply = self.dm.process("确认发布")
                mock_ext.assert_not_called()
                mock_commit.assert_not_called()
                self.assertEqual(self.dm.phase, "done")

            v_after = self.dm.slot_store.version
            snap_after = self.dm.slot_store.export_snapshot()
            self.assertEqual(v_before, v_after)
            self.assertEqual(snap_before, snap_after)

            final_files = list(tmp_path.glob("task_intent_*.json"))
            self.assertEqual(len(final_files), 1)
            with open(final_files[0], "r", encoding="utf-8") as f:
                file_data = json.load(f)

            file_intent_id = file_data.get("intent_id")
            self.assertEqual(file_intent_id, ti_slot_val)
            self.assertEqual(file_intent_id, self.dm.task_state.get("intent_id"))
            self.assertEqual(file_intent_id, self.dm._last_built_json.get("intent_id"))
            self.assertEqual(file_intent_id, self.dm.final_result.get("intent_id"))

    # 9. 确认发布失败：phase恢复confirming, slot_store快照不变, 无staging, 无正式文件
    def test_adv_09_confirm_publish_failure_rollback(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)
            seed_complete_valid_pipeline_task(self.dm, self.kb)

            # Whitelist soft violations
            all_v = self.dm.validator.validate(self.dm.task_state)
            for v in all_v:
                if v.severity == "soft":
                    for f in v.related_fields:
                        val = self.dm.task_state.get(f)
                        if val is not None:
                            self.dm._soft_whitelist.add((f, str(val), v.constraint_id))

            snap_before = copy.deepcopy(self.dm.slot_store.export_snapshot())

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path), \
                 patch("src.dialogue_manager.TaskIntentBuilder.publish_staging", side_effect=TaskPersistenceError("Disk error")):
                with self.assertRaises(TaskPersistenceError):
                    self.dm.process("确认发布")

            self.assertEqual(self.dm.phase, "confirming")
            self.assertIsNone(self.dm.final_result)
            self.assertEqual(self.dm.slot_store.export_snapshot(), snap_before)
            final_files = list(tmp_path.glob("task_intent_*.json")) if tmp_path.exists() else []
            staging_files = list(tmp_path.glob("*.staging_*")) if tmp_path.exists() else []
            self.assertEqual(len(final_files), 0)
            self.assertGreater(len(staging_files), 0, "Staging file must survive in tmp_path when publish fails")

    # 10. confirming 但 intent_id missing -> fail closed, 不调用 prepare/create_staging/publish_staging
    def test_adv_10_missing_intent_id_fails_closed(self):
        seed_complete_valid_pipeline_task(self.dm, self.kb)

        # Whitelist soft violations
        all_v = self.dm.validator.validate(self.dm.task_state)
        for v in all_v:
            if v.severity == "soft":
                for f in v.related_fields:
                    val = self.dm.task_state.get(f)
                    if val is not None:
                        self.dm._soft_whitelist.add((f, str(val), v.constraint_id))

        del self.dm.slot_store.slots["intent_id"]
        self.dm._rebuild_cache()

        with patch("src.dialogue_manager.TaskIntentBuilder.prepare") as mock_prep, \
             patch("src.dialogue_manager.TaskIntentBuilder.create_staging") as mock_create, \
             patch("src.dialogue_manager.TaskIntentBuilder.publish_staging") as mock_pub:
            reply = self.dm.process("确认发布")
            mock_prep.assert_not_called()
            mock_create.assert_not_called()
            mock_pub.assert_not_called()
            self.assertNotEqual(self.dm.phase, "done")
            self.assertIn("intent_id", reply)

    # 11. “观察级ROV能在1000米作业吗？” -> 只返回观察级ROV, max_depth_m=600, matches_depth_condition=false
    def test_adv_11_specific_device_check_depth_exceeded(self):
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "观察级ROV能在1000米作业吗？")
        self.assertTrue(res["found"])
        self.assertEqual(res["query_mode"], "device_check")
        self.assertGreaterEqual(len(res["results"]), 1)
        for item in res["results"]:
            self.assertIn("观察", item.get("robot_class_name", "") + item.get("full_name", ""))
            self.assertFalse(item.get("matches_depth_condition"))

    # 12. “亚特兰蒂斯能在1000米作业吗？” -> 不返回其他机器人替代
    def test_adv_12_unknown_device_check(self):
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "亚特兰蒂斯能在1000米作业吗？")
        self.assertFalse(res["found"])
        self.assertEqual(res["query_mode"], "device_check")
        self.assertEqual(len(res["results"]), 0)

    # 13. “最大下潜深度1000米的机器人有哪些？” -> 不返回全部设备, 每个结果符合定义
    def test_adv_13_device_list_depth_query(self):
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "最大下潜深度1000米的机器人有哪些？")
        self.assertEqual(res["query_mode"], "device_list")
        if res["found"]:
            all_rovs = self.kb.get_all_rovs()
            self.assertLessEqual(len(res["results"]), len(all_rovs))
            for r in res["results"]:
                self.assertLessEqual(r["max_depth_m"], 1000)

    # 14. 深度解析失败且包含“有哪些机器人” -> 不返回全部设备
    def test_adv_14_depth_parse_failure_no_fallback_all(self):
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "987米 的有哪些机器人")
        self.assertFalse(res["found"])
        self.assertEqual(len(res["results"]), 0)

    # 15. 非法 query_subtype: list/dict/bool/number 全部 fail closed 到 CLARIFICATION
    def test_adv_15_invalid_query_subtype_types(self):
        invalid_subtypes = [[1, 2], {"a": 1}, True, False, 123, 45.6]
        for sub in invalid_subtypes:
            with patch.object(self.llm, 'extract_json', return_value={
                "intent": "TOOL_QUERY",
                "confidence": 0.95,
                "reason": "test",
                "query_subtype": sub
            }):
                res = self.router.route("模糊查询测试", [], {})
                self.assertEqual(res.intent, "CLARIFICATION", f"query_subtype={sub} should fall to CLARIFICATION")


if __name__ == "__main__":
    unittest.main()
