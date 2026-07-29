import unittest
from unittest.mock import MagicMock

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouteResult
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.task_request_guard import analyze_task_request


class TaskRequestGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()
        cls.task_types = cls.kb.get_all_task_type_values()

    def test_multiple_supported_tasks_require_selection(self):
        analysis = analyze_task_request(
            "我要安排管缆巡检，另外还要做个采油树控制面板插入，优先级 7",
            self.task_types,
        )

        self.assertTrue(analysis.should_block)
        self.assertEqual(
            analysis.detected_task_types,
            ("管缆巡检", "采油树控制面板插入"),
        )
        self.assertFalse(analysis.unsupported_clauses)
        self.assertIn("选择", analysis.build_reply())

    def test_mixed_supported_and_unsupported_tasks_rejects_unsupported_clause(self):
        analysis = analyze_task_request(
            "帮我在流花油田做个管缆巡检，再帮我去楼下买杯咖啡，"
            "另外再安排一个采油树插入到A03井，优先级 7",
            self.task_types,
        )

        self.assertTrue(analysis.should_block)
        self.assertEqual(
            analysis.detected_task_types,
            ("管缆巡检", "采油树控制面板插入"),
        )
        self.assertTrue(any("咖啡" in clause for clause in analysis.unsupported_clauses))
        reply = analysis.build_reply()
        self.assertIn("不支持", reply)
        self.assertIn("咖啡", reply)
        self.assertIn("选择", reply)

    def test_repeated_alias_for_same_task_does_not_block(self):
        analysis = analyze_task_request(
            "安排采油树控制面板插入，采油树插入位置在A03井",
            self.task_types,
        )

        self.assertFalse(analysis.should_block)
        self.assertEqual(
            analysis.detected_task_types,
            ("采油树控制面板插入",),
        )

    def test_explicit_task_type_replacement_does_not_block(self):
        analysis = analyze_task_request(
            "把管缆巡检改成管缆埋设",
            self.task_types,
        )

        self.assertFalse(analysis.should_block)
        self.assertTrue(analysis.is_explicit_replacement)

    def test_parameter_clause_is_not_classified_as_unsupported_task(self):
        analysis = analyze_task_request(
            "创建管缆巡检，另外支持船使用海洋石油681，还要携带前视声呐",
            self.task_types,
        )

        self.assertFalse(analysis.should_block)
        self.assertFalse(analysis.unsupported_clauses)

    def test_dialogue_manager_blocks_before_extractor_and_state_mutation(self):
        dm = DialogueManager(LLMClient(None, None), self.kb)
        dm.intent_router.route = MagicMock(
            return_value=IntentRouteResult(
                "WRITE",
                1.0,
                "用户提交任务",
                None,
            )
        )
        dm.extractor.extract_updates = MagicMock(
            side_effect=AssertionError("extractor must not run for compound task request")
        )
        before_version = dm.slot_store.version
        before_state = dm.slot_store.get_task_state()
        before_phase = dm.phase
        before_final = dm.final_result

        reply = dm.process(
            "我要安排管缆巡检，另外还要做个采油树控制面板插入，优先级 7",
            request_id="compound_task_guard",
        )

        self.assertIn("选择", reply)
        dm.extractor.extract_updates.assert_not_called()
        self.assertEqual(dm.slot_store.version, before_version)
        self.assertEqual(dm.slot_store.get_task_state(), before_state)
        self.assertEqual(dm.phase, before_phase)
        self.assertEqual(dm.final_result, before_final)

    def test_dialogue_manager_query_comparison_bypasses_write_guard(self):
        dm = DialogueManager(LLMClient(None, None), self.kb)
        route = IntentRouteResult(
            "QUERY",
            1.0,
            "用户比较任务能力",
            "KNOWLEDGE_QA",
        )
        dm.intent_router.route = MagicMock(return_value=route)
        dm._handle_non_task_route = MagicMock(return_value="比较结果")
        before_state = dm.slot_store.get_task_state()

        reply = dm.process(
            "比较管缆巡检和管缆埋设的区别",
            request_id="compound_task_query",
        )

        self.assertEqual(reply, "比较结果")
        dm._handle_non_task_route.assert_called_once_with(
            "比较管缆巡检和管缆埋设的区别",
            route,
            "compound_task_query",
        )
        self.assertEqual(dm.slot_store.get_task_state(), before_state)


if __name__ == "__main__":
    unittest.main()
