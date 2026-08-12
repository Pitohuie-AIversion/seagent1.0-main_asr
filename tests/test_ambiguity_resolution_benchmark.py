"""
tests/test_ambiguity_resolution_benchmark.py — 歧义消解 Benchmark 测试

验证目标：
1. 保留知识库多机器人别名歧义索引契约。
2. 油田简写与完整名称通过两轮显式 WRITE 和实体链接写入标准状态。
3. 未解析工具必须记录 unresolved，并只递增事务版本、不伪造业务槽位。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import (
    ScriptedLLM,
    empty_extraction,
    extraction_result,
    make_plan,
    slot_candidate,
)


class AmbiguityResolutionBenchmarkTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()

    def test_ambiguous_device_alias_index_detection(self):
        """知识库仍须暴露多设备共用别名，供上层澄清。"""
        ambiguous_terms = self.kb.get_ambiguous_device_terms()
        self.assertIn("一号机", ambiguous_terms)

    def test_incomplete_oilfield_name_disambiguation_flow(self):
        """两轮 WRITE 分别链接简写与完整油田名，不依赖文本分支 Fake。"""
        llm = ScriptedLLM(
            plans=[make_plan("WRITE"), make_plan("WRITE")],
            extractions=[
                extraction_result(
                    slot_candidate(
                        "task_type_key",
                        "pipeline_inspection",
                        raw_key="任务类型",
                        raw_value="管缆巡检",
                    )
                ),
                extraction_result(
                    slot_candidate(
                        "raw_oilfield_name",
                        "17-2",
                        raw_key="目标油田",
                        confidence=0.80,
                    )
                ),
                extraction_result(
                    slot_candidate(
                        "raw_oilfield_name",
                        "陵水17-2油田",
                        raw_key="目标油田",
                    )
                ),
            ],
            default_reply="请继续补充任务信息。",
        )
        dm = DialogueManager(llm, self.kb)

        version_before = dm.slot_store.version
        dm.process("执行17-2油田管缆巡检")
        task_state1 = dict(dm.task_state)

        self.assertGreater(dm.slot_store.version, version_before)
        self.assertEqual(task_state1.get("oilfield_match_status"), "accepted")
        self.assertEqual(task_state1.get("raw_oilfield_name"), "17-2")
        self.assertEqual(task_state1.get("oilfield_entity_id"), "lingshui_17_2")
        self.assertIsNone(task_state1.get("pending_oilfield_name"))
        raw_slot1 = dm.slot_store.slots.get("raw_oilfield_name")
        self.assertIsNotNone(raw_slot1)
        self.assertEqual(raw_slot1.status, "valid")
        self.assertEqual(raw_slot1.value, "17-2")

        version_after_short_name = dm.slot_store.version
        dm.process("确认选择陵水17-2油田")
        task_state2 = dict(dm.task_state)

        self.assertGreater(dm.slot_store.version, version_after_short_name)
        self.assertEqual(task_state2.get("oilfield_match_status"), "accepted")
        self.assertEqual(task_state2.get("raw_oilfield_name"), "陵水17-2油田")
        self.assertEqual(task_state2.get("oilfield_entity_id"), "lingshui_17_2")
        self.assertIsNone(task_state2.get("pending_oilfield_name"))
        raw_slot2 = dm.slot_store.slots.get("raw_oilfield_name")
        self.assertIsNotNone(raw_slot2)
        self.assertEqual(raw_slot2.status, "valid")
        self.assertEqual(raw_slot2.value, "陵水17-2油田")
        self.assertEqual(dm.task_state, dm.slot_store.get_task_state())

        self.assertEqual(len(llm.classify_calls), 2)
        self.assertEqual(len(llm.extract_calls), 3)
        self.assertFalse(llm.plans)
        self.assertFalse(llm.extractions)

    def test_unresolved_tool_extraction(self):
        """未知工具记入 unresolved，但不得伪造成已接受的 payload。"""
        unresolved = "未能在知识库中匹配工具: 不明挂件"
        llm = ScriptedLLM(
            plans=[make_plan("WRITE")],
            extractions=[
                extraction_result(
                    slot_candidate(
                        "task_type_key",
                        "pipeline_burial",
                        raw_key="任务类型",
                        raw_value="管缆埋设",
                    )
                ),
                empty_extraction(unresolved=[unresolved]),
            ],
            default_reply="请澄清详细信息。",
        )
        dm = DialogueManager(llm, self.kb)

        version_before = dm.slot_store.version
        dm.process("执行管缆埋设，使用金牛座一号机并携带不明挂件")

        self.assertIn(unresolved, dm.slot_store.unresolved)
        self.assertEqual(dm.slot_store.unresolved.count(unresolved), 1)
        self.assertEqual(dm.slot_store.version, version_before + 1)
        self.assertEqual(dm.task_state, dm.slot_store.get_task_state())
        self.assertEqual(dm.task_state.get("task_type_key"), "pipeline_burial")
        task_type_slot = dm.slot_store.slots.get("task_type_key")
        self.assertIsNotNone(task_type_slot)
        self.assertEqual(task_type_slot.status, "valid")
        self.assertEqual(task_type_slot.version, 1)
        self.assertNotIn("payload", dm.task_state)
        self.assertNotIn("不明挂件", str(dm.task_state))

        self.assertEqual(len(llm.classify_calls), 1)
        self.assertEqual(len(llm.extract_calls), 2)
        self.assertFalse(llm.plans)
        self.assertFalse(llm.extractions)


if __name__ == "__main__":
    unittest.main()
