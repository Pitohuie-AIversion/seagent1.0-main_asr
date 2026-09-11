"""
tests/test_equipment_subhandlers.py

测试新增的设备级联解耦子处理器:
- EquipmentScopingHandler (推荐与可见序数选项范围限定)
- EquipmentCollapseHandler (四级级联自动收敛推导)
- EquipmentCascadeResolver 组合委托透明度与兼容性
"""

import unittest
from unittest.mock import MagicMock

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import Slot
from src.handlers.equipment_scoping import EquipmentScopingHandler
from src.handlers.equipment_collapse import EquipmentCollapseHandler
from src.handlers.equipment_cascade import EquipmentCascadeResolver
from tests.interaction_plan_support import ScriptedLLM, make_plan


class TestEquipmentSubhandlers(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = ScriptedLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_scoping_source_mapping_staticmethod(self):
        """验证 EquipmentScopingHandler 来源映射静态方法与别名。"""
        mappings = {
            "canonical_exact": "user_input",
            "alias_exact": "alias_mapping",
            "llm_semantic": "llm_semantic_match",
            "type_normalization": "user_input",
            "assistant_recommendation": "assistant_recommendation",
            "visible_ordinal_selection": "assistant_option_selection",
            "unknown_type": "user_input",
            None: "user_input",
        }
        for method, expected in mappings.items():
            self.assertEqual(EquipmentScopingHandler._source_for_resolution_method(method), expected)
            self.assertEqual(EquipmentScopingHandler.source_for_resolution_method(method), expected)

            # 通过 EquipmentCascadeResolver 类与实例调用均能正确解析
            self.assertEqual(EquipmentCascadeResolver._source_for_resolution_method(method), expected)
            self.assertEqual(self.dm.slot_handler.equipment_cascade.source_for_resolution_method(method), expected)

    def test_scoping_handler_instantiation_and_proxy(self):
        """验证 EquipmentScopingHandler 实例属性代理至 manager。"""
        scoping = EquipmentScopingHandler(self.dm)
        self.assertIs(scoping.kb, self.kb)
        self.assertIs(scoping.task_state, self.dm.task_state)

        with self.assertRaises(AttributeError):
            _ = scoping.non_existent_attribute_xyz

    def test_collapse_handler_instantiation_and_proxy(self):
        """验证 EquipmentCollapseHandler 实例属性代理至 manager。"""
        collapse = EquipmentCollapseHandler(self.dm)
        self.assertIs(collapse.kb, self.kb)
        self.assertIs(collapse.task_state, self.dm.task_state)

        with self.assertRaises(AttributeError):
            _ = collapse.non_existent_attribute_xyz

    def test_resolver_subhandler_delegation(self):
        """验证 EquipmentCascadeResolver 优先向 scoping 和 collapse 委托。"""
        resolver = self.dm.slot_handler.equipment_cascade
        self.assertIsInstance(resolver.scoping, EquipmentScopingHandler)
        self.assertIsInstance(resolver.collapse, EquipmentCollapseHandler)

        # 检查子处理器的方法在 resolver 上可透明调用
        self.assertTrue(callable(getattr(resolver, "_scope_confirmed_recommendation")))
        self.assertTrue(callable(getattr(resolver, "_scope_visible_ordinal_selections")))
        self.assertTrue(callable(getattr(resolver, "_auto_collapse_robot_cascade")))

    def test_collapse_noop_without_task_type(self):
        """验证没有 task_type_key 时，EquipmentCollapseHandler 自动跳过推导。"""
        slots = {"equipment_class": Slot("equipment_class")}
        self.dm.slot_handler.equipment_cascade.collapse._auto_collapse_robot_cascade(slots)
        self.assertIsNone(slots["equipment_class"].value)
        self.assertEqual(slots["equipment_class"].status, "missing")

    def test_scope_confirmed_recommendation_non_write(self):
        """验证非 WRITE 操作下，_scope_confirmed_recommendation 原样返回。"""
        scoping = self.dm.slot_handler.equipment_cascade.scoping
        extraction = {"slot_candidates": [{"raw_key": "k", "canonical_key": "k", "normalized_value": "v"}]}
        plan = make_plan("READ")
        res = scoping._scope_confirmed_recommendation(extraction, plan, "测试消息")
        self.assertEqual(res, extraction)


if __name__ == "__main__":
    unittest.main()
