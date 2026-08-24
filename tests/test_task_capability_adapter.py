# -*- coding: utf-8 -*-
"""
TestTaskCapabilityAdapter Unit Tests

验证 TaskCapabilityAdapter 对任务模板能力要求及载荷配置引用的管理能力。
"""

import unittest
from src.task_capability_adapter import TaskCapabilityAdapter


class TestTaskCapabilityAdapter(unittest.TestCase):

    def setUp(self):
        self.task_schemas = {
            "task_templates": {
                "pipeline_inspection": {
                    "code": "PI",
                    "display_name": "管缆巡检",
                    "required_capabilities": ["inspection"],
                },
                "underwater_construction": {
                    "code": "UC",
                    "display_name": "水下施工辅助",
                    "required_capabilities": ["construction_manipulation"],
                },
            }
        }
        self.adapter = TaskCapabilityAdapter(self.task_schemas)

    def test_get_required_capabilities(self):
        caps = self.adapter.get_required_capabilities("pipeline_inspection")
        self.assertEqual(caps, ["inspection"])

        caps_const = self.adapter.get_required_capabilities("underwater_construction")
        self.assertEqual(caps_const, ["construction_manipulation"])

        caps_empty = self.adapter.get_required_capabilities("non_existent_task")
        self.assertEqual(caps_empty, [])

    def test_get_payload_options_ref(self):
        ref = self.adapter.get_payload_options_ref("pipeline_inspection")
        self.assertEqual(ref, "payload_options.pipeline_inspection")

    def test_is_payload_supported_for_task(self):
        payload_options = {
            "pipeline_inspection": ["高清水下摄像机", "TSS管缆跟踪传感器", "成像声呐"],
            "underwater_construction": ["液压剪切器", "水下螺栓扭矩扳手"],
        }
        self.assertTrue(
            self.adapter.is_payload_supported_for_task(
                "pipeline_inspection", "高清水下摄像机", payload_options
            )
        )
        self.assertFalse(
            self.adapter.is_payload_supported_for_task(
                "pipeline_inspection", "液压剪切器", payload_options
            )
        )


if __name__ == "__main__":
    unittest.main()
