import unittest
from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.slot_store import Slot

class TestFamilyToClassAutoPromotion(unittest.TestCase):
    def setUp(self):
        self.dm = DialogueManager()
        self.dm.task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_family": "轻型工作级深海机器人"
        }

    def test_family_automatically_promotes_class(self):
        # 初始状态下 equipment_class 未填
        self.assertNotIn("equipment_class", self.dm.task_state)
        
        # 触发层级收敛与补全
        new_slots = {k: slot.copy() for k, slot in self.dm.slot_store.slots.items()}
        new_slots["equipment_family"] = Slot("equipment_family")
        new_slots["equipment_family"].value = "轻型工作级深海机器人"
        new_slots["equipment_family"].status = "valid"
        new_slots["task_type_key"] = Slot("task_type_key")
        new_slots["task_type_key"].value = "pipeline_inspection"
        new_slots["task_type_key"].status = "valid"
        self.dm._auto_collapse_robot_cascade(new_slots)
        
        # 校验：equipment_class 应当被自动推导补全为 observation_rov 大类
        cls_slot = new_slots.get("equipment_class")
        self.assertIsNotNone(cls_slot, "equipment_class 应当被自动创建")
        self.assertEqual(cls_slot.status, "valid", "equipment_class 应当被设为 valid")
        self.assertEqual(cls_slot.value, "observation_rov", "equipment_class 应当正确推导为 observation_rov")

if __name__ == "__main__":
    unittest.main()
