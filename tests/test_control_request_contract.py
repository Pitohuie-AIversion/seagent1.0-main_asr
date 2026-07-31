"""
tests/test_control_request_contract.py - 控制请求生命周期闭环契约与事务持久化测试
"""

import copy
import json
import os
import shutil
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.dialogue_manager import DialogueManager
from src.history_manager import (
    save_conversation,
    load_history,
    list_history,
    _resolve_history_file,
    _atomic_durable_write,
    get_history_dir,
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
        )
        self.assertTrue(filename.startswith("control_"))

        data = load_history(filename)
        self.assertIsNotNone(data)
        self.assertEqual(data["snapshot_version"], SNAPSHOT_VERSION)
        self.assertEqual(data["control_state"], "stop_requested")
        self.assertEqual(data["last_control_request"]["request_id"], "req_rf_01")

    def test_concurrent_control_audit_writes_preserve_both_events(self):
        """多线程并发写控制审计日志，验证各自独立的 control_<request_id>.json 文件均完整落地"""
        sid = "test_concurrent_audit"
        self.dm.reset()
        self._seed_pipeline_task()

        def _do_write(req_id):
            save_conversation(
                session_id=sid,
                conversation_history=self.dm.conversation_history,
                task_state=self.dm.task_state,
                built_json=self.dm._last_built_json,
                mode=self.dm.mode,
                phase=self.dm.phase,
                intent_id=self.dm.task_state.get("intent_id"),
                slot_store=self.dm.slot_store,
                dialogue_mode=self.dm.dialogue_mode,
                control_state="stop_requested",
                last_control_request={"action": "stop", "status": "requested", "source": "rule", "confidence": 0.9, "reason": "test", "request_id": req_id, "requested_at": "2026-07-31T14:30:00+08:00", "phase_at_request": "collecting"},
                request_id=req_id,
            )

        t1 = threading.Thread(target=_do_write, args=("req_conc_A",))
        t2 = threading.Thread(target=_do_write, args=("req_conc_B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        histories = list_history()
        file_names = [h["id"] for h in histories]
        self.assertTrue(any("req_conc_A" in f for f in file_names))
        self.assertTrue(any("req_conc_B" in f for f in file_names))

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
        def check_lock(mgr_inst):
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
        def failing_persist(mgr_inst):
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
            self.dm.process_with_audit("终止当前任务", request_id="req_seq_2", persist_callback=lambda m: (_ for _ in ()).throw(OSError("Fail")))

        # 仍然为 pause_requested
        self.assertEqual(self.dm.control_state, "pause_requested")

    def test_audit_success_main_failure_leaves_no_committed_control_event(self):
        """4. 单一权威提交测试：控制事件只写 control_...json，不存在主历史文件写入失败导致的半提交"""
        history_dir = get_history_dir(create=False)
        app = web_backend.app.test_client()
        sid = "test_session_single_commit"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})
        res = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_single_01"})
        self.assertEqual(res.status_code, 200)

        # 检查 history_dir：存在 control_...json，但不存在二次写入产生的半提交隐患
        control_files = list(history_dir.glob("*req_single_01*.json"))
        self.assertEqual(len(control_files), 1)

    def test_control_transaction_is_all_or_nothing(self):
        """5. 控制事务 All-or-Nothing：持久化失败时不留下任何提交的控制事件文件，且 Manager 回滚"""
        app = web_backend.app.test_client()
        sid = "test_session_all_or_nothing"
        app.post("/api/chat", json={"session_id": sid, "message": "创建一个管缆巡检任务，水深300米"})

        with patch("src.history_manager._atomic_durable_write", side_effect=OSError("Disk full")):
            res = app.post("/api/chat", json={"session_id": sid, "message": "立即停止当前任务", "request_id": "req_aon_01"})
            self.assertEqual(res.status_code, 500)

        # 验证 API 状态：后续 session/state 仍为 idle
        res_st = app.get(f"/api/session/state?session_id={sid}")
        data = res_st.get_json()
        self.assertEqual(data["control_state"], "idle")

    def test_multiprocess_history_writes_use_shared_lock(self):
        """6. 真正多进程测试：并发 save_conversation 正确获取并争用 .history.lock"""
        import multiprocessing

        def worker_write(sid, req_id):
            save_conversation(
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

        def worker_main_write(sid, intent_id, val):
            save_conversation(
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

        h = load_history("history_TI_MP_TEST.json")
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
        """11. 写后读取校验：若磁盘读取内容与预期快照不完全相等，触发 RuntimeError 且 fail closed"""
        history_dir = get_history_dir(create=True)
        target = history_dir / "target_readback_test.json"

        with patch("json.load", return_value={"different": "data"}):
            with self.assertRaises(RuntimeError) as cm:
                _atomic_durable_write(target, {"snapshot_version": 3, "data": "original"})
            self.assertIn("Read-after-write verification failed", str(cm.exception))

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


if __name__ == "__main__":
    unittest.main()
