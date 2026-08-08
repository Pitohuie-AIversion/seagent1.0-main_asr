"""test_task_patch.py — TaskPatch 模块与 Feature Flag、Effect Parity 规范化测试套件"""

import copy
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import yaml

from src.task_patch import (
    SlotPatch,
    ListMutationPatch,
    TaskPatch,
    TaskPatchError,
    TaskPatchValidationError,
    TaskPatchAdapterError,
    build_task_patch,
    task_patch_to_legacy_updates,
)
from src.model_profile import (
    is_task_patch_v2_enabled,
    ModelProfileConfigError,
)
from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase


def snapshot_effect(dm: DialogueManager) -> dict:
    """提取 DialogueManager 槽位与任务状态 Snapshot 的 Effect 映射。"""
    working_slots = dm.slot_store.slots
    slots_summary = {}
    for key, s in working_slots.items():
        val = s.value
        if key == "internal_id" and val:
            val = "MOCKED_INTERNAL_ID"
        slots_summary[key] = {
            "value": val,
            "status": s.status,
            "candidate_value": s.candidate_value,
            "raw_value": s.raw_value,
            "validation_error": s.validation_error,
            "source": s.source,
            "confidence": s.confidence,
        }

    task_state = copy.deepcopy(dm.slot_store.get_task_state())
    task_state.pop("store_id", None)
    if "internal_id" in task_state and task_state["internal_id"]:
        task_state["internal_id"] = "MOCKED_INTERNAL_ID"

    return {
        "slot_version": dm.slot_store.version,
        "slots": slots_summary,
        "task_state": task_state,
        "unresolved": list(dm.slot_store.unresolved),
        "phase": dm.phase,
        "published": dm.final_result is not None,
    }


class TestTaskPatchUnit(unittest.TestCase):
    """L1: TaskPatch 内部校验与 API 边界单元测试。"""

    def test_valid_slot_patch(self):
        sp = SlotPatch(
            key="water_depth",
            candidate_value=300,
            raw_value="300米",
            confidence=0.95,
            source="user_input",
            resolution_method="canonical_exact",
        )
        self.assertEqual(sp.key, "water_depth")
        self.assertEqual(sp.candidate_value, 300)
        self.assertEqual(sp.raw_value, "300米")
        self.assertEqual(sp.confidence, 0.95)
        self.assertEqual(sp.source, "user_input")
        self.assertEqual(sp.resolution_method, "canonical_exact")

    def test_reject_empty_key(self):
        with self.assertRaises(TaskPatchValidationError):
            SlotPatch(
                key="  ",
                candidate_value=100,
                raw_value="100",
                confidence=1.0,
                source="user_input",
            )

    def test_reject_unknown_key(self):
        res = {
            "slot_candidates": [
                {
                    "raw_key": "未知",
                    "canonical_key": "unknown_key",
                    "normalized_value": "abc",
                    "confidence": 1.0,
                }
            ],
            "unresolved": [],
            "list_mutations": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res, allowed_keys={"water_depth", "task_type"})

    def test_reject_bool_confidence(self):
        with self.assertRaises(TaskPatchValidationError):
            SlotPatch(
                key="water_depth",
                candidate_value=300,
                raw_value="300米",
                confidence=True,  # bool 非法
                source="user_input",
            )

    def test_reject_nan_confidence(self):
        with self.assertRaises(TaskPatchValidationError):
            SlotPatch(
                key="water_depth",
                candidate_value=300,
                raw_value="300米",
                confidence=float("nan"),
                source="user_input",
            )

    def test_reject_inf_confidence(self):
        with self.assertRaises(TaskPatchValidationError):
            SlotPatch(
                key="water_depth",
                candidate_value=300,
                raw_value="300米",
                confidence=float("inf"),
                source="user_input",
            )

    def test_reject_confidence_below_zero(self):
        with self.assertRaises(TaskPatchValidationError):
            SlotPatch(
                key="water_depth",
                candidate_value=300,
                raw_value="300米",
                confidence=-0.1,
                source="user_input",
            )

    def test_reject_confidence_above_one(self):
        with self.assertRaises(TaskPatchValidationError):
            SlotPatch(
                key="water_depth",
                candidate_value=300,
                raw_value="300米",
                confidence=1.05,
                source="user_input",
            )


class TestTaskPatchBuilderAndAdapter(unittest.TestCase):
    """L2: Builder & Legacy Adapter 转换逻辑测试。"""

    def test_build_task_patch_from_extraction_result(self):
        extraction_res = {
            "slot_candidates": [
                {
                    "raw_key": "水深",
                    "canonical_key": "water_depth",
                    "raw_value": "大约300米",
                    "normalized_value": 300,
                    "confidence": 0.95,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [
                {
                    "field": "payload",
                    "operation": "add",
                    "items": ["高精声呐"],
                    "target_items": [],
                    "raw_text": "增加高精声呐",
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ],
            "unresolved": ["  一些未识别说明  ", "一些未识别说明", "", None],
        }
        patch = build_task_patch(extraction_res, allowed_keys={"water_depth", "payload"})
        self.assertEqual(len(patch.slot_updates), 1)
        self.assertEqual(patch.slot_updates[0].key, "water_depth")
        self.assertEqual(patch.slot_updates[0].candidate_value, 300)

        self.assertEqual(len(patch.list_mutations), 1)
        self.assertEqual(patch.list_mutations[0].operation, "add")
        self.assertEqual(patch.list_mutations[0].items, ("高精声呐",))

        self.assertEqual(patch.unresolved, ("一些未识别说明",))

    def test_preserves_raw_value(self):
        res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "raw_value": "差不多三百度米",
                    "normalized_value": "300",
                    "confidence": 0.8,
                    "resolution_method": "llm_semantic",
                }
            ],
            "unresolved": [],
            "list_mutations": [],
        }
        patch = build_task_patch(res, allowed_keys={"water_depth"})
        self.assertEqual(patch.slot_updates[0].raw_value, "差不多三百度米")

    def test_preserves_resolution_method(self):
        res = {
            "slot_candidates": [
                {
                    "canonical_key": "cable_type",
                    "raw_value": "光纤",
                    "normalized_value": "armored_cable",
                    "confidence": 0.9,
                    "resolution_method": "alias_exact",
                }
            ],
            "unresolved": [],
            "list_mutations": [],
        }
        patch = build_task_patch(res, allowed_keys={"cable_type"})
        self.assertEqual(patch.slot_updates[0].resolution_method, "alias_exact")

    def test_preserves_unresolved_order(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [],
            "unresolved": ["item_b", "item_a", "item_c"],
        }
        patch = build_task_patch(res)
        self.assertEqual(patch.unresolved, ("item_b", "item_a", "item_c"))

    def test_deduplicates_unresolved_stably(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [],
            "unresolved": ["abc", "", "abc", " def "],
        }
        patch = build_task_patch(res)
        self.assertEqual(patch.unresolved, ("abc", "def"))


class TestListMutationContract(unittest.TestCase):
    """ListMutation 契约与规则断言测试。"""

    def test_add_mutation_contract(self):
        m = ListMutationPatch(
            field="payload",
            operation="add",
            items=("机械手",),
            target_items=(),
            raw_text="加一个机械手",
            confidence=0.95,
            source="user_input",
        )
        self.assertEqual(m.operation, "add")

    def test_remove_mutation_contract(self):
        m = ListMutationPatch(
            field="payload",
            operation="remove",
            items=("机械手",),
            target_items=(),
            raw_text="不要机械手",
            confidence=0.95,
            source="user_input",
        )
        self.assertEqual(m.operation, "remove")

    def test_replace_mutation_contract(self):
        m = ListMutationPatch(
            field="payload",
            operation="replace",
            items=("云台摄像机",),
            target_items=("机械手",),
            raw_text="把机械手换成云台摄像机",
            confidence=0.95,
            source="user_input",
        )
        self.assertEqual(m.operation, "replace")

    def test_clear_mutation_contract(self):
        m = ListMutationPatch(
            field="payload",
            operation="clear",
            items=(),
            target_items=(),
            raw_text="清空载荷",
            confidence=0.95,
            source="user_input",
        )
        self.assertEqual(m.operation, "clear")

    def test_reject_invalid_operation(self):
        with self.assertRaises(TaskPatchValidationError):
            ListMutationPatch(
                field="payload",
                operation="ambiguous",  # 非法操作
                items=("A",),
                target_items=(),
                raw_text="测试",
                confidence=0.95,
                source="user_input",
            )

    def test_reject_replace_without_target(self):
        with self.assertRaises(TaskPatchValidationError):
            ListMutationPatch(
                field="payload",
                operation="replace",
                items=("A",),
                target_items=(),  # replace 缺 target_items 拒绝
                raw_text="测试",
                confidence=0.95,
                source="user_input",
            )

    def test_cancel_water_depth_not_payload_mutation(self):
        from src.extractor import ParameterExtractor
        list_mutations, unresolved = ParameterExtractor._detect_payload_mutation(
            user_message="取消修改水深",
            current_state={"water_depth": 300, "payload": ["云台摄像机"]},
            required=[{"key": "payload", "allowed_values": ["云台摄像机", "机械手"]}],
        )
        self.assertEqual(list_mutations, [])
        self.assertEqual(unresolved, [])


class TestTaskPatchAdapter(unittest.TestCase):
    """Adapter 映射准确性断言测试。"""

    def test_task_patch_to_legacy_preserves_candidate(self):
        patch_obj = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="water_depth",
                    candidate_value=500,
                    raw_value="500米",
                    confidence=0.95,
                    source="user_input",
                    resolution_method="canonical_exact",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        stage_upd, mutations, unresolved = task_patch_to_legacy_updates(patch_obj)
        self.assertIn("water_depth", stage_upd)
        self.assertEqual(stage_upd["water_depth"]["value"], 500)

    def test_task_patch_to_legacy_preserves_raw(self):
        patch_obj = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="water_depth",
                    candidate_value=500,
                    raw_value="差不多五百米",
                    confidence=0.95,
                    source="user_input",
                    resolution_method="canonical_exact",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        stage_upd, _, _ = task_patch_to_legacy_updates(patch_obj)
        self.assertEqual(stage_upd["water_depth"]["raw_value"], "差不多五百米")

    def test_task_patch_to_legacy_preserves_confidence(self):
        patch_obj = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="water_depth",
                    candidate_value=500,
                    raw_value="500m",
                    confidence=0.88,
                    source="user_input",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        stage_upd, _, _ = task_patch_to_legacy_updates(patch_obj)
        self.assertEqual(stage_upd["water_depth"]["confidence"], 0.88)

    def test_task_patch_to_legacy_preserves_source_semantics(self):
        patch_obj = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="cable_type",
                    candidate_value="armored_cable",
                    raw_value="铠装",
                    confidence=0.95,
                    source="alias_mapping",
                    resolution_method="alias_exact",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        stage_upd, _, _ = task_patch_to_legacy_updates(patch_obj)
        self.assertEqual(stage_upd["cable_type"]["source"], "alias_mapping")


class TestFeatureFlag(unittest.TestCase):
    """Feature Flag 开关与异常行为测试。"""

    def test_task_patch_flag_defaults_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "features.yaml"
            yaml_path.write_text(
                "schema_version: 1\nfeatures:\n  task_patch_v2: false\n",
                encoding="utf-8",
            )
            self.assertFalse(is_task_patch_v2_enabled(features_path=yaml_path))

    def test_task_patch_flag_true_uses_task_patch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "features.yaml"
            yaml_path.write_text(
                "schema_version: 1\nfeatures:\n  task_patch_v2: true\n",
                encoding="utf-8",
            )
            self.assertTrue(is_task_patch_v2_enabled(features_path=yaml_path))

    def test_task_patch_flag_false_does_not_construct_task_patch(self):
        dm = DialogueManager(llm=MagicMock())
        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=False), \
             patch("src.dialogue_manager.build_task_patch") as mock_builder:
            dm.process("创建一个管缆巡检任务")
            mock_builder.assert_not_called()

    def test_task_patch_invalid_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "features.yaml"
            # 字符串 "true" 非 bool
            yaml_path.write_text(
                'schema_version: 1\nfeatures:\n  task_patch_v2: "true"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ModelProfileConfigError):
                is_task_patch_v2_enabled(features_path=yaml_path)

    def test_task_patch_build_failure_does_not_fallback_to_write(self):
        dm = DialogueManager(llm=MagicMock())
        bad_extraction = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": 300,
                    "confidence": "INVALID_CONFIDENCE",  # 会引发 build_task_patch 校验失败
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        dm.extractor.extract_updates = MagicMock(return_value=bad_extraction)
        dm.task_type_key = "pipeline_inspection"
        dm.slot_store.commit_transaction(
            {"task_type_key": MagicMock(value="pipeline_inspection", status="valid")},
            [],
        )

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True):
            with self.assertRaises(TaskPatchError):
                dm.process("水深300米")


class TestEffectParity(unittest.TestCase):
    """L3: task_patch_v2=false 与 task_patch_v2=true 对比 12 大场景 Effect Parity。"""

    def setUp(self):
        self.mock_llm = MagicMock()

    @staticmethod
    def _get_extraction_for_turn(turn: str) -> dict:
        if "管缆巡检任务" in turn:
            return {
                "slot_candidates": [
                    {
                        "raw_key": "任务类型",
                        "canonical_key": "task_type",
                        "raw_value": "管缆巡检",
                        "normalized_value": "管缆巡检",
                        "confidence": 0.95,
                        "resolution_method": "canonical_exact",
                    },
                    {
                        "raw_key": "任务类型标识",
                        "canonical_key": "task_type_key",
                        "raw_value": "巡检",
                        "normalized_value": "pipeline_inspection",
                        "confidence": 0.95,
                        "resolution_method": "canonical_exact",
                    },
                ],
                "unresolved": [],
                "list_mutations": [],
            }
        elif "水深改成500米" in turn:
            return {
                "slot_candidates": [
                    {
                        "raw_key": "水深",
                        "canonical_key": "water_depth",
                        "raw_value": "500米",
                        "normalized_value": 500,
                        "confidence": 0.95,
                        "resolution_method": "canonical_exact",
                    }
                ],
                "unresolved": [],
                "list_mutations": [],
            }
        elif "水深300米" in turn:
            return {
                "slot_candidates": [
                    {
                        "raw_key": "水深",
                        "canonical_key": "water_depth",
                        "raw_value": "300米",
                        "normalized_value": 300,
                        "confidence": 0.95,
                        "resolution_method": "canonical_exact",
                    }
                ],
                "unresolved": [],
                "list_mutations": [],
            }
        elif "带上云台摄像机" in turn:
            return {
                "slot_candidates": [],
                "unresolved": [],
                "list_mutations": [
                    {
                        "field": "payload",
                        "operation": "add",
                        "items": ["云台摄像机"],
                        "target_items": [],
                        "raw_text": "带上云台摄像机",
                        "confidence": 0.95,
                        "source": "user_input",
                    }
                ],
            }
        elif "去掉云台摄像机" in turn:
            return {
                "slot_candidates": [],
                "unresolved": [],
                "list_mutations": [
                    {
                        "field": "payload",
                        "operation": "remove",
                        "items": ["云台摄像机"],
                        "target_items": [],
                        "raw_text": "去掉云台摄像机",
                        "confidence": 0.95,
                        "source": "user_input",
                    }
                ],
            }
        elif "把云台摄像机换成机械手" in turn:
            return {
                "slot_candidates": [],
                "unresolved": [],
                "list_mutations": [
                    {
                        "field": "payload",
                        "operation": "replace",
                        "items": ["机械手"],
                        "target_items": ["云台摄像机"],
                        "raw_text": "把云台摄像机换成机械手",
                        "confidence": 0.95,
                        "source": "user_input",
                    }
                ],
            }
        elif "清空所有载荷" in turn:
            return {
                "slot_candidates": [],
                "unresolved": [],
                "list_mutations": [
                    {
                        "field": "payload",
                        "operation": "clear",
                        "items": [],
                        "target_items": [],
                        "raw_text": "清空所有载荷",
                        "confidence": 0.95,
                        "source": "user_input",
                    }
                ],
            }
        elif "设备使用Seaeye Tiger" in turn:
            return {
                "slot_candidates": [
                    {
                        "raw_key": "设备型号",
                        "canonical_key": "equipment_type",
                        "raw_value": "Seaeye Tiger",
                        "normalized_value": "Seaeye Tiger",
                        "confidence": 0.95,
                        "resolution_method": "canonical_exact",
                    }
                ],
                "unresolved": [],
                "list_mutations": [],
            }
        return {
            "slot_candidates": [],
            "unresolved": [],
            "list_mutations": [],
        }

    def _run_turn_parity(self, turns: list[str]) -> tuple[dict, dict]:
        """对相同的对话输入，分别在 flag=False 和 flag=True 下跑完并输出 snapshot_effect。"""
        # Legacy run
        dm_legacy = DialogueManager(llm=self.mock_llm)
        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=False):
            for turn in turns:
                ext_res = self._get_extraction_for_turn(turn)
                with patch.object(dm_legacy.extractor, "extract_updates", return_value=ext_res):
                    dm_legacy.process(turn)
        effect_legacy = snapshot_effect(dm_legacy)

        # V2 run
        dm_v2 = DialogueManager(llm=self.mock_llm)
        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True):
            for turn in turns:
                ext_res = self._get_extraction_for_turn(turn)
                with patch.object(dm_v2.extractor, "extract_updates", return_value=ext_res):
                    dm_v2.process(turn)
        effect_v2 = snapshot_effect(dm_v2)

        return effect_legacy, effect_v2

    def test_parity_01_create_task(self):
        turns = ["创建一个管缆巡检任务"]
        eff_legacy, eff_v2 = self._run_turn_parity(turns)
        self.assertEqual(eff_legacy, eff_v2)

    def test_parity_02_valid_field(self):
        turns = ["创建一个管缆巡检任务", "水深300米"]
        eff_legacy, eff_v2 = self._run_turn_parity(turns)
        self.assertEqual(eff_legacy, eff_v2)

    def test_parity_03_modify_valid(self):
        turns = ["创建一个管缆巡检任务", "水深300米", "水深改成500米"]
        eff_legacy, eff_v2 = self._run_turn_parity(turns)
        self.assertEqual(eff_legacy, eff_v2)

    def test_parity_04_normalization_failure_with_old_valid(self):
        # 先填入合法水深300，再填入非法水深
        dm_legacy = DialogueManager(llm=self.mock_llm)
        dm_v2 = DialogueManager(llm=self.mock_llm)

        t1_ext = self._get_extraction_for_turn("创建一个管缆巡检任务")
        t2_ext = self._get_extraction_for_turn("水深300米")
        bad_extraction = {
            "slot_candidates": [
                {
                    "raw_key": "水深",
                    "canonical_key": "water_depth",
                    "raw_value": "非法深度",
                    "normalized_value": "invalid_depth_val",
                    "confidence": 0.95,
                    "resolution_method": "type_normalization",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=False):
            with patch.object(dm_legacy.extractor, "extract_updates", return_value=t1_ext):
                dm_legacy.process("创建一个管缆巡检任务")
            with patch.object(dm_legacy.extractor, "extract_updates", return_value=t2_ext):
                dm_legacy.process("水深300米")
            with patch.object(dm_legacy.extractor, "extract_updates", return_value=bad_extraction):
                dm_legacy.process("水深改成非法深度")
        eff_legacy = snapshot_effect(dm_legacy)

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True):
            with patch.object(dm_v2.extractor, "extract_updates", return_value=t1_ext):
                dm_v2.process("创建一个管缆巡检任务")
            with patch.object(dm_v2.extractor, "extract_updates", return_value=t2_ext):
                dm_v2.process("水深300米")
            with patch.object(dm_v2.extractor, "extract_updates", return_value=bad_extraction):
                dm_v2.process("水深改成非法深度")
        eff_v2 = snapshot_effect(dm_v2)

        self.assertEqual(eff_legacy, eff_v2)
        # 验证两边都保持 INV-04：旧 valid 值 300 保持不变，status 为 conflict，candidate_value 为 invalid_depth_val
        slot = eff_v2["slots"]["water_depth"]
        self.assertEqual(slot["value"], 300)
        self.assertEqual(slot["candidate_value"], "invalid_depth_val")
        self.assertEqual(slot["status"], "conflict")
        self.assertIsNotNone(slot["validation_error"])

    def test_parity_05_normalization_failure_without_old_valid(self):
        dm_legacy = DialogueManager(llm=self.mock_llm)
        dm_v2 = DialogueManager(llm=self.mock_llm)

        t1_ext = self._get_extraction_for_turn("创建一个管缆巡检任务")
        bad_extraction = {
            "slot_candidates": [
                {
                    "raw_key": "水深",
                    "canonical_key": "water_depth",
                    "raw_value": "非法深度",
                    "normalized_value": "invalid_depth_val",
                    "confidence": 0.95,
                    "resolution_method": "type_normalization",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=False):
            with patch.object(dm_legacy.extractor, "extract_updates", return_value=t1_ext):
                dm_legacy.process("创建一个管缆巡检任务")
            with patch.object(dm_legacy.extractor, "extract_updates", return_value=bad_extraction):
                dm_legacy.process("水深改成非法深度")
        eff_legacy = snapshot_effect(dm_legacy)

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True):
            with patch.object(dm_v2.extractor, "extract_updates", return_value=t1_ext):
                dm_v2.process("创建一个管缆巡检任务")
            with patch.object(dm_v2.extractor, "extract_updates", return_value=bad_extraction):
                dm_v2.process("水深改成非法深度")
        eff_v2 = snapshot_effect(dm_v2)

        self.assertEqual(eff_legacy, eff_v2)
        slot = eff_v2["slots"]["water_depth"]
        self.assertIsNone(slot["value"])
        self.assertEqual(slot["candidate_value"], "invalid_depth_val")
        self.assertEqual(slot["status"], "invalid")

    def test_parity_06_payload_add(self):
        turns = ["创建一个管缆巡检任务", "带上云台摄像机"]
        eff_legacy, eff_v2 = self._run_turn_parity(turns)
        self.assertEqual(eff_legacy, eff_v2)

    def test_parity_07_payload_remove(self):
        turns = ["创建一个管缆巡检任务", "带上云台摄像机", "去掉云台摄像机"]
        eff_legacy, eff_v2 = self._run_turn_parity(turns)
        self.assertEqual(eff_legacy, eff_v2)

    def test_parity_08_payload_replace(self):
        turns = ["创建一个管缆巡检任务", "带上云台摄像机", "把云台摄像机换成机械手"]
        eff_legacy, eff_v2 = self._run_turn_parity(turns)
        self.assertEqual(eff_legacy, eff_v2)

    def test_parity_09_payload_clear(self):
        turns = ["创建一个管缆巡检任务", "带上云台摄像机", "清空所有载荷"]
        eff_legacy, eff_v2 = self._run_turn_parity(turns)
        self.assertEqual(eff_legacy, eff_v2)

    def test_parity_10_cancel_water_depth(self):
        turns = ["创建一个管缆巡检任务", "水深300米", "取消修改水深"]
        eff_legacy, eff_v2 = self._run_turn_parity(turns)
        self.assertEqual(eff_legacy, eff_v2)

    def test_parity_11_equipment_candidate(self):
        turns = ["创建一个管缆巡检任务", "设备使用Seaeye Tiger"]
        eff_legacy, eff_v2 = self._run_turn_parity(turns)
        self.assertEqual(eff_legacy, eff_v2)

    def test_parity_12_unresolved_preserved(self):
        dm_legacy = DialogueManager(llm=self.mock_llm)
        dm_v2 = DialogueManager(llm=self.mock_llm)

        t1_ext = self._get_extraction_for_turn("创建一个管缆巡检任务")
        unresolved_extraction = {
            "slot_candidates": [],
            "list_mutations": [],
            "unresolved": ["未识别信息XYZ"],
        }

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=False):
            with patch.object(dm_legacy.extractor, "extract_updates", return_value=t1_ext):
                dm_legacy.process("创建一个管缆巡检任务")
            with patch.object(dm_legacy.extractor, "extract_updates", return_value=unresolved_extraction):
                dm_legacy.process("发起 包含未识别信息XYZ")
        eff_legacy = snapshot_effect(dm_legacy)

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True):
            with patch.object(dm_v2.extractor, "extract_updates", return_value=t1_ext):
                dm_v2.process("创建一个管缆巡检任务")
            with patch.object(dm_v2.extractor, "extract_updates", return_value=unresolved_extraction):
                dm_v2.process("发起 包含未识别信息XYZ")
        eff_v2 = snapshot_effect(dm_v2)

        self.assertEqual(eff_legacy, eff_v2)
        self.assertIn("未识别信息XYZ", eff_v2["unresolved"])


if __name__ == "__main__":
    unittest.main()
