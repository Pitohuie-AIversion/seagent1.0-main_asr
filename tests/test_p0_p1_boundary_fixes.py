"""
tests/test_p0_p1_boundary_fixes.py - P0/P1 边界问题测试套件

A. intent_id 格式及路径安全校验
B. 设备别名最长匹配优先
C. 待确认油田受控简称识别
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
from src.task_intent_builder import TaskIntentBuilder
from src.exceptions import TaskPersistenceError, IntentIdConflict
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
# 测试 A: intent_id 格式及路径安全校验
# ─────────────────────────────────────────────────────────────────────────────

class IntentIdValidationTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)
        self.builder = TaskIntentBuilder(self.kb)

    def _make_confirming_snap(self, intent_id_val, status="valid", store_version=3, slot_version=2):
        slots = {
            "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "version": 1},
            "task_type": {"slot_name": "task_type", "value": "管缆巡检", "status": "valid", "version": 1},
            "water_depth": {"slot_name": "water_depth", "value": 300.0, "status": "valid", "version": 1},
            "start_time": {"slot_name": "start_time", "value": "2026-08-01T08:00:00+08:00", "status": "valid", "version": 1},
            "end_time": {"slot_name": "end_time", "value": "2026-08-01T18:00:00+08:00", "status": "valid", "version": 1},
            "cable_type": {"slot_name": "cable_type", "value": "umbilical", "status": "valid", "version": 1},
            "start_point": {"slot_name": "start_point", "value": {"lat": 21.1, "lon": 112.5}, "status": "valid", "version": 1},
            "end_point": {"slot_name": "end_point", "value": {"lat": 21.2, "lon": 112.6}, "status": "valid", "version": 1},
            "equipment_type": {"slot_name": "equipment_type", "value": "crawler", "status": "valid", "version": 1},
            "robot_id": {"slot_name": "robot_id", "value": "CRAWLER-1600-001", "status": "valid", "version": 1},
            "payload": {"slot_name": "payload", "value": ["高清水下摄像机"], "status": "valid", "version": 1},
            "support_vessel": {"slot_name": "support_vessel", "value": "海洋石油681", "status": "valid", "version": 1},
        }
        if intent_id_val is not None:
            slots["intent_id"] = {
                "slot_name": "intent_id", "value": intent_id_val,
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

    def test_a1_confirming_valid_intent_id_unchanged(self):
        """1. confirming + 合法 ID ('TI2026063001'): ID 和版本完全不变"""
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
        self.assertEqual(self.dm.slot_store.slots["intent_id"].value, "TI2026063001")
        self.assertEqual(self.dm.slot_store.slots["intent_id"].version, 2)
        self.assertEqual(self.dm.slot_store.version, 3)

    def test_a2_confirming_bad_id_generates_new_id(self):
        """2. confirming + 'bad-id': 生成新 ID"""
        snap = self._make_confirming_snap("bad-id", store_version=3, slot_version=2)
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.load_snapshot(snap)

        intent_val = self.dm.slot_store.slots["intent_id"].value
        self.assertNotEqual(intent_val, "bad-id")
        self.assertTrue(str(intent_val).startswith("TI"))

    def test_a3_confirming_path_traversal_id_generates_new_id(self):
        """3. confirming + '../../outside': 生成新 ID 且不产生越界文件"""
        snap = self._make_confirming_snap("../../outside", store_version=3, slot_version=2)
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.load_snapshot(snap)

        intent_val = self.dm.slot_store.slots["intent_id"].value
        self.assertNotIn("outside", str(intent_val))
        self.assertNotIn("..", str(intent_val))
        self.assertTrue(str(intent_val).startswith("TI"))

    def test_a4_confirming_non_string_id_generates_new_id(self):
        """4. confirming + 非字符串 ID (12345, list, dict, bool): 生成新 ID"""
        for bad_val in [12345, ["TI2026063001"], {"id": "TI2026063001"}, True]:
            with self.subTest(bad_val=bad_val):
                snap = self._make_confirming_snap(bad_val, store_version=3, slot_version=2)
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_task_dir = Path(tmp_dir) / "task"
                    tmp_task_dir.mkdir(parents=True, exist_ok=True)
                    with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                         patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                        self.dm.load_snapshot(snap)

                intent_val = self.dm.slot_store.slots["intent_id"].value
                self.assertNotEqual(intent_val, bad_val)
                self.assertTrue(isinstance(intent_val, str))
                self.assertTrue(intent_val.startswith("TI"))

    def test_a5_done_invalid_id_downgrades_and_no_file_read(self):
        """5. done + 非法 ID ('bad-id' 或 '../../outside'): 不得读取非法路径，降级并生成新 ID"""
        for bad_id in ["bad-id", "../../outside", "TI/../../outside"]:
            with self.subTest(bad_id=bad_id):
                snap = {
                    "phase": "done",
                    "mode": "normal",
                    "slot_store": {
                        "store_version": 2,
                        "slots": {
                            "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "version": 1},
                            "intent_id": {"slot_name": "intent_id", "value": bad_id, "status": "valid", "version": 1},
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
                self.assertNotEqual(new_id, bad_id)
                self.assertTrue(str(new_id).startswith("TI"))

    def test_a6_task_intent_builder_rejects_invalid_id(self):
        """6. TaskIntentBuilder 收到非法 ID ('bad-id' 或 '../../outside'): 拒绝且不创建文件"""
        for bad_id in ["bad-id", "../../outside", "TI/../../outside", "TI123", 12345, True]:
            with self.subTest(bad_id=bad_id):
                intent = {
                    "intent_id": bad_id,
                    "task_type": "pipeline_inspection",
                    "priority": 7,
                    "time": {"start": None, "end": None},
                    "location": {"oilfield": None, "water_depth_m": 300.0},
                    "task": {"type": "pipeline_inspection", "details": {}},
                    "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": {"name": None}},
                    "conditions": {}
                }
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_task_dir = Path(tmp_dir) / "task"
                    tmp_task_dir.mkdir(parents=True, exist_ok=True)
                    with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir):
                        # prepare 不接受非法 intent_id
                        with self.assertRaises((TaskPersistenceError, ValueError)):
                            self.builder.prepare({}, {"intent_id": bad_id}, "normal", "pipeline_inspection", intent_id=bad_id)

                        # create_staging 不接受非法 intent_id
                        with self.assertRaises((TaskPersistenceError, ValueError)):
                            self.builder.create_staging(intent)

                        # publish_staging 不接受非法 intent_id
                        dummy_staging = tmp_task_dir / "dummy.staging"
                        with self.assertRaises((TaskPersistenceError, ValueError)):
                            self.builder.publish_staging(dummy_staging, intent)

                        # persist 不接受非法 intent_id
                        with self.assertRaises((TaskPersistenceError, ValueError)):
                            self.builder.persist(intent)

                        # 不创建任何文件
                        files = list(tmp_task_dir.iterdir())
                        self.assertEqual(len(files), 0, f"非法 ID 不应创建任何文件，得到: {files}")


# ─────────────────────────────────────────────────────────────────────────────
# 测试 B: 设备别名最长匹配优先
# ─────────────────────────────────────────────────────────────────────────────

class LongestMatchDeviceAliasRoutingTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_b7_001_in_device_context_routes_to_clarification(self):
        """7. '001最大水深是多少？' → CLARIFICATION (001对应多个设备)"""
        res = self.dm.intent_router.route("001最大水深是多少？", [], {})
        self.assertEqual(res.intent, "CLARIFICATION")
        self.assertFalse(res.should_update_slots)

    def test_b8_jinniuzuo_yihaoji_routes_to_device_capability(self):
        """8. '金牛座一号机最大水深是多少？' → DEVICE_CAPABILITY (金牛座一号机是最长匹配且唯一)"""
        res = self.dm.intent_router.route("金牛座一号机最大水深是多少？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")

    def test_b9_yihaoji_routes_to_clarification(self):
        """9. '一号机最大水深是多少？' → CLARIFICATION (一号机对应多个设备)"""
        res = self.dm.intent_router.route("一号机最大水深是多少？", [], {})
        self.assertEqual(res.intent, "CLARIFICATION")
        self.assertFalse(res.should_update_slots)

    def test_b10_jinniuzuo_001_routes_to_device_capability(self):
        """10. '金牛座001最大水深是多少？' → DEVICE_CAPABILITY"""
        res = self.dm.intent_router.route("金牛座001最大水深是多少？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")

    def test_b11_001_in_non_device_context_not_device_capability(self):
        """11. '我有001个苹果吗？' 和 '订单001什么时候到？' → 不得为 DEVICE_CAPABILITY"""
        res1 = self.dm.intent_router.route("我有001个苹果吗？", [], {})
        self.assertNotEqual(res1.intent, "DEVICE_CAPABILITY")

        res2 = self.dm.intent_router.route("订单001什么时候到？", [], {})
        self.assertNotEqual(res2.intent, "DEVICE_CAPABILITY")

    def test_b12_longest_match_unique_device_overrides_short_ambiguous(self):
        """12. 验证最长匹配唯一设备优先于短歧义别名"""
        # '金牛座一号机' 长度 6，匹配唯一设备 crawler_variant_001
        # '一号机' 长度 3，匹配 7 个设备
        # 输入 '金牛座一号机作业水深是多少' 包含 '一号机' 和 '金牛座一号机'
        # 应按最长匹配 '金牛座一号机' 路由为 DEVICE_CAPABILITY
        res = self.dm.intent_router.route("金牛座一号机作业水深是多少？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")


# ─────────────────────────────────────────────────────────────────────────────
# 测试 C: 待确认油田受控简称识别
# ─────────────────────────────────────────────────────────────────────────────

class ControlledOilfieldAbbreviationRejectionTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def _setup_pending_oilfield(self, candidate_name="流花11-1油田"):
        self.dm.reset()
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.dm.slot_store.slots["oilfield_name"] = Slot("oilfield_name", value=candidate_name, status="pending_confirmation")
        self.dm.slot_store.slots["pending_oilfield_name"] = Slot("pending_oilfield_name", value=candidate_name, status="valid")
        self.dm.slot_store.slots["pending_oilfield_candidates"] = Slot(
            "pending_oilfield_candidates",
            value=[{"name": candidate_name, "id": "OF001", "confidence": 95}],
            status="valid"
        )

    def _is_pending_cleared(self):
        pending = self.dm.slot_store.slots.get("pending_oilfield_name")
        return pending is None or pending.value is None or pending.status == "missing"

    def test_c13_abbreviation_not_target_rejects_candidate(self):
        """13. '不是流花11-1' 拒绝候选油田（流花11-1油田的受控简称）"""
        self._setup_pending_oilfield("流花11-1油田")
        reply = self.dm.process("不是流花11-1")
        self.assertTrue(self._is_pending_cleared(), "'不是流花11-1' 应拒绝候选油田 '流花11-1油田'")

    def test_c14_abbreviation_wrong_rejects_candidate(self):
        """14. '流花11-1不对' 拒绝候选油田（受控简称）"""
        self._setup_pending_oilfield("流花11-1油田")
        reply = self.dm.process("流花11-1不对")
        self.assertTrue(self._is_pending_cleared(), "'流花11-1不对' 应拒绝候选油田 '流花11-1油田'")

    def test_c15_different_oilfield_does_not_clear_candidate(self):
        """15. '不是流花11-2油田' 不清除当前候选 '流花11-1油田'"""
        self._setup_pending_oilfield("流花11-1油田")
        reply = self.dm.process("不是流花11-2油田")
        pending = self.dm.slot_store.slots.get("pending_oilfield_name")
        self.assertIsNotNone(pending)
        self.assertEqual(pending.value, "流花11-1油田")

    def test_c16_water_depth_wrong_does_not_clear_candidate(self):
        """16. '这个水深不对' 不清除当前候选油田"""
        self._setup_pending_oilfield("流花11-1油田")
        reply = self.dm.process("这个水深不对")
        pending = self.dm.slot_store.slots.get("pending_oilfield_name")
        self.assertIsNotNone(pending)
        self.assertEqual(pending.value, "流花11-1油田")

    def test_c17_rejected_abbreviation_preserves_other_slots_and_phase(self):
        """17. 拒绝简称后其他槽位、phase 和任务状态保持正确"""
        self._setup_pending_oilfield("流花11-1油田")
        orig_water_depth = self.dm.slot_store.slots["water_depth"].value

        reply = self.dm.process("流花11-1不对")

        self.assertTrue(self._is_pending_cleared())
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, orig_water_depth)
        self.assertNotEqual(self.dm.phase, "rejected")


if __name__ == "__main__":
    unittest.main()
