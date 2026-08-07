import unittest
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase


class FakeLLM:
    def extract_json(self, messages, max_tokens=800):
        return {"slot_candidates": [], "unresolved": []}

    def chat(self, messages, **kwargs):
        return "收到"

    def filter_reply(self, reply):
        return reply


class OilfieldContextFailClosedTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = FakeLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_evaluate_context_exception_fails_closed_and_blocks_publish(self):
        """测试 evaluate_context 发生异常时系统必须 fail closed 进入 blocked_hard，阻断任务发布。"""
        self.dm.task_state["oilfield_entity_id"] = "lingshui_17_2"
        self.dm.task_state["oilfield_name"] = "陵水17-2气田"
        self.dm.task_state["start_point"] = {"lat": 17.5, "lon": 110.1}
        self.dm.phase = "collecting"

        with patch.object(self.dm.oilfield_linker, "evaluate_context", side_effect=RuntimeError("模拟油田上下文计算崩溃")):
            self.dm._run_constraint_check({"start_point"})

            # 相较于静默跳过，必须强制进入 blocked_hard 状态
            self.assertEqual(self.dm.phase, "blocked_hard")
            violations = self.dm._blocking_violations
            self.assertTrue(len(violations) > 0)
            self.assertEqual(violations[0].severity, "hard")
            self.assertEqual(violations[0].constraint_id, "C029")

            # 尝试确认发布必须被阻断
            res = self.dm.process("确认发布")
            self.assertNotEqual(self.dm.phase, "done")


if __name__ == "__main__":
    unittest.main()
