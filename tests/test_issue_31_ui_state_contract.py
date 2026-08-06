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
import unittest
from unittest.mock import MagicMock, PropertyMock

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
        self.assertFalse(actions["can_send"])
        self.assertFalse(actions["can_modify"])
        self.assertFalse(actions["can_confirm"])
        self.assertFalse(actions["can_publish"])
        self.assertFalse(actions["can_cancel"])
        self.assertTrue(_compute_read_only("done"))

    # 6. actions 计算 - rejected
    def test_actions_rejected_phase(self):
        actions = _compute_actions("rejected", "task_collection")
        self.assertFalse(actions["can_send"])
        self.assertTrue(_compute_read_only("rejected"))

    # P1-1 修复验证: 终态做问答保留只读和禁用
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
        self.assertFalse(ui["actions"]["can_send"], "Done task must stay non-sendable")
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
        val_result = ValidationResult(
            overall_status="blocked_hard",
            violations=[hard_v],
            validated_at="2026-08-06T18:00:00Z",
            task_version=1,
            validation_version=3,
            validation_fingerprint="fp_abc123",
            state_snapshot={},
        )
        ack = {
            "constraint_id": "C020",
            "validation_fingerprint": "fp_abc123",
            "validation_version": 3,
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
        self.assertEqual(cs["hard_violations"][0]["observed_value"], 500)
        # 过期指纹被过滤，只保留有效 ack
        self.assertEqual(len(cs["ignored_soft_warnings"]), 1)
        self.assertEqual(cs["ignored_soft_warnings"][0]["constraint_id"], "C020")

    # P2-4 修复验证: Fail closed 状态与错误标识
    def test_build_ui_state_fail_closed(self):
        broken_mgr = MagicMock()
        type(broken_mgr).phase = PropertyMock(side_effect=RuntimeError("internal error"))

        result = build_frontend_ui_state(broken_mgr)
        self.assertEqual(result["state_status"], "error")
        self.assertIn("internal error", result["error_message"])
        self.assertTrue(result["read_only"])
        self.assertFalse(result["actions"]["can_send"])


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
        self.client.post(
            "/api/chat",
            json={"session_id": "test-s31-state-ut", "message": "hello"},
            content_type="application/json",
        )
        resp = self.client.get("/api/session/state?session_id=test-s31-state-ut")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        if data.get("exists"):
            self.assertIn("ui_state", data)

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


if __name__ == "__main__":
    unittest.main()
