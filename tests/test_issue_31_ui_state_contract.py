"""
tests/test_issue_31_ui_state_contract.py

Issue #31：统一前后端任务 UI 状态契约 (Python stdlib unittest 版本)

验证：
1. build_frontend_ui_state() 在各 phase 下返回正确 actions 和 read_only；
2. 修复 P1-1：任务在建或终态（done/rejected）下发起知识问答，已有 slots 不会被抹空，终态只读与禁用不被解除；
3. 修复 P1-2：从 SlotStore ValidationResult 与 Acknowledgements 权威指纹中提取约束状态；
4. 修复 P2-3/P2-4：包含 schema_type/value_type，fail closed 时返回 state_status="error"；
5. API 接口（/api/chat, /api/session/state, /api/history/load）均包含 ui_state 且锁操作原子同步；
6. 支持 python -m unittest 自动收集与执行。
"""

import json
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

# 确保项目根目录在 sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.ui_state_builder import (
    build_frontend_ui_state,
    _compute_actions,
    _compute_read_only,
    _build_constraint_state,
    _build_slots,
)
from src.validator import Violation, ValidationResult


def make_mock_manager(
    phase="collecting",
    dialogue_mode="task_collection",
    mode="normal",
    task_type_key="pipeline_inspection",
    task_id=None,
    task_id_preview=None,
    blocking_violations=None,
    soft_whitelist=None,
    slot_snapshot=None,
    schema_fields=None,
    validation_result=None,
    validation_acknowledgements=None,
):
    mgr = MagicMock()
    mgr.phase = phase
    mgr.dialogue_mode = dialogue_mode
    mgr.mode = mode
    mgr.task_state = {
        "task_type_key": task_type_key,
        "task_id": task_id,
    }
    mgr.task_id_preview = task_id_preview
    mgr._blocking_violations = blocking_violations or []
    mgr._soft_whitelist = soft_whitelist or set()

    # slot_store
    _snap = slot_snapshot if slot_snapshot is not None else {}
    mgr.slot_store.get_slot_snapshot.return_value = _snap
    mgr.slot_store.version = 1

    if validation_result is not None:
        mgr.slot_store.validation_result = validation_result
    elif hasattr(mgr.slot_store, "validation_result"):
        delattr(mgr.slot_store, "validation_result")

    if validation_acknowledgements is not None:
        mgr.slot_store.validation_acknowledgements = validation_acknowledgements

    # builder
    _schema = schema_fields if schema_fields is not None else []
    mgr.builder.get_schema.return_value = _schema
    mgr.builder.resolve_allowed_values.side_effect = lambda fd, ttk, ts: fd.get("allowed_values", [])

    return mgr


_SIMPLE_SCHEMA = [
    {"key": "equipment_family", "label": "作业机器人系列", "type": "string", "allowed_values": ["WROV", "AUV"]},
    {"key": "water_depth", "label": "水深", "type": "number", "allowed_values": []},
    {"key": "intent_id", "label": "意图ID", "type": "auto", "allowed_values": []},
]


class TestUIStateContract(unittest.TestCase):

    # 1. actions 计算 - collecting
    def test_actions_collecting_phase(self):
        actions = _compute_actions("collecting", "task_collection")
        self.assertTrue(actions["can_send"])
        self.assertTrue(actions["can_modify"])
        self.assertFalse(actions["can_confirm"])
        self.assertFalse(actions["can_ignore_soft_warning"])
        self.assertFalse(actions["can_publish"])
        self.assertTrue(actions["can_cancel"])

    # 2. actions 计算 - confirming
    def test_actions_confirming_phase(self):
        actions = _compute_actions("confirming", "task_collection")
        self.assertTrue(actions["can_confirm"])
        self.assertTrue(actions["can_publish"])
        self.assertTrue(actions["can_cancel"])
        self.assertTrue(actions["can_send"])

    # 3. actions 计算 - blocked_soft
    def test_actions_blocked_soft_phase(self):
        actions = _compute_actions("blocked_soft", "task_collection")
        self.assertTrue(actions["can_ignore_soft_warning"])
        self.assertFalse(actions["can_publish"])
        self.assertFalse(actions["can_confirm"])

    # 4. actions 计算 - blocked_hard
    def test_actions_blocked_hard_phase(self):
        actions = _compute_actions("blocked_hard", "task_collection")
        self.assertFalse(actions["can_ignore_soft_warning"])
        self.assertFalse(actions["can_publish"])
        self.assertFalse(actions["can_confirm"])
        self.assertTrue(actions["can_send"])

    # 5. actions 计算 - done
    def test_actions_done_phase(self):
        actions = _compute_actions("done", "task_collection")
        self.assertTrue(actions["can_send"])
        self.assertFalse(actions["can_modify"])
        self.assertFalse(actions["can_confirm"])
        self.assertFalse(actions["can_publish"])
        self.assertFalse(actions["can_cancel"])
        self.assertTrue(_compute_read_only("done"))

    # 6. actions 计算 - rejected
    def test_actions_rejected_phase(self):
        actions = _compute_actions("rejected", "task_collection")
        self.assertTrue(actions["can_send"])
        self.assertTrue(_compute_read_only("rejected"))

    # 终态任务保持只读，但对话仍可继续
    def test_done_phase_with_knowledge_qa_preserves_readonly(self):
        mgr = make_mock_manager(
            phase="done",
            dialogue_mode="knowledge_qa",
            task_type_key="pipeline_inspection",
            schema_fields=_SIMPLE_SCHEMA,
            slot_snapshot={"water_depth": {"value": 300, "status": "valid", "version": 1}},
        )
        ui = build_frontend_ui_state(mgr)
        self.assertTrue(ui["read_only"], "Done task must stay read_only even during QA")
        self.assertTrue(ui["actions"]["can_send"], "Done task must allow read-only chat")
        self.assertEqual(len(ui["slots"]), 2, "Task slots must not be cleared during QA")

    # P1-1 修复验证: 活动任务期间问答保留已有 slots
    def test_active_task_with_knowledge_qa_preserves_slots(self):
        mgr = make_mock_manager(
            phase="collecting",
            dialogue_mode="knowledge_qa",
            task_type_key="pipeline_inspection",
            schema_fields=_SIMPLE_SCHEMA,
            slot_snapshot={"water_depth": {"value": 300, "status": "valid", "version": 1}},
        )
        ui = build_frontend_ui_state(mgr)
        self.assertEqual(len(ui["slots"]), 2, "Active task slots must be preserved during QA")
        self.assertTrue(ui["actions"]["can_send"])
        self.assertFalse(ui["actions"]["can_modify"])

    # P2-3 修复验证: 槽位 schema_type 和 canonical value_type 区分
    def test_slot_merge_schema_type_and_value_type(self):
        slot_snap = {
            "equipment_family": {
                "value": "WROV",
                "value_type": "string",
                "status": "valid",
                "version": 2,
            },
        }
        mgr = make_mock_manager(schema_fields=_SIMPLE_SCHEMA, slot_snapshot=slot_snap)
        slots = _build_slots(mgr, slot_snapshot=slot_snap)
        ef_slot = next(s for s in slots if s["key"] == "equipment_family")
        self.assertEqual(ef_slot["schema_type"], "string")
        self.assertEqual(ef_slot["value_type"], "string")

    # P1-2 修复验证: 结合 Issue #14 ValidationResult 权威结果
    def test_constraint_state_from_validation_result(self):
        hard_v = Violation(
            constraint_id="C013",
            constraint_name="水深超限",
            message="超过最大水深",
            severity="hard",
            related_fields=["water_depth"],
            check_type="range",
            observed_value=500,
            threshold=300,
        )
        soft_v = Violation(
            constraint_id="C020",
            constraint_name="天气预警",
            message="风浪较大",
            severity="soft",
            related_fields=["weather"],
        )
        state_snap = {"status_ref": "ref_valid", "state_version": 1}
        val_result = ValidationResult(
            overall_status="blocked_hard",
            validated_at="2026-08-06T18:00:00Z",
            task_version=1,
            validation_version=3,
            validation_fingerprint="fp_abc123",
            state_snapshot=state_snap,
            violations=[hard_v, soft_v],
        )
        ack = {
            "constraint_id": "C020",
            "task_version": 1,
            "validation_version": 3,
            "validation_fingerprint": "fp_abc123",
            "status_ref": "ref_valid",
            "state_version": 1,
        }
        invalid_ack = {
            "constraint_id": "C021",
            "validation_fingerprint": "fp_stale_999",
            "validation_version": 1,
        }
        mgr = make_mock_manager(
            phase="blocked_hard",
            validation_result=val_result,
            validation_acknowledgements=[ack, invalid_ack],
        )
        cs = _build_constraint_state(mgr)
        self.assertEqual(cs["status"], "blocked_hard")
        self.assertEqual(cs["validation_fingerprint"], "fp_abc123")
        self.assertEqual(len(cs["hard_violations"]), 1)
        self.assertEqual(cs["hard_violations"][0]["check_type"], "range")
        # 过期指纹被过滤，只保留有效 ack
        self.assertEqual(len(cs["ignored_soft_warnings"]), 1)
        self.assertEqual(cs["ignored_soft_warnings"][0]["constraint_id"], "C020")
        # 被忽略的软警告不再出现在 soft_warnings 中
        self.assertEqual(len(cs["soft_warnings"]), 0)

    def test_soft_warnings_filter_partial_ignored(self):
        """测试存在多个软警告时，仅已确认忽略的被过滤，未确认的仍保留在 soft_warnings 中。"""
        soft_v1 = Violation(
            constraint_id="C010",
            constraint_name="定位风险",
            message="DVL失效风险",
            severity="soft",
            related_fields=["pipeline_type"],
        )
        soft_v2 = Violation(
            constraint_id="C020",
            constraint_name="天气预警",
            message="风浪较大",
            severity="soft",
            related_fields=["weather"],
        )
        state_snap = {"status_ref": "ref_valid", "state_version": 1}
        val_result = ValidationResult(
            overall_status="blocked_soft",
            validated_at="2026-08-06T18:00:00Z",
            task_version=1,
            validation_version=1,
            validation_fingerprint="fp_test",
            state_snapshot=state_snap,
            violations=[soft_v1, soft_v2],
        )
        ack_v1 = {
            "constraint_id": "C010",
            "task_version": 1,
            "validation_version": 1,
            "validation_fingerprint": "fp_test",
            "status_ref": "ref_valid",
            "state_version": 1,
        }
        mgr = make_mock_manager(
            phase="blocked_soft",
            validation_result=val_result,
            validation_acknowledgements=[ack_v1],
        )
        cs = _build_constraint_state(mgr)
        self.assertEqual(len(cs["ignored_soft_warnings"]), 1)
        self.assertEqual(cs["ignored_soft_warnings"][0]["constraint_id"], "C010")
        self.assertEqual(len(cs["soft_warnings"]), 1)
        self.assertEqual(cs["soft_warnings"][0]["constraint_id"], "C020")

    # P2-4 修复验证: Fail closed 状态与错误标识
    def test_build_ui_state_fail_closed(self):
        broken_mgr = MagicMock()
        type(broken_mgr).phase = PropertyMock(side_effect=RuntimeError("internal error"))

        result = build_frontend_ui_state(broken_mgr)
        self.assertEqual(result["state_status"], "error")
        self.assertIn("internal error", result["error_message"])
        self.assertTrue(result["read_only"])
        self.assertFalse(result["actions"]["can_send"])



    # 九-1: conflict 状态保留 value 和 candidate_value
    def test_conflict_slot_preserves_value_and_candidate_value(self):
        slot_snap = {
            "equipment_family": {
                "value": "观察级深海机器人",
                "candidate_value": "轻型工作级深海机器人",
                "status": "conflict",
                "version": 3,
                "raw_value": "轻型工作级深海机器人",
            }
        }
        mgr = make_mock_manager(schema_fields=_SIMPLE_SCHEMA, slot_snapshot=slot_snap)
        slots = _build_slots(mgr, slot_snapshot=slot_snap)
        conf_slot = next(s for s in slots if s["key"] == "equipment_family")
        self.assertEqual(conf_slot["status"], "conflict")
        self.assertEqual(conf_slot["value"], "观察级深海机器人")
        self.assertEqual(conf_slot["candidate_value"], "轻型工作级深海机器人")

    # 九-2: unresolved 状态保留 raw_value、candidate_value 和 allowed_values
    def test_unresolved_slot_preserves_raw_candidate_and_allowed_values(self):
        slot_snap = {
            "equipment_family": {
                "value": None,
                "candidate_value": "ROV",
                "raw_value": "机器人",
                "status": "unresolved",
                "version": 1,
            }
        }
        mgr = make_mock_manager(schema_fields=_SIMPLE_SCHEMA, slot_snapshot=slot_snap)
        slots = _build_slots(mgr, slot_snapshot=slot_snap)
        unres_slot = next(s for s in slots if s["key"] == "equipment_family")
        self.assertEqual(unres_slot["status"], "unresolved")
        self.assertEqual(unres_slot["raw_value"], "机器人")
        self.assertEqual(unres_slot["candidate_value"], "ROV")
        self.assertEqual(unres_slot["allowed_values"], ["WROV", "AUV"])

    # 九-3: stale acknowledgement 因 task_version 不匹配被过滤
    def test_stale_ack_filtered_by_task_version(self):
        val_result = ValidationResult(
            overall_status="blocked_soft",
            validated_at="2026-08-06T20:00:00Z",
            task_version=2,
            validation_version=1,
            validation_fingerprint="fp_100",
            state_snapshot={"status_ref": "ref1", "state_version": 1},
            violations=[Violation("C01", "W", "Warning", "soft")],
        )
        stale_ack = {
            "constraint_id": "C01",
            "task_version": 1, # 不匹配 (当前为 2)
            "validation_version": 1,
            "validation_fingerprint": "fp_100",
            "status_ref": "ref1",
            "state_version": 1,
        }
        mgr = make_mock_manager(validation_result=val_result, validation_acknowledgements=[stale_ack])
        cs = _build_constraint_state(mgr)
        self.assertEqual(len(cs["ignored_soft_warnings"]), 0)
        self.assertEqual(len(cs["legacy_acknowledgements"]), 1)

    # 九-4: stale acknowledgement 因 fingerprint 不匹配被过滤
    def test_stale_ack_filtered_by_fingerprint(self):
        val_result = ValidationResult(
            overall_status="blocked_soft",
            validated_at="2026-08-06T20:00:00Z",
            task_version=1,
            validation_version=1,
            validation_fingerprint="fp_current_999",
            state_snapshot={"status_ref": "ref1", "state_version": 1},
            violations=[Violation("C01", "W", "Warning", "soft")],
        )
        stale_ack = {
            "constraint_id": "C01",
            "task_version": 1,
            "validation_version": 1,
            "validation_fingerprint": "fp_old_000", # 不匹配
            "status_ref": "ref1",
            "state_version": 1,
        }
        mgr = make_mock_manager(validation_result=val_result, validation_acknowledgements=[stale_ack])
        cs = _build_constraint_state(mgr)
        self.assertEqual(len(cs["ignored_soft_warnings"]), 0)
        self.assertEqual(len(cs["legacy_acknowledgements"]), 1)

    # 九-5: stale acknowledgement 因 state_version 不匹配被过滤
    def test_stale_ack_filtered_by_state_version(self):
        val_result = ValidationResult(
            overall_status="blocked_soft",
            validated_at="2026-08-06T20:00:00Z",
            task_version=1,
            validation_version=1,
            validation_fingerprint="fp_100",
            state_snapshot={"status_ref": "ref1", "state_version": 5},
            violations=[Violation("C01", "W", "Warning", "soft")],
        )
        stale_ack = {
            "constraint_id": "C01",
            "task_version": 1,
            "validation_version": 1,
            "validation_fingerprint": "fp_100",
            "status_ref": "ref1",
            "state_version": 2, # 不匹配 (当前为 5)
        }
        mgr = make_mock_manager(validation_result=val_result, validation_acknowledgements=[stale_ack])
        cs = _build_constraint_state(mgr)
        self.assertEqual(len(cs["ignored_soft_warnings"]), 0)
        self.assertEqual(len(cs["legacy_acknowledgements"]), 1)

    # 九-6: ValidationResult dict 形式可以正常序列化
    def test_validation_result_dict_serialization(self):
        val_dict = {
            "overall_status": "blocked_soft",
            "validated_at": "2026-08-06T20:00:00Z",
            "task_version": 3,
            "validation_version": 2,
            "validation_fingerprint": "fp_dict_test",
            "state_snapshot": {"status_ref": "ref_a", "state_version": 1},
            "violations": [
                {"constraint_id": "C05", "constraint_name": "超限预警", "message": "预警信息", "severity": "soft"}
            ],
            "error": None,
        }
        mgr = make_mock_manager(validation_result=val_dict)
        cs = _build_constraint_state(mgr)
        self.assertEqual(cs["source"], "validation_result")
        self.assertEqual(cs["validation_fingerprint"], "fp_dict_test")
        self.assertEqual(len(cs["soft_warnings"]), 1)

    # 九-7: constraint_state 返回 task_version/state_snapshot/error 等完整契约字段
    def test_constraint_state_returns_full_contract_fields(self):
        val_result = ValidationResult(
            overall_status="none",
            validated_at="2026-08-06T20:00:00Z",
            task_version=10,
            validation_version=4,
            validation_fingerprint="fp_full_contract",
            state_snapshot={"status_ref": "ref_main", "state_version": 2},
            violations=[],
            error=None,
        )
        mgr = make_mock_manager(validation_result=val_result)
        cs = _build_constraint_state(mgr)
        self.assertIn("task_version", cs)
        self.assertEqual(cs["task_version"], 10)
        self.assertIn("state_snapshot", cs)
        self.assertEqual(cs["state_snapshot"]["status_ref"], "ref_main")
        self.assertIn("source", cs)
        self.assertEqual(cs["source"], "validation_result")


class TestFlaskAPIIntegration(unittest.TestCase):

    def setUp(self):
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("SEAGENT_OFFLINE_MOCK", "1")
        import web_backend
        web_backend.app.config["TESTING"] = True
        self.client = web_backend.app.test_client()

    def test_api_chat_returns_ui_state(self):
        resp = self.client.post(
            "/api/chat",
            json={"session_id": "test-s31-unittest", "message": "你好"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("ui_state", data)
        ui = data["ui_state"]
        self.assertEqual(ui["state_status"], "ok")
        self.assertIn("actions", ui)
        self.assertIn("slots", ui)

    def test_api_session_state_returns_ui_state(self):
        post_resp = self.client.post(
            "/api/chat",
            json={"session_id": "test-s31-state-ut", "message": "hello"},
            content_type="application/json",
        )
        self.assertEqual(post_resp.status_code, 200)
        post_data = post_resp.get_json()
        self.assertIn("ui_state", post_data)
        post_ui = post_data["ui_state"]
        self.assertEqual(post_ui["state_status"], "ok")
        self.assertIn("actions", post_ui)
        self.assertIn("slots", post_ui)

        resp = self.client.get("/api/session/state?session_id=test-s31-state-ut")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIs(data.get("exists"), True)
        self.assertIn("ui_state", data)
        ui = data["ui_state"]
        self.assertEqual(ui["state_status"], "ok")
        self.assertIn("actions", ui)
        self.assertIn("slots", ui)

    def test_compat_fields_still_present(self):
        resp = self.client.post(
            "/api/chat",
            json={"session_id": "test-s31-compat-ut", "message": "管缆巡检"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        for field in ("done", "rejected", "collected", "missing"):
            self.assertIn(field, data)

    def test_api_reset_waits_for_session_lock_before_resetting_manager(self):
        import web_backend

        sid = "test-s31-reset-lock"
        manager = web_backend.DialogueManager(
            web_backend._shared_llm,
            web_backend._shared_kb,
            session_id=sid,
        )
        with web_backend._sessions_lock:
            web_backend._sessions_manager[sid] = manager

        result = {}

        def invoke_reset():
            with web_backend.app.test_client() as client:
                response = client.post(
                    "/api/reset",
                    json={"session_id": sid},
                    content_type="application/json",
                )
                result["status_code"] = response.status_code
                result["payload"] = response.get_json()

        with manager._session_lock:
            worker = threading.Thread(target=invoke_reset)
            worker.start()
            worker.join(timeout=0.2)
            self.assertTrue(
                worker.is_alive(),
                "reset must wait for an in-flight operation holding the session lock",
            )

        worker.join(timeout=2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result.get("status_code"), 200)
        self.assertEqual(result.get("payload"), {"ok": True, "reset": True})
        with web_backend._sessions_lock:
            self.assertNotIn(sid, web_backend._sessions_manager)

    def test_api_chat_rejects_manager_invalidated_before_processing(self):
        import web_backend

        sid = "test-s31-invalidated-manager"
        manager = web_backend.DialogueManager(
            web_backend._shared_llm,
            web_backend._shared_kb,
            session_id=sid,
        )
        with web_backend._sessions_lock:
            web_backend._sessions_manager[sid] = manager

        manager_captured = threading.Event()
        result = {}

        def capture_manager(requested_sid):
            self.assertEqual(requested_sid, sid)
            manager_captured.set()
            return manager

        def invoke_chat():
            with web_backend.app.test_client() as client:
                response = client.post(
                    "/api/chat",
                    json={"session_id": sid, "message": "你好"},
                    content_type="application/json",
                )
                result["status_code"] = response.status_code
                result["payload"] = response.get_json()

        with patch.object(web_backend, "get_or_create_manager", side_effect=capture_manager):
            with manager._session_lock:
                worker = threading.Thread(target=invoke_chat)
                worker.start()
                self.assertTrue(manager_captured.wait(timeout=1.0))
                with web_backend._sessions_lock:
                    web_backend._sessions_manager.pop(sid, None)

            worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result.get("status_code"), 409)
        self.assertEqual(result.get("payload", {}).get("error"), "SessionReset")
        with web_backend._sess_lock:
            self.assertNotIn(sid, web_backend._sessions)


if __name__ == "__main__":
    unittest.main()
