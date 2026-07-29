"""
tests/test_intent_routing_matrix.py — IntentRouter 行为验收测试矩阵

涵盖场景：
1. QUERY:
   - "天鹰座一号机最大水深是多少？" -> interaction_type=QUERY, query_intent=DEVICE_CAPABILITY, SlotStore 不变化
   - "管缆巡检需要什么工具？" -> interaction_type=QUERY, query_intent=KNOWLEDGE_QA 或 TOOL_QUERY, SlotStore 不变化
   - "当前任务缺少什么？" -> interaction_type=QUERY, query_intent=TASK_STATUS, SlotStore 不变化
2. WRITE:
   - "执行流花11-1油田管缆巡检" -> interaction_type=WRITE, 提取 task_type 和 oilfield_name
   - "使用天鹰座一号机，携带机械臂和声呐" -> interaction_type=WRITE, 提取 equipment_type 和 payload
3. 边界场景:
   - "水深500米合适吗？" -> 必须识别为 QUERY, SlotStore 状态不发生写入修改
   - "把水深改成500米" -> 必须识别为 WRITE, 触发水深参数修改
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.intent_router import IntentRouter, IntentRouteResult
from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase


class FakeLLMForRoutingMatrix:
    def route_mock(self, user_message: str):
        msg = user_message.strip()
        if msg == "水深500米合适吗？":
            return {
                "interaction_type": "QUERY",
                "query_intent": "DEVICE_CAPABILITY",
                "confidence": 0.96,
                "reason": "用户在条件性询问水深500米是否合适，未提交写入意图"
            }
        elif msg == "把水深改成500米":
            return {
                "interaction_type": "WRITE",
                "query_intent": None,
                "confidence": 0.98,
                "reason": "用户明确提交准备写入的任务水深参数"
            }
        elif "最大水深是多少" in msg:
            return {
                "interaction_type": "QUERY",
                "query_intent": "DEVICE_CAPABILITY",
                "confidence": 0.97,
                "reason": "询问设备能力参数"
            }
        elif "需要什么工具" in msg:
            return {
                "interaction_type": "QUERY",
                "query_intent": "KNOWLEDGE_QA",
                "confidence": 0.95,
                "reason": "询问作业工具知识"
            }
        elif "缺少什么" in msg:
            return {
                "interaction_type": "QUERY",
                "query_intent": "TASK_STATUS",
                "confidence": 0.95,
                "reason": "询问任务进度及缺失槽位"
            }
        elif "执行流花11-1油田管缆巡检" in msg:
            return {
                "interaction_type": "WRITE",
                "query_intent": None,
                "confidence": 0.99,
                "reason": "提交任务类型与油田"
            }
        elif "携带机械臂和声呐" in msg:
            return {
                "interaction_type": "WRITE",
                "query_intent": None,
                "confidence": 0.98,
                "reason": "提交设备与载荷"
            }
        else:
            return {
                "interaction_type": "QUERY",
                "query_intent": "GENERAL_CHAT",
                "confidence": 0.90,
                "reason": "普通查询"
            }

    def classify_interaction(self, messages, max_tokens=260):
        last_msg = messages[-1]["content"]
        # extract latest user message from context prompt
        if "【最新用户输入】:" in last_msg:
            user_msg = last_msg.split("【最新用户输入】:")[1].strip().strip('"')
        else:
            user_msg = last_msg
        return self.route_mock(user_msg)

    def extract_json(self, messages, max_tokens=800):
        last_msg = messages[-1]["content"]
        if "执行流花11-1油田管缆巡检" in last_msg:
            return {
                "slot_candidates": [
                    {"raw_key": "作业类型标识", "canonical_key": "task_type_key", "raw_value": "管缆巡检", "normalized_value": "pipeline_inspection", "confidence": 0.99},
                    {"raw_key": "作业类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 0.99},
                    {"raw_key": "目标油田", "canonical_key": "raw_oilfield_name", "raw_value": "流花11-1油田", "normalized_value": "流花11-1油田", "confidence": 0.99},
                ],
                "unresolved": []
            }
        elif "携带机械臂和声呐" in last_msg:
            return {
                "slot_candidates": [
                    {"raw_key": "使用设备", "canonical_key": "equipment_type", "raw_value": "天鹰座一号机", "normalized_value": "轻型工作级深海机器人", "confidence": 0.95},
                    {"raw_key": "携带工具", "canonical_key": "payload", "raw_value": "机械臂", "normalized_value": "机械臂", "confidence": 0.95},
                    {"raw_key": "携带工具", "canonical_key": "payload", "raw_value": "声呐", "normalized_value": "前视声呐", "confidence": 0.95},
                ],
                "unresolved": []
            }
        elif "把水深改成500米" in last_msg:
            return {
                "slot_candidates": [
                    {"raw_key": "作业水深", "canonical_key": "water_depth", "raw_value": "500米", "normalized_value": 500, "confidence": 0.99}
                ],
                "unresolved": []
            }
        return {"slot_candidates": [], "unresolved": []}

    def chat(self, messages, **kwargs):
        return "响应消息"

    def filter_reply(self, reply):
        return reply


class IntentRoutingMatrixTest(unittest.TestCase):
    def setUp(self):
        self.llm = FakeLLMForRoutingMatrix()
        self.router = IntentRouter(self.llm)
        self.kb = KnowledgeBase()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_query_device_capability(self):
        msg = "天鹰座一号机最大水深是多少？"
        route = self.router.route(msg, [], {})
        self.assertEqual(route.interaction_type, "QUERY")
        self.assertEqual(route.query_intent, "DEVICE_CAPABILITY")

        # DialogueManager 处理 QUERY 必须保证 SlotStore 状态不发生变化
        ver_before = self.dm.slot_store.version
        state_before = dict(self.dm.task_state)
        self.dm.process(msg)
        self.assertEqual(self.dm.slot_store.version, ver_before)
        self.assertEqual(self.dm.task_state, state_before)

    def test_query_knowledge_qa(self):
        msg = "管缆巡检需要什么工具？"
        route = self.router.route(msg, [], {})
        self.assertEqual(route.interaction_type, "QUERY")
        self.assertIn(route.query_intent, ("KNOWLEDGE_QA", "TOOL_QUERY"))

        ver_before = self.dm.slot_store.version
        self.dm.process(msg)
        self.assertEqual(self.dm.slot_store.version, ver_before)

    def test_query_task_status(self):
        msg = "当前任务缺少什么？"
        route = self.router.route(msg, [], {})
        self.assertEqual(route.interaction_type, "QUERY")
        self.assertEqual(route.query_intent, "TASK_STATUS")

        ver_before = self.dm.slot_store.version
        self.dm.process(msg)
        self.assertEqual(self.dm.slot_store.version, ver_before)

    def test_write_task_creation(self):
        msg = "执行流花11-1油田管缆巡检"
        route = self.router.route(msg, [], {})
        self.assertEqual(route.interaction_type, "WRITE")
        self.assertIsNone(route.query_intent)

        reply = self.dm.process(msg)
        self.assertEqual(self.dm.task_state.get("task_type_key"), "pipeline_inspection")
        self.assertTrue(
            self.dm.task_state.get("oilfield_name") == "流花11-1油田" or
            self.dm.task_state.get("raw_oilfield_name") == "流花11-1油田" or
            self.dm.task_state.get("pending_oilfield_name") == "流花11-1油田"
        )

    def test_write_equipment_and_payload(self):
        # 先建立任务
        self.dm.process("执行流花11-1油田管缆巡检")
        msg = "使用天鹰座一号机，携带机械臂和声呐"
        route = self.router.route(msg, self.dm.conversation_history, self.dm.task_state)
        self.assertEqual(route.interaction_type, "WRITE")

        self.dm.process(msg)
        self.assertEqual(self.dm.task_state.get("equipment_type"), "轻型工作级深海机器人")

    def test_boundary_depth_query_vs_write(self):
        # 边界 1: "水深500米合适吗？" 必须识别为 QUERY，且不能更新水深
        msg_query = "水深500米合适吗？"
        route_q = self.router.route(msg_query, [], {})
        self.assertEqual(route_q.interaction_type, "QUERY")
        self.assertEqual(route_q.query_intent, "DEVICE_CAPABILITY")

        ver_before = self.dm.slot_store.version
        self.dm.process(msg_query)
        self.assertEqual(self.dm.slot_store.version, ver_before)
        self.assertNotIn("water_depth", self.dm.task_state)

        # 边界 2: "把水深改成500米" 必须识别为 WRITE，且更新水深
        self.dm.process("执行流花11-1油田管缆巡检")
        msg_write = "把水深改成500米"
        route_w = self.router.route(msg_write, self.dm.conversation_history, self.dm.task_state)
        self.assertEqual(route_w.interaction_type, "WRITE")

        self.dm.process(msg_write)
        self.assertEqual(self.dm.task_state.get("water_depth"), 500)


if __name__ == "__main__":
    unittest.main()
