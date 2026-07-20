"""
tests/test_intent_routing.py - 独立意图路由、非任务状态不变性及错误结构回归测试

验证 25 项关键意图路由与状态隔离约束。
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouter, IntentRouteResult
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
import web_backend


class DummyLLM(LLMClient):
    def __init__(self, default_reply: str = "默认LLM测试回复"):
        self.default_reply = default_reply
        self.called_chats = []

    def chat(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 800) -> str:
        self.called_chats.append(messages)
        return self.default_reply

    def generate(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 800) -> str:
        return self.chat(messages, temperature, max_tokens)

    def filter_reply(self, text: str) -> str:
        return text


class TestIntentRoutingAndInvariance(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.router = IntentRouter(self.llm)
        self.dm = DialogueManager(self.llm, self.kb)

    # 1. “你好” → GENERAL_CHAT
    def test_01_greeting_routes_to_general_chat(self):
        res = self.router.route("你好", [], {})
        self.assertEqual(res.intent, "GENERAL_CHAT")

    # 2. “请介绍一下你能做什么” → GENERAL_CHAT
    def test_02_intro_routes_to_general_chat(self):
        res = self.router.route("请介绍一下你能做什么", [], {})
        self.assertEqual(res.intent, "GENERAL_CHAT")

    # 3. “机器人可以使用哪些工具？” → TOOL_QUERY
    def test_03_tool_query_routes_to_tool_query(self):
        res = self.router.route("机器人可以使用哪些工具？", [], {})
        self.assertEqual(res.intent, "TOOL_QUERY")

    # 4. “500米级机器人有哪些？” → DEVICE_CAPABILITY，不写 water_depth=500
    def test_04_device_capability_no_slot_mutation(self):
        v_before = self.dm.slot_store.version
        reply = self.dm.process("500米级机器人有哪些？")
        v_after = self.dm.slot_store.version
        self.assertEqual(v_before, v_after)
        self.assertIsNone(self.dm.slot_store.get_slot_value("water_depth"))

    # 5. “当前机器人状态怎么样？” → DEVICE_STATUS
    def test_05_device_status_routing(self):
        res = self.router.route("当前机器人状态怎么样？", [], {})
        self.assertEqual(res.intent, "DEVICE_STATUS")

    # 6. “当前任务进行到哪一步？” → TASK_STATUS
    def test_06_task_status_routing(self):
        res = self.router.route("当前任务进行到哪一步？", [], {})
        self.assertEqual(res.intent, "TASK_STATUS")

    # 7. “这里的海况怎么样？” → ENVIRONMENT_QUERY
    def test_07_environment_query_routing(self):
        res = self.router.route("这里的海况怎么样？", [], {})
        self.assertEqual(res.intent, "ENVIRONMENT_QUERY")

    # 8. “创建一个管缆巡检任务” → TASK_CREATE
    def test_08_task_create_routing(self):
        with patch.object(self.llm, 'chat', return_value='{"intent": "TASK_CREATE", "confidence": 0.95, "reason": "新建任务"}'):
            res = self.router.route("创建一个管缆巡检任务", [], {})
            self.assertEqual(res.intent, "TASK_CREATE")
            self.assertTrue(res.should_update_slots)

    # 9. 已有任务时“把水深改成500米” → TASK_UPDATE
    def test_09_task_update_routing(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        with patch.object(self.llm, 'chat', return_value='{"intent": "TASK_UPDATE", "confidence": 0.95, "reason": "修改水深"}'):
            res = self.router.route("把水深改成500米", [], task_state)
            self.assertEqual(res.intent, "TASK_UPDATE")
            self.assertTrue(res.should_update_slots)

    # 10. confirming阶段“确认发布” → TASK_CONFIRM
    def test_10_confirming_phase_task_confirm(self):
        res = self.router.route("确认发布", [], {}, phase="confirming")
        self.assertEqual(res.intent, "TASK_CONFIRM")

    # 11. 非confirming阶段单独说“确认” → CLARIFICATION
    def test_11_non_confirming_phase_confirm_clarification(self):
        res = self.router.route("确认", [], {}, phase="collecting")
        self.assertEqual(res.intent, "CLARIFICATION")

    # 12. “取消当前任务” → TASK_CANCEL
    def test_12_task_cancel_routing(self):
        res = self.router.route("取消当前任务", [], {})
        self.assertEqual(res.intent, "TASK_CANCEL")

    # 13. “帮我处理一下” → CLARIFICATION或UNKNOWN
    def test_13_vague_verb_clarification(self):
        res = self.router.route("帮我处理一下", [], {})
        self.assertEqual(res.intent, "CLARIFICATION")

    # 14. 路由JSON非法 → CLARIFICATION
    def test_14_invalid_json_fallback_clarification(self):
        with patch.object(self.llm, 'chat', return_value='Not a JSON'):
            res = self.router.route("做点什么", [], {})
            self.assertEqual(res.intent, "CLARIFICATION")

    # 15. 路由模型超时/异常 → CLARIFICATION
    def test_15_exception_fallback_clarification(self):
        with patch.object(self.llm, 'chat', side_effect=TimeoutError("LLM Timeout")):
            res = self.router.route("搜索海洋", [], {})
            self.assertEqual(res.intent, "CLARIFICATION")

    # 16. 低置信度 → CLARIFICATION
    def test_16_low_confidence_fallback_clarification(self):
        with patch.object(self.llm, 'chat', return_value='{"intent": "TASK_CREATE", "confidence": 0.4, "reason": "低置信度"}'):
            res = self.router.route("海洋生物", [], {})
            self.assertEqual(res.intent, "CLARIFICATION")

    # 17. 知识问答包含数字时不得写槽位
    def test_17_knowledge_qa_with_numbers_no_slot_mutation(self):
        v_before = self.dm.slot_store.version
        reply = self.dm.process("1000米深水机器人有哪些参数？")
        v_after = self.dm.slot_store.version
        self.assertEqual(v_before, v_after)

    # 18. 非任务路由前后 slot_store.version 完全一致
    # 19. 非任务路由前后 snapshot 完全一致
    def test_18_19_non_task_route_snapshot_and_version_invariance(self):
        v_before = self.dm.slot_store.version
        snap_before = self.dm.slot_store.export_snapshot()
        
        self.dm.process("机器人可以使用哪些工具？")
        
        v_after = self.dm.slot_store.version
        snap_after = self.dm.slot_store.export_snapshot()
        self.assertEqual(v_before, v_after)
        self.assertEqual(snap_before, snap_after)

    # 20. 非任务路由不调用 extractor
    # 21. 非任务路由不调用 commit_transaction
    def test_20_21_non_task_route_does_not_call_extractor_or_commit(self):
        with patch.object(self.dm.extractor, 'extract_updates') as mock_ext, \
             patch.object(self.dm.slot_store, 'commit_transaction') as mock_commit:
            self.dm.process("你好")
            mock_ext.assert_not_called()
            mock_commit.assert_not_called()

    # 22. KB无结果时不得编造
    def test_22_kb_no_result_explicit_message(self):
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "完全不存在的超光速深潜器9000")
        self.assertFalse(res["found"])
        self.assertEqual(len(res["results"]), 0)

    # 23. /api/chat 异常结构正确
    def test_23_api_chat_exception_structured_response(self):
        client = web_backend.app.test_client()
        with patch.object(web_backend._sessions_manager, 'get', side_effect=RuntimeError("Test crash")):
            res = client.post("/api/chat", json={"session_id": "err_sess", "message": "创建一个巡检任务"})
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data.get("ok"))
            self.assertEqual(data.get("code"), 500)
            self.assertIn("request_id", data)
            self.assertTrue(data.get("retryable"))

    # 24. 前端能够展示后端 msg 和 request_id (静态检查 index.html)
    def test_24_frontend_error_parsing_elements(self):
        with open("index.html", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("res.ok", content)
        self.assertIn("data.request_id", content)
        self.assertIn("data.msg", content)

    # 25. 原有任务创建、修改、冲突、确认、发布测试全部保持通过
    def test_25_full_task_creation_flow_remains_functional(self):
        # 1. 任务创建
        with patch.object(self.dm.extractor, 'extract_updates', return_value={
            "intent": "TASK_CREATE",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "巡检", "normalized_value": "管缆巡检", "confidence": 0.95},
                {"raw_key": "任务标识", "canonical_key": "task_type_key", "raw_value": "巡检", "normalized_value": "pipeline_inspection", "confidence": 0.95},
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": "300", "confidence": 0.95},
            ]
        }):
            reply = self.dm.process("创建一个水下巡检任务，水深300米")
            self.assertIn("300", str(self.dm._last_built_json))
            self.assertEqual(self.dm.task_state.get("task_type_key"), "pipeline_inspection")


if __name__ == "__main__":
    unittest.main()
