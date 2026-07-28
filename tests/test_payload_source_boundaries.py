import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge_retriever import KnowledgeBase
from src.output_builder import OutputBuilder


class PayloadSourceBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.builder = OutputBuilder(self.kb)

    def test_write_context_does_not_inject_task_payload_options(self):
        context = self.kb.get_context_for_state({
            "task_type_key": "pipeline_inspection",
            "equipment_type": "轻型工作级深海机器人",
        })

        self.assertNotIn("常用携带工具建议", context)
        self.assertNotIn("INS惯性导航系统", context)
        self.assertNotIn("DVL多普勒测速仪", context)

    def test_missing_payload_choices_use_selected_robot_supported_payloads_only(self):
        robot = self.kb.get_rov_for_task("轻型工作级深海机器人", "pipeline_inspection")
        state = {
            "task_type_key": "pipeline_inspection",
            "task_type": "管缆巡检",
            "equipment_type": robot["full_name"],
        }

        built, missing = self.builder.build(state, "pipeline_inspection")
        payload_missing = next(item for item in missing if item["key"] == "payload")

        self.assertEqual(payload_missing["allowed_values"], robot["supported_payloads"])
        self.assertIn("激光标尺", payload_missing["allowed_values"])
        self.assertNotIn("INS惯性导航系统", payload_missing["allowed_values"])
        self.assertNotIn("DVL多普勒测速仪", payload_missing["allowed_values"])

    def test_output_builder_does_not_treat_payload_options_as_legal_payload_ref(self):
        allowed = self.builder.resolve_allowed_values(
            {
                "key": "payload",
                "label": "携带工具",
                "type": "list",
                "allowed_values_ref": "payload_options.pipeline_inspection",
            },
            "pipeline_inspection",
            {"equipment_type": "轻型工作级深海机器人"},
        )

        self.assertEqual(allowed, [])

    def test_tool_query_task_tools_still_uses_assets_payload_options(self):
        evidence = self.kb.execute_typed_query(
            "TOOL_QUERY",
            "管缆巡检任务支持什么工具？",
            {"task_type_key": "pipeline_inspection"},
        )

        self.assertEqual(evidence["results"][0]["category"], "task_payload_suggestions")
        self.assertEqual(
            evidence["results"][0]["current_task_suggestions"],
            self.kb.assets["payload_options"]["pipeline_inspection"],
        )


if __name__ == "__main__":
    unittest.main()
