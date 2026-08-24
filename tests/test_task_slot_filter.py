# -*- coding: utf-8 -*-
"""
TestTaskSlotFilter Unit Tests

验证 TaskSlotFilter 对按任务 Schema 限定槽位、非模板槽位拒绝及坐标引导的正确性。
"""

import unittest
from src.task_slot_filter import TaskSlotFilter


class TestTaskSlotFilter(unittest.TestCase):

    def setUp(self):
        self.task_schemas = {
            "task_templates": {
                "pipeline_inspection": {
                    "display_name": "管缆巡检",
                    "required_capabilities": ["inspection"],
                },
                "tree_valve_operation": {
                    "display_name": "水面采油树阀门操作",
                    "required_capabilities": ["valve_manipulation"],
                },
            }
        }
        self.filter = TaskSlotFilter(self.task_schemas)

    def test_supports_oilfield_slots(self):
        self.assertTrue(self.filter.supports_oilfield_slots({"oilfield_name", "start_point"}))
        self.assertTrue(self.filter.supports_oilfield_slots({"oilfield_coordinates", "water_depth"}))
        self.assertFalse(self.filter.supports_oilfield_slots({"start_point", "end_point", "water_depth"}))

    def test_filter_candidates_accepts_valid_schema_keys(self):
        effective_keys = {"start_point", "end_point", "water_depth", "cable_type"}
        candidates = [
            {"canonical_key": "water_depth", "normalized_value": 300, "raw_value": "300米"},
            {"canonical_key": "cable_type", "normalized_value": "海底油气管道", "raw_value": "海底油气管道"},
        ]
        projected, unresolved = self.filter.filter_candidates(
            task_type_key="pipeline_inspection",
            effective_schema_keys=effective_keys,
            candidates=candidates,
        )
        self.assertEqual(len(projected), 2)
        self.assertEqual(len(unresolved), 0)

    def test_filter_candidates_rejects_oilfield_for_pipeline_inspection(self):
        effective_keys = {"start_point", "end_point", "water_depth", "cable_type"}
        candidates = [
            {"canonical_key": "oilfield_name", "normalized_value": "流花11-1油田", "raw_value": "流花11-1油田"},
        ]
        projected, unresolved = self.filter.filter_candidates(
            task_type_key="pipeline_inspection",
            effective_schema_keys=effective_keys,
            candidates=candidates,
        )
        self.assertEqual(len(projected), 0)
        self.assertEqual(len(unresolved), 1)
        self.assertIn("未包含油田槽位", unresolved[0])
        self.assertIn("无法通过油田名称“流花11-1油田”进行坐标映射", unresolved[0])

    def test_filter_candidates_allows_oilfield_when_supported(self):
        effective_keys = {"oilfield_name", "tree_valve_id", "water_depth"}
        candidates = [
            {"canonical_key": "oilfield_name", "normalized_value": "流花11-1油田", "raw_value": "流花11-1油田"},
        ]
        projected, unresolved = self.filter.filter_candidates(
            task_type_key="tree_valve_operation",
            effective_schema_keys=effective_keys,
            candidates=candidates,
        )
        self.assertEqual(len(projected), 1)
        self.assertEqual(len(unresolved), 0)


if __name__ == "__main__":
    unittest.main()
