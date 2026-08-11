import unittest
from src.llm_client import LLMClient
from src.extractor import ParameterExtractor


class TestEquipmentTypeExtraction(unittest.TestCase):
    def setUp(self):
        self.mock_llm = LLMClient(llm_instance=None, tokenizer=None)
        self.extractor = ParameterExtractor(self.mock_llm)
        self.required = [
            {
                "key": "equipment_type",
                "label": "作业设备类型",
                "type": "string",
                "allowed_values": ["观察级ROV", "工作级ROV", "海底拖拉机", "调查型AUV"],
            }
        ]

    def test_extract_observation_rov(self):
        res = self.extractor.extract_updates(
            user_message="使用观察级rov",
            conversation_history=[],
            current_state={"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"},
            task_type_key="pipeline_inspection",
            required=self.required,
        )
        self.assertEqual(res.get("equipment_type"), "观察级ROV")

    def test_extract_observation_rov_with_space(self):
        res = self.extractor.extract_updates(
            user_message="观察级 ROV",
            conversation_history=[],
            current_state={"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"},
            task_type_key="pipeline_inspection",
            required=self.required,
        )
        self.assertEqual(res.get("equipment_type"), "观察级ROV")

    def test_extract_work_class_rov(self):
        res = self.extractor.extract_updates(
            user_message="工作级 ROV",
            conversation_history=[],
            current_state={"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"},
            task_type_key="pipeline_inspection",
            required=self.required,
        )
        self.assertEqual(res.get("equipment_type"), "工作级ROV")

    def test_extract_auv(self):
        res = self.extractor.extract_updates(
            user_message="使用AUV进行巡检",
            conversation_history=[],
            current_state={"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"},
            task_type_key="pipeline_inspection",
            required=self.required,
        )
        self.assertEqual(res.get("equipment_type"), "调查型AUV")

    def test_pipeline_inspection_auv_constraint(self):
        from src.knowledge_retriever import KnowledgeBase
        from src.validator import TaskValidator
        kb = KnowledgeBase()
        constraint_str = kb._task_rov_constraint("pipeline_inspection")
        self.assertIn("观察级ROV 或 调查型AUV", constraint_str)

        validator = TaskValidator(kb)
        violations = validator.validate({
            "task_type_key": "pipeline_inspection",
            "equipment_name": "sealien_inspection_auv"
        })
        self.assertFalse(validator.has_hard_violations(violations))


if __name__ == "__main__":
    unittest.main()
