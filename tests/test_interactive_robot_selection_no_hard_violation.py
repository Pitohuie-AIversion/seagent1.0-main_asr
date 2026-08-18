"""
tests/test_interactive_robot_selection_no_hard_violation.py

验证：
1. 交互收集阶段（purpose="interactive"）下，已选上级机器人类别但尚未完成下级规格/单机选择时，不误报硬阻断违规；
2. DialogueManager 推荐接受协议中，用户输入别名（如“天鹰座”）能正确匹配上一轮助手回复中的别名/规范名，不产生来源不可靠告警并成功写入槽位。
"""

import unittest
from unittest.mock import MagicMock

from src.dialogue_manager import DialogueManager
from src.interaction_plan import InteractionPlan
from src.knowledge_retriever import KnowledgeBase
from src.validator import TaskValidator


class TestInteractiveRobotSelectionNoHardViolation(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.validator = TaskValidator(self.kb)

    def test_interactive_mode_does_not_hard_block_on_partial_candidate_unresolved(self):
        """交互收集模式下，选定类别但尚未确定型号时不产生阻断性 validation_error。"""
        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_class": "observation_rov",
        }
        res = self.validator.validate_task(task_state, purpose="interactive")
        self.assertNotEqual(res.overall_status, "validation_error")
        self.assertNotEqual(res.overall_status, "blocked_hard")

    def test_recommendation_provenance_alias_matching(self):
        """助手推荐“天鹰座”时，用户回复“使用天鹰座”，别名能正确比对过关并正常锁定推荐槽位。"""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = "好的，已记录。"
        mock_llm.filter_reply.side_effect = lambda text, *args, **kwargs: text

        dm = DialogueManager(llm=mock_llm, kb=self.kb)
        dm.task_state = {
            "task_type": "管缆巡检",
            "task_type_key": "pipeline_inspection",
        }
        dm.conversation_history = [
            {"role": "user", "content": "帮我推荐一个适合管缆巡检的机器人类别"},
            {"role": "assistant", "content": "推荐使用天鹰座 (轻型工作级深海机器人 150HP)，适合水下巡检任务。"},
        ]

        from tests.interaction_plan_support import make_plan

        plan_dict = make_plan(
            "WRITE",
            subject_type="device",
            subject_text="轻型工作级深海机器人 150HP",
            relation="recommend",
            confidence=1.0,
        )
        plan = InteractionPlan(**plan_dict)

        extraction_result = {
            "slot_candidates": [
                {
                    "raw_key": "作业设备型号",
                    "canonical_key": "equipment_type",
                    "raw_value": "天鹰座",
                    "normalized_value": "轻型工作级深海机器人 150HP",
                    "confidence": 1.0,
                    "resolution_method": "alias_lookup",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        res = dm._scope_confirmed_recommendation(extraction_result, plan, "使用天鹰座")
        self.assertEqual(res.get("unresolved"), [])
        canonical_keys = [c.get("canonical_key") for c in res.get("slot_candidates", [])]
        self.assertIn("equipment_type", canonical_keys)

    def test_class_not_allowed_stays_collecting_in_interactive_mode(self):
        """采油树阀门插拔任务误填 AUV (CLASS_NOT_ALLOWED_FOR_TASK) 时，DialogueManager 保持在 collecting 状态进行选型引导。"""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = "采油树阀门插拔任务需要配备机械臂与液压工具的工作级ROV，AUV不适用。请选择工作级ROV。"
        mock_llm.filter_reply.side_effect = lambda text, *args, **kwargs: text

        dm = DialogueManager(llm=mock_llm, kb=self.kb)
        dm.task_state = {
            "task_type": "采油树阀门插拔",
            "task_type_key": "tree_valve_operation",
            "equipment_class": "auv",
        }
        ctx = dm._run_constraint_check({"equipment_class"}, purpose="interactive")
        self.assertEqual(dm.phase, "collecting")
        self.assertNotEqual(dm.phase, "blocked_hard")

    def test_family_mismatch_stays_collecting_in_interactive_mode(self):
        """类别与系列冲突 (FAMILY_CLASS_MISMATCH) 时，DialogueManager 保持在 collecting 状态进行引导。"""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = "所选系列与类别不匹配，请重新确认。"
        mock_llm.filter_reply.side_effect = lambda text, *args, **kwargs: text

        dm = DialogueManager(llm=mock_llm, kb=self.kb)
        dm.task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_class": "auv",
            "equipment_family": "light_work_class_rov",
        }
        ctx = dm._run_constraint_check({"equipment_class", "equipment_family"}, purpose="interactive")
        self.assertEqual(dm.phase, "collecting")
        self.assertNotEqual(dm.phase, "blocked_hard")


if __name__ == "__main__":
    unittest.main()
