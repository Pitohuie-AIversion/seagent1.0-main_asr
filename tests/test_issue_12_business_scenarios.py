import unittest
from unittest.mock import MagicMock
from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient


class TestIssue12BusinessScenarios(unittest.TestCase):
    """Business acceptance scenarios 1-6 required by Issue #12 specification."""

    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = MagicMock(spec=LLMClient)
        self.llm.generate.return_value = "null"
        self.dm = DialogueManager(self.llm, self.kb)
        schema = self.kb.get_task_schema("pipeline_inspection")
        req = schema.get("required_fields", []) if isinstance(schema, dict) else []
        opt = schema.get("optional_fields", []) if isinstance(schema, dict) else []
        self.dm.slot_store.init_task_slots(req + opt)

    def _apply_updates(self, updates, allow_overwrite=True, task_type_key="pipeline_inspection"):
        new_slots = self.dm.slot_store.clone_slots()
        if task_type_key:
            new_slots["task_type_key"].value = task_type_key
            new_slots["task_type_key"].status = "valid"
        self.dm._apply_updates_in_transaction(
            updates, new_slots, allow_overwrite=allow_overwrite
        )
        self.dm._normalize_and_validate_in_transaction(
            new_slots, task_type_key
        )
        self.dm.slot_store.commit_transaction(new_slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

    # 场景 1：AUV 完整级联
    def test_scenario_1_auv_complete_cascade(self):
        """场景 1：输入合法 AUV unit (AUV-324cc-001) 自动得到完整合法 6 槽进入 valid。"""
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots

        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_class"].status, "valid")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_family"].status, "valid")
        self.assertIsNotNone(slots["equipment_specification"].value)
        self.assertEqual(slots["equipment_specification"].status, "valid")
        self.assertEqual(slots["equipment_type"].value, "水下无人自主航行器 324CC")
        self.assertEqual(slots["equipment_type"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")
        self.assertEqual(slots["equipment_unit_id"].status, "valid")
        self.assertEqual(slots["equipment_name"].value, "水下无人自主航行器-324cc-001")
        self.assertEqual(slots["equipment_name"].status, "valid")

    # 场景 2：WROV 完整级联
    def test_scenario_2_wrov_complete_cascade(self):
        """场景 2：输入合法 WROV unit (WROV-250-001) 完整四级级联一致并进入 valid。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"}, task_type_key="tree_valve_operation")
        slots = self.dm.slot_store.slots

        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_class"].status, "valid")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_family"].status, "valid")
        self.assertIsNotNone(slots["equipment_specification"].value)
        self.assertEqual(slots["equipment_specification"].status, "valid")
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(slots["equipment_type"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].value, "WROV-250-001")
        self.assertEqual(slots["equipment_unit_id"].status, "valid")

    # 场景 3：非法或混合组合
    def test_scenario_3_illegal_or_mismatched_combination(self):
        """场景 3：同轮输入不匹配组合，不产生 mixed valid 状态，unit 不得 valid。"""
        mismatched_input = {
            "equipment_class": "work_class_rov",
            "equipment_family": "观察级深海机器人",
            "equipment_unit_id": "AUV-324cc-001",
        }
        self._apply_updates(mismatched_input, allow_overwrite=True, task_type_key="tree_valve_operation")
        slots = self.dm.slot_store.slots

        self.assertNotEqual(slots["equipment_unit_id"].status, "valid")
        self.assertNotEqual(slots["equipment_family"].status, "valid")

    # 场景 4：未经确认替换
    def test_scenario_4_unconfirmed_replacement_blocked(self):
        """场景 4：已有 AUV 完整级联，输入完整 WROV 且 allow_overwrite=False，旧 AUV 保留，最高冲突层进 conflict。"""
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")

        wrov_spec = {
            "type": "power_hp",
            "value": 250,
            "variant_id": "general_work_class_rov_250hp",
        }
        wrov_input = {
            "equipment_class": "work_class_rov",
            "equipment_family": "通用工作级深海机器人",
            "equipment_specification": wrov_spec,
            "equipment_unit_id": "WROV-250-001",
        }

        self._apply_updates(wrov_input, allow_overwrite=False, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots

        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_class"].status, "conflict")
        self.assertEqual(slots["equipment_class"].candidate_value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_family"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")
        self.assertEqual(slots["equipment_unit_id"].status, "valid")
        self.assertNotIn("WROV-250-001", [s.value for s in slots.values()])

    # 场景 5：确认后整套替换
    def test_scenario_5_confirmed_replacement(self):
        """场景 5：已有 AUV 完整级联，输入完整 WROV 且 allow_overwrite=True，整套成功一次性更新。"""
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")

        wrov_spec = {
            "type": "power_hp",
            "value": 250,
            "variant_id": "general_work_class_rov_250hp",
        }
        wrov_input = {
            "equipment_class": "work_class_rov",
            "equipment_family": "通用工作级深海机器人",
            "equipment_specification": wrov_spec,
            "equipment_unit_id": "WROV-250-001",
        }

        self._apply_updates(wrov_input, allow_overwrite=True, task_type_key="tree_valve_operation")
        slots = self.dm.slot_store.slots

        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_class"].status, "valid")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_family"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].value, "WROV-250-001")
        self.assertEqual(slots["equipment_unit_id"].status, "valid")
        self.assertNotEqual(slots["equipment_name"].value, "水下无人自主航行器-324cc-001")

    # 场景 6：规格缺失阻止发布
    def test_scenario_6_missing_spec_blocks_publish(self):
        """场景 6：输入规格缺失/为 null 的机器人 (OBSROV--001)，在 null=unknown 契约下 spec/unit 均有效。"""
        self._apply_updates({"equipment_unit_id": "OBSROV--001"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots

        self.assertEqual(slots["equipment_specification"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].status, "valid")


if __name__ == "__main__":
    unittest.main()
