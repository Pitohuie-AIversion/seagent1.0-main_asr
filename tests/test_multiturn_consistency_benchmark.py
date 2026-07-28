"""
tests/test_multiturn_consistency_benchmark.py — 多轮任务一致性 Benchmark 测试

验证场景：
用户连续输入：
1. 创建任务: "执行流花11-1油田管缆巡检"
2. 补充设备: "使用金牛座一号机"
3. 补充 payload: "携带激光标尺和机械式声呐"
4. 修改参数: "把水深改成300米"

验证：
- 最终 task_state 与 SlotStore.get_task_state() 完全一致（SSOT）。
- 每一个 WRITE 轮次 SlotStore.version 递增。
- 多轮积累参数完整保留，无字段丢失或状态污染。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.intent_router import IntentRouter


class FakeLLMForMultiTurnBenchmark:
    def classify_interaction(self, messages, max_tokens=260):
        last_msg = messages[-1]["content"]
        if "【最新用户输入】:" in last_msg:
            user_msg = last_msg.split("【最新用户输入】:")[1].strip().strip('"')
        else:
            user_msg = last_msg
        
        # 本测试全为 WRITE 交互
        return {
            "interaction_type": "WRITE",
            "query_intent": None,
            "confidence": 0.98,
            "reason": "用户正在提交或修改任务参数"
        }

    def extract_json(self, messages, max_tokens=800):
        last_msg = messages[-1]["content"]
        if "【最新用户输入】:" in last_msg:
            current_input = last_msg.split("【最新用户输入】:")[1].split("\n")[0].strip().strip('"')
        else:
            current_input = last_msg

        if "执行流花11-1油田管缆巡检" in current_input or "流花11-1油田" in current_input:
            return {
                "slot_candidates": [
                    {"raw_key": "作业类型标识", "canonical_key": "task_type_key", "raw_value": "管缆巡检", "normalized_value": "pipeline_inspection", "confidence": 0.99},
                    {"raw_key": "作业类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 0.99},
                    {"raw_key": "目标油田", "canonical_key": "raw_oilfield_name", "raw_value": "流花11-1油田", "normalized_value": "流花11-1油田", "confidence": 0.99},
                ],
                "unresolved": []
            }
        elif "金牛座一号机" in current_input:
            return {
                "slot_candidates": [
                    {"raw_key": "使用设备", "canonical_key": "equipment_type", "raw_value": "金牛座一号机", "normalized_value": "轻型工作级深海机器人", "confidence": 0.95},
                    {"raw_key": "使用设备", "canonical_key": "equipment_name", "raw_value": "金牛座一号机", "normalized_value": "金牛座一号机", "confidence": 0.95},
                ],
                "unresolved": []
            }
        elif "激光标尺" in current_input or "机械式声呐" in current_input:
            return {
                "slot_candidates": [
                    {"raw_key": "携带工具", "canonical_key": "payload", "raw_value": "激光标尺", "normalized_value": "激光标尺", "confidence": 0.95},
                    {"raw_key": "携带工具", "canonical_key": "payload", "raw_value": "机械式声呐", "normalized_value": "机械式声呐", "confidence": 0.95},
                ],
                "unresolved": []
            }
        elif "300米" in current_input or "水深" in current_input:
            return {
                "slot_candidates": [
                    {"raw_key": "作业水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300, "confidence": 0.99}
                ],
                "unresolved": []
            }
        return {"slot_candidates": [], "unresolved": []}

    def chat(self, messages, **kwargs):
        return "参数已接收。"

    def filter_reply(self, reply):
        return reply


class MultiTurnConsistencyBenchmarkTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = FakeLLMForMultiTurnBenchmark()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_four_turn_write_accumulation_and_ssot_consistency(self):
        # Initial state
        initial_ver = self.dm.slot_store.version

        # Turn 1: 创建任务
        r1 = self.dm.process("执行流花11-1油田管缆巡检")
        v1 = self.dm.slot_store.version
        self.assertGreater(v1, initial_ver)
        self.assertEqual(self.dm.task_state.get("task_type_key"), "pipeline_inspection")
        self.assertEqual(self.dm.task_state.get("raw_oilfield_name"), "流花11-1油田")
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())

        # Turn 2: 补充设备
        r2 = self.dm.process("使用金牛座一号机")
        v2 = self.dm.slot_store.version
        self.assertGreater(v2, v1)
        self.assertEqual(self.dm.task_state.get("equipment_type"), "轻型工作级深海机器人")
        self.assertEqual(self.dm.task_state.get("raw_oilfield_name"), "流花11-1油田")
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())

        # Turn 3: 补充 payload
        r3 = self.dm.process("携带激光标尺和机械式声呐")
        v3 = self.dm.slot_store.version
        self.assertGreater(v3, v2)
        payload = self.dm.task_state.get("payload")
        self.assertIsInstance(payload, list)
        self.assertIn("激光标尺", payload)
        self.assertIn("机械式声呐", payload)
        self.assertEqual(self.dm.task_state.get("equipment_type"), "轻型工作级深海机器人")
        self.assertEqual(self.dm.task_state.get("raw_oilfield_name"), "流花11-1油田")
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())

        # Turn 4: 修改水深参数
        r4 = self.dm.process("把水深改成300米")
        v4 = self.dm.slot_store.version
        self.assertGreater(v4, v3)
        self.assertEqual(self.dm.task_state.get("water_depth"), 300)
        self.assertEqual(self.dm.task_state.get("equipment_type"), "轻型工作级深海机器人")
        self.assertEqual(self.dm.task_state.get("raw_oilfield_name"), "流花11-1油田")
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())


if __name__ == "__main__":
    unittest.main()
