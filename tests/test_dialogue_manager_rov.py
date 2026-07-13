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

        self.assertEqual(dm.task_state.get("equipment_name"), "sealien_inspection")
        self.assertEqual(dm.task_state.get("equipment_type"), "观察级ROV")

    def test_apply_updates_with_rov_alias_work(self):
        dm = DialogueManager(self.llm, self.kb)
        dm.task_state["task_type_key"] = "tree_valve_operation"
        dm.task_state["task_type"] = "采油树控制面板插入"

        updates = {"equipment_name": "工作级"}
        dm._apply_updates(updates)

        self.assertEqual(dm.task_state.get("equipment_name"), "sealien_work_class")
        self.assertEqual(dm.task_state.get("equipment_type"), "工作级ROV")

    def test_apply_updates_with_rov_alias_tractor(self):
        dm = DialogueManager(self.llm, self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.task_state["task_type"] = "管缆巡检"

        updates = {"equipment_name": "金牛座"}
        dm._apply_updates(updates)

        self.assertEqual(dm.task_state.get("equipment_name"), "taurus_tractor")
        self.assertEqual(dm.task_state.get("equipment_type"), "海底拖拉机")


if __name__ == "__main__":
    unittest.main()
