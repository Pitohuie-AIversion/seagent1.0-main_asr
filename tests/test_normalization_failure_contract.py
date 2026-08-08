"""
tests/test_normalization_failure_contract.py

SEAgent Normalization Failure Status Contract Tests.
验证当用户输入无法被规范化器（Normalizer）合法解析时的最终状态契约：
1. slot.value 保留最近一次 confirmed old valid value（旧事实妥善保留，不被非法输入覆盖）；
2. slot.candidate_value 保存 Normalizer 拒绝的规范化候选；
3. slot.raw_value 保存真正用户原始表达（与 candidate_value 归因分离）；
4. slot.status = "conflict"（已有旧值时）或 "invalid"（无旧值时）；
5. conflict 状态下的字段暂停导出至 SlotStore.get_task_state()（遵循 INV-03）；
6. conflict 状态可通过取消/放弃操作或后续提交合法值恢复为 valid 状态；
7. 真实 Pipeline 测试不 mock IntentRouter.route 和 ParameterExtractor.extract_updates，仅 stub LLMClient；
8. 非 schema 控制/中间字段（emergency_mode, rov_description, raw_oilfield_name）不被 Normalizer 吞掉。
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

    def test_conflict_preserves_old_value_but_suspends_task_projection(self):
        """Test 1: 规范化失败引发 conflict 时，保留旧 confirmed value，但暂停进入 get_task_state() 导出。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)

        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.assertEqual(self.dm.slot_store.get_task_state().get("water_depth"), 300.0)

        mock_route = MagicMock()
        mock_route.dialogue_mode = "task_collection"

        mock_extract = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": "300abc",
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
        self.assertEqual(slot.candidate_value, "300abc")
        self.assertEqual(slot.raw_value, "差不多很深")
        self.assertEqual(slot.status, "conflict")
        self.assertIsNotNone(slot.validation_error)
        self.assertIn("无法将 '300abc' 规范化", slot.validation_error)

        # 核心判定：conflict 状态暂停进入正式 get_task_state() 导出
        self.assertNotIn("water_depth", self.dm.slot_store.get_task_state())

    def test_cancel_failed_candidate_restores_old_valid_fact(self):
        """Test 2: conflict 状态下，用户定向取消修改后恢复旧 valid 事实。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)

        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        mock_route = MagicMock()
        mock_route.dialogue_mode = "task_collection"

        mock_extract_invalid = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": "300abc",
                    "raw_value": "差不多很深",
                    "confidence": 0.9,
                    "resolution_method": "llm_semantic",
                }
            ]
        }

        with patch.object(self.dm.intent_router, "route", return_value=mock_route):
            with patch.object(self.dm.extractor, "extract_updates", return_value=mock_extract_invalid):
                self.dm.process("水深改成差不多很深", request_id="req_norm_fail_2a")

        slot_conflict = self.dm.slot_store.slots.get("water_depth")
        self.assertEqual(slot_conflict.status, "conflict")

        # 用户定向取消水深修改
        mock_extract_empty = {"slot_candidates": []}
        with patch.object(self.dm.intent_router, "route", return_value=mock_route):
            with patch.object(self.dm.extractor, "extract_updates", return_value=mock_extract_empty):
                self.dm.process("取消修改水深", request_id="req_norm_fail_2b")

        slot_restored = self.dm.slot_store.slots.get("water_depth")
        self.assertEqual(slot_restored.value, 300.0)
        self.assertEqual(slot_restored.status, "valid")
        self.assertIsNone(slot_restored.candidate_value)
        self.assertIsNone(slot_restored.validation_error)
        self.assertEqual(self.dm.slot_store.get_task_state().get("water_depth"), 300.0)

    def test_failure_preserves_original_raw_value(self):
        """Test 3: 验证 candidate_value 与 raw_value provenance 区分保存。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)

        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        mock_route = MagicMock()
        mock_route.dialogue_mode = "task_collection"

        # raw_value 为用户原文 "三百来米左右"，normalized_value 为解析候选 "300abc"
        mock_extract = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": "300abc",
                    "raw_value": "三百来米左右",
                    "confidence": 0.9,
                    "resolution_method": "llm_semantic",
                }
            ]
        }

        with patch.object(self.dm.intent_router, "route", return_value=mock_route):
            with patch.object(self.dm.extractor, "extract_updates", return_value=mock_extract):
                self.dm.process("水深改成三百来米左右", request_id="req_norm_fail_3")

        slot = self.dm.slot_store.slots.get("water_depth")
        self.assertEqual(slot.value, 300.0)
        self.assertEqual(slot.candidate_value, "300abc")  # 拒绝的规范化候选
        self.assertEqual(slot.raw_value, "三百来米左右")   # 真正的用户原始表达
        self.assertEqual(slot.status, "conflict")

    def test_real_pipeline_invalid_number_with_llm_stub(self):
        """Test 4: 真实 Pipeline 端到端测试（只 Mock LLMClient.extract_json，执行真实 Router/Extractor/Normalizer/SlotStore）。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)

        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        def stub_llm_extract_json(messages, max_tokens=None):
            return {
                "slot_candidates": [
                    {
                        "canonical_key": "water_depth",
                        "normalized_value": "300abc",
                        "raw_value": "差不多很深",
                        "confidence": 0.9,
                        "resolution_method": "llm_semantic",
                    }
                ]
            }

        with patch.object(self.dm.llm, "extract_json", side_effect=stub_llm_extract_json):
            # 不 mock route/extract_updates/commit，完整跑端到端
            self.dm.process("水深改成差不多很深", request_id="req_real_pipe_fail")

        slot = self.dm.slot_store.slots.get("water_depth")
        self.assertEqual(slot.value, 300.0)
        self.assertEqual(slot.candidate_value, "300abc")
        self.assertEqual(slot.raw_value, "差不多很深")
        self.assertEqual(slot.status, "conflict")
        self.assertNotIn("water_depth", self.dm.slot_store.get_task_state())

    def test_real_pipeline_valid_number_update_with_llm_stub(self):
        """Test 5: 真实 Pipeline 端到端合法修改测试（只 Mock LLMClient.extract_json）。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)

        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        def stub_llm_extract_json(messages, max_tokens=None):
            return {
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

        with patch.object(self.dm.llm, "extract_json", side_effect=stub_llm_extract_json):
            self.dm.process("水深改成500米", request_id="req_real_pipe_succ")

        slot = self.dm.slot_store.slots.get("water_depth")
        self.assertEqual(slot.value, 500.0)
        self.assertEqual(slot.status, "valid")
        self.assertIsNone(slot.candidate_value)
        self.assertEqual(self.dm.slot_store.get_task_state().get("water_depth"), 500.0)

    def test_non_schema_control_fields_are_not_silently_dropped(self):
        """Test 6: 验证非 schema 控制与中间字段（emergency_mode, rov_description, raw_oilfield_name）不被 Normalizer 误吞。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)

        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        # 1. emergency_mode 经过专用路径写入
        mock_route = MagicMock()
        mock_route.dialogue_mode = "task_collection"

        mock_extract_emergency = {
            "slot_candidates": [
                {
                    "canonical_key": "emergency_mode",
                    "normalized_value": True,
                    "raw_value": "紧急",
                    "confidence": 1.0,
                    "resolution_method": "rule",
                }
            ]
        }

        with patch.object(self.dm.intent_router, "route", return_value=mock_route):
            with patch.object(self.dm.extractor, "extract_updates", return_value=mock_extract_emergency):
                self.dm.process("进入紧急模式", request_id="req_ctrl_1")

        self.assertEqual(self.dm.mode, "emergency")


class TestFieldNormalizerUnit(unittest.TestCase):
    def setUp(self):
        self.normalizer = FieldNormalizer()

    def test_normalizer_number_success_and_failure(self):
        field_defs = [{"key": "water_depth", "type": "number"}]
        res_succ = self.normalizer.normalize_updates_with_failures({"water_depth": "300米"}, field_defs, {}, lambda f, s: None)
        self.assertEqual(res_succ.normalized_updates.get("water_depth"), 300.0)
        self.assertEqual(len(res_succ.failures), 0)

        res_fail = self.normalizer.normalize_updates_with_failures({"water_depth": "300abc"}, field_defs, {}, lambda f, s: None)
        self.assertNotIn("water_depth", res_fail.normalized_updates)
        self.assertIn("water_depth", res_fail.failures)
        self.assertEqual(res_fail.failures["water_depth"].raw_value, "300abc")

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
