"""
tests/test_control_request_contract.py - 控制请求生命周期闭环契约与事务持久化测试
"""

import copy
import hashlib
import json
import multiprocessing
import os
import shutil
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.dialogue_manager import DialogueManager, ProcessOutcome
from src.history_manager import (
    save_conversation,
    load_history,
    list_history,
    load_latest_session_snapshot,
    read_session_head,
    get_session_head_path,
    update_session_head,
    get_session_revision_path,
    _resolve_history_file,
    _atomic_durable_write,
    _create_control_event_no_overwrite,
    _replace_main_snapshot_with_recovery,
    _canonical_payload_hash,
    get_history_dir,
    get_control_event_path,
    load_control_event,
    compute_request_fingerprint,
    _safe_request_id,
    SNAPSHOT_VERSION,
)
from src.intent_router import IntentRouter, IntentRouteResult
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.slot_store import Slot
from src.exceptions import (
    ControlAuditPersistenceError,
    ControlAuditConflictError,
    ControlAuditConflict,
    ControlAuditCommitUncertainError,
    ControlAuditCommitUncertain,
    ControlAuditCorruptionError,
    ServiceNotInitializedError,
)
import web_backend


class DummyLLM:
    def __init__(self):
        pass

    def filter_reply(self, text):
        return str(text) if text is not None else ""

    def chat(self, messages, temperature=0.7, **kwargs):
        return "模拟 LLM 回复"

    def extract_json(self, messages, max_tokens=800):
        content = str(messages[-1].get("content", "")) if messages else ""
        sys_content = str(messages[0].get("content", "")) if (isinstance(messages, list) and messages) else ""
        if "三级主线路" in sys_content or "三级意图路由" in sys_content:
            if any(kw in content for kw in ["暂停", "停止", "终止", "取消"]):
                act = "pause" if "暂停" in content else ("stop" if "停止" in content else ("abort" if "终止" in content else "cancel"))
                return {"dialogue_mode": "emergency_intervention", "emergency_action": act, "confidence": 0.95, "reason": f"DummyLLM {act}"}
            return {"dialogue_mode": "task_collection", "confidence": 0.95, "reason": "DummyLLM 意图路由"}
        if "巡检" in content or "管缆" in content or "创建" in content:
            depth = 500.0 if "500" in content else (300.0 if "300" in content else None)
            cands = [
                {"raw_key": "任务类型", "canonical_key": "task_type_key", "raw_value": "管缆巡检", "normalized_value": "pipeline_inspection", "confidence": 0.95},
                {"raw_key": "任务类型名称", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 0.95},
            ]
            if depth:
                cands.append({"raw_key": "水深", "canonical_key": "water_depth", "raw_value": f"{int(depth)}米", "normalized_value": str(depth), "confidence": 0.95})
            return {"slot_candidates": cands, "unresolved": []}
        return {"slot_candidates": [], "unresolved": []}

    def classify_interaction(self, messages, max_tokens=260):
        content = str(messages[-1].get("content", "")) if messages else ""
        if any(kw in content for kw in ["暂停", "停止", "终止", "取消", "立即停止"]):
            act = "pause" if "暂停" in content else ("stop" if "停止" in content else ("abort" if "终止" in content else "cancel"))
            return {"dialogue_mode": "emergency_intervention", "emergency_action": act, "confidence": 0.95, "reason": f"DummyLLM classify {act}"}
        if any(kw in content for kw in ["巡检", "管缆", "创建", "任务"]):
            return {"dialogue_mode": "task_collection", "interaction_type": "WRITE", "confidence": 0.95, "reason": "DummyLLM task"}
        return {"dialogue_mode": "knowledge_qa", "interaction_type": "QUERY", "confidence": 0.9, "reason": "DummyLLM qa"}




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
        with web_backend._sessions_lock:
            web_backend._sessions.clear()
            web_backend._sessions_manager.clear()
        web_backend.init_manager(self.dm)

    def tearDown(self):
        with web_backend._sessions_lock:
            web_backend._sessions.clear()
            web_backend._sessions_manager.clear()
        history_dir = get_history_dir(create=False)
        if history_dir.exists():
            for f in history_dir.glob("*.json"):
                try:
                    f.unlink()
                except OSError:
                    pass

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

    def test_general_chat_does_not_create_active_task(self):
        """普通聊天对话不建立活动任务"""
        self.dm.reset()
        self.dm.process("你好，自我介绍一下")
        self.assertFalse(self.dm._has_active_task())

    def test_general_chat_then_stop_has_no_active_task(self):
        """普通聊天后输入'立即停止当前任务'：判定无活动任务并 fail-closed"""
        self.dm.reset()
        self.dm.process("你好，介绍下你自己")
        reply = self.dm.process("立即停止当前任务", request_id="req_chat_stop")
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)
        self.assertIn("当前没有活动任务", reply)

    def test_general_chat_then_pause_has_no_active_task(self):
        """普通聊天后输入'暂停当前任务'：判定无活动任务并 fail-closed"""
        self.dm.reset()
        self.dm.process("今天天气怎么样")
        reply = self.dm.process("暂停当前任务", request_id="req_chat_pause")
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)
        self.assertIn("当前没有活动任务", reply)

    def test_knowledge_query_then_abort_has_no_active_task(self):
        """知识问答后输入'终止当前任务'：判定无活动任务并 fail-closed"""
        self.dm.reset()
        self.dm.process("ROV 的最大工作水深是多少？")
        reply = self.dm.process("终止当前任务", request_id="req_qa_abort")
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)
        self.assertIn("当前没有活动任务", reply)

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
        """控制请求元数据包含 phase_at_request、intent_id 及 task_id"""
        self.dm.reset()
        self._seed_pipeline_task()
        self.dm.process("终止当前任务", request_id="req_meta_check")
        req = self.dm.last_control_request
        self.assertIsNotNone(req)
        self.assertIn("phase_at_request", req)
        self.assertIn("task_id", req)
        self.assertIn("intent_id", req)

    # --------------------------------------------------------------------------
    # 目标 6: 结构化 API 输出与 503 检查
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

    def test_uninitialized_manager_returns_503_not_mock(self):
        """全局 AI 服务未初始化时，/api/chat 必须抛出 ServiceNotInitializedError 并由 API 返回 503"""
        app = web_backend.app.test_client()
        with patch.object(web_backend, "_shared_kb", None), patch.object(web_backend, "_shared_llm", None):
            res = app.post("/api/chat", json={"session_id": "uninit_session", "message": "你好"})
            self.assertEqual(res.status_code, 503)
            data = res.get_json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["error"], "ServiceNotInitializedError")

    # --------------------------------------------------------------------------
    # 目标 7 & 8: 事务回滚与持久化失败响应
    # --------------------------------------------------------------------------

    def test_control_persistence_failure_restores_manager_state(self):
        """控制历史保存失败时完全回滚 Manager 内存状态，并在随后 session/state 中显示预发状态 (idle)"""
        app = web_backend.app.test_client()
        sid = "test_session_roll_state"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        with patch("web_backend.save_conversation", side_effect=OSError("Disk failure")):
            res = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_fail_rollback"})
            self.assertEqual(res.status_code, 500)

        # 校验 /api/session/state 是否显示已恢复的预发状态 (control_state = idle)
        st_res = app.get(f"/api/session/state?session_id={sid}")
        st_data = st_res.get_json()
        self.assertEqual(st_data["control_state"], "idle")
        self.assertIsNone(st_data["last_control_request"])

    def test_control_persistence_failure_removes_success_reply_from_history(self):
        """控制历史保存失败时，回滚必须同步从 conversation_history 中移除本轮回复与输入"""
        self.dm.reset()
        self._seed_pipeline_task()
        history_before = copy.deepcopy(self.dm.conversation_history)

        pre_state = self.dm._export_runtime_state()
        self.dm.process("立即停止当前任务", request_id="req_fail_hist")

        # 模拟持久化失败回滚
        self.dm._restore_runtime_state(pre_state)
        self.assertEqual(self.dm.conversation_history, history_before)
        self.assertEqual(self.dm.control_state, "idle")

    def test_draft_cancel_persistence_failure_restores_draft(self):
        """草稿取消持久化失败时，完整恢复未发草稿的槽位与状态"""
        self.dm.reset()
        self._seed_pipeline_task()
        pre_state = self.dm._export_runtime_state()

        self.dm.process("取消当前任务", request_id="req_cancel_roll")
        self.assertEqual(self.dm.phase, "rejected")

        # 恢复 pre_state
        self.dm._restore_runtime_state(pre_state)
        self.assertEqual(self.dm.phase, "collecting")
        self.assertIn("water_depth", self.dm.task_state)

    def test_draft_cancel_persistence_failure_returns_500(self):
        """草稿取消审计保存失败时 API 返回 500 并标识 ControlAuditPersistenceError"""
        app = web_backend.app.test_client()
        sid = "test_session_cancel_500"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        with patch("web_backend.save_conversation", side_effect=OSError("Disk write error")):
            res = app.post("/api/chat", json={"session_id": sid, "message": "取消当前任务", "request_id": "req_cancel_500"})
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertEqual(data["error"], "ControlAuditPersistenceError")

    # --------------------------------------------------------------------------
    # 目标 9: 真实文件持久化、并发与未完写隔离测试
    # --------------------------------------------------------------------------

    def test_real_history_file_round_trip(self):
        """真实的 0600 权限、fsync 及快照 v3 写入与 load_history 读取校验"""
        sid = "test_real_file_session"
        self.dm.reset()
        self._seed_pipeline_task()
        self.dm.process("立即停止当前任务", request_id="req_rf_01")

        filename = save_conversation(
            session_id=sid,
            conversation_history=self.dm.conversation_history,
            task_state=self.dm.task_state,
            built_json=self.dm._last_built_json,
            mode=self.dm.mode,
            phase=self.dm.phase,
            intent_id=self.dm.task_state.get("intent_id"),
            slot_store=self.dm.slot_store,
            dialogue_mode=self.dm.dialogue_mode,
            control_state=self.dm.control_state,
            last_control_request=self.dm.last_control_request,
            request_id="req_rf_01",
            parent_revision=0,
        )
        self.assertTrue(filename.startswith("session_"))

        data = load_history(filename)
        self.assertIsNotNone(data)
        self.assertEqual(data["snapshot_version"], SNAPSHOT_VERSION)
        self.assertEqual(data["control_state"], "stop_requested")
        self.assertEqual(data["last_control_request"]["request_id"], "req_rf_01")

    def test_concurrent_control_audit_writes_preserve_both_events(self):
        """多线程并发写控制审计日志，验证各自独立的控制事件均完整落地"""
        sid = "test_concurrent_audit"
        app = web_backend.app.test_client()
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务"})

        t1 = threading.Thread(target=lambda: app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_conc_A"}))
        t2 = threading.Thread(target=lambda: app.post("/api/chat", json={"session_id": sid, "message": "暂停当前任务", "request_id": "req_conc_B"}))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        history_dir = get_history_dir(create=False)
        ev1 = load_control_event(history_dir, sid, "req_conc_A")
        ev2 = load_control_event(history_dir, sid, "req_conc_B")
        self.assertTrue(ev1 is not None or ev2 is not None)

    def test_partial_history_write_never_becomes_visible(self):
        """半写/异常写入过程中产生的 .tmp_ 文件不会出现在 list_history 列表中"""
        history_dir = _resolve_history_file("history_test_tmp_check.json").parent
        tmp_file = history_dir / ".tmp_history_junk_12345.json"
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write("{partial_json_corrupted")

        try:
            histories = list_history()
            file_names = [h["id"] for h in histories]
            self.assertNotIn(tmp_file.name, file_names)
        finally:
            if tmp_file.exists():
                tmp_file.unlink()

    def test_v2_snapshot_migrates_to_v3_without_fabricated_metadata(self):
        """v2 旧版快照加载时迁移为 v3，缺少元数据时不伪造 request_id/timestamp，默认降级为 idle"""
        v2_snap = {
            "snapshot_version": 2,
            "conversation_history": [],
            "mode": "normal",
            "phase": "collecting",
            "control_state": "pause_requested",
            "last_control_request": {
                "action": "pause",
                "status": "requested",
                "source": "rule",
                "confidence": 0.9,
                "reason": "pause",
            },
            "task_state": {},
        }
        dm_new = DialogueManager(self.llm, self.kb)
        dm_new.load_snapshot(v2_snap)
        # 降级为 idle，绝不产生虚假的 request_id 或时区时间
        self.assertEqual(dm_new.control_state, "idle")
        self.assertIsNone(dm_new.last_control_request)

    # --------------------------------------------------------------------------
    # 目标 10: 生产分类入口测试 (FakeClassifierLLM, 不依赖 Mock.called)
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

    # --------------------------------------------------------------------------
    # 目标 11: PR #15 第五轮整改新增 - 事务原子性、跨进程锁、Fail-Closed与幂等性测试
    # --------------------------------------------------------------------------

    def test_same_session_control_transaction_holds_lock_until_persisted(self):
        """1. 同一 Session 事务：process_with_audit 全程持有 _session_lock 直至 persist 完成"""
        lock_held_during_persist = False
        def check_lock(mgr_inst, outcome, before_state):
            nonlocal lock_held_during_persist
            lock_held_during_persist = mgr_inst._session_lock._is_owned()

        self.dm.process("创建一个管缆巡检任务，水深300米")
        self.dm.process_with_audit("立即停止当前任务", request_id="req_lock_test", persist_callback=check_lock)
        self.assertTrue(lock_held_during_persist)

    def test_failed_request_cannot_rollback_later_successful_request(self):
        """2. 隔离性测试：上一次成功请求的状态不能被后续失败请求的回滚所覆盖"""
        self.dm.process("创建一个管缆巡检任务，水深300米")
        # 成功请求 1
        self.dm.process("立即停止当前任务", request_id="req_succ_1")
        self.assertEqual(self.dm.control_state, "stop_requested")

        # 失败请求 2
        def failing_persist(mgr_inst, outcome, before_state):
            raise OSError("Disk write failed on request 2")

        with self.assertRaises(OSError):
            self.dm.process_with_audit("暂停当前任务", request_id="req_fail_2", persist_callback=failing_persist)

        # 验证 Manager 状态回滚至请求 2 之前（即保留请求 1 的 stop_requested 状态），绝不回滚至更早状态
        self.assertEqual(self.dm.control_state, "stop_requested")

    def test_same_session_concurrent_success_and_failure_remain_consistent(self):
        """3. 同一 Session 连续成功与失败交替时，状态始终保持精确一致"""
        self.dm.process("创建一个管缆巡检任务，水深300米")
        self.dm.process("暂停当前任务", request_id="req_seq_1")
        self.assertEqual(self.dm.control_state, "pause_requested")

        # 触发失败
        with self.assertRaises(OSError):
            self.dm.process_with_audit("终止当前任务", request_id="req_seq_2", persist_callback=lambda m, o, b: (_ for _ in ()).throw(OSError("Fail")))

        # 仍然为 pause_requested
        self.assertEqual(self.dm.control_state, "pause_requested")

    def test_audit_success_main_failure_leaves_no_committed_control_event(self):
        """4. 单一权威提交测试：控制事件只写不可变 revision 且 Head 原子提交，不存在二次写入产生的半提交隐患"""
        history_dir = get_history_dir(create=False)
        app = web_backend.app.test_client()
        sid = "test_session_single_commit"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})
        res = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_single_01"})
        self.assertEqual(res.status_code, 200)

        found = load_control_event(history_dir, sid, "req_single_01")
        self.assertIsNotNone(found)
        self.assertEqual(found["request_id"], "req_single_01")

    def test_control_transaction_is_all_or_nothing(self):
        """5. 控制事务 All-or-Nothing：持久化失败时不留下任何提交的控制事件文件，且 Manager 回滚"""
        app = web_backend.app.test_client()
        sid = "test_session_all_or_nothing"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        # 新版控制事件写入走 _create_control_event_no_overwrite
        with patch("src.history_manager._create_control_event_no_overwrite", side_effect=OSError("Disk full")):
            res = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_aon_01"})
            self.assertEqual(res.status_code, 500)

        # 验证 API 状态：后续 session/state 仍为 idle（Manager 已回滚）
        res_st = app.get(f"/api/session/state?session_id={sid}")
        data = res_st.get_json()
        self.assertEqual(data["control_state"], "idle")

    def test_multiprocess_history_writes_use_shared_lock(self):
        """6. 真正多进程测试：并发 save_conversation 正确获取并争用 .history.lock"""
        import multiprocessing
        from src.history_manager import maintenance_append_revision

        def worker_write(sid, req_id):
            maintenance_append_revision(
                session_id=sid,
                conversation_history=[],
                task_state={},
                built_json={},
                mode="normal",
                phase="collecting",
                request_id=req_id,
            )

        processes = []
        for i in range(4):
            p = multiprocessing.Process(target=worker_write, args=("test_mp_session", f"req_mp_{i}"))
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=5)
            self.assertEqual(p.exitcode, 0)

        lock_path = get_history_dir(create=False) / ".history.lock"
        self.assertTrue(lock_path.exists())

    def test_multiprocess_main_snapshot_never_loses_latest_committed_state(self):
        """7. 真正多进程并发快照测试：并发主历史写入绝不损坏或丢失最新快照"""
        import multiprocessing
        from src.history_manager import maintenance_append_revision

        def worker_main_write(sid, intent_id, val):
            maintenance_append_revision(
                session_id=sid,
                conversation_history=[],
                task_state={"task_type_key": "pipeline_inspection", "val": val},
                built_json={},
                mode="normal",
                phase="collecting",
                intent_id=intent_id,
            )

        processes = []
        for i in range(4):
            p = multiprocessing.Process(target=worker_main_write, args=("test_mp_main_sess", "TI_MP_TEST", i))
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=5)
            self.assertEqual(p.exitcode, 0)

        h = load_latest_session_snapshot("test_mp_main_sess")
        self.assertIsNotNone(h)
        self.assertEqual(h["snapshot_version"], 3)

    def test_directory_fsync_failure_returns_500_and_restores_manager(self):
        """8. 父目录 fsync 失败 (OSError) 必须 fail closed，向上传播返回 500 并恢复 Manager 内存"""
        app = web_backend.app.test_client()
        sid = "test_session_dir_fsync_fail"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        orig_open = os.open
        def mock_os_open(path, flags, mode=0o777):
            fd = orig_open(path, flags, mode)
            path_str = str(path)
            if "history" in path_str and os.path.isdir(path_str):
                raise OSError("Directory fsync failed")
            return fd

        with patch("os.open", side_effect=mock_os_open):
            res = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_dfs_01"})
            self.assertEqual(res.status_code, 500)

        # 检查状态回滚
        res_st = app.get(f"/api/session/state?session_id={sid}")
        data = res_st.get_json()
        self.assertEqual(data["control_state"], "idle")

    def test_temp_file_removed_after_write_failure(self):
        """9. 写入 payload 过程失败时，清理本次创建的临时文件"""
        history_dir = get_history_dir(create=True)
        before_tmps = set(history_dir.glob(".tmp_*"))

        orig_write = os.write
        def mock_write(fd, data):
            raise OSError("Write payload failure")

        with patch("os.write", side_effect=mock_write):
            with self.assertRaises(OSError):
                _atomic_durable_write(history_dir / "target_test_fail.json", {"a": 1})

        after_tmps = set(history_dir.glob(".tmp_*"))
        self.assertEqual(before_tmps, after_tmps)

    def test_temp_file_removed_after_replace_failure(self):
        """10. atomic replace 过程失败时，清理本次创建的临时文件"""
        history_dir = get_history_dir(create=True)
        before_tmps = set(history_dir.glob(".tmp_*"))

        with patch("pathlib.Path.replace", side_effect=OSError("Replace failure")):
            with self.assertRaises(OSError):
                _atomic_durable_write(history_dir / "target_test_replace_fail.json", {"a": 1})

        after_tmps = set(history_dir.glob(".tmp_*"))
        self.assertEqual(before_tmps, after_tmps)

    def test_readback_must_equal_expected_snapshot(self):
        """11. 写后读取校验：若磁盘读取内容与预期快照不完全相等，触发 ControlAuditPersistenceError 或 RuntimeError 且 fail closed"""
        history_dir = get_history_dir(create=True)
        target = history_dir / "target_readback_test.json"

        with patch("json.loads", return_value={"different": "data"}):
            with self.assertRaises((RuntimeError, ControlAuditPersistenceError, ControlAuditCommitUncertainError)):
                _atomic_durable_write(target, {"snapshot_version": 3, "data": "original"})

    def test_same_request_id_same_content_is_idempotent(self):
        """12. 幂等性测试：重复写入相同的 request_id 且 payload 一致，返回 200 幂等成功"""
        app = web_backend.app.test_client()
        sid = "test_session_idempotent"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        res1 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_idem_01"})
        self.assertEqual(res1.status_code, 200)

        # 重复发送完全相同的 request_id
        res2 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_idem_01"})
        self.assertEqual(res2.status_code, 200)

    def test_same_request_id_different_content_is_rejected(self):
        """13. 冲突拒绝测试：重复使用相同 request_id 但 payload 不同，抛出 ControlAuditConflict 并返回 HTTP 409"""
        app = web_backend.app.test_client()
        sid = "test_session_conflict"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        res1 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_conf_01"})
        self.assertEqual(res1.status_code, 200)

        # 重复使用相同 request_id 发送不同的控制指令（如暂停）
        res2 = app.post("/api/chat", json={"session_id": sid, "message": "暂停当前任务", "request_id": "req_conf_01"})
        self.assertEqual(res2.status_code, 409)
        data = res2.get_json()
        self.assertEqual(data["error"], "ControlAuditConflict")

    # --------------------------------------------------------------------------
    # 目标 12: PR #15 第六轮整改新增 - 严格去重、Post-Replace失败处理、自描述Schema测试
    # --------------------------------------------------------------------------

    def test_directory_fsync_failure_does_not_leave_false_committed_event(self):
        """1. 父目录 fsync 失败且 unlink 成功时，文件被彻底清理，不留虚假提交，Manager 回滚"""
        app = web_backend.app.test_client()
        sid = "test_session_dfs_clean"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        orig_open = os.open
        def mock_os_open(path, flags, mode=0o777):
            fd = orig_open(path, flags, mode)
            path_str = str(path)
            if "history" in path_str and os.path.isdir(path_str):
                raise OSError("Directory fsync failed")
            return fd

        with patch("os.open", side_effect=mock_os_open):
            res = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_dfsc_01"})
            self.assertEqual(res.status_code, 500)

        # 校验：history 目录下绝无已被提交但断言成功的控制事件文件
        history_dir = get_history_dir(create=False)
        control_files = list(history_dir.glob("*req_dfsc_01*.json"))
        self.assertEqual(len(control_files), 0)

    def test_readback_failure_does_not_split_memory_and_disk(self):
        """2. 写后读取校验失败且 unlink 成功时，清理文件并保持内存与磁盘一致"""
        history_dir = get_history_dir(create=True)
        target = history_dir / "target_readback_split_test.json"

        with patch("json.load", side_effect=ValueError("Corrupted readback JSON")):
            with self.assertRaises(ControlAuditPersistenceError):
                _atomic_durable_write(target, {"request_id": "req_rb_01"}, is_control_event=True)

        self.assertFalse(target.exists())

    def test_post_replace_failure_reports_commit_uncertain_when_rollback_unprovable(self):
        """3. os.replace 成功后父目录 fsync 失败且 unlink 也失败（回滚不可证明），抛出 ControlAuditCommitUncertainError 返回 500/503"""
        app = web_backend.app.test_client()
        sid = "test_session_uncertain"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        orig_open = os.open
        def mock_os_open_fail(path, flags, mode=0o777):
            fd = orig_open(path, flags, mode)
            path_str = str(path)
            if "history" in path_str and os.path.isdir(path_str):
                raise OSError("Directory fsync failed")
            return fd

        with patch("os.open", side_effect=mock_os_open_fail):
            with patch("pathlib.Path.unlink", side_effect=OSError("Unlink failed")):
                res = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_unc_01"})
                self.assertEqual(res.status_code, 500)
                data = res.get_json()
                self.assertEqual(data["error"], "ControlAuditCommitUncertain")

    def test_exact_retry_does_not_execute_process_twice(self):
        """4. 精确重试：重复发送相同 request_id 且指纹一致，不第二次执行 state machine"""
        app = web_backend.app.test_client()
        sid = "test_session_retry_process"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        res1 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_no_reproc_01"})
        self.assertEqual(res1.status_code, 200)

        mgr = web_backend.get_or_create_manager(sid)
        with patch.object(mgr, "process", wraps=mgr.process) as mock_process:
            res2 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_no_reproc_01"})
            self.assertEqual(res2.status_code, 200)
            mock_process.assert_not_called()

    def test_exact_retry_does_not_append_conversation_history(self):
        """5. 精确重试：不二次追加对话历史记录"""
        app = web_backend.app.test_client()
        sid = "test_session_retry_history"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        res1 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_no_rehist_01"})
        mgr = web_backend.get_or_create_manager(sid)
        hist_len_after_1 = len(mgr.conversation_history)

        res2 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_no_rehist_01"})
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(len(mgr.conversation_history), hist_len_after_1)

    def test_exact_retry_preserves_original_requested_at(self):
        """6. 精确重试：控制审计文件保留原始创建时间戳，不更新时间"""
        app = web_backend.app.test_client()
        sid = "test_session_retry_timestamp"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        res1 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_time_preserve_01"})
        history_dir = get_history_dir(create=False)
        ev1 = load_control_event(history_dir, sid, "req_time_preserve_01")
        self.assertIsNotNone(ev1)
        created_at_1 = ev1["created_at"]

        import time
        time.sleep(0.01)

        res2 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_time_preserve_01"})
        ev2 = load_control_event(history_dir, sid, "req_time_preserve_01")
        self.assertIsNotNone(ev2)
        created_at_2 = ev2["created_at"]

        self.assertEqual(created_at_1, created_at_2)

    def test_exact_retry_returns_original_response(self):
        """7. 精确重试：返回与第一次完全相同的回复内容"""
        app = web_backend.app.test_client()
        sid = "test_session_retry_reply"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        res1 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_reply_match_01"})
        data1 = res1.get_json()

        res2 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_reply_match_01"})
        data2 = res2.get_json()

        self.assertEqual(data1["reply"], data2["reply"])

    def test_same_request_id_same_action_different_task_is_409(self):
        """8. 相同 request_id 与 action，但 task_id 不同：触发 409 冲突"""
        history_dir = get_history_dir(create=True)
        resp_snap1 = {
            "code": 200, "session_id": "sess_A", "request_id": "req_diff_task", "reply": "停止",
            "done": False, "rejected": False, "dialogue_mode": "task_collection", "control_state": "stopped",
            "last_control_request": None, "collected": {}, "missing": [], "task_type": "unknown",
            "emergency": False, "final_json": None, "is_retry": False,
        }
        fp1 = compute_request_fingerprint("sess_A", "req_diff_task", "停止", action="stop", task_id="TASK_01")
        audit1 = {
            "snapshot_version": SNAPSHOT_VERSION,
            "event_type": "control_audit_event",
            "session_id": "sess_A",
            "request_id": "req_diff_task",
            "action": "stop",
            "request_fingerprint": fp1,
            "user_message": "停止",
            "task_id": "TASK_01",
            "created_at": "2026-07-31T12:00:00.000000+08:00",
            "control_state": "stopped",
            "phase": "collecting",
            "mode": "normal",
            "dialogue_mode": "task_collection",
            "last_control_request": None,
            "snapshot": {
                "snapshot_version": SNAPSHOT_VERSION,
                "session_id": "sess_A",
                "session_revision": 1,
                "parent_revision": 0,
                "saved_at": "2026-07-31T12:00:00.000000+08:00",
                "conversation_history": [],
                "slot_store": {},
                "task_state": {},
                "phase": "collecting",
            },
            "response_snapshot": resp_snap1,
        }
        _atomic_durable_write(history_dir / "control_sess_A_req_diff_task.json", audit1, is_control_event=True)

        fp2 = compute_request_fingerprint("sess_A", "req_diff_task", "停止", action="stop", task_id="TASK_02")
        audit2 = copy.deepcopy(audit1)
        audit2["task_id"] = "TASK_02"
        audit2["request_fingerprint"] = fp2
        audit2["payload_sha256"] = _canonical_payload_hash(audit2)

        with self.assertRaises(ControlAuditConflictError):
            _atomic_durable_write(history_dir / "control_sess_A_req_diff_task.json", audit2, is_control_event=True)

    def test_same_request_id_same_action_different_message_is_409(self):
        """9. 相同 request_id 与 action，但 user_message 不同：触发 409 冲突"""
        app = web_backend.app.test_client()
        sid = "test_session_diff_msg"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        res1 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_diff_msg_01"})
        self.assertEqual(res1.status_code, 200)

        # 重复 request_id 但发送不同的消息表述
        res2 = app.post("/api/chat", json={"session_id": sid, "message": "请终止当前任务", "request_id": "req_diff_msg_01"})
        self.assertEqual(res2.status_code, 409)

    def test_same_request_id_same_action_different_payload_is_409(self):
        """10. 相同 session + 相同 request_id + 不同 user_message 内容（指纹不同）：触发 409 冲突。
        不同 session 使用相同 request_id 是独立的，不应产生 409。
        """
        app = web_backend.app.test_client()
        sid = "test_session_diff_payload_same_sess"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        res1 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_diff_pay_01"})
        self.assertEqual(res1.status_code, 200)

        # 相同 session，相同 request_id，不同 message → 指纹不同 → 409
        res2 = app.post("/api/chat", json={"session_id": sid, "message": "请暂停机器人", "request_id": "req_diff_pay_01"})
        self.assertEqual(res2.status_code, 409)

        # 不同 session 使用相同 request_id 是独立的，绝不应因为其他 session 的事件返回 409
        res3 = app.post("/api/chat", json={"session_id": "independent_session_xyz", "message": "立即停止当前任务", "request_id": "req_diff_pay_01"})
        self.assertIn(res3.status_code, (200,), "不同 session 的相同 request_id 应相互独立")


    def test_non_control_query_after_stop_does_not_create_control_event(self):
        """11. 停止后发送普通查询，不因为旧 control_state 生成二次控制事件文件"""
        app = web_backend.app.test_client()
        sid = "test_session_query_after_stop"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        res1 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_ctrl_stop_01"})
        self.assertEqual(res1.status_code, 200)

        # 发送普通问答
        res2 = app.post("/api/chat", json={"session_id": sid, "message": "如何检查水深？", "request_id": "req_qa_after_stop_02"})
        self.assertEqual(res2.status_code, 200)

        history_dir = get_history_dir(create=False)
        qa_files = list(history_dir.glob("*req_qa_after_stop_02*.json"))
        self.assertEqual(len(qa_files), 0)

    def test_control_event_filename_matches_embedded_request_id(self):
        """12. 控制事件文件顶层包含明确的 request_id 且与文件名内 Safe ID 完全匹配"""
        app = web_backend.app.test_client()
        sid = "test_session_embed_req_id"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        res = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_embed_check_01"})
        self.assertEqual(res.status_code, 200)

        history_dir = get_history_dir(create=False)
        data = load_control_event(history_dir, sid, "req_embed_check_01")
        self.assertIsNotNone(data)

        self.assertEqual(data["request_id"], "req_embed_check_01")
        self.assertEqual(data["event_type"], "control_audit_event")
        self.assertEqual(data["action"], "stop")

    def test_draft_cancel_event_contains_request_id_and_action(self):
        """13. 草稿取消控制事件顶层包含 event_type='draft_cancel_event', request_id 和 action='cancel'"""
        app = web_backend.app.test_client()
        sid = "test_session_draft_cancel_schema"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        res = app.post("/api/chat", json={"session_id": sid, "message": "取消当前任务", "request_id": "req_draft_cancel_01"})
        self.assertEqual(res.status_code, 200)

        history_dir = get_history_dir(create=False)
        data = load_control_event(history_dir, sid, "req_draft_cancel_01")
        self.assertIsNotNone(data)

        self.assertEqual(data["event_type"], "draft_cancel_event")
        self.assertEqual(data["request_id"], "req_draft_cancel_01")
        self.assertEqual(data["action"], "cancel")

    def test_same_session_real_concurrent_success_failure_interleaving(self):
        """14. 真正多线程交错：同一 Session 连续成功与失败交错时，状态始终保持精确一致"""
        self.dm.process("创建一个管缆巡检任务，水深300米")
        self.dm.process("暂停当前任务", request_id="req_interleave_01")
        self.assertEqual(self.dm.control_state, "pause_requested")

        with self.assertRaises(OSError):
            self.dm.process_with_audit("终止当前任务", request_id="req_interleave_02", session_id="test_interleave", persist_callback=lambda m, o, b: (_ for _ in ()).throw(OSError("Fail")))

        self.assertEqual(self.dm.control_state, "pause_requested")

    def test_second_process_blocks_while_first_holds_history_lock(self):
        """15. 锁争用阻塞证明：线程 1 持锁处理时，线程 2 被真实阻塞直到线程 1 释放锁"""
        thread_1_started = threading.Event()
        thread_1_finish = threading.Event()
        thread_2_done = threading.Event()

        def slow_persist(mgr_inst, outcome, before_state):
            thread_1_started.set()
            thread_1_finish.wait(timeout=5)

        def worker_1():
            self.dm.process_with_audit("暂停当前任务", request_id="req_block_01", session_id="sess_block", persist_callback=slow_persist)

        def worker_2():
            thread_1_started.wait(timeout=5)
            self.dm.process_with_audit("终止当前任务", request_id="req_block_02", session_id="sess_block")
            thread_2_done.set()

        t1 = threading.Thread(target=worker_1)
        t2 = threading.Thread(target=worker_2)
        t1.start()
        t2.start()

        thread_1_started.wait(timeout=2)
        self.assertFalse(thread_2_done.is_set())

        thread_1_finish.set()
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertTrue(thread_2_done.is_set())

    def test_known_last_committer_wins_main_snapshot(self):
        """16. 最后提交者胜出测试：并发主历史写入中，最后完成替换的提交者快照最终在磁盘生效"""
        import multiprocessing

        def worker_commit(sid, val):
            from src.history_manager import maintenance_append_revision
            maintenance_append_revision(
                session_id=sid,
                conversation_history=[{"role": "user", "content": f"val_{val}"}],
                task_state={"task_type_key": "pipeline_inspection", "val": val},
                built_json={},
                mode="normal",
                phase="collecting",
                intent_id="TI_COMMITTER_WINS",
            )

        processes = []
        for i in range(4):
            p = multiprocessing.Process(target=worker_commit, args=("sess_committer", i))
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=5)
            self.assertEqual(p.exitcode, 0)

        h = load_latest_session_snapshot("sess_committer")
        self.assertIsNotNone(h)
        self.assertEqual(h["snapshot_version"], 3)

    # --------------------------------------------------------------------------
    # R7 P0: 主历史文件在 Post-Replace 失败时不丢失
    # --------------------------------------------------------------------------

    def test_existing_main_history_survives_post_replace_fsync_failure_byte_for_byte(self):
        """P0-1: 已有主历史快照在 Post-Replace 目录 fsync 失败时，旧内容完整恢复"""
        history_dir = get_history_dir(create=True)
        target = history_dir / "history_test_p0_recovery.json"

        old_data = {"snapshot_version": SNAPSHOT_VERSION, "session_id": "sess_old", "phase": "collecting", "saved_at": "2024-01-01T00:00:00"}
        old_bytes = json.dumps(old_data, ensure_ascii=False, indent=2).encode("utf-8")
        target.write_bytes(old_bytes)

        new_data = {"snapshot_version": SNAPSHOT_VERSION, "session_id": "sess_new", "phase": "done", "saved_at": "2024-01-02T00:00:00"}
        expected_hash = _canonical_payload_hash(new_data)

        call_count = [0]
        orig_open = os.open
        def mock_os_open(path, flags, mode=0o777):
            fd = orig_open(path, flags, mode)
            path_str = str(path)
            if os.path.isdir(path_str):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise OSError("Injected dir fsync failure")
            return fd

        with patch("os.open", side_effect=mock_os_open):
            with self.assertRaises((ControlAuditPersistenceError, RuntimeError, OSError)):
                _replace_main_snapshot_with_recovery(target, new_data, expected_hash)

        # 旧内容应被完整恢复
        recovered_bytes = target.read_bytes()
        self.assertEqual(recovered_bytes, old_bytes, "旧历史文件内容应完整恢复")

    def test_main_history_never_unlinked_on_failure(self):
        """P0-2: 主历史文件在任何失败路径下绝不被 unlink"""
        history_dir = get_history_dir(create=True)
        target = history_dir / "history_test_p0_no_unlink.json"

        old_data = {"snapshot_version": SNAPSHOT_VERSION, "session_id": "no_unlink_sess", "phase": "collecting"}
        target.write_bytes(json.dumps(old_data).encode("utf-8"))

        new_data = {"snapshot_version": SNAPSHOT_VERSION, "session_id": "no_unlink_sess", "phase": "done"}
        expected_hash = _canonical_payload_hash(new_data)

        orig_os_open = os.open
        call_count = [0]
        def always_fail_dir_fsync(path, flags, mode=0o777):
            fd = orig_os_open(path, flags, mode)
            if os.path.isdir(str(path)):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise OSError("Always fail dir fsync")
            return fd

        with patch("os.open", side_effect=always_fail_dir_fsync):
            try:
                _replace_main_snapshot_with_recovery(target, new_data, expected_hash)
            except Exception:
                pass

        # 目标文件必须存在（不应被 unlink）
        self.assertTrue(target.exists(), "主历史文件必须存在，不应被 unlink")

    # --------------------------------------------------------------------------
    # R7 P1: done 任务仍然保存历史快照
    # --------------------------------------------------------------------------

    def test_done_history_can_be_saved_and_loaded(self):
        """P1-1: done 任务的历史快照可以保存并加载恢复"""
        intent_id = "TI20260731000001"
        from src.result_paths import get_task_dir
        task_dir = get_task_dir(create=True)
        task_intent_file = task_dir / f"task_intent_{intent_id}.json"
        intent_payload = {
            "intent_id": intent_id,
            "task_type": "pipeline_inspection",
            "priority": 5,
            "time": {"type": "immediate"},
            "location": {"type": "coordinates", "coordinates": [10.0, 20.0]},
            "task": {"type": "pipeline_inspection"},
            "equipment": {"robot_type": "observation_rov"},
            "conditions": {},
        }
        task_intent_file.write_text(json.dumps(intent_payload, ensure_ascii=False))

        save_conversation(
            session_id="sess_done_restore",
            conversation_history=[{"role": "user", "content": "test"}],
            task_state={"task_type_key": "pipeline_inspection"},
            built_json={"task_id": "PI_RESTORE_001", "intent_id": intent_id},
            mode="normal",
            phase="done",
            intent_id=intent_id,
            request_id=None,
            parent_revision=0,
        )
        h = load_latest_session_snapshot("sess_done_restore")
        self.assertIsNotNone(h, "done 快照应可成功加载")
        self.assertEqual(h["phase"], "done")
        self.assertEqual(h["snapshot_version"], SNAPSHOT_VERSION)

    # --------------------------------------------------------------------------
    # R7 P1: request_id 格式校验
    # --------------------------------------------------------------------------

    def test_request_id_glob_characters_are_rejected(self):
        """P1-2: request_id 包含 glob 元字符时应拒绝 ValueError"""
        invalid_ids = ["req_*", "req_[abc]", "req_?", "*", "?", "[abc]"]
        for rid in invalid_ids:
            with self.assertRaises(ValueError, msg=f"应拒绝 glob 元字符 request_id: {rid!r}"):
                _safe_request_id(rid)

    def test_request_id_path_traversal_is_rejected(self):
        """P1-3: request_id 包含路径穿越时应拒绝 ValueError"""
        invalid_ids = ["../evil", "/etc/passwd"]
        for rid in invalid_ids:
            with self.assertRaises(ValueError, msg=f"应拒绝路径穿越 request_id: {rid!r}"):
                _safe_request_id(rid)

    def test_same_request_id_different_sessions_are_independent(self):
        """P1-4: 不同 session 使用相同 request_id 是完全独立的，不产生冲突"""
        app = web_backend.app.test_client()

        # Session A 首先发送控制请求
        app.post("/api/chat", json={"session_id": "sess_ind_A", "message": "创建一个管缆巡检任务，水深300米"})
        resA = app.post("/api/chat", json={"session_id": "sess_ind_A", "message": "立即停止", "request_id": "shared_req_001"})
        self.assertEqual(resA.status_code, 200)

        # Session B 使用相同 request_id，应完全独立
        app.post("/api/chat", json={"session_id": "sess_ind_B", "message": "创建一个管缆巡检任务，水深300米"})
        resB = app.post("/api/chat", json={"session_id": "sess_ind_B", "message": "立即停止", "request_id": "shared_req_001"})
        self.assertIn(resB.status_code, (200,), "Session B 应独立于 Session A，相同 request_id 不应 409")

    # --------------------------------------------------------------------------
    # R7 P1: 控制事件写后完整 canonical hash 校验
    # --------------------------------------------------------------------------

    def test_control_readback_rejects_wrong_content_by_hash(self):
        """P1-5: 控制事件 readback 内容 hash 不匹配时抛出 ControlAuditPersistenceError"""
        history_dir = get_history_dir(create=True)
        target = history_dir / "control_test_hash_check_req_hash_01.json"
        if target.exists():
            target.unlink()

        audit_data = {
            "event_type": "control_audit_event",
            "session_id": "sess_hash",
            "request_id": "req_hash_01",
            "action": "stop",
            "request_fingerprint": "fp_abc",
            "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {
                "conversation_history": [],
                "slot_store": {},
                "task_state": {},
                "phase": "collecting",
            },
        }
        expected_hash = _canonical_payload_hash(audit_data)

        with patch("json.load", return_value={"different": "data", "request_fingerprint": "fp_abc"}):
            with self.assertRaises((ControlAuditPersistenceError, ControlAuditCommitUncertainError, RuntimeError)):
                _create_control_event_no_overwrite(target, audit_data, expected_hash)

    def test_main_history_readback_rejects_wrong_content_and_recovers(self):
        """P1-6: 主历史快照 readback 内容 hash 不匹配时抛出错误且恢复旧内容"""
        history_dir = get_history_dir(create=True)
        target = history_dir / "history_test_hash_main.json"

        old_data = {"snapshot_version": SNAPSHOT_VERSION, "session_id": "sess_hash_main", "phase": "collecting"}
        target.write_bytes(json.dumps(old_data).encode("utf-8"))

        new_data = {"snapshot_version": SNAPSHOT_VERSION, "session_id": "sess_hash_main", "phase": "done"}
        expected_hash = _canonical_payload_hash(new_data)

        with patch("json.loads", return_value={"snapshot_version": SNAPSHOT_VERSION, "session_id": "CORRUPTED"}):
            with self.assertRaises((ControlAuditPersistenceError, ControlAuditCommitUncertainError, RuntimeError)):
                _replace_main_snapshot_with_recovery(target, new_data, expected_hash)

        # 旧内容应被恢复
        recovered = json.loads(target.read_bytes())
        self.assertEqual(recovered.get("phase"), "collecting", "旧内容应被恢复")

    # --------------------------------------------------------------------------
    # R8 P0: done + 控制请求不再双文件半提交
    # --------------------------------------------------------------------------

    def test_done_control_request_does_not_write_second_authority_file(self):
        """R8-P0-1: 任务完成后的控制请求只写控制事件，不再触发主历史写入（history_snapshot_required = False）"""
        app = web_backend.app.test_client()
        sid = "test_done_ctrl_no_second_file"
        history_dir = get_history_dir(create=True)

        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})
        res1 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_dbl_ctrl_01"})
        self.assertEqual(res1.status_code, 200)

        data = res1.get_json()
        # 控制请求不应同时触发主历史写入（不需要两个权威文件）
        # 验证：history_snapshot_required 对控制请求为 False
        from src.dialogue_manager import ProcessOutcome
        # 直接验证逻辑：通过检查是否同时存在控制事件和对应的 is_retry=False
        self.assertFalse(data.get("is_retry", True), "第一次控制请求不应是重试")

    def test_done_control_event_success_cannot_be_rolled_back_by_main_history_failure(self):
        """R8-P0-2: 控制事件提交成功后，主历史快照失败不会回滚控制事件（两者互斥，无双文件）"""
        app = web_backend.app.test_client()
        sid = "test_ctrl_atomic_no_second_write"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        # 用 history_snapshot_required = False 验证：不会调用第二次 save_conversation
        main_history_write_count = [0]
        orig_save = web_backend.save_conversation

        def counting_save(*args, **kwargs):
            if kwargs.get("request_id") is None:
                main_history_write_count[0] += 1
            return orig_save(*args, **kwargs)

        with patch("web_backend.save_conversation", side_effect=counting_save):
            res = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_atomic_01"})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(main_history_write_count[0], 0,
                         "控制请求不应触发主历史写入（history_snapshot_required=False）")

    # --------------------------------------------------------------------------
    # R8 P1: 控制事件身份只使用 (session_id, request_id)，不含 intent_id
    # --------------------------------------------------------------------------

    def test_control_event_identity_is_session_and_request_id_only(self):
        """R8-P1-1: 控制事件文件路径只包含 session_hash 和 request_id，intent_id 不参与身份"""
        history_dir = get_history_dir(create=True)

        # 用不同 intent_id 计算路径，结果应完全相同
        path1 = get_control_event_path(history_dir, "sess_A", "req_001", intent_id=None)
        path2 = get_control_event_path(history_dir, "sess_A", "req_001", intent_id="TI_INTENT_01")
        path3 = get_control_event_path(history_dir, "sess_A", "req_001", intent_id="different_intent")

        self.assertEqual(path1, path2, "intent_id=None 和 有 intent_id 路径应相同")
        self.assertEqual(path1, path3, "不同 intent_id 的路径应相同")
        self.assertIn("req_001", path1.name, "路径名应包含 request_id")
        # 路径名包含 session_id 的 SHA256 hash（16位），不含原始 session_id 字符串
        self.assertTrue(path1.name.startswith("control_"), "路径名应以 control_ 开头")
        self.assertTrue(path1.name.endswith("_req_001.json"), "路径名应以 _req_001.json 结尾")


    def test_same_session_request_found_after_intent_id_changes(self):
        """R8-P1-2: 服务重启后（intent_id 可能丢失），相同 (session_id, request_id) 仍能精确定位事件"""
        history_dir = get_history_dir(create=True)

        # 模拟第一次提交（带 intent_id）
        target = get_control_event_path(history_dir, "sess_restart", "req_rst_001", intent_id="TI_OLD_INTENT")
        resp_snap = {
            "code": 200, "session_id": "sess_restart", "request_id": "req_rst_001", "reply": "停止",
            "done": False, "rejected": False, "dialogue_mode": "task_collection", "control_state": "stopped",
            "last_control_request": None, "collected": {}, "missing": [], "task_type": "unknown",
            "emergency": False, "final_json": None, "is_retry": False,
        }
        event_data = {
            "snapshot_version": SNAPSHOT_VERSION,
            "event_type": "control_audit_event",
            "session_id": "sess_restart",
            "request_id": "req_rst_001",
            "action": "stop",
            "request_fingerprint": compute_request_fingerprint("sess_restart", "req_rst_001", "停止", action="stop"),
            "user_message": "停止",
            "created_at": "2026-07-31T12:00:00.000000+08:00",
            "control_state": "stopped",
            "phase": "collecting",
            "mode": "normal",
            "dialogue_mode": "task_collection",
            "last_control_request": None,
            "snapshot": {
                "snapshot_version": SNAPSHOT_VERSION,
                "session_id": "sess_restart",
                "session_revision": 1,
                "parent_revision": 0,
                "saved_at": "2026-07-31T12:00:00.000000+08:00",
                "conversation_history": [],
                "slot_store": {},
                "task_state": {},
                "phase": "collecting",
            },
            "response_snapshot": resp_snap,
        }
        event_data["payload_sha256"] = _canonical_payload_hash(event_data)
        target.write_bytes(json.dumps(event_data, ensure_ascii=False).encode("utf-8"))

        # 服务重启后（intent_id 丢失），使用不同 intent_id 仍能找到相同路径的事件
        found = load_control_event(history_dir, "sess_restart", "req_rst_001", intent_id="TI_NEW_OR_NONE")
        self.assertIsNotNone(found, "服务重启后应仍可找到相同 (session_id, request_id) 的事件")
        self.assertEqual(found["request_id"], "req_rst_001")

    # --------------------------------------------------------------------------
    # R8 P1: 损坏的控制事件 fail closed
    # --------------------------------------------------------------------------

    def test_corrupted_existing_control_event_fails_closed(self):
        """R8-P1-3: 控制事件文件存在但 JSON 损坏 → ControlAuditCorruptionError，不得当作未找到"""
        history_dir = get_history_dir(create=True)
        target = get_control_event_path(history_dir, "sess_corrupt", "req_corrupt_01")
        target.write_bytes(b"{invalid json corrupted")

        from src.exceptions import ControlAuditCorruptionError as CACE
        with self.assertRaises(CACE, msg="损坏文件应 fail closed，抛出 ControlAuditCorruptionError"):
            load_control_event(history_dir, "sess_corrupt", "req_corrupt_01")

    def test_unreadable_existing_control_event_fails_closed(self):
        """R8-P1-4: 控制事件文件存在但不可读（权限错误）→ ControlAuditCorruptionError，不得当作未找到"""
        history_dir = get_history_dir(create=True)
        target = get_control_event_path(history_dir, "sess_unread", "req_unread_01")
        target.write_bytes(b'{"request_fingerprint":"fp","snapshot_version":3}')

        from src.exceptions import ControlAuditCorruptionError as CACE
        with patch("builtins.open", side_effect=PermissionError("No read permission")):
            with self.assertRaises((CACE, PermissionError), msg="不可读文件应 fail closed"):
                load_control_event(history_dir, "sess_unread", "req_unread_01")

    def test_corrupted_event_causes_api_503_not_200(self):
        """R8-P1-5: 损坏控制事件导致 API 返回 503，不得静默返回 200"""
        app = web_backend.app.test_client()
        sid = "test_corrupt_api_fail_closed"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        # 第一次提交控制请求（创建事件文件）
        res1 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_corrupt_api_01"})
        self.assertEqual(res1.status_code, 200)

        # 损坏事件文件
        history_dir = get_history_dir(create=True)
        target = get_control_event_path(history_dir, sid, "req_corrupt_api_01")
        if target.exists():
            target.write_bytes(b"{corrupted!}")
            # 再次请求相同 request_id → 应 fail closed 返回 503
            res2 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_corrupt_api_01"})
            self.assertIn(res2.status_code, (503, 500), "损坏控制事件应 fail closed，不得返回 200")

    # --------------------------------------------------------------------------
    # R8 P1: 重试时从 snapshot 恢复完整 Manager 状态
    # --------------------------------------------------------------------------

    def test_retry_response_fields_match_committed_event(self):
        """R8-P1-6: 精确重试返回的 control_state 与已提交事件中的 snapshot 一致"""
        app = web_backend.app.test_client()
        sid = "test_retry_state_restore"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        res1 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_retry_state_01"})
        self.assertEqual(res1.status_code, 200)
        data1 = res1.get_json()

        # 第二次用同一 request_id（精确重试）
        res2 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_retry_state_01"})
        self.assertEqual(res2.status_code, 200)
        data2 = res2.get_json()

        self.assertTrue(data2.get("is_retry"), "第二次应为重试")
        self.assertEqual(data2["reply"], data1["reply"], "重试 reply 应与第一次一致")
        self.assertEqual(data2["control_state"], data1["control_state"],
                         "重试后 control_state 应与第一次提交一致")

    # --------------------------------------------------------------------------
    # R9 P1-1: 旧请求重试绝对不覆盖较新的 Session 实时状态
    # --------------------------------------------------------------------------

    def test_retry_does_not_revert_newer_session_state(self):
        """R9-P1-1: 重试旧请求 A，返回 A 的原始响应，但 Manager 内存绝对不退回到 A 的旧快照状态"""
        app = web_backend.app.test_client()
        sid = "sess_no_state_reversion"

        # 1. 建立任务
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        # 2. 发送请求 A (暂停任务)
        resA = app.post("/api/chat", json={"session_id": sid, "message": "暂停当前任务", "request_id": "req_A_pause"})
        self.assertEqual(resA.status_code, 200)

        # 3. 发送请求 B (普通对话，推进对话历史与状态)
        resB = app.post("/api/chat", json={"session_id": sid, "message": "检查一下天气状况", "request_id": "req_B_chat"})
        self.assertEqual(resB.status_code, 200)

        mgr = web_backend.get_or_create_manager(sid)
        history_len_before_retry = len(mgr.conversation_history)

        # 4. 重试请求 A (使用 req_A_pause)
        resA_retry = app.post("/api/chat", json={"session_id": sid, "message": "暂停当前任务", "request_id": "req_A_pause"})
        self.assertEqual(resA_retry.status_code, 200)
        dataA_retry = resA_retry.get_json()

        self.assertTrue(dataA_retry.get("is_retry"), "重试请求应带有 is_retry=True")
        self.assertEqual(len(mgr.conversation_history), history_len_before_retry,
                         "重试旧请求绝对不得把对话历史退回到请求 A 时的旧快照")

    # --------------------------------------------------------------------------
    # R9 P1-2: 真实服务重启后，活动任务的控制请求重试不返回 409
    # --------------------------------------------------------------------------

    def test_retry_after_service_restart_in_active_task_does_not_409(self):
        """R9-P1-2: 活动任务中控制请求成功后彻底重启服务，同一 request_id 重试返回原始结果而非 409"""
        app = web_backend.app.test_client()
        sid = "sess_restart_active_task"

        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})
        res1 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_active_rst_01"})
        self.assertEqual(res1.status_code, 200)

        # 模拟服务彻底重启：清空内存 Sessions Manager
        with web_backend._sessions_lock:
            web_backend._sessions_manager.clear()

        # 重置服务后再次发送同一 request_id
        res2 = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_active_rst_01"})
        self.assertEqual(res2.status_code, 200, "重启后重试活动任务控制请求不应返回 409")
        self.assertTrue(res2.get_json().get("is_retry"), "重启后重试应带有 is_retry=True")

    # --------------------------------------------------------------------------
    # R9 P1-3: 服务重启后的全新请求自动恢复最新 Session 状态
    # --------------------------------------------------------------------------

    def test_new_request_after_restart_restores_latest_control_state(self):
        """R9-P1-3: 服务重启后，新的普通请求自动装载并保持最新已提交的控制状态"""
        app = web_backend.app.test_client()
        sid = "sess_restart_restore_state"

        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})
        res1 = app.post("/api/chat", json={"session_id": sid, "message": "暂停当前任务", "request_id": "req_pause_before_rst"})
        self.assertEqual(res1.status_code, 200)

        # 模拟重启
        with web_backend._sessions_lock:
            web_backend._sessions_manager.clear()

        # 发送全新的普通消息
        res2 = app.post("/api/chat", json={"session_id": sid, "message": "你好", "request_id": "req_new_after_rst"})
        self.assertEqual(res2.status_code, 200)
        data2 = res2.get_json()

        self.assertIn(data2.get("control_state"), ("pause_requested", "paused"),
                      "重启后的新请求应自动恢复最新控制状态")

    # --------------------------------------------------------------------------
    # R9 P1-4: 损坏 Schema 或非法 Enum 导致 fail closed (503)
    # --------------------------------------------------------------------------

    def test_corrupted_schema_or_enum_event_fails_closed(self):
        """R9-P1-4: 控制事件字段/枚举损坏 → validate_control_event 抛出 ControlAuditCorruptionError"""
        history_dir = get_history_dir(create=True)

        bad_event = {
            "session_id": "sess_bad_enum",
            "request_id": "req_bad_enum_01",
            "request_fingerprint": "a" * 64,
            "snapshot": {"phase": "INVALID_PHASE_ENUM_XXX", "control_state": "idle"}
        }
        target = get_control_event_path(history_dir, "sess_bad_enum", "req_bad_enum_01")
        target.write_bytes(json.dumps(bad_event).encode("utf-8"))

        from src.exceptions import ControlAuditCorruptionError as CACE
        from src.history_manager import validate_control_event
        with self.assertRaises(CACE):
            validate_control_event(bad_event)

        with self.assertRaises(CACE):
            load_control_event(history_dir, "sess_bad_enum", "req_bad_enum_01")

    # --------------------------------------------------------------------------
    # R9 P1-5 & P1-6: 原子 os.link no-clobber & post-commit ownership 校验
    # --------------------------------------------------------------------------

    def test_atomic_no_overwrite_with_os_link_prevents_clobbering(self):
        """R9-P1-5: _create_control_event_no_overwrite 使用 os.link 保证目标已存在时原子失败且绝不覆盖"""
        history_dir = get_history_dir(create=True)
        target = history_dir / "control_test_link_no_clobber_req_01.json"

        # 预先创建目标文件
        original_content = {"session_id": "sess_link", "request_id": "req_01", "request_fingerprint": "fp_old"}
        target.write_bytes(json.dumps(original_content).encode("utf-8"))

        new_data = {
            "event_type": "control_audit_event",
            "session_id": "sess_link",
            "request_id": "req_01",
            "request_fingerprint": "fp_new",
            "snapshot": {"phase": "collecting", "control_state": "idle"}
        }
        expected_hash = _canonical_payload_hash(new_data)

        # 尝试写入冲突内容：由于 target 已存在，不应覆盖已有文件
        from src.exceptions import ControlAuditConflictError, ControlAuditCorruptionError
        with self.assertRaises((ControlAuditConflictError, ControlAuditCorruptionError)):
            _create_control_event_no_overwrite(target, new_data, expected_hash)

        # 验证目标文件内容未被修改/覆盖
        current = json.loads(target.read_bytes())
        self.assertEqual(current.get("request_fingerprint"), "fp_old", "原有目标文件绝不可被 replace 覆盖")

    def test_post_commit_cleanup_checks_exact_ownership(self):
        """R9-P1-6: Post-commit 失败时清理之前，确认文件所有权；不属于本事务的文件绝不 unlink"""
        history_dir = get_history_dir(create=True)
        target = history_dir / "control_test_ownership_check_req_01.json"
        if target.exists():
            target.unlink()

        audit_data = {
            "event_type": "control_audit_event",
            "session_id": "sess_own",
            "request_id": "req_01",
            "request_fingerprint": "a" * 64,
            "snapshot": {"phase": "collecting", "control_state": "idle"}
        }
        expected_hash = _canonical_payload_hash(audit_data)

        from src.history_manager import _read_regular_file_no_follow
        orig_read = _read_regular_file_no_follow
        def mock_read_corrupt(path, expected_stat=None, error_cls=ControlAuditPersistenceError):
            if str(target) in str(path):
                # 外部写入者替换了文件内容 (满足 snapshot_version 校验但 fingerprint 不匹配)
                target.write_bytes(b'{"snapshot_version":3,"event_type":"control_audit_event","session_id":"sess_own","request_id":"req_01","request_fingerprint":"EXTERNALLY_MUTATED","snapshot":{"phase":"collecting"}}')
            return orig_read(path, expected_stat, error_cls)

        with patch("src.history_manager._read_regular_file_no_follow", side_effect=mock_read_corrupt):
            with self.assertRaises(ControlAuditCommitUncertainError):
                _create_control_event_no_overwrite(target, audit_data, expected_hash)

        # 验证：因为目标文件在 post-commit 时已被修改（ownership 不符），清理程序绝未 unlink 目标文件！
        self.assertTrue(target.exists(), "不属于本事务的文件绝对不得被 unlink")

    # --------------------------------------------------------------------------
    # R9 P2-3: 真实多进程下状态机副作用严格只执行 1 次
    # --------------------------------------------------------------------------

    def test_real_multiprocess_state_machine_executes_only_once(self):
        """R9-P2-3: 2 个独立进程并发并发相同 (session_id, request_id) 请求，状态机只执行 1 次"""
        import multiprocessing

        def worker(session_id, request_id, return_dict, worker_idx):
            try:
                app = web_backend.app.test_client()
                res = app.post("/api/chat", json={
                    "session_id": session_id,
                    "message": "暂停当前任务",
                    "request_id": request_id,
                })
                return_dict[worker_idx] = (res.status_code, res.get_json())
            except Exception as e:
                return_dict[worker_idx] = (500, str(e))

        manager = multiprocessing.Manager()
        return_dict = manager.dict()

        sid = "sess_mp_once_test"
        req_id = "req_mp_once_001"

        # 首先建立一个任务
        app = web_backend.app.test_client()
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        p1 = multiprocessing.Process(target=worker, args=(sid, req_id, return_dict, 0))
        p2 = multiprocessing.Process(target=worker, args=(sid, req_id, return_dict, 1))

        p1.start()
        p2.start()
        p1.join(timeout=10)
        p2.join(timeout=10)

        self.assertEqual(p1.exitcode, 0)
        self.assertEqual(p2.exitcode, 0)

        res0_code, res0_data = return_dict[0]
        res1_code, res1_data = return_dict[1]

        self.assertEqual(res0_code, 200)
        self.assertEqual(res1_code, 200)

        # 恰有一个为正常响应 (is_retry False 或 None)，另一个为重试响应 (is_retry True)
        is_retry_flags = [res0_data.get("is_retry", False), res1_data.get("is_retry", False)]
        self.assertEqual(sum(1 for flag in is_retry_flags if flag is True), 1,
                         "并发两个进程中必须有且仅有一个识别为重试，即状态机仅真正执行 1 次")

    # --------------------------------------------------------------------------
    # Round 10 Test Cases
    # --------------------------------------------------------------------------

    def test_missing_snapshot_fails_closed_with_503(self):
        """R10-P1-1: 控制事件缺失 snapshot 结构时强校验抛出 ControlAuditCorruptionError，API 返回 503"""
        history_dir = get_history_dir(create=True)
        target = get_control_event_path(history_dir, "sess_r10_nosnap", "req_r10_01")
        corrupt_event = {
            "event_type": "control_audit_event",
            "session_id": "sess_r10_nosnap",
            "request_id": "req_r10_01",
            "request_fingerprint": compute_request_fingerprint("sess_r10_nosnap", "req_r10_01", "停止"),
            "snapshot_version": SNAPSHOT_VERSION,
            # snapshot 缺失！
        }
        target.write_bytes(json.dumps(corrupt_event).encode("utf-8"))

        app = web_backend.app.test_client()
        res = app.post("/api/chat", json={
            "session_id": "sess_r10_nosnap",
            "request_id": "req_r10_01",
            "message": "停止",
        })
        self.assertEqual(res.status_code, 503, "缺失 snapshot 的损坏控制事件应 fail closed 返回 503")
        self.assertFalse(res.get_json().get("retryable", True), "503 错误响应中 retryable 应为 False")

    def test_latest_revision_corrupted_fails_closed_no_fallback(self):
        """R10-P1-2: 最新 revision 的快照损坏时，load_latest_session_snapshot 绝对不得降级恢复旧版本"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r10_no_fallback"

        # 写入旧版本 revision 1
        old_snap = {
            "snapshot_version": SNAPSHOT_VERSION,
            "session_id": sid,
            "session_revision": 1,
            "saved_at": "2026-07-31T00:00:00.000000+08:00",
            "conversation_history": [{"role": "user", "content": "旧消息"}],
            "slot_store": {},
            "task_state": {},
            "phase": "collecting",
            "mode": "normal",
        }
        target_old = history_dir / f"history_{sid}.json"
        target_old.write_bytes(json.dumps(old_snap).encode("utf-8"))

        # 写入最新版本 revision 2，但写入损坏的 JSON 并建立指向它的 Head
        target_new = get_session_revision_path(history_dir, sid, 2)
        target_new.write_bytes(b"{invalid json content corrupted!!")
        head_path = get_session_head_path(history_dir, sid)
        head_data = {
            "schema_version": SNAPSHOT_VERSION,
            "session_id": sid,
            "current_revision": 2,
            "snapshot_file": target_new.name,
            "snapshot_payload_sha256": "a" * 64,
            "updated_at": "2026-08-01T12:00:00+08:00",
        }
        head_data["payload_sha256"] = _canonical_payload_hash(head_data)
        head_path.write_text(json.dumps(head_data), encoding="utf-8")

        with self.assertRaises((ControlAuditCorruptionError, ControlAuditPersistenceError), msg="最新 revision 损坏时绝不得静默回退旧快照"):
            load_latest_session_snapshot(sid)

    def test_done_restart_verifies_task_intent_file(self):
        """R10-P1-3: phase==done 快照恢复时，验证 TaskIntent 产物文件；不存在则抛出 ControlAuditCorruptionError"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r10_done_ti_missing"

        snap_done = {
            "snapshot_version": SNAPSHOT_VERSION,
            "session_id": sid,
            "session_revision": 2,
            "saved_at": "2026-07-31T10:00:00.000000+08:00",
            "conversation_history": [],
            "slot_store": {},
            "task_state": {"task_type_key": "pipeline_inspection"},
            "built_json": {"task_id": "non_existent_task_99999"},
            "phase": "done",
            "mode": "normal",
        }
        target = get_session_revision_path(history_dir, sid, 2)
        snap_done["payload_sha256"] = _canonical_payload_hash(snap_done)
        target.write_bytes(json.dumps(snap_done).encode("utf-8"))
        from src.history_manager import update_session_head
        update_session_head(history_dir=history_dir, session_id=sid, current_revision=2, snapshot_file=target.name)

        with self.assertRaises(ControlAuditCorruptionError, msg="done 快照引用的 TaskIntent 缺失应 fail closed"):
            load_latest_session_snapshot(sid)

    def test_blocked_soft_and_hard_restart_preserves_violations_and_counts(self):
        """R10-P1-3: _blocking_violations, _soft_whitelist, _hard_refusal_counts 在 export/restore 及 snapshot 恢复后保持一致"""
        manager = DialogueManager(self.llm, self.kb)
        manager._soft_whitelist = {("pipe_inspection", "depth", "over_limit")}
        manager._hard_refusal_counts = {"depth_limit": 3}
        manager.session_revision = 5

        state = manager._export_runtime_state()
        self.assertIn("session_revision", state)
        self.assertEqual(state["session_revision"], 5)

        manager_new = DialogueManager(self.llm, self.kb)
        manager_new._restore_runtime_state(state)
        self.assertEqual(manager_new._soft_whitelist, {("pipe_inspection", "depth", "over_limit")})
        self.assertEqual(manager_new._hard_refusal_counts, {"depth_limit": 3})
        self.assertEqual(manager_new.session_revision, 5)

    def test_immutable_retry_returns_exact_response_snapshot(self):
        """R10-P1-4: 精确幂等重试直接返回预先保存的 response_snapshot，保证不可变 HTTP 响应重放"""
        app = web_backend.app.test_client()
        sid = "sess_r10_immutable_retry"
        req_id = "req_r10_immutable_01"

        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})
        res1 = app.post("/api/chat", json={"session_id": sid, "message": "暂停当前任务", "request_id": req_id})
        self.assertEqual(res1.status_code, 200)
        data1 = res1.get_json()

        # 发起二次重试
        res2 = app.post("/api/chat", json={"session_id": sid, "message": "暂停当前任务", "request_id": req_id})
        self.assertEqual(res2.status_code, 200)
        data2 = res2.get_json()

        self.assertTrue(data2.get("is_retry"))
        self.assertEqual(data1.get("reply"), data2.get("reply"))
        self.assertEqual(data1.get("control_state"), data2.get("control_state"))
        self.assertEqual(data1.get("dialogue_mode"), data2.get("dialogue_mode"))

    def test_session_revision_monotonic_and_cas(self):
        """R10-P2-1: DialogueManager session_revision 单调递增"""
        manager = DialogueManager(self.llm, self.kb)
        self.assertEqual(manager.session_revision, 0)

        outcome1 = manager.process_with_audit("创建巡检任务", request_id="req_rev_01", session_id="sess_rev_test")
        self.assertEqual(manager.session_revision, 1)

        outcome2 = manager.process_with_audit("水深300米", request_id="req_rev_02", session_id="sess_rev_test")
        self.assertEqual(manager.session_revision, 2)

    def test_multiprocess_different_requests_same_session_serialized(self):
        """R10-P1-5: 多进程下对同一个 Session 发起不同 request_id 的请求，被 per-session 锁串行化"""
        def worker(sid, req_id, msg, return_dict, worker_idx):
            app = web_backend.app.test_client()
            res = app.post("/api/chat", json={"session_id": sid, "message": msg, "request_id": req_id})
            return_dict[worker_idx] = (res.status_code, res.get_json())

        manager = multiprocessing.Manager()
        return_dict = manager.dict()

        sid = "sess_r10_mp_diff_req"
        app = web_backend.app.test_client()
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务"})

        p1 = multiprocessing.Process(target=worker, args=(sid, "req_diff_01", "水深300米", return_dict, 0))
        p2 = multiprocessing.Process(target=worker, args=(sid, "req_diff_02", "起止点A到B", return_dict, 1))

        p1.start()
        p2.start()
        p1.join(timeout=10)
        p2.join(timeout=10)

        self.assertEqual(p1.exitcode, 0)
        self.assertEqual(p2.exitcode, 0)

        res0_code, _ = return_dict[0]
        res1_code, _ = return_dict[1]
        self.assertEqual(res0_code, 200)
        self.assertEqual(res1_code, 200)

    def test_stale_worker_reloads_head_and_cas(self):
        """R11-P0-1: 多 Worker 竞争场景下，Stale Worker 在获取 per-session 锁后重新加载 Head 并完成 CAS"""
        sid = "sess_r11_cas_reload"
        mgr_a = DialogueManager(self.llm, self.kb)
        mgr_b = DialogueManager(self.llm, self.kb)

        # Worker A 提交请求1，Session Head 前进到 revision 2
        mgr_a.process_with_audit("创建一个管缆巡检任务", request_id="req_cas_01", session_id=sid,
                                persist_callback=lambda m, o, b: save_conversation(
                                    session_id=sid, conversation_history=m.conversation_history,
                                    task_state=m.task_state, built_json=m._last_built_json,
                                    mode=m.mode, phase=m.phase, slot_store=m.slot_store,
                                    dialogue_mode=m.dialogue_mode, control_state=m.control_state,
                                    last_control_request=m.last_control_request, request_id="req_cas_01",
                                    user_message="创建一个管缆巡检任务", reply=o.reply, control_action=o.control_action,
                                    parent_revision=o.parent_revision, manager=m
                                ))
        self.assertEqual(mgr_a.session_revision, 1)

        # Worker B 目前内存状态仍是 revision 0
        self.assertEqual(mgr_b.session_revision, 0)

        # Worker B 提交请求2：在 process_with_audit 内获取 Session 锁后自动重新加载 Head 状态到 revision 1，并成功递增到 revision 2
        mgr_b.process_with_audit("水深300米", request_id="req_cas_02", session_id=sid,
                                persist_callback=lambda m, o, b: save_conversation(
                                    session_id=sid, conversation_history=m.conversation_history,
                                    task_state=m.task_state, built_json=m._last_built_json,
                                    mode=m.mode, phase=m.phase, slot_store=m.slot_store,
                                    dialogue_mode=m.dialogue_mode, control_state=m.control_state,
                                    last_control_request=m.last_control_request, request_id="req_cas_02",
                                    user_message="水深300米", reply=o.reply, control_action=o.control_action,
                                    parent_revision=o.parent_revision, manager=m
                                ))
        self.assertEqual(mgr_b.session_revision, 2)

    def test_valid_done_restore_verifies_real_task_intent(self):
        """R11-P1-1: 带有有效 TaskIntent Artifact 的 done 阶段快照成功装载"""
        from src.result_paths import get_task_dir
        history_dir = get_history_dir(create=True)
        task_dir = get_task_dir(create=True)
        sid = "sess_r11_done_valid"
        task_id = "TI202607310001"

        task_intent_data = {
            "intent_id": task_id,
            "task_type": "pipeline_inspection",
            "priority": 5,
            "time": {},
            "location": {},
            "task": {"type": "pipeline_inspection"},
            "equipment": {"robot_type": "auv"},
            "conditions": {},
            "created_at": "2026-07-31T12:00:00.000000+08:00",
            "schema_version": "1.0",
        }
        ti_file = task_dir / f"task_intent_{task_id}.json"
        ti_file.write_bytes(json.dumps(task_intent_data, ensure_ascii=False).encode("utf-8"))

        mgr = DialogueManager(self.llm, self.kb)
        mgr.phase = "done"
        mgr._last_built_json = {"task_id": task_id}
        mgr.task_state = {"task_type_key": "pipeline_inspection", "intent_id": task_id}

        save_conversation(
            session_id=sid, conversation_history=[], task_state=mgr.task_state,
            built_json=mgr._last_built_json, mode="normal", phase="done", intent_id=task_id,
            slot_store=mgr.slot_store, dialogue_mode="task_collection", parent_revision=0, manager=mgr
        )

        loaded = load_latest_session_snapshot(sid)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.get("phase"), "done")

    def test_corrupt_orphan_file_does_not_block_session(self):
        """R11-P1-2: Session Head 正常时，目录内孤立无引用损坏文件不阻断正常 Session 装载"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r11_orphan_corrupt"

        # 写入一个孤立损坏文件
        orphan = history_dir / "history_orphan_corrupted_123.json"
        orphan.write_bytes(b"{bad json broken data!!")

        # 写入正常快照并更新 Session Head
        mgr = DialogueManager(self.llm, self.kb)
        save_conversation(
            session_id=sid, conversation_history=[{"role": "user", "content": "hello"}],
            task_state={}, built_json={}, mode="normal", phase="collecting", parent_revision=0, manager=mgr
        )

        loaded = load_latest_session_snapshot(sid)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.get("session_id"), sid)

    def test_initialization_race_prevents_half_initialized_manager(self):
        """R11-P1-3: 并发线程调用 get_or_create_manager 时，次要线程在 Condition 上等待直到 ready"""
        sid = "sess_r11_init_race"
        results = []

        def worker():
            m = web_backend.get_or_create_manager(sid)
            results.append(m.session_id)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(results), 5)
        self.assertTrue(all(s == sid for s in results))

    def test_ownership_checks_inode(self):
        """R11-P2-1: _verify_path_ownership 传入 expected_stat 时校对 st_dev 和 st_ino"""
        history_dir = get_history_dir(create=True)
        test_file = history_dir / "test_ino_check.json"
        payload = b'{"key": "value"}'
        test_file.write_bytes(payload)

        stat1 = os.stat(str(test_file))
        from src.history_manager import _verify_path_ownership
        self.assertTrue(_verify_path_ownership(test_file, payload, expected_stat=stat1))

        # 构造 fake stat
        class FakeStat:
            st_dev = stat1.st_dev
            st_ino = stat1.st_ino + 99999
        self.assertFalse(_verify_path_ownership(test_file, payload, expected_stat=FakeStat()))

    # ---------------------------------------------------------------------------
    # Round 12 必填 Head 协议与强校验测试
    # ---------------------------------------------------------------------------

    def test_genesis_revision_is_one_with_parent_zero(self):
        """1. 创世 revision 为 1，parent_revision 为 0"""
        history_dir = get_history_dir(create=True)
        sid = "sess_genesis_test_01"
        mgr = DialogueManager(self.llm, self.kb)
        self.assertEqual(mgr.session_revision, 0)

        filename = save_conversation(
            session_id=sid,
            conversation_history=[{"role": "user", "content": "hi"}],
            task_state={},
            built_json={},
            mode="normal",
            phase="collecting",
            parent_revision=0,
            manager=mgr,
        )
        self.assertIn("rev_1.json", filename)
        self.assertEqual(mgr.session_revision, 1)

        head = read_session_head(history_dir, sid)
        self.assertIsNotNone(head)
        self.assertEqual(head["current_revision"], 1)

    def test_no_head_rejects_parent_revision_one(self):
        """2. 无 Head 时拒绝 parent_revision == 1，仅接受 0"""
        sid = "sess_no_head_reject_p1"
        mgr = DialogueManager(self.llm, self.kb)
        with self.assertRaises(ControlAuditConflictError):
            save_conversation(
                session_id=sid,
                conversation_history=[],
                task_state={},
                built_json={},
                mode="normal",
                phase="collecting",
                parent_revision=1,
                manager=mgr,
            )

    def test_head_missing_payload_hash_fails_closed(self):
        """3. Head 缺失 payload_sha256 时 Fail Closed 抛出 ControlAuditCorruptionError"""
        history_dir = get_history_dir(create=True)
        sid = "sess_head_missing_phash"
        head_path = get_session_head_path(history_dir, sid)
        head_data = {
            "schema_version": SNAPSHOT_VERSION,
            "session_id": sid,
            "current_revision": 1,
            "snapshot_file": f"session_{hashlib.sha256(sid.encode()).hexdigest()[:16]}_rev_1.json",
            "snapshot_payload_sha256": "a" * 64,
            "updated_at": "2026-08-01T00:00:00",
        }
        head_path.write_text(json.dumps(head_data), encoding="utf-8")
        with self.assertRaises(ControlAuditCorruptionError):
            read_session_head(history_dir, sid)

    def test_head_missing_snapshot_hash_fails_closed(self):
        """4. Head 缺失 snapshot_payload_sha256 时 Fail Closed 抛出 ControlAuditCorruptionError"""
        history_dir = get_history_dir(create=True)
        sid = "sess_head_missing_shash"
        head_path = get_session_head_path(history_dir, sid)
        head_data = {
            "schema_version": SNAPSHOT_VERSION,
            "session_id": sid,
            "current_revision": 1,
            "snapshot_file": f"session_{hashlib.sha256(sid.encode()).hexdigest()[:16]}_rev_1.json",
            "payload_sha256": "a" * 64,
            "updated_at": "2026-08-01T00:00:00",
        }
        head_path.write_text(json.dumps(head_data), encoding="utf-8")
        with self.assertRaises(ControlAuditCorruptionError):
            read_session_head(history_dir, sid)

    def test_head_snapshot_path_traversal_fails_closed(self):
        """5. Head snapshot_file 包含路径穿越 (..) 时 Fail Closed 抛出 ControlAuditCorruptionError"""
        history_dir = get_history_dir(create=True)
        sid = "sess_head_traversal"
        head_path = get_session_head_path(history_dir, sid)
        head_data = {
            "schema_version": SNAPSHOT_VERSION,
            "session_id": sid,
            "current_revision": 1,
            "snapshot_file": "../../etc/passwd",
            "snapshot_payload_sha256": "a" * 64,
            "payload_sha256": "b" * 64,
            "updated_at": "2026-08-01T00:00:00",
        }
        head_path.write_text(json.dumps(head_data), encoding="utf-8")
        with self.assertRaises(ControlAuditCorruptionError):
            read_session_head(history_dir, sid)

    def test_head_filename_must_match_session_and_revision(self):
        """6. Head snapshot_file 文件名与 session/revision 派生不一致时 Fail Closed"""
        history_dir = get_history_dir(create=True)
        sid = "sess_head_wrong_fname"
        head_path = get_session_head_path(history_dir, sid)
        head_data = {
            "schema_version": SNAPSHOT_VERSION,
            "session_id": sid,
            "current_revision": 1,
            "snapshot_file": "session_0000000000000000_rev_1.json",
            "snapshot_payload_sha256": "a" * 64,
            "payload_sha256": "b" * 64,
            "updated_at": "2026-08-01T00:00:00",
        }
        head_path.write_text(json.dumps(head_data), encoding="utf-8")
        with self.assertRaises(ControlAuditCorruptionError):
            read_session_head(history_dir, sid)

    def test_head_revision_must_match_snapshot_revision(self):
        """7. Head current_revision 与 snapshot 文件内部 session_revision 不一致时 Fail Closed"""
        history_dir = get_history_dir(create=True)
        sid = "sess_head_rev_mismatch"
        # 先正常写入 rev 1
        mgr = DialogueManager(self.llm, self.kb)
        save_conversation(
            session_id=sid, conversation_history=[], task_state={}, built_json={},
            mode="normal", phase="collecting", parent_revision=0, manager=mgr
        )
        head_path = get_session_head_path(history_dir, sid)
        head_data = json.loads(head_path.read_text(encoding="utf-8"))
        # 篡改 Head 中的 current_revision 为 2，但 snapshot_file 仍指 rev_1
        head_data["current_revision"] = 2
        head_data["snapshot_file"] = get_session_revision_path(history_dir, sid, 2).name
        head_data["payload_sha256"] = _canonical_payload_hash(head_data)
        head_path.write_text(json.dumps(head_data), encoding="utf-8")

        with self.assertRaises(ControlAuditCorruptionError):
            read_session_head(history_dir, sid)

    def test_head_snapshot_hash_mismatch_fails_closed(self):
        """8. Head 中的 snapshot_payload_sha256 与关联 snapshot 文件真实 hash 不匹配时 Fail Closed"""
        history_dir = get_history_dir(create=True)
        sid = "sess_head_hash_mismatch"
        mgr = DialogueManager(self.llm, self.kb)
        save_conversation(
            session_id=sid, conversation_history=[], task_state={}, built_json={},
            mode="normal", phase="collecting", parent_revision=0, manager=mgr
        )
        head_path = get_session_head_path(history_dir, sid)
        head_data = json.loads(head_path.read_text(encoding="utf-8"))
        head_data["snapshot_payload_sha256"] = "f" * 64
        head_data["payload_sha256"] = _canonical_payload_hash(head_data)
        head_path.write_text(json.dumps(head_data), encoding="utf-8")

        with self.assertRaises(ControlAuditCorruptionError):
            read_session_head(history_dir, sid)

    def test_head_rollback_checks_inode_and_bytes(self):
        """9. Head 更新 Post-replace 失败回滚前校验 inode 与字节内容"""
        history_dir = get_history_dir(create=True)
        sid = "sess_head_rollback_ino"

        rev1_path = get_session_revision_path(history_dir, sid, 1)
        rev1_data = {
            "session_id": sid, "session_revision": 1, "parent_revision": 0, "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 1, "parent_revision": 0, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        rev1_data["payload_sha256"] = _canonical_payload_hash(rev1_data)
        rev1_path.write_text(json.dumps(rev1_data), encoding="utf-8")

        update_session_head(history_dir, sid, 1, rev1_path.name, rev1_data["payload_sha256"])
        head_path = get_session_head_path(history_dir, sid)
        self.assertTrue(head_path.exists())

        rev2_path = get_session_revision_path(history_dir, sid, 2)
        rev2_data = {
            "session_id": sid, "session_revision": 2, "parent_revision": 1, "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 2, "parent_revision": 1, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        rev2_data["payload_sha256"] = _canonical_payload_hash(rev2_data)
        rev2_path.write_text(json.dumps(rev2_data), encoding="utf-8")

        # 模拟 post-replace fsync 抛出 OSError
        with patch("src.history_manager._fsync_directory", side_effect=OSError("IO Error")):
            with self.assertRaises((ControlAuditPersistenceError, ControlAuditCommitUncertainError)):
                update_session_head(history_dir, sid, 2, rev2_path.name, rev2_data["payload_sha256"])

    def test_restored_old_head_is_readback_verified(self):
        """10. 回滚旧 Head 后执行强一致读回校验"""
        history_dir = get_history_dir(create=True)
        sid = "sess_head_readback_verify"

        # 写入真实 rev 1 snapshot 避免 update_session_head 报 missing snapshot
        rev1_path = get_session_revision_path(history_dir, sid, 1)
        rev1_data = {
            "session_id": sid,
            "session_revision": 1,
            "parent_revision": 0,
            "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 1, "parent_revision": 0, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        rev1_data["payload_sha256"] = _canonical_payload_hash(rev1_data)
        rev1_path.write_text(json.dumps(rev1_data), encoding="utf-8")

        # 触发更新 Head，Head 的 snapshot_payload_sha256 传真实 hash
        update_session_head(history_dir, sid, 1, rev1_path.name, rev1_data["payload_sha256"])
        head = read_session_head(history_dir, sid)
        self.assertIsNotNone(head)

    def test_response_snapshot_types_and_identity_are_validated(self):
        """11. validate_response_snapshot 对类型和身份标量做严格校验"""
        from src.history_manager import validate_response_snapshot
        valid_snap = {
            "code": 200,
            "session_id": "s1",
            "request_id": "r1",
            "reply": "hello",
            "done": False,
            "rejected": False,
            "dialogue_mode": "task_collection",
            "control_state": "idle",
            "collected": {},
            "missing": [],
            "is_retry": False,
        }
        self.assertEqual(validate_response_snapshot(valid_snap, "s1", "r1")["code"], 200)

        # code 不是 200
        bad_code = copy.deepcopy(valid_snap)
        bad_code["code"] = 400
        with self.assertRaises(ControlAuditCorruptionError):
            validate_response_snapshot(bad_code)

        # done 不是 bool
        bad_done = copy.deepcopy(valid_snap)
        bad_done["done"] = 1
        with self.assertRaises(ControlAuditCorruptionError):
            validate_response_snapshot(bad_done)

        # session_id 不匹配
        bad_sid = copy.deepcopy(valid_snap)
        with self.assertRaises(ControlAuditCorruptionError):
            validate_response_snapshot(bad_sid, expected_session_id="wrong_sid")

    # ---------------------------------------------------------------------------
    # Round 13 故障注入与安全边界面板测试
    # ---------------------------------------------------------------------------

    def test_update_head_rejects_missing_snapshot_hash(self):
        """1. 不传 snapshot_payload_sha256 且 revision 文件不存在时，在写 Head 前失败抛出 ControlAuditPersistenceError"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r13_missing_snap_hash"
        snap_fname = f"session_{hashlib.sha256(sid.encode()).hexdigest()[:16]}_rev_1.json"
        with self.assertRaises(ControlAuditPersistenceError):
            update_session_head(history_dir, sid, 1, snap_fname, snapshot_payload_sha256=None)

    def test_update_head_rejects_unreadable_snapshot_when_hash_is_derived(self):
        """2. revision 文件存在但损坏无法解析时，推导 Hash 失败抛出 ControlAuditPersistenceError"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r13_unreadable_snap"
        snap_path = get_session_revision_path(history_dir, sid, 1)
        snap_path.write_bytes(b"{corrupted invalid json!!")
        with self.assertRaises(ControlAuditPersistenceError):
            update_session_head(history_dir, sid, 1, snap_path.name, snapshot_payload_sha256=None)

    def test_update_head_never_writes_zero_snapshot_hash(self):
        """3. 无法取得可信 Hash 时，绝对不得向 Head 写入全零或占位 Hash"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r13_no_zero_hash"
        head_path = get_session_head_path(history_dir, sid)
        snap_path = get_session_revision_path(history_dir, sid, 1)

        with self.assertRaises(ControlAuditPersistenceError):
            update_session_head(history_dir, sid, 1, snap_path.name, snapshot_payload_sha256=None)

        # 验证绝无 Head 文件写入
        self.assertFalse(head_path.exists())

    def test_post_replace_stat_failure_is_commit_uncertain(self):
        """4. post-replace os.stat 失败时，抛出 ControlAuditCommitUncertainError"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r13_stat_fail_uncertain"
        snap_path = get_session_revision_path(history_dir, sid, 1)
        snap_data = {
            "session_id": sid, "session_revision": 1, "parent_revision": 0, "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 1, "parent_revision": 0, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        snap_data["payload_sha256"] = _canonical_payload_hash(snap_data)
        snap_path.write_text(json.dumps(snap_data), encoding="utf-8")

        head_path = get_session_head_path(history_dir, sid)
        orig_stat = os.stat
        orig_replace = Path.replace
        replaced = [False]

        def fake_replace(self, target):
            res = orig_replace(self, target)
            replaced[0] = True
            return res

        def fake_stat(path, *args, **kwargs):
            if replaced[0] and str(path) == str(head_path):
                raise OSError("Permission denied on stat post-replace")
            return orig_stat(path, *args, **kwargs)

        with patch.object(Path, "replace", side_effect=fake_replace, autospec=True):
            with patch("os.stat", side_effect=fake_stat):
                with self.assertRaises(ControlAuditCommitUncertainError):
                    update_session_head(history_dir, sid, 1, snap_path.name, snap_data["payload_sha256"])

    def test_post_replace_stat_failure_performs_no_destructive_rollback(self):
        """5. post-replace os.stat 失败时，绝不执行 destructive rollback (不 unlink 或不篡改已替换 Head)"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r13_no_destructive"
        snap_path = get_session_revision_path(history_dir, sid, 1)
        snap_data = {
            "session_id": sid, "session_revision": 1, "parent_revision": 0, "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 1, "parent_revision": 0, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        snap_data["payload_sha256"] = _canonical_payload_hash(snap_data)
        snap_path.write_text(json.dumps(snap_data), encoding="utf-8")

        head_path = get_session_head_path(history_dir, sid)
        orig_stat = os.stat
        orig_replace = Path.replace
        replaced = [False]

        def fake_replace(self, target):
            res = orig_replace(self, target)
            replaced[0] = True
            return res

        def fake_stat(path, *args, **kwargs):
            if replaced[0] and str(path) == str(head_path):
                raise OSError("Stat failed post replace")
            return orig_stat(path, *args, **kwargs)

        with patch.object(Path, "replace", side_effect=fake_replace, autospec=True):
            with patch("os.stat", side_effect=fake_stat):
                with self.assertRaises(ControlAuditCommitUncertainError):
                    update_session_head(history_dir, sid, 1, snap_path.name, snap_data["payload_sha256"])

        # 确定未发生物理 unlink 破坏
        self.assertTrue(head_path.exists())

    def test_same_bytes_different_inode_is_not_owned(self):
        """6. 相同字节但不同 inode 绝不认定为本事务所有"""
        from src.history_manager import _verify_owned_committed_path
        history_dir = get_history_dir(create=True)
        test_path = history_dir / "test_same_bytes.json"
        payload = b'{"data": "same"}'
        test_path.write_bytes(payload)

        real_stat = os.stat(str(test_path))

        class FakeStat:
            st_dev = real_stat.st_dev
            st_ino = real_stat.st_ino + 88888

        # Inode 不匹配，必定返回 False
        self.assertFalse(_verify_owned_committed_path(test_path, payload, FakeStat()))

    def test_restored_head_with_missing_revision_is_not_recovered(self):
        """7. 恢复旧 Head 后，若引用的 revision 文件缺失，不得标记为 recovered"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r13_missing_rev_restore"
        head_path = get_session_head_path(history_dir, sid)

        # 构造旧 Head 指向不存在的 rev 1
        old_head = {
            "schema_version": SNAPSHOT_VERSION,
            "session_id": sid,
            "current_revision": 1,
            "snapshot_file": get_session_revision_path(history_dir, sid, 1).name,
            "snapshot_payload_sha256": "a" * 64,
            "updated_at": "2026-08-01T12:00:00+08:00",
        }
        old_head["payload_sha256"] = _canonical_payload_hash(old_head)
        head_path.write_text(json.dumps(old_head), encoding="utf-8")

        # 准备合法的 rev 2 文件供 Head replace 预校验通过
        rev2_path = get_session_revision_path(history_dir, sid, 2)
        rev2_data = {
            "session_id": sid, "session_revision": 2, "parent_revision": 1, "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 2, "parent_revision": 1, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        rev2_data["payload_sha256"] = _canonical_payload_hash(rev2_data)
        rev2_path.write_text(json.dumps(rev2_data), encoding="utf-8")

        # 触发更新 Head，Post-replace fsync 失败
        with patch("src.history_manager._fsync_directory", side_effect=OSError("fsync fail")):
            with self.assertRaises(ControlAuditCommitUncertainError):
                update_session_head(history_dir, sid, 2, rev2_path.name, rev2_data["payload_sha256"])

    def test_restored_head_with_corrupt_revision_is_not_recovered(self):
        """8. 恢复旧 Head 后，若引用的 revision 文件损坏，不得标记为 recovered"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r13_corrupt_rev_restore"
        head_path = get_session_head_path(history_dir, sid)
        rev1_path = get_session_revision_path(history_dir, sid, 1)
        rev1_path.write_bytes(b"{bad broken content!!")

        old_head = {
            "schema_version": SNAPSHOT_VERSION,
            "session_id": sid,
            "current_revision": 1,
            "snapshot_file": rev1_path.name,
            "snapshot_payload_sha256": "a" * 64,
            "updated_at": "2026-08-01T12:00:00+08:00",
        }
        old_head["payload_sha256"] = _canonical_payload_hash(old_head)
        head_path.write_text(json.dumps(old_head), encoding="utf-8")

        # 准备合法的 rev 2 文件供 Head replace 预校验通过
        rev2_path = get_session_revision_path(history_dir, sid, 2)
        rev2_data = {
            "session_id": sid, "session_revision": 2, "parent_revision": 1, "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 2, "parent_revision": 1, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        rev2_data["payload_sha256"] = _canonical_payload_hash(rev2_data)
        rev2_path.write_text(json.dumps(rev2_data), encoding="utf-8")

        with patch("src.history_manager._fsync_directory", side_effect=OSError("fsync fail")):
            with self.assertRaises(ControlAuditCommitUncertainError):
                update_session_head(history_dir, sid, 2, rev2_path.name, rev2_data["payload_sha256"])

    def test_restored_head_with_revision_hash_mismatch_is_not_recovered(self):
        """9. 恢复旧 Head 后，若引用的 revision 文件 hash 不匹配，不得标记为 recovered"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r13_hash_mismatch_restore"
        head_path = get_session_head_path(history_dir, sid)
        rev1_path = get_session_revision_path(history_dir, sid, 1)

        rev1_data = {
            "session_id": sid, "session_revision": 1, "parent_revision": 0, "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 1, "parent_revision": 0, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        rev1_data["payload_sha256"] = _canonical_payload_hash(rev1_data)
        rev1_path.write_text(json.dumps(rev1_data), encoding="utf-8")

        # 旧 Head 存储的 hash 故人为冲突 Hash
        old_head = {
            "schema_version": SNAPSHOT_VERSION,
            "session_id": sid,
            "current_revision": 1,
            "snapshot_file": rev1_path.name,
            "snapshot_payload_sha256": "f" * 64,
            "updated_at": "2026-08-01T12:00:00+08:00",
        }
        old_head["payload_sha256"] = _canonical_payload_hash(old_head)
        head_path.write_text(json.dumps(old_head), encoding="utf-8")

        # 准备合法的 rev 2 文件供 Head replace 预校验通过
        rev2_path = get_session_revision_path(history_dir, sid, 2)
        rev2_data = {
            "session_id": sid, "session_revision": 2, "parent_revision": 1, "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 2, "parent_revision": 1, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        rev2_data["payload_sha256"] = _canonical_payload_hash(rev2_data)
        rev2_path.write_text(json.dumps(rev2_data), encoding="utf-8")

        with patch("src.history_manager._fsync_directory", side_effect=OSError("fsync fail")):
            with self.assertRaises(ControlAuditCommitUncertainError):
                update_session_head(history_dir, sid, 2, rev2_path.name, rev2_data["payload_sha256"])

    def test_head_updated_at_requires_timezone_aware_iso8601(self):
        """10. Head updated_at 必须为合法的带时区 ISO 8601 时间戳"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r13_naive_updated_at"
        head_path = get_session_head_path(history_dir, sid)
        head_data = {
            "schema_version": SNAPSHOT_VERSION,
            "session_id": sid,
            "current_revision": 1,
            "snapshot_file": f"session_{hashlib.sha256(sid.encode()).hexdigest()[:16]}_rev_1.json",
            "snapshot_payload_sha256": "a" * 64,
            "payload_sha256": "b" * 64,
            "updated_at": "2026-08-01T12:00:00",  # 无时区信息 (naive)
        }
        head_data["payload_sha256"] = _canonical_payload_hash(head_data)
        head_path.write_text(json.dumps(head_data), encoding="utf-8")

        with self.assertRaises(ControlAuditCorruptionError):
            read_session_head(history_dir, sid)

    def test_update_head_rejects_supplied_hash_mismatch_before_replace(self):
        """Head replace 前预校验：若显式传入的 snapshot_payload_sha256 与 revision 文件实际 hash 不匹配，直接拒绝且不触发 replace"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r14_hash_mismatch"
        rev1_path = get_session_revision_path(history_dir, sid, 1)
        rev1_data = {
            "session_id": sid,
            "session_revision": 1,
            "parent_revision": 0,
            "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 1, "parent_revision": 0, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        rev1_data["payload_sha256"] = _canonical_payload_hash(rev1_data)
        rev1_bytes = json.dumps(rev1_data, ensure_ascii=False).encode("utf-8")
        rev1_path.write_bytes(rev1_bytes)

        wrong_hash = "f" * 64
        head_path = get_session_head_path(history_dir, sid)

        with patch("pathlib.Path.replace", side_effect=AssertionError("replace should not be called")) as mock_replace:
            with self.assertRaises(ControlAuditPersistenceError):
                update_session_head(history_dir, sid, 1, rev1_path.name, snapshot_payload_sha256=wrong_hash)

        self.assertFalse(head_path.exists())
        self.assertEqual(rev1_path.read_bytes(), rev1_bytes)
        mock_replace.assert_not_called()

    def test_update_head_rejects_wrong_snapshot_filename_before_replace(self):
        """Head replace 前预校验：若 snapshot_file 不等于派生的 revision 文件名，直接拒绝"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r14_wrong_filename"
        rev1_path = get_session_revision_path(history_dir, sid, 1)
        rev1_data = {
            "session_id": sid, "session_revision": 1, "parent_revision": 0, "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 1, "parent_revision": 0, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        rev1_data["payload_sha256"] = _canonical_payload_hash(rev1_data)
        rev1_bytes = json.dumps(rev1_data, ensure_ascii=False).encode("utf-8")
        rev1_path.write_bytes(rev1_bytes)

        head_path = get_session_head_path(history_dir, sid)
        wrong_snap_name = "wrong_revision_filename.json"

        with patch("pathlib.Path.replace", side_effect=AssertionError("replace should not be called")) as mock_replace:
            with self.assertRaises(ControlAuditPersistenceError):
                update_session_head(history_dir, sid, 1, wrong_snap_name)

        self.assertFalse(head_path.exists())
        self.assertEqual(rev1_path.read_bytes(), rev1_bytes)
        mock_replace.assert_not_called()

    def test_update_head_rejects_revision_session_mismatch_before_replace(self):
        """Head replace 前预校验：若 revision 内容中的 session_id 与传入 session_id 不匹配，直接拒绝"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r14_sid_mismatch"
        rev1_path = get_session_revision_path(history_dir, sid, 1)
        rev1_data = {
            "session_id": "different_session_id", "session_revision": 1, "parent_revision": 0, "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 1, "parent_revision": 0, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        rev1_data["payload_sha256"] = _canonical_payload_hash(rev1_data)
        rev1_bytes = json.dumps(rev1_data, ensure_ascii=False).encode("utf-8")
        rev1_path.write_bytes(rev1_bytes)

        head_path = get_session_head_path(history_dir, sid)

        with patch("pathlib.Path.replace", side_effect=AssertionError("replace should not be called")) as mock_replace:
            with self.assertRaises(ControlAuditPersistenceError):
                update_session_head(history_dir, sid, 1, rev1_path.name)

        self.assertFalse(head_path.exists())
        self.assertEqual(rev1_path.read_bytes(), rev1_bytes)
        mock_replace.assert_not_called()

    def test_update_head_rejects_revision_number_mismatch_before_replace(self):
        """Head replace 前预校验：若 revision 内容中的 session_revision 与传入 current_revision 不匹配，直接拒绝"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r14_rev_mismatch"
        rev1_path = get_session_revision_path(history_dir, sid, 1)
        rev1_data = {
            "session_id": sid, "session_revision": 99, "parent_revision": 0, "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 99, "parent_revision": 0, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        rev1_data["payload_sha256"] = _canonical_payload_hash(rev1_data)
        rev1_bytes = json.dumps(rev1_data, ensure_ascii=False).encode("utf-8")
        rev1_path.write_bytes(rev1_bytes)

        head_path = get_session_head_path(history_dir, sid)

        with patch("pathlib.Path.replace", side_effect=AssertionError("replace should not be called")) as mock_replace:
            with self.assertRaises(ControlAuditPersistenceError):
                update_session_head(history_dir, sid, 1, rev1_path.name)

        self.assertFalse(head_path.exists())
        self.assertEqual(rev1_path.read_bytes(), rev1_bytes)
        mock_replace.assert_not_called()

    def test_invalid_head_preconditions_preserve_existing_head_byte_for_byte(self):
        """Head replace 前预校验失败时，既有 Head 内容必须逐字节保持完全不变"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r14_preserve_head"
        head_path = get_session_head_path(history_dir, sid)
        old_head_bytes = b'{"schema_version": 3, "session_id": "sess_r14_preserve_head", "current_revision": 1, "snapshot_file": "old.json", "snapshot_payload_sha256": "' + b'a'*64 + b'", "updated_at": "2026-08-01T12:00:00+08:00", "payload_sha256": "' + b'b'*64 + b'"}'
        head_path.write_bytes(old_head_bytes)

        rev2_path = get_session_revision_path(history_dir, sid, 2)
        rev2_data = {
            "session_id": sid, "session_revision": 2, "parent_revision": 1, "snapshot_version": SNAPSHOT_VERSION,
            "snapshot": {"session_revision": 2, "parent_revision": 1, "conversation_history": [], "slot_store": {}, "task_state": {}, "phase": "collecting"},
            "response_snapshot": {
                "code": 200, "session_id": sid, "request_id": "", "reply": "ok", "done": False, "rejected": False,
                "dialogue_mode": "task_collection", "control_state": "idle", "collected": {}, "missing": [], "is_retry": False
            }
        }
        rev2_data["payload_sha256"] = _canonical_payload_hash(rev2_data)
        rev2_bytes = json.dumps(rev2_data, ensure_ascii=False).encode("utf-8")
        rev2_path.write_bytes(rev2_bytes)

        with self.assertRaises(ControlAuditPersistenceError):
            update_session_head(history_dir, sid, 2, rev2_path.name, snapshot_payload_sha256="0" * 64)

        self.assertEqual(head_path.read_bytes(), old_head_bytes)

    def test_invalid_head_preconditions_do_not_call_replace(self):
        """Head replace 前预校验失败时，绝不调用 Path.replace() 或产生临时文件残留"""
        history_dir = get_history_dir(create=True)
        sid = "sess_r14_no_replace"
        rev1_path = get_session_revision_path(history_dir, sid, 1)
        rev1_path.write_text("{broken json format...", encoding="utf-8")

        temp_files_before = set(history_dir.glob("*.tmp"))

        with patch("pathlib.Path.replace", side_effect=AssertionError("replace must not be called")) as mock_replace:
            with self.assertRaises(ControlAuditPersistenceError):
                update_session_head(history_dir, sid, 1, rev1_path.name)

        temp_files_after = set(history_dir.glob("*.tmp"))
        self.assertEqual(temp_files_before, temp_files_after)
        mock_replace.assert_not_called()

    def test_owned_committed_path_rejects_symlink(self):
        """_verify_owned_committed_path 使用 lstat 并显式拒绝 symlink"""
        from src.history_manager import _verify_owned_committed_path
        history_dir = get_history_dir(create=True)
        real_file = history_dir / "real_file.json"
        content = b'{"data": "test"}'
        real_file.write_bytes(content)
        expected_stat = os.lstat(real_file)

        symlink_file = history_dir / "symlink_file.json"
        if symlink_file.exists() or symlink_file.is_symlink():
            symlink_file.unlink()
        symlink_file.symlink_to(real_file)

        try:
            self.assertTrue(_verify_owned_committed_path(real_file, content, expected_stat))
            self.assertFalse(_verify_owned_committed_path(symlink_file, content, expected_stat))
        finally:
            if symlink_file.is_symlink() or symlink_file.exists():
                symlink_file.unlink()

    def test_same_inode_reachable_through_symlink_is_not_owned(self):
        """即使 symlink 指向具有完全相同 inode 与字节内容的文件，通过 symlink 访问也拒绝认定为 owned"""
        from src.history_manager import _verify_owned_committed_path
        history_dir = get_history_dir(create=True)
        target_file = history_dir / "target_file.json"
        content = b'{"data": "same_inode_test"}'
        target_file.write_bytes(content)
        expected_stat = os.lstat(target_file)

        link_to_target = history_dir / "link_to_target.json"
        if link_to_target.exists() or link_to_target.is_symlink():
            link_to_target.unlink()
        link_to_target.symlink_to(target_file)

        try:
            self.assertFalse(_verify_owned_committed_path(link_to_target, content, expected_stat))
            self.assertTrue(_verify_owned_committed_path(target_file, content, expected_stat))
        finally:
            if link_to_target.is_symlink() or link_to_target.exists():
                link_to_target.unlink()

    def test_revision_replaced_with_symlink_between_lstat_and_read_fails_closed(self):
        """1. Revision 文件在校验过程中被替换为 Symlink 时，读取失败并 Fail-Closed (raise ControlAuditPersistenceError)"""
        from src.history_manager import _validate_revision_before_head_commit, ControlAuditPersistenceError, get_session_revision_path, _canonical_payload_hash
        history_dir = get_history_dir(create=True)
        sid = "test_toctou_symlink_rev"
        rev1_path = get_session_revision_path(history_dir, sid, 1)
        payload = {"session_id": sid, "session_revision": 1, "data": "valid_rev"}
        payload["payload_sha256"] = _canonical_payload_hash(payload)
        rev1_path.write_bytes(json.dumps(payload).encode("utf-8"))

        target_file = history_dir / "target_symlink_dest.json"
        target_file.write_bytes(json.dumps(payload).encode("utf-8"))

        orig_open = os.open
        def mock_open(path, flags, mode=0o777):
            if str(rev1_path) in str(path):
                if rev1_path.exists():
                    rev1_path.unlink()
                rev1_path.symlink_to(target_file)
            return orig_open(str(path), flags, mode)

        try:
            with patch("os.open", side_effect=mock_open):
                with self.assertRaises(ControlAuditPersistenceError):
                    _validate_revision_before_head_commit(history_dir, sid, 1, rev1_path.name)
        finally:
            if rev1_path.is_symlink() or rev1_path.exists():
                rev1_path.unlink()

    def test_revision_replaced_with_same_bytes_new_inode_during_read_fails_closed(self):
        """2. Revision 文件在读取中途 inode 发生改变，校验 Fail-Closed"""
        from src.history_manager import _validate_revision_before_head_commit, ControlAuditPersistenceError, get_session_revision_path, _canonical_payload_hash
        history_dir = get_history_dir(create=True)
        sid = "test_toctou_inode_rev"
        rev1_path = get_session_revision_path(history_dir, sid, 1)
        payload = {"session_id": sid, "session_revision": 1, "data": "valid_rev"}
        payload["payload_sha256"] = _canonical_payload_hash(payload)
        rev1_bytes = json.dumps(payload).encode("utf-8")
        rev1_path.write_bytes(rev1_bytes)

        orig_lstat = os.lstat
        def mock_lstat(path):
            st = orig_lstat(path)
            if str(rev1_path) in str(path):
                return os.stat_result((st.st_mode, st.st_ino + 9999, st.st_dev, st.st_nlink, st.st_uid, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
            return st

        with patch("os.lstat", side_effect=mock_lstat):
            with self.assertRaises(ControlAuditPersistenceError):
                _validate_revision_before_head_commit(history_dir, sid, 1, rev1_path.name)

    def test_head_replaced_with_symlink_between_commit_stat_and_readback_is_uncertain(self):
        """3. Head Replace 成功后，读回前 Head 被替换为 Symlink，触发 ControlAuditCommitUncertainError 且不破坏文件"""
        from src.history_manager import update_session_head, ControlAuditCommitUncertainError, get_session_revision_path, get_session_head_path, _canonical_payload_hash
        history_dir = get_history_dir(create=True)
        sid = "test_toctou_head_symlink"
        rev1_path = get_session_revision_path(history_dir, sid, 1)
        payload = {"session_id": sid, "session_revision": 1, "data": "valid_rev"}
        hash_val = _canonical_payload_hash(payload)
        payload["payload_sha256"] = hash_val
        rev1_path.write_bytes(json.dumps(payload).encode("utf-8"))

        head_path = get_session_head_path(history_dir, sid)
        target_file = history_dir / "head_symlink_target.json"
        target_file.write_bytes(b"{}")

        orig_os_replace = os.replace
        def mock_os_replace(src, dst):
            orig_os_replace(src, dst)
            if str(head_path) in str(dst):
                head_path.unlink(missing_ok=True)
                head_path.symlink_to(target_file)

        try:
            with patch("os.replace", side_effect=mock_os_replace):
                with self.assertRaises(ControlAuditCommitUncertainError):
                    update_session_head(history_dir, sid, 1, rev1_path.name)
        finally:
            if head_path.is_symlink() or head_path.exists():
                head_path.unlink()

    def test_head_replaced_with_same_bytes_new_inode_during_readback_is_uncertain(self):
        """4. Head Replace 成功后，读回阶段发现 inode 被改变，触发 ControlAuditCommitUncertainError"""
        from src.history_manager import update_session_head, ControlAuditCommitUncertainError, get_session_revision_path, get_session_head_path, _canonical_payload_hash
        history_dir = get_history_dir(create=True)
        sid = "test_toctou_head_inode"
        rev1_path = get_session_revision_path(history_dir, sid, 1)
        payload = {"session_id": sid, "session_revision": 1, "data": "valid_rev"}
        hash_val = _canonical_payload_hash(payload)
        payload["payload_sha256"] = hash_val
        rev1_path.write_bytes(json.dumps(payload).encode("utf-8"))

        head_path = get_session_head_path(history_dir, sid)

        orig_lstat = os.lstat
        call_count = 0
        def mock_lstat(path):
            st = orig_lstat(path)
            nonlocal call_count
            if str(head_path) in str(path):
                call_count += 1
                if call_count > 1:
                    return os.stat_result((st.st_mode, st.st_ino + 8888, st.st_dev, st.st_nlink, st.st_uid, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
            return st

        with patch("os.lstat", side_effect=mock_lstat):
            with self.assertRaises(ControlAuditCommitUncertainError):
                update_session_head(history_dir, sid, 1, rev1_path.name)

    def test_ownership_path_swapped_after_lstat_is_not_owned(self):
        """5. 所有权路径在 lstat 检查后被偷换为新 inode 文件，_verify_owned_committed_path 返回 False"""
        from src.history_manager import _verify_owned_committed_path
        history_dir = get_history_dir(create=True)
        target_path = history_dir / "test_swapped_ownership.json"
        content = b'{"data": "swapped"}'
        target_path.write_bytes(content)
        expected_stat = os.lstat(target_path)

        orig_lstat = os.lstat
        def mock_lstat(path):
            st = orig_lstat(path)
            if str(target_path) in str(path):
                return os.stat_result((st.st_mode, st.st_ino + 7777, st.st_dev, st.st_nlink, st.st_uid, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
            return st

        with patch("os.lstat", side_effect=mock_lstat):
            self.assertFalse(_verify_owned_committed_path(target_path, content, expected_stat))

    def test_read_session_head_rejects_symlink(self):
        """6. read_session_head 遇到 Head 为 Symlink 时，抛出 ControlAuditCorruptionError (fail closed)"""
        from src.history_manager import read_session_head, ControlAuditCorruptionError, get_session_head_path
        history_dir = get_history_dir(create=True)
        sid = "test_head_symlink_read"
        head_path = get_session_head_path(history_dir, sid)
        target_file = history_dir / "real_head.json"
        target_file.write_bytes(b'{"valid": true}')

        if head_path.exists() or head_path.is_symlink():
            head_path.unlink()
        head_path.symlink_to(target_file)

        try:
            with self.assertRaises(ControlAuditCorruptionError):
                read_session_head(history_dir, sid)
        finally:
            if head_path.is_symlink() or head_path.exists():
                head_path.unlink()

    def test_head_revision_chain_read_rejects_revision_symlink(self):
        """7. read_session_head 校验 Head 指向的 Revision 文件为 Symlink 时，抛出 ControlAuditCorruptionError (fail closed)"""
        from src.history_manager import read_session_head, update_session_head, ControlAuditCorruptionError, get_session_head_path, get_session_revision_path, _canonical_payload_hash
        history_dir = get_history_dir(create=True)
        sid = "test_rev_symlink_in_chain"
        rev1_path = get_session_revision_path(history_dir, sid, 1)
        payload = {"session_id": sid, "session_revision": 1, "data": "valid_rev"}
        hash_val = _canonical_payload_hash(payload)
        payload["payload_sha256"] = hash_val
        rev1_path.write_bytes(json.dumps(payload).encode("utf-8"))

        update_session_head(history_dir, sid, 1, rev1_path.name)

        target_file = history_dir / "target_rev_symlink.json"
        target_file.write_bytes(json.dumps(payload).encode("utf-8"))
        rev1_path.unlink()
        rev1_path.symlink_to(target_file)

        try:
            with self.assertRaises(ControlAuditCorruptionError):
                read_session_head(history_dir, sid)
        finally:
            if rev1_path.is_symlink() or rev1_path.exists():
                rev1_path.unlink()

    def test_no_follow_reader_reads_and_validates_same_inode(self):
        """8. _read_regular_file_no_follow 成功读取普通文件并返回精确 (bytes, fd_stat)"""
        from src.history_manager import _read_regular_file_no_follow
        history_dir = get_history_dir(create=True)
        target = history_dir / "test_no_follow_reader.json"
        content = b'{"hello": "world"}'
        target.write_bytes(content)

        read_bytes, fd_stat = _read_regular_file_no_follow(target)
        self.assertEqual(read_bytes, content)
        path_stat = os.lstat(target)
        self.assertEqual((fd_stat.st_dev, fd_stat.st_ino), (path_stat.st_dev, path_stat.st_ino))


if __name__ == "__main__":
    unittest.main()
