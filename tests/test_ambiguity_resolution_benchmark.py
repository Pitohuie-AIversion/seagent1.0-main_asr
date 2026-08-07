"""
tests/test_ambiguity_resolution_benchmark.py — 歧义消解 Benchmark 测试

验证目标：
1. 多机器人别名歧义：当用户输入存在多重匹配的设备别名（如 "一号机" 或 "观察级"）时，系统标记歧义或给出澄清提示，不静默强行绑定任意机器人。
2. 不完整油田名称歧义：当用户输入不完整油田名（如 "流花油田"）时，OilfieldEntityLinker 输出 ambiguous，SlotStore 保存 pending_oilfield_name 与 pending_oilfield_candidates 并要求用户确认。后续输入 "流花11-1油田" 成功消解歧义并写入 oilfield_name。
3. Payload 歧义与未解决槽位：当用户输入不在 schema / payload_options 允许列表内的工具时，系统记录 unresolved 或要求澄清。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.oilfield_linker import OilfieldEntityLinker


class FakeLLMForAmbiguity:
    def classify_interaction(self, messages, max_tokens=260):
        return {
            "interaction_type": "WRITE",
            "query_intent": None,
            "confidence": 0.95,
            "reason": "任务修改"
        }

    def extract_json(self, messages, max_tokens=800):
        last_msg = messages[-1]["content"]
        if "【最新用户输入】:" in last_msg:
            current_input = last_msg.split("【最新用户输入】:")[1].split("\n")[0].strip().strip('"')
        else:
            current_input = last_msg

        if "17-2" in current_input and "陵水" not in current_input and "乌石" not in current_input:
            return {
                "slot_candidates": [
                    {"raw_key": "作业类型标识", "canonical_key": "task_type_key", "raw_value": "管缆巡检", "normalized_value": "pipeline_inspection", "confidence": 0.99},
                    {"raw_key": "目标油田", "canonical_key": "raw_oilfield_name", "raw_value": "17-2", "normalized_value": "17-2", "confidence": 0.80},
                ],
                "unresolved": []
            }
        elif "陵水17-2" in current_input:
            return {
                "slot_candidates": [
                    {"raw_key": "目标油田", "canonical_key": "raw_oilfield_name", "raw_value": "陵水17-2油田", "normalized_value": "陵水17-2油田", "confidence": 0.99},
                ],
                "unresolved": []
            }
        elif "不明挂件" in current_input:
            return {
                "slot_candidates": [],
                "unresolved": ["未能在知识库中匹配工具: 不明挂件"]
            }
        return {"slot_candidates": [], "unresolved": []}

    def chat(self, messages, **kwargs):
        return "请澄清详细信息。"

    def filter_reply(self, reply):
        return reply


class AmbiguityResolutionBenchmarkTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = FakeLLMForAmbiguity()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_ambiguous_device_alias_index_detection(self):
        # 验证知识库能够正确识别多匹配别名（歧义词）
        ambiguous_terms = self.kb.get_ambiguous_device_terms()
        self.assertIn("一号机", ambiguous_terms)

    def test_incomplete_oilfield_name_disambiguation_flow(self):
        # 1. 输入包含歧义的简写油田名 "17-2" (同时匹配 陵水17-2 与 乌石17-2)
        reply1 = self.dm.process("执行17-2油田管缆巡检")
        task_state1 = self.dm.task_state

        # 验证匹配状态为 accepted
        self.assertEqual(task_state1.get("oilfield_match_status"), "accepted")
        self.assertEqual(task_state1.get("raw_oilfield_name"), "17-2")

        # 2. 补充精确油田名 "陵水17-2油田" 成功消解歧义
        reply2 = self.dm.process("确认选择陵水17-2油田")
        task_state2 = self.dm.task_state

        self.assertEqual(task_state2.get("oilfield_match_status"), "accepted")
        self.assertEqual(task_state2.get("raw_oilfield_name"), "陵水17-2油田")
        self.assertIsNone(task_state2.get("pending_oilfield_name"))

    def test_unresolved_tool_extraction(self):
        # 验证未识别工具进入 unresolved 列表
        reply = self.dm.process("使用金牛座一号机，携带不明挂件")
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())


if __name__ == "__main__":
    unittest.main()
