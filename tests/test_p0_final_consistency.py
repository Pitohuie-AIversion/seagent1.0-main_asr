"""
tests/test_p0_final_consistency.py - P0 状态一致性收口 5 项缺陷复现与收口测试
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouter
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.exceptions import TaskPersistenceError
from src.slot_store import SlotVersionConflict
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


class P0FinalConsistencyDefectTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    # ── 问题一：否定取消测试 ──

    def test_p1_negation_cancel_does_not_reject_task(self):
        """输入'不要取消任务，水深改成500米'：应走 TASK_UPDATE, 槽位修改为500, phase != rejected, 不写磁盘文件"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)

        with patch.object(self.dm.extractor, 'extract_updates', return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "water_depth", "normalized_value": 500.0, "raw_value": "500米", "confidence": 1.0}
            ]
        }):
            reply = self.dm.process("不要取消任务，水深改成500米")
            self.assertNotEqual(self.dm.phase, "rejected")
            self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)

    def test_p1_dont_cancel_prefix_update(self):
        """输入'别取消，把水深设置为600米'：只修改水深，不取消"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)

        with patch.object(self.dm.extractor, 'extract_updates', return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "water_depth", "normalized_value": 600.0, "raw_value": "600米", "confidence": 1.0}
            ]
        }):
            reply = self.dm.process("别取消，把水深设置为600米")
            self.assertNotEqual(self.dm.phase, "rejected")
            self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 600.0)

    def test_p1_cancel_task_pure_control(self):
        """输入'取消当前任务'：走 TASK_CANCEL, 不调用 extractor, SlotStore 不变, phase == rejected"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        snap_before = self.dm.slot_store.export_snapshot()

        with patch.object(self.dm.extractor, 'extract_updates') as mock_ext:
            reply = self.dm.process("取消当前任务")
            mock_ext.assert_not_called()
            self.assertEqual(self.dm.phase, "rejected")
            self.assertEqual(self.dm.slot_store.export_snapshot(), snap_before)

    # ── 问题二：done 状态修改测试 ──

    def test_p2_done_state_modification_recreates_draft_and_new_intent_id(self):
        """done 状态下修改槽位：phase != done, final_result is None, 新 intent_id 生成且写回 SlotStore, 原发布文件不变"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)

            seed_complete_valid_pipeline_task(self.dm, self.kb)
            all_v = self.dm.validator.validate(self.dm.task_state)
            for v in all_v:
                if v.severity == "soft":
                    for f in v.related_fields:
                        val = self.dm.task_state.get(f)
                        if val is not None:
                            self.dm._soft_whitelist.add((f, str(val), v.constraint_id))

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.process("确认发布")
                self.assertEqual(self.dm.phase, "done")
                orig_final_result = copy.deepcopy(self.dm.final_result)
                orig_intent_id = orig_final_result["intent_id"]

                final_files_1 = list(tmp_path.glob("task_intent_*.json"))
                self.assertEqual(len(final_files_1), 1)

                with patch.object(self.dm.extractor, 'extract_updates', return_value={
                    "intent": "TASK_UPDATE",
                    "slot_candidates": [
                        {"canonical_key": "water_depth", "normalized_value": 500.0, "raw_value": "500米", "confidence": 1.0}
                    ]
                }):
                    reply = self.dm.process("水深改成500米")

                self.assertNotEqual(self.dm.phase, "done")
                self.assertIsNone(self.dm.final_result)
                self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)

                new_intent_id = self.dm.slot_store.slots["intent_id"].value
                self.assertIsNotNone(new_intent_id)
                self.assertNotEqual(new_intent_id, orig_intent_id)

                with open(final_files_1[0], "r", encoding="utf-8") as f:
                    orig_file_data = json.load(f)
                self.assertEqual(orig_file_data["intent_id"], orig_intent_id)
                self.assertEqual(orig_file_data["location"]["water_depth_m"], 300.0)

                final_files_2 = list(tmp_path.glob("task_intent_*.json"))
                self.assertEqual(len(final_files_2), 1)

                reply_pub = self.dm.process("确认发布")
                self.assertEqual(self.dm.phase, "done")
                final_files_3 = list(tmp_path.glob("task_intent_*.json"))
                self.assertEqual(len(final_files_3), 2)
                self.assertEqual(self.dm.final_result["intent_id"], new_intent_id)
                self.assertEqual(self.dm.final_result["water_depth"], 500.0)

    def test_p2_done_modification_commit_failure_rollback(self):
        """done 状态下修改槽位如果 commit_transaction 失败：所有状态与 final_result 保持原值"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)

            seed_complete_valid_pipeline_task(self.dm, self.kb)
            all_v = self.dm.validator.validate(self.dm.task_state)
            for v in all_v:
                if v.severity == "soft":
                    for f in v.related_fields:
                        val = self.dm.task_state.get(f)
                        if val is not None:
                            self.dm._soft_whitelist.add((f, str(val), v.constraint_id))

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.process("确认发布")
                self.assertEqual(self.dm.phase, "done")
                orig_final_result = copy.deepcopy(self.dm.final_result)
                orig_snapshot = copy.deepcopy(self.dm.slot_store.export_snapshot())

                with patch.object(self.dm.slot_store, 'commit_transaction', side_effect=SlotVersionConflict("Version conflict simulation")):
                    with self.assertRaises(SlotVersionConflict):
                        self.dm.process("水深改成500米")

                self.assertEqual(self.dm.phase, "done")
                self.assertEqual(self.dm.final_result, orig_final_result)
                self.assertEqual(self.dm.slot_store.export_snapshot(), orig_snapshot)

    # ── 问题三：槽位冲突解决测试 ──

    def test_p3_negation_confirm_does_not_accept_unrelated_conflict(self):
        """support_vessel 处于 conflict 时，输入'不确认发布，水深改成500米'：water_depth 更新，support_vessel 仍保持 conflict"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.dm.slot_store.slots["support_vessel"].status = "conflict"
        self.dm.slot_store.slots["support_vessel"].candidate_value = "海洋石油681"

        with patch.object(self.dm.extractor, 'extract_updates', return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "water_depth", "normalized_value": 500.0, "raw_value": "500米", "confidence": 1.0}
            ]
        }):
            reply = self.dm.process("不确认发布，水深改成500米")

        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].status, "conflict")
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].candidate_value, "海洋石油681")

    def test_p3_targeted_confirm_support_vessel_conflict(self):
        """输入'确认将支持船修改为海洋石油681'：只解决 support_vessel 冲突"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.dm.slot_store.slots["support_vessel"].status = "conflict"
        self.dm.slot_store.slots["support_vessel"].candidate_value = "海洋石油681"

        reply = self.dm.process("确认将支持船修改为海洋石油681")
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].value, "海洋石油681")

    def test_p3_targeted_cancel_support_vessel_conflict(self):
        """输入'取消支持船修改'：丢弃 candidate_value，保留原值并转为 valid"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        orig_vessel = self.dm.slot_store.slots["support_vessel"].value
        self.dm.slot_store.slots["support_vessel"].status = "conflict"
        self.dm.slot_store.slots["support_vessel"].candidate_value = "海洋石油681"

        reply = self.dm.process("取消支持船修改")
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].value, orig_vessel)
        self.assertIsNone(self.dm.slot_store.slots["support_vessel"].candidate_value)

    # ── 问题四与问题五：设备别名路由与回复测试 ──

    def test_p4_device_alias_end_to_end_routing(self):
        """金牛座/天鹰座/观察级ROV/亚特兰蒂斯 均能稳定路由至 DEVICE_CAPABILITY，不误识别为槽位"""
        aliases_to_test = ["金牛座能在1000米作业吗？", "天鹰座最大水深是多少？", "观察级ROV能在1000米作业吗？", "亚特兰蒂斯能在1000米作业吗？"]
        for q in aliases_to_test:
            res = self.dm.intent_router.route(q, [], {})
            self.assertEqual(res.intent, "DEVICE_CAPABILITY", f"Query '{q}' failed to route to DEVICE_CAPABILITY")
            self.assertFalse(res.should_update_slots)

    def test_p5_device_check_depth_exceeded_response(self):
        """DialogueManager.process('金牛座能在1000米作业吗？')：回复包含500米，明确表示不能满足1000米，不包含'符合条件的设备如下'"""
        self.dm.reset()
        reply = self.dm.process("金牛座能在1000米作业吗？")
        self.assertIn("500", reply)
        self.assertTrue("不能" in reply or "无法" in reply or "不满足" in reply)
        self.assertNotIn("符合条件的设备如下", reply)

    def test_p5_unknown_device_check_response(self):
        """DialogueManager.process('亚特兰蒂斯能在1000米作业吗？')：回复包含未提供该信息，不返回其他设备代替"""
        self.dm.reset()
        reply = self.dm.process("亚特兰蒂斯能在1000米作业吗？")
        self.assertTrue("未提供" in reply or "未找到" in reply or "不存在" in reply or "暂无" in reply)

    # ── 历史快照恢复测试 ──

    def test_legacy_snapshot_restore_confirming_and_done(self):
        """测试兼容快照恢复：confirming 自动补全 intent_id，done 快照若无 intent_id 则降级为 confirming"""
        self.dm.reset()
        # legacy confirming 缺失 intent_id
        legacy_confirming = {
            "phase": "confirming",
            "task_state": {"task_type_key": "pipeline_inspection", "water_depth": 300.0},
            "built_json": {"task_type_key": "pipeline_inspection", "water_depth": 300.0},
            "slot_store": {
                "slots": {
                    "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid"},
                    "water_depth": {"slot_name": "water_depth", "value": 300.0, "status": "valid"}
                }
            }
        }
        self.dm.load_snapshot(legacy_confirming)
        self.assertEqual(self.dm.phase, "confirming")
        self.assertIn("intent_id", self.dm.slot_store.slots)
        self.assertEqual(self.dm.slot_store.slots["intent_id"].status, "valid")

        # legacy done 缺失 intent_id 且无文件关联 -> 降级为 confirming
        legacy_done_missing_id = {
            "phase": "done",
            "task_state": {"task_type_key": "pipeline_inspection", "water_depth": 300.0},
            "built_json": {"task_type_key": "pipeline_inspection", "water_depth": 300.0},
            "slot_store": {
                "slots": {
                    "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid"},
                    "water_depth": {"slot_name": "water_depth", "value": 300.0, "status": "valid"}
                }
            }
        }
        self.dm.load_snapshot(legacy_done_missing_id)
        self.assertNotEqual(self.dm.phase, "done")
        self.assertIn(self.dm.phase, ["confirming", "collecting"])


if __name__ == "__main__":
    unittest.main()
