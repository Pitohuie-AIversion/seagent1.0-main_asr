"""
tests/test_query_write_mixed_benchmark.py — Query/Write 混合 Benchmark 测试

验证目标：
在多轮任务规划过程中插入 QUERY 消息（例如查询设备能力、查询缺失字段、查询实时状态）。
验证：
- QUERY 交互永远不改变 SlotStore.version。
- QUERY 交互永远不改变 dm.task_state。
- 经过多轮 QUERY/WRITE 交叉后，最终 task_state 与 SlotStore 完全一致，且参数无丢失。
"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase


class FakeLLMForQueryWriteMixed:
    def classify_interaction(self, messages, max_tokens=260):
        last_msg = messages[-1]["content"]
        if "【最新用户输入】:" in last_msg:
            user_msg = last_msg.split("【最新用户输入】:")[1].split("\n")[0].strip().strip('"')
        else:
            user_msg = last_msg

        if "最大水深" in user_msg or "能力" in user_msg:
            return {
                "interaction_type": "QUERY",
                "query_intent": "DEVICE_CAPABILITY",
                "confidence": 0.98,
                "reason": "设备能力查询"
            }
        elif "缺少" in user_msg or "进度" in user_msg:
            return {
                "interaction_type": "QUERY",
                "query_intent": "TASK_STATUS",
                "confidence": 0.98,
                "reason": "任务进度/缺失字段查询"
            }
        elif "当前深度" in user_msg or "实时状态" in user_msg:
            return {
                "interaction_type": "QUERY",
                "query_intent": "DEVICE_STATUS",
                "confidence": 0.98,
                "reason": "实时状态查询"
            }
        else:
            return {
                "interaction_type": "WRITE",
                "query_intent": None,
                "confidence": 0.98,
                "reason": "任务参数修改"
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
        elif "水下无人自主航行器一号机" in current_input:
            return {
                "slot_candidates": [
                    {"raw_key": "使用设备", "canonical_key": "equipment_type", "raw_value": "AUV", "normalized_value": "AUV", "confidence": 0.95},
                    {"raw_key": "使用设备", "canonical_key": "equipment_name", "raw_value": "水下无人自主航行器-324cc-001", "normalized_value": "水下无人自主航行器-324cc-001", "confidence": 0.95},
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
        sys_msg = messages[0]["content"] if messages else ""
        if "350" in sys_msg:
            return "设备当前深度为 350m。"
        return "查询完成。"

    def filter_reply(self, reply):
        return reply


class QueryWriteMixedBenchmarkTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self._state_temp_dir = tempfile.TemporaryDirectory()
        self.kb.state_info.state_file = Path(self._state_temp_dir.name) / "state.yaml"
        self.llm = FakeLLMForQueryWriteMixed()
        self.dm = DialogueManager(self.llm, self.kb)

    def tearDown(self):
        self._state_temp_dir.cleanup()

    def test_interleaved_query_write_invariance(self):
        ver_0 = self.dm.slot_store.version
        state_0 = dict(self.dm.task_state)

        # Turn 1 (WRITE): 创建任务
        self.dm.process("执行流花11-1油田管缆巡检")
        ver_1 = self.dm.slot_store.version
        state_1 = dict(self.dm.task_state)
        self.assertGreater(ver_1, ver_0)

        # Turn 2 (QUERY): 设备能力查询
        reply_q1 = self.dm.process("水下无人自主航行器一号机最大水深是多少？")
        ver_q1 = self.dm.slot_store.version
        state_q1 = dict(self.dm.task_state)
        self.assertEqual(ver_q1, ver_1, "QUERY 操作不得改变 SlotStore 版本号")
        self.assertEqual(state_q1, state_1, "QUERY 操作不得改变 task_state 内容")

        # Turn 3 (WRITE): 补充设备
        self.dm.process("使用水下无人自主航行器一号机")
        ver_2 = self.dm.slot_store.version
        state_2 = dict(self.dm.task_state)
        self.assertGreater(ver_2, ver_1)

        # Turn 4 (QUERY): 任务进度与缺失字段查询
        reply_q2 = self.dm.process("当前任务缺少什么？")
        ver_q2 = self.dm.slot_store.version
        state_q2 = dict(self.dm.task_state)
        self.assertEqual(ver_q2, ver_2, "QUERY 操作不得改变 SlotStore 版本号")
        self.assertEqual(state_q2, state_2, "QUERY 操作不得改变 task_state 内容")

        # Turn 5 (WRITE): 修改水深
        self.dm.process("把水深改成300米")
        ver_3 = self.dm.slot_store.version
        state_3 = dict(self.dm.task_state)
        self.assertGreater(ver_3, ver_2)

        # Turn 6 (QUERY): 实时状态查询
        # 先设置实时数据
        self.kb.state_info.set_status("AUV-324cc-001", {"depth": 350})
        self.kb.state_info.set_status("水下无人自主航行器 324CC", {"depth": 350})
        reply_q3 = self.dm.process("当前设备实时深度？")
        ver_q3 = self.dm.slot_store.version
        state_q3 = dict(self.dm.task_state)
        self.assertEqual(ver_q3, ver_3, "QUERY 操作不得改变 SlotStore 版本号")
        self.assertEqual(state_q3, state_3, "QUERY 操作不得改变 task_state 内容")
        self.assertIn("350", reply_q3)

        # 最终一致性校验
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())


if __name__ == "__main__":
    unittest.main()
