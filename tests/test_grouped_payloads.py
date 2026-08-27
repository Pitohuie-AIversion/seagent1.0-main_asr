import unittest

from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.output_builder import OutputBuilder
from src.ui_state_builder import build_frontend_ui_state


class GroupedPayloadsTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()

    def test_robot_supported_payload_groups_are_preserved_and_flattened(self):
        robot = self.kb.get_rov("通用工作级深海机器人 250HP")

        self.assertIn("payload_groups", robot)
        self.assertIn("Mechanical_arm", robot["payload_groups"])
        self.assertIn("End_effector", robot["payload_groups"])
        self.assertIn("Multiple_load", robot["payload_groups"])
        self.assertIn("电液机械臂", robot["supported_payloads"])
        self.assertIn("腐蚀检测探头", robot["supported_payloads"])
        self.assertIn("三维视觉系统", robot["supported_payloads"])

    def test_payload_options_follow_flattened_group_order_for_selected_robot(self):
        builder = OutputBuilder(self.kb)
        values = builder.resolve_allowed_values(
            {"allowed_values_ref": "payload_options.tree_valve_operation"},
            "tree_valve_operation",
            {"equipment_type": "通用工作级深海机器人 250HP"},
        )

        self.assertIn("电液机械臂", values)
        self.assertIn("腐蚀检测探头", values)
        self.assertIn("三维视觉系统", values)
        self.assertLess(
            values.index("电液机械臂"),
            values.index("机械臂工具快换装置"),
        )

    def test_grouped_payloads_are_valid_robot_payload_requirements(self):
        domain = self.kb.get_feasible_robot_selection_domain(
            "tree_valve_operation",
            {
                "equipment_type": "通用工作级深海机器人 250HP",
                "payload": ["电液机械臂", "阀门扭矩工具", "三维视觉系统"],
            },
        )

        variant_names = [
            variant["full_name"]
            for robot_class in domain.get("classes", [])
            for family in robot_class.get("families", [])
            for variant in family.get("variants", [])
        ]
        self.assertIn("通用工作级深海机器人 250HP", variant_names)

    def test_payload_ui_state_includes_payload_groups_for_selected_robot(self):
        manager = DialogueManager(kb=self.kb)
        manager.task_state.update(
            {
                "task_type_key": "tree_valve_operation",
                "equipment_type": "通用工作级深海机器人 250HP",
            }
        )

        ui_state = build_frontend_ui_state(manager)
        payload_slot = next(slot for slot in ui_state["slots"] if slot["key"] == "payload")

        self.assertEqual(
            ["Mechanical_arm", "End_effector", "Multiple_load"],
            list(payload_slot["payload_groups"]),
        )
        self.assertEqual(["电液机械臂"], payload_slot["payload_groups"]["Mechanical_arm"])


if __name__ == "__main__":
    unittest.main()
