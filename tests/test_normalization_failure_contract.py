"""
tests/test_normalization_failure_contract.py

SEAgent Normalization Failure Status Contract Tests.
验证当用户输入无法被规范化器（Normalizer）合法解析时：
1. 若已有 valid 旧事实，保留 slot.value = old_valid_value，slot.candidate_value = raw_input，slot.status = "conflict"；
2. 若无 valid 旧事实，slot.value = None，slot.candidate_value = raw_input，slot.status = "invalid"；
3. SlotStore.get_task_state() 正确投影旧 valid 事实，非法输入不覆盖旧事实也不泄露至正式导出；
4. 后续输入合法值可正常恢复为 valid 状态；
5. 覆盖 number, datetime, coord, enum 等多字段类型。
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.llm_client import LLMClient
from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.slot_store import Slot, SlotStore
from src.normalizer import FieldNormalizer


def _make_dm(tmp_dir: Path) -> DialogueManager:
    state_file = tmp_dir / "state.yaml"
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.state_file = state_file
    llm = LLMClient(None, None)
    return DialogueManager(llm, kb)


class TestNormalizationFailureContract(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self._tmp)
        self.dm = _make_dm(self.tmp_path)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_invalid_number_does_not_overwrite_old_valid_value(self):
        """Test 1: 已有 valid 水深 300.0，尝试修改为非法数值'差不多很深'。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)

        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.assertEqual(self.dm.slot_store.get_task_state().get("water_depth"), 300.0)

        # 真实 Router / Extractor 链路（仅 Mock 核心 LLM 函数，不 mock Router/Extractor 本身）
        mock_route = MagicMock()
        mock_route.dialogue_mode = "task_collection"

        mock_extract = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": "差不多很深",
                    "raw_value": "差不多很深",
                    "confidence": 0.9,
                    "resolution_method": "llm_semantic",
                }
            ]
        }

        with patch.object(self.dm.intent_router, "route", return_value=mock_route):
            with patch.object(self.dm.extractor, "extract_updates", return_value=mock_extract):
                self.dm.process("水深改成差不多很深", request_id="req_norm_fail_1")

        slot = self.dm.slot_store.slots.get("water_depth")
        self.assertEqual(slot.value, 300.0)
        self.assertEqual(slot.candidate_value, "差不多很深")
        self.assertEqual(slot.raw_value, "差不多很深")
        self.assertEqual(slot.status, "conflict")
        self.assertIsNotNone(slot.validation_error)
        self.assertIn("无法将 '差不多很深' 规范化", slot.validation_error)

        self.assertNotEqual(self.dm.slot_store.get_task_state().get("water_depth"), "差不多很深")

    def test_invalid_input_without_old_valid_value(self):
        """Test 2: 槽位原无 valid 值，用户输入非法值，slot.value 为 None，status 为 invalid。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)

        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        mock_route = MagicMock()
        mock_route.dialogue_mode = "task_collection"

        mock_extract = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": "差不多很深",
                    "raw_value": "差不多很深",
                    "confidence": 0.9,
                    "resolution_method": "llm_semantic",
                }
            ]
        }

        with patch.object(self.dm.intent_router, "route", return_value=mock_route):
            with patch.object(self.dm.extractor, "extract_updates", return_value=mock_extract):
                self.dm.process("水深差不多很深", request_id="req_norm_fail_2")

        slot = self.dm.slot_store.slots.get("water_depth")
        self.assertIsNone(slot.value)
        self.assertEqual(slot.candidate_value, "差不多很深")
        self.assertEqual(slot.raw_value, "差不多很深")
        self.assertEqual(slot.status, "invalid")
        self.assertIsNotNone(slot.validation_error)

        self.assertNotIn("water_depth", self.dm.slot_store.get_task_state())

    def test_valid_modification_after_invalid_input(self):
        """Test 3: 非法输入处于 conflict 状态后，后续提供合法值，槽位正常恢复为 valid。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)

        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        # Step 1: 尝试非法修改
        mock_route = MagicMock()
        mock_route.dialogue_mode = "task_collection"

        mock_extract_invalid = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": "差不多很深",
                    "raw_value": "差不多很深",
                    "confidence": 0.9,
                    "resolution_method": "llm_semantic",
                }
            ]
        }

        with patch.object(self.dm.intent_router, "route", return_value=mock_route):
            with patch.object(self.dm.extractor, "extract_updates", return_value=mock_extract_invalid):
                self.dm.process("水深改成差不多很深", request_id="req_norm_fail_3a")

        slot_conflict = self.dm.slot_store.slots.get("water_depth")
        self.assertEqual(slot_conflict.status, "conflict")
        self.assertEqual(slot_conflict.value, 300.0)

        # Step 2: 输入合法修改 "改成500米"
        mock_extract_valid = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": "500",
                    "raw_value": "500米",
                    "confidence": 0.95,
                    "resolution_method": "regex_rule",
                }
            ]
        }

        with patch.object(self.dm.intent_router, "route", return_value=mock_route):
            with patch.object(self.dm.extractor, "extract_updates", return_value=mock_extract_valid):
                self.dm.process("改成500米", request_id="req_norm_fail_3b")

        slot_valid = self.dm.slot_store.slots.get("water_depth")
        self.assertEqual(slot_valid.value, 500.0)
        self.assertEqual(slot_valid.status, "valid")
        self.assertIsNone(slot_valid.candidate_value)
        self.assertIsNone(slot_valid.validation_error)
        self.assertEqual(self.dm.slot_store.get_task_state().get("water_depth"), 500.0)

    def test_datetime_normalization_failure(self):
        """Test 4: datetime 无法规范化时，保留旧 valid 时间。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)

        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["start_time"] = Slot("start_time", value="2026-08-10T09:00:00", status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        mock_route = MagicMock()
        mock_route.dialogue_mode = "task_collection"

        mock_extract = {
            "slot_candidates": [
                {
                    "canonical_key": "start_time",
                    "normalized_value": "随便什么时候",
                    "raw_value": "随便什么时候",
                    "confidence": 0.9,
                    "resolution_method": "llm_semantic",
                }
            ]
        }

        with patch.object(self.dm.intent_router, "route", return_value=mock_route):
            with patch.object(self.dm.extractor, "extract_updates", return_value=mock_extract):
                self.dm.process("开始时间改成随便什么时候", request_id="req_dt_fail")

        slot = self.dm.slot_store.slots.get("start_time")
        self.assertEqual(slot.value, "2026-08-10T09:00:00")
        self.assertEqual(slot.candidate_value, "随便什么时候")
        self.assertEqual(slot.status, "conflict")
        self.assertIsNotNone(slot.validation_error)
        self.assertNotEqual(self.dm.slot_store.get_task_state().get("start_time"), "随便什么时候")

    def test_coord_normalization_failure(self):
        """Test 5: coord 无法规范化时，保留旧 valid 坐标。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)

        old_coord = {"lat": 20.0, "lon": 110.0}
        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["start_point"] = Slot("start_point", value=old_coord, status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        mock_route = MagicMock()
        mock_route.dialogue_mode = "task_collection"

        mock_extract = {
            "slot_candidates": [
                {
                    "canonical_key": "start_point",
                    "normalized_value": "非法坐标文本",
                    "raw_value": "非法坐标文本",
                    "confidence": 0.9,
                    "resolution_method": "llm_semantic",
                }
            ]
        }

        with patch.object(self.dm.intent_router, "route", return_value=mock_route):
            with patch.object(self.dm.extractor, "extract_updates", return_value=mock_extract):
                self.dm.process("起点改为非法坐标文本", request_id="req_coord_fail")

        slot = self.dm.slot_store.slots.get("start_point")
        self.assertEqual(slot.value, old_coord)
        self.assertEqual(slot.candidate_value, "非法坐标文本")
        self.assertEqual(slot.status, "conflict")
        self.assertNotEqual(self.dm.slot_store.get_task_state().get("start_point"), "非法坐标文本")
        self.assertEqual(slot.value, old_coord)

    def test_enum_normalization_failure(self):
        """Test 6: enum/string 包含限定集合时，无法规范化保留旧 valid 对应值。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)

        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["cable_type"] = Slot("cable_type", value="电力电缆", status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        mock_route = MagicMock()
        mock_route.dialogue_mode = "task_collection"

        mock_extract = {
            "slot_candidates": [
                {
                    "canonical_key": "cable_type",
                    "normalized_value": "魔法线缆",
                    "raw_value": "魔法线缆",
                    "confidence": 0.9,
                    "resolution_method": "llm_semantic",
                }
            ]
        }

        with patch.object(self.dm.intent_router, "route", return_value=mock_route):
            with patch.object(self.dm.extractor, "extract_updates", return_value=mock_extract):
                self.dm.process("管缆类型改成魔法线缆", request_id="req_enum_fail")

        slot = self.dm.slot_store.slots.get("cable_type")
        self.assertEqual(slot.value, "电力电缆")
        self.assertEqual(slot.candidate_value, "魔法线缆")
        self.assertEqual(slot.status, "conflict")
        self.assertIsNotNone(slot.validation_error)
        self.assertNotEqual(self.dm.slot_store.get_task_state().get("cable_type"), "魔法线缆")


class TestFieldNormalizerUnit(unittest.TestCase):
    def setUp(self):
        self.normalizer = FieldNormalizer()

    def test_normalizer_number_success_and_failure(self):
        field_defs = [{"key": "water_depth", "type": "number"}]
        # 成功
        res_succ = self.normalizer.normalize_updates_with_failures({"water_depth": "300米"}, field_defs, {}, lambda f, s: None)
        self.assertEqual(res_succ.normalized_updates.get("water_depth"), 300.0)
        self.assertEqual(len(res_succ.failures), 0)

        # 失败：非法 raw 值绝不进入 normalized_updates，进入 failures
        res_fail = self.normalizer.normalize_updates_with_failures({"water_depth": "差不多很深"}, field_defs, {}, lambda f, s: None)
        self.assertNotIn("water_depth", res_fail.normalized_updates)
        self.assertIn("water_depth", res_fail.failures)
        self.assertEqual(res_fail.failures["water_depth"].raw_value, "差不多很深")

    def test_normalizer_datetime_success_and_failure(self):
        field_defs = [{"key": "start_time", "type": "datetime"}]
        res_succ = self.normalizer.normalize_updates_with_failures({"start_time": "2026-08-10 09:00:00"}, field_defs, {}, lambda f, s: None)
        self.assertEqual(res_succ.normalized_updates.get("start_time"), "2026-08-10T09:00:00")

        res_fail = self.normalizer.normalize_updates_with_failures({"start_time": "随便什么时候"}, field_defs, {}, lambda f, s: None)
        self.assertNotIn("start_time", res_fail.normalized_updates)
        self.assertIn("start_time", res_fail.failures)

    def test_normalizer_coord_success_and_failure(self):
        field_defs = [{"key": "start_point", "type": "coord"}]
        res_succ = self.normalizer.normalize_updates_with_failures({"start_point": "北纬20度，东经110度"}, field_defs, {}, lambda f, s: None)
        self.assertEqual(res_succ.normalized_updates.get("start_point"), {"lat": 20.0, "lon": 110.0})

        res_fail = self.normalizer.normalize_updates_with_failures({"start_point": "非法坐标"}, field_defs, {}, lambda f, s: None)
        self.assertNotIn("start_point", res_fail.normalized_updates)
        self.assertIn("start_point", res_fail.failures)

    def test_normalizer_enum_success_and_failure(self):
        field_defs = [{"key": "cable_type", "type": "string"}]
        allowed = ["电力电缆", "光纤通信缆"]
        res_succ = self.normalizer.normalize_updates_with_failures({"cable_type": "电力电缆"}, field_defs, {}, lambda f, s: allowed)
        self.assertEqual(res_succ.normalized_updates.get("cable_type"), "电力电缆")

        res_fail = self.normalizer.normalize_updates_with_failures({"cable_type": "魔法线缆"}, field_defs, {}, lambda f, s: allowed)
        self.assertNotIn("cable_type", res_fail.normalized_updates)
        self.assertIn("cable_type", res_fail.failures)

