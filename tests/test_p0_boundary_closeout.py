"""
tests/test_p0_boundary_closeout.py - P0 最后一轮边界收口测试套件
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouter
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
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


class P0BoundaryCloseoutTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    # ── 问题一：done 修订包含 invalid 值的 intent_id 关联 ──

    def test_p1_done_revision_with_invalid_value_changes_intent_id(self):
        """done 状态下输入无效值：必须立即生成新的草稿 intent_id，后续有效修改沿用该草稿 ID"""
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
                orig_intent_id = self.dm.final_result["intent_id"]
                orig_file = tmp_path / f"task_intent_{orig_intent_id}.json"
                self.assertTrue(orig_file.exists())

                # Turn 1: 输入无效数值 'abc'
                with patch.object(self.dm.extractor, 'extract_updates', return_value={
                    "intent": "TASK_UPDATE",
                    "slot_candidates": [
                        {"canonical_key": "water_depth", "normalized_value": None, "raw_value": "abc", "confidence": 1.0}
                    ]
                }):
                    self.dm.process("水深改为abc")

                self.assertNotEqual(self.dm.phase, "done")
                self.assertIsNone(self.dm.final_result)
                draft_intent_id = self.dm.slot_store.slots["intent_id"].value
                self.assertIsNotNone(draft_intent_id)
                self.assertNotEqual(draft_intent_id, orig_intent_id)

                # Turn 2: 水深改成合法 500 米
                with patch.object(self.dm.extractor, 'extract_updates', return_value={
                    "intent": "TASK_UPDATE",
                    "slot_candidates": [
                        {"canonical_key": "water_depth", "normalized_value": 500.0, "raw_value": "500米", "confidence": 1.0}
                    ]
                }):
                    self.dm.process("水深改成500米")

                self.assertEqual(self.dm.phase, "confirming")
                self.assertEqual(self.dm.slot_store.slots["intent_id"].value, draft_intent_id)

                # Turn 3: 确认发布
                self.dm.process("确认发布")
                self.assertEqual(self.dm.phase, "done")
                new_file = tmp_path / f"task_intent_{draft_intent_id}.json"
                self.assertTrue(new_file.exists())
                self.assertTrue(orig_file.exists())
                with open(new_file, "r", encoding="utf-8") as f:
                    new_data = json.load(f)
                self.assertEqual(new_data["intent_id"], draft_intent_id)
                self.assertEqual(new_data["location"]["water_depth_m"], 500.0)

    def test_p1_done_revision_transaction_failure_rollback(self):
        """done 状态下修改发生 SlotVersionConflict：内存完全回滚到原 done 状态"""
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
                orig_result = copy.deepcopy(self.dm.final_result)
                orig_snap = copy.deepcopy(self.dm.slot_store.export_snapshot())

                with patch.object(self.dm.extractor, 'extract_updates', return_value={
                    "intent": "TASK_UPDATE",
                    "slot_candidates": [
                        {"canonical_key": "water_depth", "normalized_value": None, "raw_value": "abc", "confidence": 1.0}
                    ]
                }):
                    with patch.object(self.dm.slot_store, 'commit_transaction', side_effect=SlotVersionConflict("Version error")):
                        with self.assertRaises(SlotVersionConflict):
                            self.dm.process("水深改为abc")

                self.assertEqual(self.dm.phase, "done")
                self.assertEqual(self.dm.final_result, orig_result)
                self.assertEqual(self.dm.slot_store.export_snapshot(), orig_snap)

    # ── 问题二：pending oilfield 的结构化处理与优先级 ──

    def test_p2_pending_oilfield_does_not_intercept_negation_update(self):
        """存在 pending oilfield 时，输入'不要取消任务，水深改成500米'：水深更新为500，pending oilfield 保持不被误杀"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        if "oilfield_name" not in self.dm.slot_store.slots:
            from src.slot_store import Slot
            self.dm.slot_store.slots["oilfield_name"] = Slot("oilfield_name", value="流花11-1油田", status="valid")
        self.dm.slot_store.slots["oilfield_name"].status = "pending_confirmation"
        self.dm.slot_store.slots["oilfield_name"].candidate_value = "流花11-1油田"

        with patch.object(self.dm.extractor, 'extract_updates', return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "water_depth", "normalized_value": 500.0, "raw_value": "500米", "confidence": 1.0}
            ]
        }):
            reply = self.dm.process("不要取消任务，水深改成500米")

        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)
        self.assertEqual(self.dm.slot_store.slots["oilfield_name"].status, "pending_confirmation")
        self.assertNotEqual(self.dm.phase, "rejected")

    def test_p2_pending_oilfield_task_cancel_priority(self):
        """存在 pending oilfield 时，输入'取消当前任务'：必须走 TASK_CANCEL，相较于局部油田处理有更高优先级"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        if "oilfield_name" not in self.dm.slot_store.slots:
            from src.slot_store import Slot
            self.dm.slot_store.slots["oilfield_name"] = Slot("oilfield_name", value="流花11-1油田", status="valid")
        self.dm.slot_store.slots["oilfield_name"].status = "pending_confirmation"
        self.dm.slot_store.slots["oilfield_name"].candidate_value = "流花11-1油田"

        with patch.object(self.dm.extractor, 'extract_updates') as mock_ext:
            reply = self.dm.process("取消当前任务")
            mock_ext.assert_not_called()

        self.assertEqual(self.dm.phase, "rejected")
        self.assertIsNone(self.dm.final_result)

    def test_p2_pending_oilfield_explicit_rejection(self):
        """输入'这个油田不对'：只清除 pending oilfield 恢复原值，其他槽位不变"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        from src.slot_store import Slot
        self.dm.slot_store.slots["oilfield_name"] = Slot("oilfield_name", value="流花11-1油田", status="valid")
        self.dm.slot_store.slots["pending_oilfield_name"] = Slot("pending_oilfield_name", value="流花11-1油田", status="valid")
        self.dm.slot_store.slots["pending_oilfield_candidates"] = Slot("pending_oilfield_candidates", value=[{"name": "流花11-1油田", "id": "OF001", "confidence": 95}], status="valid")
        orig_oilfield = self.dm.slot_store.slots["oilfield_name"].value

        reply = self.dm.process("这个油田不对")
        self.assertEqual(self.dm.slot_store.slots["oilfield_name"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["oilfield_name"].value, orig_oilfield)

    def test_p2_pending_oilfield_explicit_confirmation(self):
        """输入'确认使用流花11-1油田'：只确认目标油田，不自动发布任务"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        from src.slot_store import Slot
        self.dm.slot_store.slots["oilfield_name"] = Slot("oilfield_name", value="流花11-1油田", status="pending_confirmation")
        self.dm.slot_store.slots["pending_oilfield_name"] = Slot("pending_oilfield_name", value="流花11-1油田", status="valid")
        self.dm.slot_store.slots["pending_oilfield_candidates"] = Slot("pending_oilfield_candidates", value=[{"name": "流花11-1油田", "id": "OF001", "confidence": 95}], status="valid")

        reply = self.dm.process("确认使用流花11-1油田")
        self.assertEqual(self.dm.slot_store.slots["oilfield_name"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["oilfield_name"].value, "流花11-1油田")
        self.assertNotEqual(self.dm.phase, "done")

    # ── 问题三：done 快照真实发布证据验证与无缝迁移 ──

    def test_p3_done_snapshot_missing_disk_file_downgrades_phase(self):
        """恢复 done 快照但关联的 task_intent 文件不存在：不得保持 done，降级为 confirming/collecting 并生成新草稿 ID"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)

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

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.load_snapshot(snap)

            self.assertNotEqual(self.dm.phase, "done")
            self.assertIsNone(self.dm.final_result)
            self.assertNotEqual(self.dm.slot_store.slots["intent_id"].value, "TI2026063001")

    def test_p3_done_snapshot_valid_disk_file_restores_done(self):
        """恢复 done 快照且磁盘上存在内容匹配的发布 JSON 文件：成功恢复 done 阶段和 final_result"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)
            pub_file = tmp_task_dir / "task_intent_TI2026063001.json"
            valid_intent = {
                "intent_id": "TI2026063001",
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
                    "payload": ["camera_hd"],
                    "support_vessel": {"name": "海洋石油201"},
                },
                "conditions": {"max_current_speed_knots": 2.0, "sea_state_level": 3},
            }
            with open(pub_file, "w", encoding="utf-8") as f:
                json.dump(valid_intent, f)

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

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.load_snapshot(snap)

            self.assertEqual(self.dm.phase, "done")
            self.assertIsNotNone(self.dm.final_result)
            self.assertEqual(self.dm.slot_store.slots["intent_id"].value, "TI2026063001")

    # ── 问题四：设备词表去歧义与序列号过滤 ──

    def test_p4_naked_sequence_number_not_routed_to_device_capability(self):
        """带有纯数字'001'的非设备问题：不得误路由为 DEVICE_CAPABILITY"""
        res = self.dm.intent_router.route("我有001个苹果吗？", [], {})
        self.assertNotEqual(res.intent, "DEVICE_CAPABILITY")

        res_num = self.dm.intent_router.route("编号001是什么？", [], {})
        self.assertNotEqual(res_num.intent, "DEVICE_CAPABILITY")

    def test_p4_specific_device_sequence_number_routes_to_device_capability(self):
        """特定带设备前缀的型号（如'金牛座001'、'CRAWLER-1600-001'）：稳定路由为 DEVICE_CAPABILITY"""
        res1 = self.dm.intent_router.route("金牛座001最大水深是多少？", [], {})
        self.assertEqual(res1.intent, "DEVICE_CAPABILITY")

        res2 = self.dm.intent_router.route("CRAWLER-1600-001能在500米作业吗？", [], {})
        self.assertEqual(res2.intent, "DEVICE_CAPABILITY")

    # ── 问题五：多槽位冲突与差异化 Candidate 验证 ──

    def test_p5_support_vessel_different_candidate_value_confirmation(self):
        """support_vessel 原值 A, candidate_value 为与 A 不同的 B：显式确认后值变为 B"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.dm.slot_store.slots["support_vessel"].value = "海洋石油681"
        self.dm.slot_store.slots["support_vessel"].status = "conflict"
        self.dm.slot_store.slots["support_vessel"].candidate_value = "海洋石油286"

        with patch.object(self.dm.extractor, 'extract_updates', return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "support_vessel", "normalized_value": "海洋石油286", "raw_value": "海洋石油286", "confidence": 1.0}
            ]
        }):
            reply = self.dm.process("确认将支持船修改为海洋石油286")

        self.assertEqual(self.dm.slot_store.slots["support_vessel"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].value, "海洋石油286")

    def test_p5_multiple_conflicts_targeted_confirmation(self):
        """support_vessel 和 water_depth 同时处于 conflict：只确认 support_vessel 时，water_depth 保持 conflict"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.dm.slot_store.slots["support_vessel"].status = "conflict"
        self.dm.slot_store.slots["support_vessel"].candidate_value = "海洋石油286"
        self.dm.slot_store.slots["water_depth"].status = "conflict"
        self.dm.slot_store.slots["water_depth"].candidate_value = 800.0

        with patch.object(self.dm.extractor, 'extract_updates', return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "support_vessel", "normalized_value": "海洋石油286", "raw_value": "海洋石油286", "confidence": 1.0}
            ]
        }):
            reply = self.dm.process("确认支持船为海洋石油286")

        self.assertEqual(self.dm.slot_store.slots["support_vessel"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "conflict")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].candidate_value, 800.0)

    def test_p5_payload_conflict_targeted_cancellation(self):
        """payload 处于 conflict：输入'取消载荷修改'，保留原 payload 并清除候选值"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.dm.slot_store.slots["payload"].status = "conflict"
        self.dm.slot_store.slots["payload"].candidate_value = ["高清摄像机", "机械臂"]

        with patch.object(self.dm.extractor, 'extract_updates', return_value={"intent": "TASK_UPDATE", "slot_candidates": []}):
            reply = self.dm.process("取消载荷修改")

        self.assertEqual(self.dm.slot_store.slots["payload"].status, "valid")
        self.assertTrue(isinstance(self.dm.slot_store.slots["payload"].value, list))
        self.assertIsNone(self.dm.slot_store.slots["payload"].candidate_value)

    def test_p5_multiple_conflicts_ambiguous_confirmation_requires_clarification(self):
        """两个槽位同时 conflict 时，输入模糊的'确认这个修改'：不更新任何冲突槽位，要求澄清"""
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.dm.slot_store.slots["support_vessel"].status = "conflict"
        self.dm.slot_store.slots["support_vessel"].candidate_value = "海洋石油286"
        self.dm.slot_store.slots["water_depth"].status = "conflict"
        self.dm.slot_store.slots["water_depth"].candidate_value = 800.0

        reply = self.dm.process("确认这个修改")
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].status, "conflict")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "conflict")


if __name__ == "__main__":
    unittest.main()
