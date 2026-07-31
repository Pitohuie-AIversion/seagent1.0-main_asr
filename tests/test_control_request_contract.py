"""
tests/test_control_request_contract.py - 控制请求生命周期闭环契约测试
"""

import copy
import json
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

from src.dialogue_manager import DialogueManager
from src.history_manager import save_conversation, load_history
from src.intent_router import IntentRouter, IntentRouteResult
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.slot_store import Slot
from src.exceptions import ControlAuditPersistenceError
import web_backend


class DummyLLM:
    def __init__(self):
        pass

    def filter_reply(self, text):
        return str(text) if text is not None else ""

    def chat(self, messages, temperature=0.7):
        return "模拟 LLM 回复"

    def extract_json(self, messages, max_tokens=800):
        return {}


class FakeClassifierLLM:
    def __init__(self, mode="knowledge_qa", action=None):
        self.mode = mode
        self.action = action

    def classify_interaction(self, messages, max_tokens=260):
        res = {
            "dialogue_mode": self.mode,
            "interaction_type": "WRITE" if self.mode == "task_collection" else "QUERY",
            "query_intent": "KNOWLEDGE_QA" if self.mode == "knowledge_qa" else None,
            "confidence": 0.95,
            "reason": "FakeClassifierLLM 离线生成分类结果",
        }
        if self.action:
            res["emergency_action"] = self.action
        return res

    def filter_reply(self, text):
        return str(text) if text is not None else ""

    def chat(self, messages, temperature=0.7):
        return "Fake LLM 响应"


class TestControlRequestContract(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)
        self.router = IntentRouter(self.llm)

    def _seed_pipeline_task(self):
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        self.dm.slot_store.slots["equipment_type"] = Slot("equipment_type", value="Haida_ROV_3000", status="valid")
        self.dm.slot_store.slots["payload"] = Slot("payload", value=["camera"], status="valid", value_type="list")
        self.dm._rebuild_cache()

    # --------------------------------------------------------------------------
    # 目标 1: 活动任务判定 (No active task -> Fail Closed)
    # --------------------------------------------------------------------------

    def test_stop_without_active_task_does_not_create_request(self):
        """空会话输入'立即停止当前任务'：保持 idle，不产生控制请求"""
        self.dm.reset()
        reply = self.dm.process("立即停止当前任务", request_id="req_test_01")
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)
        self.assertIn("当前没有活动任务", reply)

    def test_pause_without_active_task_does_not_create_request(self):
        """空会话输入'暂停当前任务'：保持 idle，不产生控制请求"""
        self.dm.reset()
        reply = self.dm.process("暂停当前任务", request_id="req_test_02")
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)
        self.assertIn("当前没有活动任务", reply)

    def test_abort_without_active_task_does_not_create_request(self):
        """空会话输入'终止当前任务'：保持 idle，不产生控制请求"""
        self.dm.reset()
        reply = self.dm.process("终止当前任务", request_id="req_test_03")
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)
        self.assertIn("当前没有活动任务", reply)

    def test_cancel_without_active_task_does_not_reject_session(self):
        """空会话或 rejected 阶段输入'取消当前任务'：不破坏状态，提示没有可取消的未发布任务"""
        self.dm.reset()
        reply = self.dm.process("取消当前任务", request_id="req_test_04")
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)
        self.assertIn("当前没有可取消的未发布任务", reply)

    # --------------------------------------------------------------------------
    # 目标 2: 区分全局控制与局部修改
    # --------------------------------------------------------------------------

    def test_cancel_current_task_is_global_control(self):
        """'取消当前任务'为明确全局 cancel"""
        self._seed_pipeline_task()
        res = self.router.route("取消当前任务", [], self.dm.task_state)
        self.assertEqual(res.dialogue_mode, "emergency_intervention")
        self.assertEqual(res.emergency_action, "cancel")

    def test_cancel_task_payload_modification_is_local(self):
        """'取消载荷修改'、'取消任务载荷修改'为局部槽位操作，走 task_collection"""
        inputs = ["取消载荷修改", "取消任务载荷修改"]
        for inp in inputs:
            self._seed_pipeline_task()
            res = self.router.route(inp, [], self.dm.task_state)
            self.assertEqual(res.dialogue_mode, "task_collection")
            self.assertIsNone(res.emergency_action)

    def test_cancel_support_vessel_change_is_local(self):
        """'取消支持船修改'为局部槽位操作，走 task_collection"""
        self._seed_pipeline_task()
        res = self.router.route("取消支持船修改", [], self.dm.task_state)
        self.assertEqual(res.dialogue_mode, "task_collection")
        self.assertIsNone(res.emergency_action)

    def test_stop_task_printing_is_not_global_control(self):
        """'停止任务打印'为非任务控制，走 knowledge_qa"""
        self._seed_pipeline_task()
        res = self.router.route("停止任务打印", [], self.dm.task_state)
        self.assertEqual(res.dialogue_mode, "knowledge_qa")
        self.assertIsNone(res.emergency_action)

    def test_terminate_explanation_output_is_not_global_control(self):
        """'终止说明输出'为非任务控制，走 knowledge_qa"""
        self._seed_pipeline_task()
        res = self.router.route("终止说明输出", [], self.dm.task_state)
        self.assertEqual(res.dialogue_mode, "knowledge_qa")
        self.assertIsNone(res.emergency_action)

    # --------------------------------------------------------------------------
    # 目标 3: 草稿取消保留对话审计
    # --------------------------------------------------------------------------

    def test_draft_cancel_preserves_conversation_history(self):
        """草稿取消：清空槽位与任务数据，但保留已有 conversation_history 记录"""
        self.dm.reset()
        self._seed_pipeline_task()
        self.dm.conversation_history.append({"role": "user", "content": "你好"})
        self.dm.conversation_history.append({"role": "assistant", "content": "您好"})
        history_count_before = len(self.dm.conversation_history)

        reply = self.dm.process("取消当前任务", request_id="req_cancel_hist")
        self.assertEqual(self.dm.phase, "rejected")
        self.assertEqual(self.dm.task_state, {})
        # 对话历史不被 wipe，且增加了用户取消与系统回复
        self.assertGreater(len(self.dm.conversation_history), history_count_before)
        self.assertEqual(self.dm.conversation_history[0]["content"], "你好")
        self.assertIn("取消", reply)

    def test_draft_cancel_clears_task_state_only(self):
        """草稿取消：仅清空任务草稿状态，控制状态保持 idle"""
        self.dm.reset()
        self._seed_pipeline_task()
        self.dm.process("取消当前任务", request_id="req_cancel_state")
        self.assertEqual(self.dm.task_state, {})
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)
        self.assertIsNone(self.dm.final_result)

    # --------------------------------------------------------------------------
    # 目标 4 & 5: 控制请求结构体与 API Request ID
    # --------------------------------------------------------------------------

    def test_control_request_contains_request_id(self):
        """控制请求必须准确记录由 API 传入的 request_id"""
        self.dm.reset()
        self._seed_pipeline_task()
        self.dm.process("立即停止当前任务", request_id="req_custom_12345")
        self.assertEqual(self.dm.control_state, "stop_requested")
        self.assertIsNotNone(self.dm.last_control_request)
        self.assertEqual(self.dm.last_control_request["request_id"], "req_custom_12345")

    def test_control_request_contains_timezone_aware_timestamp(self):
        """控制请求 requested_at 必须包含时区偏移 ISO 8601"""
        self.dm.reset()
        self._seed_pipeline_task()
        self.dm.process("暂停当前任务", request_id="req_tz_check")
        req = self.dm.last_control_request
        self.assertIsNotNone(req)
        requested_at = req["requested_at"]
        dt = datetime.fromisoformat(requested_at)
        self.assertIsNotNone(dt.tzinfo)

    def test_control_request_contains_task_and_intent_ids(self):
        """控制请求元数据包含 phase_at_request、intent_id 及 task_id (草稿阶段为 None/字符串)"""
        self.dm.reset()
        self._seed_pipeline_task()
        self.dm.process("终止当前任务", request_id="req_meta_check")
        req = self.dm.last_control_request
        self.assertIsNotNone(req)
        self.assertIn("phase_at_request", req)
        self.assertIn("task_id", req)
        self.assertIn("intent_id", req)

    # --------------------------------------------------------------------------
    # 目标 6: 结构化 API 输出
    # --------------------------------------------------------------------------

    def test_api_chat_exposes_control_state(self):
        """/api/chat 端点必须返回 dialogue_mode, control_state, last_control_request"""
        app = web_backend.app.test_client()
        sid = "test_session_api_chat"
        # 写入任务
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})
        res = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_api_01"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("dialogue_mode", data)
        self.assertIn("control_state", data)
        self.assertIn("last_control_request", data)
        self.assertEqual(data["control_state"], "stop_requested")
        self.assertIsNotNone(data["last_control_request"])
        self.assertEqual(data["last_control_request"]["request_id"], "req_api_01")

    def test_session_state_exposes_control_state(self):
        """/api/session/state 端点必须返回 dialogue_mode, control_state, last_control_request"""
        app = web_backend.app.test_client()
        sid = "test_session_api_state"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})
        app.post("/api/chat", json={"session_id": sid, "message": "暂停当前任务", "request_id": "req_api_02"})
        res = app.get(f"/api/session/state?session_id={sid}")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["exists"])
        self.assertEqual(data["control_state"], "pause_requested")
        self.assertIsNotNone(data["last_control_request"])

    # --------------------------------------------------------------------------
    # 目标 7 & 8: 控制状态持久化、恢复与失败回滚
    # --------------------------------------------------------------------------

    def test_control_request_survives_history_round_trip(self):
        """控制请求及元数据可保存至历史 JSON 并通过 load_snapshot 完整恢复"""
        self.dm.reset()
        self._seed_pipeline_task()
        self.dm.process("立即停止当前任务", request_id="req_roundtrip_01")

        snap = {
            "conversation_history": self.dm.conversation_history,
            "mode": self.dm.mode,
            "phase": self.dm.phase,
            "dialogue_mode": self.dm.dialogue_mode,
            "control_state": self.dm.control_state,
            "last_control_request": self.dm.last_control_request,
            "slot_store": self.dm.slot_store.export_snapshot(),
            "task_state": copy.deepcopy(self.dm.task_state),
        }

        dm_new = DialogueManager(self.llm, self.kb)
        dm_new.load_snapshot(snap)
        self.assertEqual(dm_new.control_state, "stop_requested")
        self.assertIsNotNone(dm_new.last_control_request)
        self.assertEqual(dm_new.last_control_request["request_id"], "req_roundtrip_01")

    def test_legacy_snapshot_defaults_to_idle_control_state(self):
        """无新增控制元数据的旧版快照恢复时自动默认 control_state 为 idle"""
        legacy_snap = {
            "conversation_history": [],
            "mode": "normal",
            "phase": "collecting",
            "task_state": {},
        }
        dm_new = DialogueManager(self.llm, self.kb)
        dm_new.load_snapshot(legacy_snap)
        self.assertEqual(dm_new.dialogue_mode, "task_collection")
        self.assertEqual(dm_new.control_state, "idle")
        self.assertIsNone(dm_new.last_control_request)

    def test_invalid_control_metadata_fails_closed(self):
        """非法控制元数据（如 confidence 为 bool/NaN、无时区时间、空 request_id）拒不恢复并抛出 ValueError"""
        invalids = [
            # bool 作为 confidence
            {"dialogue_mode": "emergency_intervention", "control_state": "stop_requested", "last_control_request": {"action": "stop", "status": "requested", "source": "rule", "confidence": True, "reason": "test", "request_id": "r1", "requested_at": "2026-07-31T14:30:00+08:00", "phase_at_request": "collecting"}},
            # NaN 作为 confidence
            {"dialogue_mode": "emergency_intervention", "control_state": "stop_requested", "last_control_request": {"action": "stop", "status": "requested", "source": "rule", "confidence": float("nan"), "reason": "test", "request_id": "r1", "requested_at": "2026-07-31T14:30:00+08:00", "phase_at_request": "collecting"}},
            # 空 request_id
            {"dialogue_mode": "emergency_intervention", "control_state": "stop_requested", "last_control_request": {"action": "stop", "status": "requested", "source": "rule", "confidence": 0.9, "reason": "test", "request_id": "  ", "requested_at": "2026-07-31T14:30:00+08:00", "phase_at_request": "collecting"}},
            # 无时区 ISO 时间
            {"dialogue_mode": "emergency_intervention", "control_state": "stop_requested", "last_control_request": {"action": "stop", "status": "requested", "source": "rule", "confidence": 0.9, "reason": "test", "request_id": "r1", "requested_at": "2026-07-31T14:30:00", "phase_at_request": "collecting"}},
        ]
        for inv in invalids:
            dm_test = DialogueManager(self.llm, self.kb)
            state_before = copy.deepcopy(dm_test.get_status())
            with self.assertRaises(ValueError):
                dm_test.load_snapshot(inv)
            self.assertEqual(dm_test.get_status(), state_before)

    def test_control_history_persistence_failure_rolls_back_state(self):
        """控制历史快照保存失败抛出 ControlAuditPersistenceError 且 API 返回 500"""
        app = web_backend.app.test_client()
        sid = "test_session_pers_fail"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        with patch("web_backend.save_conversation", side_effect=OSError("Disk write failure")):
            res = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_fail_01"})
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["error"], "ControlAuditPersistenceError")

    # --------------------------------------------------------------------------
    # 目标 9: 生产分类入口测试 (FakeClassifierLLM, 不依赖 Mock.called)
    # --------------------------------------------------------------------------

    def test_production_classify_interaction_path_cannot_bypass_safety_veto(self):
        """基于 FakeClassifierLLM (实现 classify_interaction) 验证生产调用路径下安全否决权不可被绕过"""
        fake_llm = FakeClassifierLLM(mode="emergency_intervention", action="stop")
        router = IntentRouter(fake_llm)

        # 输入疑问句："如何停止当前任务？"
        res = router.route("如何停止当前任务？", [], {})
        self.assertEqual(res.dialogue_mode, "knowledge_qa")
        self.assertIsNone(res.emergency_action)

        # 输入否定句："不要停止任务，水深改成500米"
        res2 = router.route("不要停止任务，水深改成500米", [], {})
        self.assertEqual(res2.dialogue_mode, "task_collection")
        self.assertIsNone(res2.emergency_action)

    def test_production_classifier_emergency_requires_deterministic_action(self):
        """基于 FakeClassifierLLM 验证：即使分类器输出 emergency_intervention，若缺失确定性紧急动作仍降级为 uncertain 澄清"""
        fake_llm = FakeClassifierLLM(mode="emergency_intervention", action=None)
        router = IntentRouter(fake_llm)

        res = router.route("进行一些不明确的紧急描述", [], {})
        self.assertEqual(res.dialogue_mode, "uncertain")
        self.assertEqual(res.query_intent, "CLARIFICATION")
        self.assertIsNone(res.emergency_action)


if __name__ == "__main__":
    unittest.main()
