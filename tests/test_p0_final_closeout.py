"""
tests/test_p0_final_closeout.py - P0 最终小范围收口测试套件

问题一：confirming 快照恢复时合法 intent_id 被无条件替换
问题二："不是候选油田"没有拒绝 pending oilfield（缺少动态候选名匹配）
问题三：歧义设备别名仍被确定性路由到 DEVICE_CAPABILITY
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
# 问题一：confirming 快照恢复 intent_id 精确保留
# ─────────────────────────────────────────────────────────────────────────────

class SnapshotIntentIdPreservationTest(unittest.TestCase):

    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def _make_confirming_snap(self, intent_id, status="valid", store_version=3, slot_version=2):
        slots = {
            "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection",
                              "status": "valid", "version": 1},
            "task_type": {"slot_name": "task_type", "value": "管缆巡检", "status": "valid", "version": 1},
            "water_depth": {"slot_name": "water_depth", "value": 300.0, "status": "valid", "version": 1},
            "start_time": {"slot_name": "start_time", "value": "2026-08-01T08:00:00+08:00",
                           "status": "valid", "version": 1},
            "end_time": {"slot_name": "end_time", "value": "2026-08-01T18:00:00+08:00",
                         "status": "valid", "version": 1},
            "cable_type": {"slot_name": "cable_type", "value": "umbilical", "status": "valid", "version": 1},
            "start_point": {"slot_name": "start_point", "value": {"lat": 21.1, "lon": 112.5},
                            "status": "valid", "version": 1},
            "end_point": {"slot_name": "end_point", "value": {"lat": 21.2, "lon": 112.6},
                          "status": "valid", "version": 1},
            "equipment_type": {"slot_name": "equipment_type", "value": "crawler", "status": "valid", "version": 1},
            "robot_id": {"slot_name": "robot_id", "value": "CRAWLER-1600-001", "status": "valid", "version": 1},
            "payload": {"slot_name": "payload", "value": ["高清水下摄像机"], "status": "valid", "version": 1},
            "support_vessel": {"slot_name": "support_vessel", "value": "海洋石油681",
                               "status": "valid", "version": 1},
        }
        if intent_id is not None:
            slots["intent_id"] = {
                "slot_name": "intent_id", "value": intent_id,
                "status": status, "version": slot_version
            }
        return {
            "phase": "confirming",
            "mode": "normal",
            "slot_store": {
                "store_version": store_version,
                "slots": slots,
                "unresolved": []
            }
        }

    def test_c1_confirming_valid_intent_id_preserved(self):
        """confirming 快照有合法 intent_id：恢复后 ID 不变、store_version 不变、slot.version 不变、next_daily_id 未调用"""
        snap = self._make_confirming_snap("TI2026063001", store_version=3, slot_version=2)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)), \
                 patch("src.id_sequence.next_daily_id") as mock_next_id:
                self.dm.load_snapshot(snap)
                mock_next_id.assert_not_called()

        self.assertEqual(self.dm.phase, "confirming")
        self.assertEqual(self.dm.slot_store.slots["intent_id"].value, "TI2026063001",
                         "合法 confirming 快照的 intent_id 不得被替换")
        self.assertEqual(self.dm.slot_store.slots["intent_id"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["intent_id"].version, 2,
                         "slot.version 不得增加")
        self.assertEqual(self.dm.slot_store.version, 3,
                         "store_version 不得增加")

    def test_c2_confirming_missing_intent_id_generates_new(self):
        """confirming 快照无 intent_id 槽位：必须生成新 ID 并更新 store_version"""
        snap = self._make_confirming_snap(None)
        snap["slot_store"]["store_version"] = 2

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.load_snapshot(snap)

        self.assertEqual(self.dm.phase, "confirming")
        intent_slot = self.dm.slot_store.slots.get("intent_id")
        self.assertIsNotNone(intent_slot, "应生成新的 intent_id slot")
        self.assertEqual(intent_slot.status, "valid")
        self.assertIsNotNone(intent_slot.value)
        self.assertTrue(str(intent_slot.value).startswith("TI"),
                        f"生成的 intent_id 应以 TI 开头，实际: {intent_slot.value}")
        self.assertGreater(self.dm.slot_store.version, 2,
                           "生成新 ID 时 store_version 应增加")

    def test_c3_confirming_invalid_intent_id_generates_new(self):
        """confirming 快照 intent_id status=invalid：不保留旧值，生成新 ID"""
        snap = self._make_confirming_snap("TI2026063001", status="invalid", store_version=1, slot_version=1)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.load_snapshot(snap)

        self.assertEqual(self.dm.phase, "confirming")
        intent_slot = self.dm.slot_store.slots.get("intent_id")
        self.assertIsNotNone(intent_slot)
        self.assertEqual(intent_slot.status, "valid",
                         "无效 intent_id 应被替换为 valid 的新 ID")
        self.assertNotEqual(intent_slot.value, "TI2026063001",
                            "无效的旧 ID 不应保留")

    def test_c4_done_valid_pub_file_preserves_done_and_id(self):
        """done 快照发布文件合法：保持 done 阶段和原 intent_id"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)
            pub_file = tmp_task_dir / "task_intent_TI2026070001.json"
            with open(pub_file, "w", encoding="utf-8") as f:
                json.dump({"intent_id": "TI2026070001", "task_type": "pipeline_inspection"}, f)

            snap = {
                "phase": "done",
                "mode": "normal",
                "slot_store": {
                    "store_version": 5,
                    "slots": {
                        "task_type_key": {"slot_name": "task_type_key",
                                          "value": "pipeline_inspection", "status": "valid", "version": 1},
                        "intent_id": {"slot_name": "intent_id", "value": "TI2026070001",
                                      "status": "valid", "version": 3},
                    },
                    "unresolved": []
                }
            }

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.load_snapshot(snap)

        self.assertEqual(self.dm.phase, "done")
        self.assertIsNotNone(self.dm.final_result)
        self.assertEqual(self.dm.slot_store.slots["intent_id"].value, "TI2026070001",
                         "合法 done 快照的 intent_id 不得被替换")

    def test_c5_done_invalid_pub_evidence_downgrades_and_generates_new_id(self):
        """done 快照无发布文件：降级并生成新草稿 intent_id，原 ID 不保留"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)

            snap = {
                "phase": "done",
                "mode": "normal",
                "slot_store": {
                    "store_version": 2,
                    "slots": {
                        "task_type_key": {"slot_name": "task_type_key",
                                          "value": "pipeline_inspection", "status": "valid", "version": 1},
                        "intent_id": {"slot_name": "intent_id", "value": "TI2026063001",
                                      "status": "valid", "version": 1},
                    },
                    "unresolved": []
                }
            }

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.load_snapshot(snap)

        self.assertNotEqual(self.dm.phase, "done", "无效发布证据下不得保持 done 阶段")
        self.assertIsNone(self.dm.final_result)
        intent_slot = self.dm.slot_store.slots.get("intent_id")
        self.assertIsNotNone(intent_slot)
        self.assertNotEqual(intent_slot.value, "TI2026063001",
                            "无效发布证据：旧 done ID 不得被保留作为草稿 ID")
        self.assertEqual(intent_slot.status, "valid")


# ─────────────────────────────────────────────────────────────────────────────
# 问题二：pending oilfield 拒绝解析需动态结合候选名称
# ─────────────────────────────────────────────────────────────────────────────

class PendingOilfieldRejectionTest(unittest.TestCase):

    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def _setup_pending(self, candidate_name="流花11-1油田"):
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.dm.slot_store.slots["oilfield_name"] = Slot(
            "oilfield_name", value=candidate_name, status="pending_confirmation")
        self.dm.slot_store.slots["pending_oilfield_name"] = Slot(
            "pending_oilfield_name", value=candidate_name, status="valid")
        self.dm.slot_store.slots["pending_oilfield_candidates"] = Slot(
            "pending_oilfield_candidates",
            value=[{"name": candidate_name, "id": "OF001", "confidence": 95}],
            status="valid"
        )

    def _is_pending_cleared(self):
        pending = self.dm.slot_store.slots.get("pending_oilfield_name")
        return pending is None or pending.value is None or pending.status == "missing"

    def test_o1_negation_with_candidate_name_rejects(self):
        """'不是流花11-1油田' 应清除 pending oilfield，不影响其他槽位，phase 不变为 rejected"""
        self._setup_pending("流花11-1油田")
        reply = self.dm.process("不是流花11-1油田")

        self.assertTrue(self._is_pending_cleared(),
                        "'不是流花11-1油田' 应清除 pending_oilfield_name")
        oil_slot = self.dm.slot_store.slots.get("oilfield_name")
        if oil_slot:
            self.assertNotEqual(oil_slot.status, "pending_confirmation")
        self.assertNotEqual(self.dm.phase, "rejected")

    def test_o2_candidate_name_suffix_not_right_rejects(self):
        """'流花11-1油田不对' 只拒绝候选油田"""
        self._setup_pending("流花11-1油田")
        orig_wd = self.dm.slot_store.slots["water_depth"].value
        reply = self.dm.process("流花11-1油田不对")

        self.assertTrue(self._is_pending_cleared(),
                        "'流花11-1油田不对' 应清除 pending oilfield")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, orig_wd)
        self.assertNotEqual(self.dm.phase, "rejected")

    def test_o3_task_update_does_not_clear_pending(self):
        """'不是要取消任务，水深改成500米' 应走 TASK_UPDATE，水深更新，pending 油田不变"""
        self._setup_pending("流花11-1油田")

        with patch.object(self.dm.extractor, "extract_updates", return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "water_depth", "normalized_value": 500.0,
                 "raw_value": "500米", "confidence": 1.0}
            ]
        }):
            reply = self.dm.process("不是要取消任务，水深改成500米")

        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)
        pending = self.dm.slot_store.slots.get("pending_oilfield_name")
        self.assertIsNotNone(pending)
        self.assertTrue(pending.value == "流花11-1油田" and pending.status == "valid",
                        "TASK_UPDATE 不应清除 pending oilfield")
        self.assertNotEqual(self.dm.phase, "rejected")

    def test_o4_task_confirm_denial_does_not_clear_pending(self):
        """'不确认发布' 不得清除 pending oilfield"""
        self._setup_pending("流花11-1油田")
        reply = self.dm.process("不确认发布")

        pending = self.dm.slot_store.slots.get("pending_oilfield_name")
        self.assertIsNotNone(pending)
        self.assertTrue(pending.value is not None and pending.status != "missing",
                        "'不确认发布' 不应清除 pending oilfield")

    def test_o5_confirm_oilfield_does_not_publish(self):
        """'确认使用流花11-1油田' 只确认油田，不发布任务"""
        self._setup_pending("流花11-1油田")
        reply = self.dm.process("确认使用流花11-1油田")

        oil_slot = self.dm.slot_store.slots.get("oilfield_name")
        if oil_slot:
            self.assertNotEqual(oil_slot.status, "pending_confirmation")
        self.assertNotEqual(self.dm.phase, "done")


# ─────────────────────────────────────────────────────────────────────────────
# 问题三：歧义设备别名应进入 CLARIFICATION
# ─────────────────────────────────────────────────────────────────────────────

class AmbiguousDeviceAliasRoutingTest(unittest.TestCase):

    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_d1_ambiguous_alias_routes_to_clarification(self):
        """'一号机最大水深是多少？' - 一号机对应多个设备，必须 CLARIFICATION"""
        res = self.dm.intent_router.route("一号机最大水深是多少？", [], {})
        self.assertEqual(res.intent, "CLARIFICATION",
                         f"歧义别名'一号机'应路由到 CLARIFICATION，实际: {res.intent}")
        self.assertFalse(res.should_update_slots)

    def test_d2_qualified_device_name_routes_to_device_capability(self):
        """'金牛座001最大水深是多少？' - 含家族前缀的限定名，应 DEVICE_CAPABILITY"""
        res = self.dm.intent_router.route("金牛座001最大水深是多少？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY",
                         f"限定别名'金牛座001'应路由到 DEVICE_CAPABILITY，实际: {res.intent}")

    def test_d3_crawler_model_routes_to_device_capability(self):
        """'CRAWLER-1600-001能在500米作业吗？' - 型号编码，应 DEVICE_CAPABILITY"""
        res = self.dm.intent_router.route("CRAWLER-1600-001能在500米作业吗？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY",
                         f"型号'CRAWLER-1600-001'应路由到 DEVICE_CAPABILITY，实际: {res.intent}")

    def test_d4_unknown_device_routes_to_device_capability(self):
        """'亚特兰蒂斯能在1000米作业吗？' - 未知设备但结构明确，应 DEVICE_CAPABILITY"""
        res = self.dm.intent_router.route("亚特兰蒂斯能在1000米作业吗？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY",
                         f"未知设备能力问句应路由到 DEVICE_CAPABILITY，实际: {res.intent}")

    def test_d5_pure_number_context_not_device_capability(self):
        """'我有001个苹果吗？' - 纯数字上下文，不得路由为 DEVICE_CAPABILITY"""
        res = self.dm.intent_router.route("我有001个苹果吗？", [], {})
        self.assertNotEqual(res.intent, "DEVICE_CAPABILITY",
                            "纯数字'001'在非设备上下文中不应误路由为 DEVICE_CAPABILITY")

    def test_d6_kb_alias_index_built_dynamically(self):
        """KnowledgeBase 动态建立别名索引：一号机应在歧义集合中"""
        alias_index = self.kb.get_device_alias_index()
        self.assertIsInstance(alias_index, dict)
        self.assertIn("一号机", alias_index,
                      "一号机应在别名索引中")
        self.assertGreater(len(alias_index["一号机"]), 1,
                           f"一号机应对应多个设备，实际: {alias_index.get('一号机')}")

    def test_d7_kb_ambiguous_device_terms_set(self):
        """KnowledgeBase.get_ambiguous_device_terms() 应包含 一号机 和 001"""
        ambiguous = self.kb.get_ambiguous_device_terms()
        self.assertIsInstance(ambiguous, set)
        self.assertIn("一号机", ambiguous, "一号机应在歧义别名集合中")
        self.assertIn("001", ambiguous, "001 应在歧义别名集合中（被多个设备共用）")


if __name__ == "__main__":
    unittest.main()
