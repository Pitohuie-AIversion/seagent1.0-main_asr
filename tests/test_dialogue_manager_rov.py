from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.llm_client import LLMClient


class DialogueManagerROVTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()
        cls.llm = MagicMock(spec=LLMClient)
        cls.llm.generate.return_value = "null"

    def test_apply_updates_with_rov_alias_observation(self):
        dm = DialogueManager(self.llm, self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.task_state["task_type"] = "管缆巡检"

        updates = {"equipment_name": "观察级"}
        dm._apply_updates(updates)

        self.assertEqual(dm.task_state.get("equipment_name"), "观察级深海机器人 HP")
        self.assertEqual(dm.task_state.get("equipment_type"), "观察级深海机器人 HP")

    def test_apply_updates_with_rov_alias_work(self):
        dm = DialogueManager(self.llm, self.kb)
        dm.task_state["task_type_key"] = "tree_valve_operation"
        dm.task_state["task_type"] = "采油树控制面板插入"

        updates = {"equipment_name": "工作级"}
        dm._apply_updates(updates)

        self.assertEqual(dm.task_state.get("equipment_name"), "通用工作级深海机器人 250HP")
        self.assertEqual(dm.task_state.get("equipment_type"), "通用工作级深海机器人 250HP")

    def test_apply_updates_with_rov_alias_tractor(self):
        dm = DialogueManager(self.llm, self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.task_state["task_type"] = "管缆巡检"

        updates = {"equipment_name": "金牛座"}
        dm._apply_updates(updates)

        self.assertEqual(dm.task_state.get("equipment_name"), "履带式海底重载作业机器人 1600HP")
        self.assertEqual(dm.task_state.get("equipment_type"), "履带式海底重载作业机器人 1600HP")


if __name__ == "__main__":
    unittest.main()
