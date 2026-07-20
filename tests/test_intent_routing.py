"""
tests/test_intent_routing.py - 独立意图路由、否定语义、非任务状态不变性及错误结构回归测试
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
        self.llm = None
        self.default_reply = default_reply
        self.called_chats = []

    def chat(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 800) -> str:
        self.called_chats.append(messages)
        res = super().chat(messages, temperature, max_tokens)
        if res and res != "收到您的信息，请继续补充任务描述。":
            return res
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

    # 1. “这个任务适合使用什么工具？” → TOOL_QUERY
    def test_r01_tool_query_routing(self):
        res = self.router.route("这个任务适合使用什么工具？", [], {})
        self.assertEqual(res.intent, "TOOL_QUERY")
        self.assertFalse(res.should_update_slots)

    # 2. 已有任务时同一句仍为 TOOL_QUERY
    def test_r02_active_task_tool_query_routing(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("这个任务适合使用什么工具？", [], task_state)
        self.assertEqual(res.intent, "TOOL_QUERY")
        self.assertFalse(res.should_update_slots)

    # 3. 已有任务时“谢谢” → GENERAL_CHAT
    def test_r03_active_task_thanks_routing(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("谢谢", [], task_state)
        self.assertEqual(res.intent, "GENERAL_CHAT")
        self.assertFalse(res.should_update_slots)

    # 4. 已有任务时无关输入 → CLARIFICATION 或 UNKNOWN
    def test_r04_active_task_irrelevant_input_routing(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("今天天气不错啊", [], task_state)
        self.assertIn(res.intent, ("CLARIFICATION", "UNKNOWN"))
        self.assertFalse(res.should_update_slots)

    # 5. “不好，水深改成500米”不得确认发布
    def test_r05_negation_confirm_does_not_confirm(self):
        self.dm.phase = "confirming"
        self.dm.task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("不好，水深改成500米", [], self.dm.task_state, phase=self.dm.phase)
        self.assertNotEqual(res.intent, "TASK_CONFIRM")
        self.assertIn(res.intent, ("TASK_CREATE", "TASK_UPDATE"))

    # 6. “不确认”不得确认发布
    def test_r06_unconfirm_does_not_confirm(self):
        self.dm.phase = "confirming"
        res = self.router.route("不确认", [], self.dm.task_state, phase=self.dm.phase)
        self.assertNotEqual(res.intent, "TASK_CONFIRM")

    # 7. “不要发布”不得确认发布
    def test_r07_dont_publish_does_not_confirm(self):
        self.dm.phase = "confirming"
        res = self.router.route("不要发布", [], self.dm.task_state, phase=self.dm.phase)
        self.assertNotEqual(res.intent, "TASK_CONFIRM")

    # 8. “不要取消任务”不得取消任务
    def test_r08_dont_cancel_does_not_cancel(self):
        res = self.router.route("不要取消任务", [], self.dm.task_state, phase="collecting")
        self.assertNotEqual(res.intent, "TASK_CANCEL")

    # 9. “确认发布”在 confirming 阶段正常发布
    def test_r09_confirm_publish_in_confirming_phase(self):
        res = self.router.route("确认发布", [], self.dm.task_state, phase="confirming")
        self.assertEqual(res.intent, "TASK_CONFIRM")

    # 10. “取消当前任务”正常取消
    def test_r10_cancel_current_task(self):
        res = self.router.route("取消当前任务", [], self.dm.task_state)
        self.assertEqual(res.intent, "TASK_CANCEL")

    # 11. 知识问题包含500、1000等数字不得写入 water_depth
    def test_r11_knowledge_query_numbers_no_slot_mutation(self):
        v_before = self.dm.slot_store.version
        reply = self.dm.process("500米级机器人有哪些？")
        v_after = self.dm.slot_store.version
        self.assertEqual(v_before, v_after)
        wd = self.dm.slot_store.slots.get("water_depth")
        self.assertTrue(wd is None or wd.value is None)

    # 12. 每一种非任务路由均不调用 extractor 和 commit_transaction
    def test_r12_non_task_routes_no_extractor_or_commit(self):
        non_task_queries = [
            "你好",
            "机器人可以使用哪些工具？",
            "500米级机器人有哪些？",
            "当前任务进行到哪一步？",
            "谢谢",
        ]
        for q in non_task_queries:
            with patch.object(self.dm.extractor, 'extract_updates') as mock_ext, \
                 patch.object(self.dm.slot_store, 'commit_transaction') as mock_commit:
                self.dm.process(q)
                mock_ext.assert_not_called()
                mock_commit.assert_not_called()

    # 13. 非任务路由前后完整状态快照一致
    def test_r13_non_task_route_snapshot_invariance(self):
        v_before = self.dm.slot_store.version
        snap_before = self.dm.slot_store.export_snapshot()
        self.dm.process("机器人可以使用哪些工具？")
        v_after = self.dm.slot_store.version
        snap_after = self.dm.slot_store.export_snapshot()
        self.assertEqual(v_before, v_after)
        self.assertEqual(snap_before, snap_after)

    # 14. 500米级设备查询必须真正过滤结果
    def test_r14_device_capability_500m_filtering(self):
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "500米级机器人有哪些？")
        self.assertTrue(res["found"])
        for r in res["results"]:
            self.assertEqual(r.get("max_depth_m"), 500)

    # 15. KB found=false 时回复不得编造事实
    def test_r15_kb_not_found_no_hallucination(self):
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "超光速神潜器9000")
        self.assertFalse(res["found"])
        self.assertEqual(len(res["results"]), 0)
        reply = self.dm.process("超光速神潜器9000有哪些参数？")
        self.assertIn("当前知识库未提供该信息", reply)

    # ── LLM 抽取的专置测试 (包含 extract_json 的 assert_called_once 校验) ──
    def test_llm_router_invalid_json_fallback(self):
        with patch.object(self.llm, 'extract_json', return_value="Not A Dict") as mock_extract:
            res = self.router.route("一些模糊未知的查询句", [], {})
            mock_extract.assert_called_once()
            self.assertEqual(res.intent, "CLARIFICATION")
            self.assertFalse(res.should_update_slots)

    def test_llm_router_exception_fallback(self):
        with patch.object(self.llm, 'extract_json', side_effect=RuntimeError("LLM error")) as mock_extract:
            res = self.router.route("一些模糊未知的查询句", [], {})
            mock_extract.assert_called_once()
            self.assertEqual(res.intent, "CLARIFICATION")

    def test_llm_router_invalid_intent_fallback(self):
        with patch.object(self.llm, 'extract_json', return_value={"intent": "INVALID_INTENT", "confidence": 0.9}) as mock_extract:
            res = self.router.route("一些模糊未知的查询句", [], {})
            mock_extract.assert_called_once()
            self.assertEqual(res.intent, "CLARIFICATION")

    def test_llm_router_low_confidence_fallback(self):
        with patch.object(self.llm, 'extract_json', return_value={"intent": "TASK_CREATE", "confidence": 0.4}) as mock_extract:
            res = self.router.route("一些模糊未知的查询句", [], {})
            mock_extract.assert_called_once()
            self.assertEqual(res.intent, "CLARIFICATION")
            self.assertFalse(res.should_update_slots)

    def test_api_chat_exception_structured_response(self):
        client = web_backend.app.test_client()
        with patch.object(web_backend, 'get_or_create_manager', side_effect=RuntimeError("Test crash")):
            res = client.post("/api/chat", json={"session_id": "err_sess", "message": "创建一个巡检任务"})
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data.get("ok"))
            self.assertEqual(data.get("code"), 500)
            self.assertIn("request_id", data)
            self.assertTrue(data.get("retryable"))


if __name__ == "__main__":
    unittest.main()
