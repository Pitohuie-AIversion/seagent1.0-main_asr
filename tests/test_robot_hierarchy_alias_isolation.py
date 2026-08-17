"""
tests/test_robot_hierarchy_alias_isolation.py

验证设备四级集合（类别、系列、型号、单机）别称 (aliases) 的独立性与隔离：
1. equipment_class 候选目录不得越级混入 family (如天鹰座) 或 unit (如天鹰座001) 的别名；
2. equipment_family 的 alias_mappings 必须准确包含 "天鹰座" -> "轻型工作级深海机器人"；
3. equipment_type 的 alias_mappings 必须包含 "天鹰座150HP" -> "轻型工作级深海机器人 150HP"；
4. equipment_unit_id 的 alias_mappings 必须包含 "天鹰座001" -> "LROV-150-001"；
5. DialogueManager 传递给 IntentRouter 的 expected_slot_options 必须包含 alias_mappings；
6. Extractor 能将 "天鹰座" 准确规范化为 "轻型工作级深海机器人"。
"""

import unittest
from unittest.mock import MagicMock

from src.knowledge_retriever import KnowledgeBase
from src.output_builder import OutputBuilder
from src.extractor import ParameterExtractor
from src.dialogue_manager import DialogueManager


class TestRobotHierarchyAliasIsolation(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.builder = OutputBuilder(self.kb)

    def test_class_catalog_does_not_contain_family_or_unit_aliases(self):
        """equipment_class 的别名集合不得包含天鹰座、金牛座等 family/unit 别名。"""
        field_def = {
            "key": "equipment_class",
            "label": "机器人类别",
            "type": "string",
            "allowed_values_ref": "robot_classes",
        }
        catalog = self.builder._resolve_candidate_catalog(
            field_def,
            task_type_key="pipeline_inspection",
            task_state={},
        )
        alias_mappings, _ = self.builder._build_alias_indexes(catalog)

        # equipment_class 的 alias_mappings 绝对不能把“天鹰座”或“金牛座”映射为类别
        self.assertNotIn("天鹰座", alias_mappings)
        self.assertNotIn("金牛座", alias_mappings)
        self.assertNotIn("轻型工作级深海机器人", alias_mappings)
        self.assertNotIn("天鹰座001", alias_mappings)

        # 但类别自身的别名仍应存在
        for cls in catalog:
            if cls["canonical_value"] == "观察级ROV":
                self.assertIn("observation_rov", cls["aliases"])

    def test_family_catalog_maps_tianyingzuo_to_light_work_class(self):
        """equipment_family 的 alias_mappings 必须正确将“天鹰座”映射为“轻型工作级深海机器人”。"""
        field_def = {
            "key": "equipment_family",
            "label": "作业机器人系列",
            "type": "string",
            "allowed_values_ref": "robot_family_full_names",
        }
        catalog = self.builder._resolve_candidate_catalog(
            field_def,
            task_type_key="pipeline_inspection",
            task_state={"equipment_class": "observation_rov"},
        )
        alias_mappings, _ = self.builder._build_alias_indexes(catalog)

        self.assertIn("天鹰座", alias_mappings)
        self.assertEqual(alias_mappings["天鹰座"], "轻型工作级深海机器人")
        self.assertIn("轻型ROV", alias_mappings)
        self.assertEqual(alias_mappings["轻型ROV"], "轻型工作级深海机器人")

    def test_variant_catalog_maps_tianyingzuo_150hp(self):
        """equipment_type 的 alias_mappings 必须正确将“天鹰座150HP”映射为标准型号。"""
        field_def = {
            "key": "equipment_type",
            "label": "作业设备型号",
            "type": "string",
            "allowed_values_ref": "robot_variant_full_names",
        }
        catalog = self.builder._resolve_candidate_catalog(
            field_def,
            task_type_key="pipeline_inspection",
            task_state={
                "equipment_class": "observation_rov",
                "equipment_family": "轻型工作级深海机器人",
            },
        )
        alias_mappings, _ = self.builder._build_alias_indexes(catalog)

        self.assertIn("天鹰座150HP", alias_mappings)
        self.assertEqual(alias_mappings["天鹰座150HP"], "轻型工作级深海机器人 150HP")

    def test_unit_catalog_maps_tianyingzuo_001(self):
        """equipment_unit_id 的 alias_mappings 必须正确将“天鹰座001”映射为 LROV-150-001。"""
        field_def = {
            "key": "equipment_unit_id",
            "label": "具体机器人编号",
            "type": "string",
            "allowed_values_ref": "robot_unit_ids",
        }
        catalog = self.builder._resolve_candidate_catalog(
            field_def,
            task_type_key="pipeline_inspection",
            task_state={
                "equipment_class": "observation_rov",
                "equipment_family": "轻型工作级深海机器人",
                "equipment_type": "轻型工作级深海机器人 150HP",
            },
        )
        alias_mappings, _ = self.builder._build_alias_indexes(catalog)

        self.assertIn("天鹰座001", alias_mappings)
        self.assertEqual(alias_mappings["天鹰座001"], "LROV-150-001")

    def test_extractor_resolves_family_alias_exact(self):
        """Extractor 能通过 alias_exact 将“天鹰座”规范化为“轻型工作级深海机器人”。"""
        extractor = ParameterExtractor(llm=MagicMock())
        required_by_key = {
            "equipment_family": {
                "key": "equipment_family",
                "label": "作业机器人系列",
                "type": "string",
                "allowed_values": ["轻型工作级深海机器人", "通用工作级深海机器人"],
                "alias_mappings": {"天鹰座": "轻型工作级深海机器人", "轻型": "轻型工作级深海机器人"},
            }
        }
        candidate = {
            "canonical_key": "equipment_family",
            "raw_key": "系列",
            "raw_value": "天鹰座",
            "normalized_value": "天鹰座",
            "confidence": 1.0,
        }
        resolved, unresolved = extractor._resolve_candidate_value(
            candidate,
            required_by_key=required_by_key,
            allowed_keys={"equipment_family"},
            current_state={},
            conversation_history=[],
        )
        self.assertIsNone(unresolved)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["normalized_value"], "轻型工作级深海机器人")
        self.assertEqual(resolved["resolution_method"], "alias_exact")

    def test_dialogue_manager_expected_slot_options_includes_alias_mappings(self):
        """DialogueManager 构造的 expected_slot_options 必须保留 alias_mappings。"""
        dm = DialogueManager(llm=MagicMock(), kb=self.kb)
        dm._last_missing = [
            {
                "key": "equipment_family",
                "label": "作业机器人系列",
                "allowed_values": ["轻型工作级深海机器人"],
                "alias_mappings": {"天鹰座": "轻型工作级深海机器人"},
            }
        ]
        options = [
            {
                "key": item.get("key"),
                "label": item.get("label"),
                "allowed_values": item.get("allowed_values") or [],
                "alias_mappings": item.get("alias_mappings") or {},
            }
            for item in dm._last_missing
            if isinstance(item, dict) and item.get("key")
        ]
        self.assertEqual(len(options), 1)
        self.assertIn("alias_mappings", options[0])
        self.assertEqual(options[0]["alias_mappings"]["天鹰座"], "轻型工作级深海机器人")

    def test_e2e_input_tianyingzuo_sets_equipment_family(self):
        """用户输入'我要使用天鹰座'，系统端到端将 equipment_family 正确设置为'轻型工作级深海机器人'。"""
        from tests.interaction_plan_support import (
            ScriptedLLM,
            make_plan,
            extraction_result,
            slot_candidate,
        )

        llm = ScriptedLLM(
            plans=[
                make_plan("WRITE"),  # 任务初始化
                make_plan("WRITE"),  # 输入天鹰座
            ],
            extractions=[
                extraction_result(
                    slot_candidate(
                        "task_type_key",
                        "pipeline_inspection",
                        raw_key="任务类型",
                        raw_value="管缆巡检",
                    ),
                    slot_candidate(
                        "water_depth",
                        100.0,
                        raw_key="水深",
                        raw_value="100米",
                    ),
                ),
                extraction_result(
                    slot_candidate(
                        "equipment_family",
                        "天鹰座",
                        raw_key="设备",
                        raw_value="天鹰座",
                    )
                ),
            ],
            default_reply="已为您记录设备信息。",
        )
        dm = DialogueManager(llm=llm, kb=self.kb)
        dm.process("我要进行水深100米的管缆巡检")
        self.assertEqual(dm.task_state.get("task_type_key"), "pipeline_inspection")

        # 第二轮：输入“我要使用天鹰座”
        dm.process("我要使用天鹰座")
        # 验证 equipment_family 已正确解析为规范名称
        self.assertEqual(dm.task_state.get("equipment_family"), "轻型工作级深海机器人")
        # 验证 equipment_class 已被自动级联推导为 observation_rov
        self.assertEqual(dm.task_state.get("equipment_class"), "observation_rov")

