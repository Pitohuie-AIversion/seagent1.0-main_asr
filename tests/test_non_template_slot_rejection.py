import unittest
from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import Slot
from tests.interaction_plan_support import ScriptedLLM, make_plan, empty_extraction


class TestNonTemplateSlotRejection(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()

    def test_pipeline_inspection_rejects_oilfield_and_guides_to_coordinates(self):
        """测试：管缆巡检任务（未包含油田槽位）在收到油田名称回答时应拒绝且不进行坐标映射，并引导提供具体坐标。"""
        # 模型返回 WRITE 操作与提取出的 oilfield_name 候选
        llm = ScriptedLLM(
            plans=[make_plan("WRITE")],
            extractions=[
                {
                    "slot_candidates": [
                        {
                            "canonical_key": "oilfield_name",
                            "raw_value": "流花11-1油田",
                            "normalized_value": "流花11-1油田",
                            "confidence": 1.0,
                        }
                    ],
                    "unresolved": [],
                }
            ],
        )
        dm = DialogueManager(llm, self.kb)

        # 设置任务为管缆巡检
        dm.slot_store.slots["task_type_key"] = Slot(
            "task_type_key", value="pipeline_inspection", status="valid"
        )
        dm._transition_phase("collecting", reason="unit_test")

        # 用户回答油田名称（如：油田选择流花11-1油田）
        reply = dm.process("油田选择流花11-1油田")

        # 验证：1. oilfield_name 槽位未写入或不属于有效状态
        oilfield_slot = dm.slot_store.slots.get("oilfield_name")
        self.assertTrue(
            oilfield_slot is None
            or oilfield_slot.status != "valid"
            or oilfield_slot.value is None
        )

        # 验证：2. 没有将油田默认坐标注入为管缆巡检坐标 (start_point / end_point)
        start_point_slot = dm.slot_store.slots.get("start_point")
        self.assertTrue(
            start_point_slot is None
            or start_point_slot.status != "valid"
            or start_point_slot.value is None
        )

        # 验证：3. 回复中包含明确拒绝油田映射并引导明确坐标的信息
        self.assertIn("未包含油田槽位", reply)
        self.assertIn("无法通过油田", reply)
        self.assertIn("坐标", reply)

    def test_tree_valve_operation_accepts_oilfield(self):
        """测试：采油树阀门插拔任务（包含油田槽位）正常接受并映射油田。"""
        llm = ScriptedLLM(
            plans=[make_plan("WRITE")],
            extractions=[
                {
                    "slot_candidates": [
                        {
                            "canonical_key": "oilfield_name",
                            "raw_value": "流花11-1油田",
                            "normalized_value": "流花11-1油田",
                            "confidence": 1.0,
                        }
                    ],
                    "unresolved": [],
                }
            ],
        )
        dm = DialogueManager(llm, self.kb)

        # 设置任务为采油树控制面板插入
        dm.slot_store.slots["task_type_key"] = Slot(
            "task_type_key", value="tree_valve_operation", status="valid"
        )
        dm._transition_phase("collecting", reason="unit_test")

        # 用户回答油田名称
        reply = dm.process("油田选择流花11-1油田")

        # 验证：采油树任务中 oilfield_name 正常写入且映射油田坐标
        oilfield_slot = dm.slot_store.slots.get("oilfield_name")
        self.assertIsNotNone(oilfield_slot)
        self.assertEqual(oilfield_slot.status, "valid")
        self.assertEqual(oilfield_slot.value, "流花11-1油田")

        coord_slot = dm.slot_store.slots.get("oilfield_coordinates")
        self.assertIsNotNone(coord_slot)
        self.assertEqual(coord_slot.status, "valid")


if __name__ == "__main__":
    unittest.main()
