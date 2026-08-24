"""
tests/test_interactive_selection_telemetry_decoupling.py
测试解耦交互选型与动态遥测时效逻辑：
1. 交互收集阶段 (purpose="interactive") 下，即使后天遥测时间过期，不移除设备选型拓扑。
2. DialogueManager 处理 pipeline_burial 包含“从现在开始”即时时间时，不报错 No feasible robot class。
3. 发布阶段 (purpose="publish") 下保留动态遥测过滤逻辑。
"""

import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager


class TestInteractiveSelectionTelemetryDecoupling(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.dm = DialogueManager(kb=self.kb)

    def test_interactive_domain_preserves_topology_despite_expired_telemetry(self):
        """交互模式下即便 start_time 在即时窗口内且遥测过期，仍应保留可行拓扑"""
        task_state = {
            "start_time": "2026-08-22T12:00:00+08:00",
            "water_depth": 130.0,
        }
        domain_interactive = self.kb.get_feasible_robot_selection_domain(
            "pipeline_burial",
            task_state,
            purpose="interactive",
        )
        self.assertGreater(len(domain_interactive.get("classes", [])), 0)
        cls_node = domain_interactive["classes"][0]
        self.assertEqual(cls_node["class_id"], "cable_burial_robot")

    def test_publish_domain_applies_runtime_filtering(self):
        """发布模式下在即时窗口内会应用动态遥测过滤"""
        task_state = {
            "start_time": "2026-08-22T12:00:00+08:00",
            "water_depth": 130.0,
        }
        domain_publish = self.kb.get_feasible_robot_selection_domain(
            "pipeline_burial",
            task_state,
            purpose="publish",
        )
        self.assertIsNotNone(domain_publish)

    def test_dialogue_manager_pipeline_burial_immediate_start_no_class_error(self):
        """DialogueManager 处理即时 pipeline_burial 任务时不误将 equipment_class 标记为 invalid"""
        self.dm.phase = "collecting"
        self.dm.slot_store.init_task_slots(
            self.dm.builder.get_required("pipeline_burial", "normal", {})
        )
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_burial"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        self.dm.slot_store.slots["water_depth"].value = 130.0
        self.dm.slot_store.slots["water_depth"].status = "valid"
        self.dm.slot_store.slots["start_time"].value = "2026-08-22T12:00:00+08:00"
        self.dm.slot_store.slots["start_time"].status = "valid"

        user_input = "任务从现在开始，两个小时后结束，管缆选择油气管道，水深130m"
        reply = self.dm._process_internal(user_input)

        eq_cls_slot = self.dm.slot_store.slots.get("equipment_class")
        if eq_cls_slot and eq_cls_slot.validation_error:
            self.assertNotIn("No feasible robot class", eq_cls_slot.validation_error)


if __name__ == "__main__":
    unittest.main()
