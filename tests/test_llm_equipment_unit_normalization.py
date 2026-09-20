"""
tests/test_llm_equipment_unit_normalization.py

测试全字段大模型/规则吸附归一化功能：
1. 机器人编号（equipment_unit_id）：如“改2号级”、“2号级”、“改成2号机”精准对齐到 LROV-150-002；
2. 支持船只（support_vessel）：如“改用286号船”、“改成681”精准对齐到标准船名；
3. 管缆类型（cable_type）：如“改成油气管”、“改成光缆”精准对齐到标准管缆名称；
4. 油田区域（oilfield_name）：如“改成流花11-1”精准对齐到标准油田名称；
5. 验证 DialogueManager 与 Extractor 能够将全字段口语、缩写及错字吸附解析存入 SlotStore。
"""

import unittest
from unittest.mock import MagicMock

from src.dialogue_manager import DialogueManager
from src.extraction.extractor import ParameterExtractor
from src.knowledge_retriever import KnowledgeBase
from src.dispatch.output_builder import OutputBuilder


class TestLLMEquipmentUnitNormalization(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.builder = OutputBuilder(self.kb)

    def test_alias_matching_homophone_2_hao_ji(self):
        """验证 ParameterExtractor 匹配算法对同音错字 '2号级' / '改2号级' 的匹配。"""
        field_def = {
            "key": "equipment_unit_id",
            "label": "具体机器人编号",
            "allowed_values": ["LROV-150-001", "LROV-150-002"],
            "alias_mappings": {
                "LROV-150-001": "LROV-150-001",
                "LROV-150-002": "LROV-150-002",
                "1号机": "LROV-150-001",
                "2号机": "LROV-150-002",
                "天鹰座001": "LROV-150-001",
                "天鹰座002": "LROV-150-002",
            },
        }

        matched_1 = ParameterExtractor._match_alias_value("改2号级", field_def)
        self.assertEqual(matched_1, "LROV-150-002")

        matched_2 = ParameterExtractor._match_alias_value("2号级", field_def)
        self.assertEqual(matched_2, "LROV-150-002")

        matched_3 = ParameterExtractor._match_alias_value("改成2号机", field_def)
        self.assertEqual(matched_3, "LROV-150-002")

    def test_support_vessel_normalization(self):
        """验证支持船只（support_vessel）口语、简称剥离与吸附对齐。"""
        field_def = {
            "key": "support_vessel",
            "label": "支持船只",
            "allowed_values": ["海洋石油681", "海洋石油286", "DSV-Oceanic"],
            "alias_mappings": {
                "681": "海洋石油681",
                "681号船": "海洋石油681",
                "286": "海洋石油286",
                "286号船": "海洋石油286",
                "大洋": "DSV-Oceanic",
                "大洋号": "DSV-Oceanic",
            },
        }

        matched_1 = ParameterExtractor._match_alias_value("改用286号船", field_def)
        self.assertEqual(matched_1, "海洋石油286")

        matched_2 = ParameterExtractor._match_alias_value("把支持船改成大洋号", field_def)
        self.assertEqual(matched_2, "DSV-Oceanic")

        matched_3 = ParameterExtractor._match_allowed_value("把船只改成海洋石油681号船", field_def["allowed_values"])
        self.assertEqual(matched_3, "海洋石油681")

    def test_cable_type_normalization(self):
        """验证管缆类型（cable_type）口语简称吸附。"""
        field_def = {
            "key": "cable_type",
            "label": "管缆类型",
            "allowed_values": ["海底油气管道", "电力电缆", "光纤通信缆"],
            "alias_mappings": {
                "油气管道": "海底油气管道",
                "油气管": "海底油气管道",
                "输油管": "海底油气管道",
                "电缆": "电力电缆",
                "电力线": "电力电缆",
                "光纤": "光纤通信缆",
                "光缆": "光纤通信缆",
                "通信缆": "光纤通信缆",
            },
        }

        matched_1 = ParameterExtractor._match_alias_value("改成油气管", field_def)
        self.assertEqual(matched_1, "海底油气管道")

        matched_2 = ParameterExtractor._match_alias_value("改成光缆", field_def)
        self.assertEqual(matched_2, "光纤通信缆")

    def test_oilfield_name_normalization(self):
        """验证油田区域（oilfield_name）口语简称吸附。"""
        field_def = {
            "key": "oilfield_name",
            "label": "油田名称",
            "allowed_values": ["流花11-1油田", "陵水17-2油田", "荔湾3-1油田"],
            "alias_mappings": {
                "流花11-1": "流花11-1油田",
                "流花": "流花11-1油田",
                "陵水17-2": "陵水17-2油田",
                "陵水": "陵水17-2油田",
                "荔湾3-1": "荔湾3-1油田",
                "荔湾": "荔湾3-1油田",
            },
        }

        matched_1 = ParameterExtractor._match_alias_value("把作业点改成流花11-1", field_def)
        self.assertEqual(matched_1, "流花11-1油田")

        matched_2 = ParameterExtractor._match_allowed_value("去陵水17-2油田", field_def["allowed_values"])
        self.assertEqual(matched_2, "陵水17-2油田")

    def test_dialogue_manager_update_equipment_unit_with_homophone(self):
        """验证 DialogueManager 交易中处理 '改2号级' 时能够更新 SlotStore 并对齐状态。"""
        dm = DialogueManager()

        schema = dm.builder.get_schema("pipeline_inspection", "normal")
        dm.slot_store.init_task_slots(schema)
        slots, unresolved, version = dm.slot_store.snapshot()
        slots["task_type"].value = "管缆巡检"
        slots["task_type"].status = "valid"
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"
        slots["vessel_id" if "vessel_id" in slots else "support_vessel"].value = "海洋石油681"
        slots["vessel_id" if "vessel_id" in slots else "support_vessel"].status = "valid"

        # 设置初始状态为 LROV-150-001
        slots["equipment_family"].value = "轻型工作级深海机器人"
        slots["equipment_family"].status = "valid"
        slots["equipment_type"].value = "轻型工作级深海机器人 150HP"
        slots["equipment_type"].status = "valid"
        slots["equipment_unit_id"].value = "LROV-150-001"
        slots["equipment_unit_id"].status = "valid"

        dm.slot_store.commit_transaction(slots, unresolved, expected_version=version)
        dm.task_state = dm.slot_store.get_task_state()

        # 模拟事务更新设备为 '改2号级'
        dm._handle_equipment_updates_in_transaction(
            {"equipment_unit_id": "改2号级"},
            dm.slot_store.slots,
            allow_overwrite=True,
        )

        # 验证 SlotStore 中的 equipment_unit_id 已成功更新为 LROV-150-002
        updated_unit = dm.slot_store.slots["equipment_unit_id"].value
        self.assertEqual(updated_unit, "LROV-150-002")


if __name__ == "__main__":
    unittest.main()
