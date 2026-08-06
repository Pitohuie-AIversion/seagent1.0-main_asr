"""
tests/test_issue_31_ui_state_contract.py

Issue #31：统一前后端任务 UI 状态契约

验证：
1. build_frontend_ui_state() 在各 phase 下返回正确 actions 和 read_only
2. slots 字段正确合并 schema + slot_snapshot
3. constraint_state 正确序列化 Violation 对象
4. 三个 API 接口（/api/chat, /api/session/state, /api/history/load）均包含 ui_state
5. 普通对话不受影响（回归保护）
6. 旧兼容字段（collected/missing/done）仍存在
"""

import json
import sys
import os
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

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
from src.validator import Violation


# ─────────────────────────────────────────────────────────────────────────────
# 辅助函数：构建 mock DialogueManager
# ─────────────────────────────────────────────────────────────────────────────

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
    mgr.slot_store.version = max((item.get("version", 0) for item in _snap.values()), default=0)

    # builder
    _schema = schema_fields if schema_fields is not None else []
    mgr.builder.get_schema.return_value = _schema
    # resolve_allowed_values 降级为静态值
    mgr.builder.resolve_allowed_values.side_effect = lambda fd, ttk, ts: fd.get("allowed_values", [])

    return mgr


# 简单 schema 字段（非 auto/fixed）
_SIMPLE_SCHEMA = [
    {"key": "equipment_family", "label": "作业机器人系列", "type": "string", "allowed_values": ["WROV", "AUV"]},
    {"key": "water_depth", "label": "水深", "type": "number", "allowed_values": []},
    # auto 字段，不应暴露给前端
    {"key": "intent_id", "label": "意图ID", "type": "auto", "allowed_values": []},
]


# ─────────────────────────────────────────────────────────────────────────────
# 1. actions 计算 - collecting
# ─────────────────────────────────────────────────────────────────────────────

def test_actions_collecting_phase():
    actions = _compute_actions("collecting", "task_collection")
    assert actions["can_send"] is True
    assert actions["can_modify"] is True
    assert actions["can_confirm"] is False
    assert actions["can_ignore_soft_warning"] is False
    assert actions["can_publish"] is False
    assert actions["can_cancel"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 2. actions 计算 - confirming
# ─────────────────────────────────────────────────────────────────────────────

def test_actions_confirming_phase():
    actions = _compute_actions("confirming", "task_collection")
    assert actions["can_confirm"] is True
    assert actions["can_publish"] is True
    assert actions["can_cancel"] is True
    assert actions["can_send"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. actions 计算 - blocked_soft
# ─────────────────────────────────────────────────────────────────────────────

def test_actions_blocked_soft_phase():
    actions = _compute_actions("blocked_soft", "task_collection")
    assert actions["can_ignore_soft_warning"] is True
    assert actions["can_publish"] is False
    assert actions["can_confirm"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 4. actions 计算 - blocked_hard（不允许绕过）
# ─────────────────────────────────────────────────────────────────────────────

def test_actions_blocked_hard_phase():
    actions = _compute_actions("blocked_hard", "task_collection")
    assert actions["can_ignore_soft_warning"] is False
    assert actions["can_publish"] is False
    assert actions["can_confirm"] is False
    assert actions["can_send"] is True  # 用户仍可输入修正


# ─────────────────────────────────────────────────────────────────────────────
# 5. done 阶段：read_only=True，所有 can_* = False
# ─────────────────────────────────────────────────────────────────────────────

def test_actions_done_phase():
    actions = _compute_actions("done", "task_collection")
    assert actions["can_send"] is False
    assert actions["can_modify"] is False
    assert actions["can_confirm"] is False
    assert actions["can_publish"] is False
    assert actions["can_cancel"] is False
    assert _compute_read_only("done") is True


# ─────────────────────────────────────────────────────────────────────────────
# 6. rejected 阶段：read_only=True
# ─────────────────────────────────────────────────────────────────────────────

def test_actions_rejected_phase():
    actions = _compute_actions("rejected", "task_collection")
    assert actions["can_send"] is False
    assert _compute_read_only("rejected") is True


# ─────────────────────────────────────────────────────────────────────────────
# 7. slots 合并：schema 字段 + slot_snapshot 运行时状态
# ─────────────────────────────────────────────────────────────────────────────

def test_slot_merge_with_schema():
    slot_snap = {
        "equipment_family": {
            "value": "WROV",
            "raw_value": "工作级ROV",
            "status": "valid",
            "source": "user_input",
            "confidence": 0.95,
            "validation_error": None,
            "candidate_value": None,
            "version": 2,
        },
        "water_depth": {
            "value": None,
            "raw_value": None,
            "status": "missing",
            "source": None,
            "confidence": None,
            "validation_error": None,
            "candidate_value": None,
            "version": 0,
        },
    }
    mgr = make_mock_manager(schema_fields=_SIMPLE_SCHEMA, slot_snapshot=slot_snap)
    slots = _build_slots(mgr)

    # auto 字段 intent_id 不应出现
    keys = [s["key"] for s in slots]
    assert "equipment_family" in keys
    assert "water_depth" in keys
    assert "intent_id" not in keys

    ef_slot = next(s for s in slots if s["key"] == "equipment_family")
    assert ef_slot["status"] == "valid"
    assert ef_slot["value"] == "WROV"
    assert ef_slot["version"] == 2
    assert "WROV" in ef_slot["allowed_values"]
    # label 应为 dict
    assert isinstance(ef_slot["label"], dict)

    wd_slot = next(s for s in slots if s["key"] == "water_depth")
    assert wd_slot["status"] == "missing"
    assert wd_slot["value"] is None


# ─────────────────────────────────────────────────────────────────────────────
# 8. constraint_state 序列化
# ─────────────────────────────────────────────────────────────────────────────

def test_constraint_state_serialization():
    hard_v = Violation(
        constraint_id="C013",
        constraint_name="作业水深超限",
        message="当前水深超过机器人最大作业深度",
        severity="hard",
        related_fields=["water_depth"],
    )
    soft_v = Violation(
        constraint_id="C020",
        constraint_name="流速较高",
        message="海流流速偏高，建议检查",
        severity="soft",
        related_fields=["current_speed"],
    )
    mgr = make_mock_manager(
        phase="blocked_hard",
        blocking_violations=[hard_v, soft_v],
    )
    cs = _build_constraint_state(mgr)
    assert cs["status"] == "hard_blocked"
    assert len(cs["hard_violations"]) == 1
    assert cs["hard_violations"][0] == {"code": "C013", "message": "当前水深超过机器人最大作业深度", "severity": "hard", "field": "water_depth"}
    assert len(cs["soft_warnings"]) == 1
    assert cs["soft_warnings"][0]["code"] == "C020"
    assert cs["soft_warnings"][0]["field"] == "current_speed"


# ─────────────────────────────────────────────────────────────────────────────
# 9. build_frontend_ui_state 整体结构正确
# ─────────────────────────────────────────────────────────────────────────────

def test_build_ui_state_collecting():
    mgr = make_mock_manager(
        phase="collecting",
        schema_fields=_SIMPLE_SCHEMA,
        slot_snapshot={"water_depth": {"value": None, "status": "missing", "version": 0}},
    )
    ui = build_frontend_ui_state(mgr)
    assert ui["phase"] == "collecting"
    assert ui["dialogue_mode"] == "task_collection"
    assert ui["read_only"] is False
    assert isinstance(ui["slots"], list)
    assert isinstance(ui["constraint_state"], dict)
    assert isinstance(ui["actions"], dict)
    assert ui["actions"]["can_send"] is True
    assert ui["actions"]["can_publish"] is False
    # constraint_state 结构完整
    assert "hard_violations" in ui["constraint_state"]
    assert "soft_warnings" in ui["constraint_state"]
    assert "ignored_soft_warnings" in ui["constraint_state"]


# ─────────────────────────────────────────────────────────────────────────────
# 10. fail closed：manager 内部异常时返回安全默认值
# ─────────────────────────────────────────────────────────────────────────────

def test_build_ui_state_fail_closed():
    """build_frontend_ui_state 内部异常时必须 fail closed，不抛出。"""
    broken_mgr = MagicMock()
    # 访问 phase 时抛出异常
    type(broken_mgr).phase = PropertyMock(side_effect=RuntimeError("internal error"))

    result = build_frontend_ui_state(broken_mgr)
    # 必须返回安全默认值，不能抛出
    assert isinstance(result, dict)
    assert result["read_only"] is True
    assert result["actions"]["can_send"] is False
    assert result["actions"]["can_publish"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 11. 普通对话 dialogue_mode 时 actions 正确
# ─────────────────────────────────────────────────────────────────────────────

def test_ordinary_chat_actions():
    """普通 LLM 对话时 dialogue_mode != task_collection，can_modify/confirm/publish = False。"""
    actions = _compute_actions("collecting", "normal_chat")
    assert actions["can_send"] is True
    assert actions["can_modify"] is False
    assert actions["can_confirm"] is False
    assert actions["can_publish"] is False
    assert actions["can_cancel"] is False


def test_ordinary_chat_ui_state_slots_empty():
    """知识问答保留旧任务状态时，UI 仍必须可交互且不暴露旧 slots。"""
    mgr = make_mock_manager(
        phase="done",
        dialogue_mode="knowledge_qa",
        task_type_key="pipeline_inspection",
        schema_fields=_SIMPLE_SCHEMA,
        slot_snapshot={"water_depth": {"value": 300, "status": "valid", "version": 3}},
    )
    ui = build_frontend_ui_state(mgr)
    assert ui["slots"] == []
    assert ui["read_only"] is False
    assert ui["actions"]["can_send"] is True
    assert ui["actions"]["can_modify"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 12. API 接口集成测试：/api/chat 返回 ui_state
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def flask_client():
    """构造离线 Flask 测试客户端，mock LLM 调用。"""
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("SEAGENT_OFFLINE_MOCK", "1")
    import web_backend
    web_backend.app.config["TESTING"] = True
    with web_backend.app.test_client() as client:
        yield client


def test_api_chat_returns_ui_state(flask_client):
    """POST /api/chat 响应必须包含 ui_state 字段，且结构正确。"""
    resp = flask_client.post(
        "/api/chat",
        json={"session_id": "test-s31-chat", "message": "你好"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert "ui_state" in data, f"响应缺少 ui_state: {list(data.keys())}"
    ui = data["ui_state"]
    for key in ("dialogue_mode", "phase", "slots", "constraint_state", "actions", "read_only"):
        assert key in ui, f"ui_state 缺少字段 '{key}'"
    for action_key in ("can_send", "can_modify", "can_confirm", "can_publish", "can_cancel"):
        assert action_key in ui["actions"], f"ui_state.actions 缺少 '{action_key}'"
    # 旧兼容字段仍存在
    assert "done" in data
    assert "collected" in data
    assert "missing" in data


def test_api_session_state_returns_ui_state(flask_client):
    """GET /api/session/state 响应必须包含 ui_state（已存在的会话）。"""
    # 先建立会话
    flask_client.post(
        "/api/chat",
        json={"session_id": "test-s31-state", "message": "hello"},
        content_type="application/json",
    )
    resp = flask_client.get("/api/session/state?session_id=test-s31-state")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("exists") is True
    assert "ui_state" in data, f"已存在会话响应缺少 ui_state: {list(data.keys())}"
    assert "actions" in data["ui_state"]
    assert "slots" in data["ui_state"]


def test_api_history_load_returns_ui_state(flask_client):
    """POST /api/history/load 成功恢复时必须返回统一契约和兼容字段。"""
    import web_backend
    source_manager = web_backend.get_or_create_manager("test-s31-history-source")
    snapshot = source_manager.export_snapshot()
    with patch.object(web_backend, "load_history", return_value=snapshot):
        resp = flask_client.post(
            "/api/history/load",
            json={"history_id": "history_test-s31.json", "session_id": "test-s31-history-target"},
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert "ui_state" in data
    assert "conversation_history" in data
    for field in ("built_json", "missing", "task_type", "mode", "phase"):
        assert field in data


def test_compat_fields_still_present(flask_client):
    """旧兼容字段 collected、missing、done 必须仍然存在（兼容性保证）。"""
    resp = flask_client.post(
        "/api/chat",
        json={"session_id": "test-s31-compat", "message": "测试任务"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    data = resp.get_json()
    for field in ("done", "rejected", "collected", "missing"):
        assert field in data, f"兼容字段 '{field}' 已丢失"


def test_no_regression_ordinary_chat(flask_client):
    """普通 LLM 对话响应结构不被破坏（回归保护）。"""
    resp = flask_client.post(
        "/api/chat",
        json={"session_id": "test-s31-regression", "message": "今天天气怎么样"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("code") == 200
    assert "reply" in data
    # ui_state 存在且结构完整
    assert "ui_state" in data
    # 普通对话 reply 不为空
    assert data["reply"]
