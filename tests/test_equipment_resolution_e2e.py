"""
tests/test_equipment_resolution_e2e.py — Equipment Resolution End-to-End Test

验证要求：
从自然语言设备口语别名（例如 "观察级一号机" 或 "天鹰座001"）出发，
经过系统级 Extractor -> alias resolution -> unit_id -> variant -> family -> validation，
验证 SlotStore 与 task_state 正确解析并填充各个字段，无任何手工 Slot 篡改行为。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase


class EquipmentResolutionE2ELLM:
    def __init__(self, alias_input="观察级一号机"):
        self.alias_input = alias_input

    def classify_interaction(self, messages, max_tokens=260):
        return {
            "interaction_type": "WRITE",
            "query_intent": None,
            "confidence": 0.99,
            "reason": "设备选择",
        }

    def chat(self, messages, temperature=0.7, max_tokens=1500):
        return "设备解析成功。"

    def filter_reply(self, text):
        return text

    def extract_json(self, messages, max_tokens=800):
        task_type_key = "pipeline_inspection"
        raw_task_type = "管缆巡检"
        if "通用工作级" in self.alias_input:
            task_type_key = "tree_valve_operation"
            raw_task_type = "采油树控制面板插入"
        elif "金牛座" in self.alias_input:
            task_type_key = "pipeline_burial"
            raw_task_type = "管缆埋设"

        return {
            "task_type_key": task_type_key,
            "slot_candidates": [
                {
                    "raw_key": "任务类型",
                    "canonical_key": "task_type_key",
                    "raw_value": raw_task_type,
                    "normalized_value": task_type_key,
                    "confidence": 0.99,
                },
                {
                    "raw_key": "设备名称",
                    "canonical_key": "equipment_name",
                    "raw_value": self.alias_input,
                    "normalized_value": self.alias_input,
                    "confidence": 0.99,
                },
                {
                    "raw_key": "作业水深",
                    "canonical_key": "water_depth",
                    "raw_value": "300米",
                    "normalized_value": 300.0,
                    "confidence": 0.99,
                },
            ],
            "unresolved": [],
        }


class EquipmentResolutionE2ETest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()

    def test_alias_to_unit_variant_family_e2e_flow(self):
        """1. 验证口语别名 '通用工作级一号机' 完整解析链"""
        llm = EquipmentResolutionE2ELLM("通用工作级一号机")
        dm = DialogueManager(llm, self.kb)

        reply = dm.process("执行采油树控制面板插入，配备通用工作级一号机，水深300米")

        # 验证单机 ID 映射正确
        self.assertEqual(dm.task_state.get("equipment_unit_id"), "WROV-250-001")
        # 验证设备展示名正确
        self.assertEqual(dm.task_state.get("equipment_name"), "通用工作级深海机器人250HP-001")
        # 验证型号全称映射正确
        self.assertEqual(dm.task_state.get("equipment_type"), "通用工作级深海机器人 250HP")
        # 验证设备族群全称映射正确
        self.assertEqual(dm.task_state.get("equipment_family"), "通用工作级深海机器人")
        # 验证 SSOT 一致性
        self.assertEqual(dm.task_state, dm.slot_store.get_task_state())

    def test_alias_tianying_to_unit_variant_e2e_flow(self):
        """2. 验证口语别名 '金牛座001' 完整解析链"""
        llm = EquipmentResolutionE2ELLM("金牛座001")
        dm = DialogueManager(llm, self.kb)

        reply = dm.process("执行管缆埋设作业，配备金牛座001，水深300米")

        # 验证单机 ID 映射正确
        self.assertEqual(dm.task_state.get("equipment_unit_id"), "CRAWLER-1600-001")
        # 验证型号全称映射正确
        self.assertIn("履带式海底重载作业机器人 1600HP", dm.task_state.get("equipment_type", ""))
        # 验证 SSOT 一致性
        self.assertEqual(dm.task_state, dm.slot_store.get_task_state())


if __name__ == "__main__":
    unittest.main()
