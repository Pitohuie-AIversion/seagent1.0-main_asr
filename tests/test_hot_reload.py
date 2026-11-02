import os
import importlib
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import web_backend
from src.dialogue_manager import DialogueManager
from src.hot_reload import (
    check_changed_files,
    force_reload,
    get_reload_events,
    maybe_auto_reload,
    perform_reload,
)


def test_force_reload_success():
    """配置刷新不创建与处理器缓存绑定冲突的新模块类型。"""
    with patch("importlib.reload", side_effect=AssertionError("must not replace code")):
        res = force_reload()
        assert res["ok"] is True
        assert res["reloaded_modules"] == []
        assert "配置刷新" in res["msg"]


def test_maybe_auto_reload_without_changes():
    """测试在无文件修改时 maybe_auto_reload 不触发重载"""
    # 第一次检查建立初始 mtime 映射
    check_changed_files()
    
    # 再次检查应该没有变化
    res = maybe_auto_reload()
    assert res is None


def test_session_state_migration_during_reload():
    """测试热重载期间已有的会话状态能够被无缝迁移保留"""
    # 构造 mock llm 和 kb
    mock_llm = MagicMock()
    mock_kb = MagicMock()
    
    # 设置 web_backend 中的全局共享对象
    web_backend._shared_llm = mock_llm
    web_backend._shared_kb = mock_kb
    
    sid = "test-session-hot-reload-001"
    mgr = DialogueManager(mock_llm, mock_kb, session_id=sid)
    mgr.mode = "emergency"
    mgr.conversation_history = [{"role": "user", "content": "hello"}]
    
    with web_backend._sessions_lock:
        web_backend._sessions_manager[sid] = mgr

    # 执行热重载
    with patch("importlib.reload", side_effect=lambda m: m):
        success, msg, _ = perform_reload()
    assert success is True

    # 验证 session 中的 manager 仍然保留了之前的状态
    with web_backend._sessions_lock:
        migrated_mgr = web_backend._sessions_manager.get(sid)
        assert migrated_mgr is not None
        assert migrated_mgr.session_id == sid
        assert migrated_mgr.mode == "emergency"
        assert len(migrated_mgr.conversation_history) == 1
        assert migrated_mgr.conversation_history[0]["content"] == "hello"

    # 清理
    with web_backend._sessions_lock:
        web_backend._sessions_manager.pop(sid, None)


def test_api_dev_reload_endpoint(monkeypatch):
    """测试 /api/dev/reload 接口返回正确 JSON"""
    monkeypatch.setenv("SEAGENT_ENABLE_CODE_RELOAD", "1")
    monkeypatch.delenv("DISABLE_HOT_RELOAD", raising=False)
    with patch("importlib.reload", side_effect=lambda m: m):
        with web_backend.app.test_client() as client:
            resp = client.get("/api/dev/reload")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert data["reloaded_modules"] == []


def test_perform_reload_records_frontend_event():
    before_events = get_reload_events()
    before_id = before_events[-1]["event_id"] if before_events else 0

    with patch("importlib.reload", side_effect=lambda m: m):
        success, msg, reloaded = perform_reload(changed_files=["/tmp/state.yaml"])

    assert success is True
    events = get_reload_events(after_event_id=before_id)
    assert len(events) == 1
    event = events[0]
    assert event["ok"] is True
    assert event["message"] == msg
    assert event["changed_files"] == ["state.yaml"]
    assert event["reloaded_modules_count"] == len(reloaded)


def test_api_dev_reload_events_endpoint_returns_new_events():
    before_events = get_reload_events()
    before_id = before_events[-1]["event_id"] if before_events else 0

    with patch("importlib.reload", side_effect=lambda m: m):
        perform_reload(changed_files=["/tmp/config/state.yaml"])

    with web_backend.app.test_client() as client:
        resp = client.get(f"/api/dev/reload-events?after={before_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["events"]
        assert data["events"][-1]["changed_files"] == ["state.yaml"]


def test_reload_preserves_state_contract_exception_identity():
    """Reload must not break already-imported contract exception handlers."""
    import src.exceptions as exc_mod
    import src.session.session_state as session_mod
    import src.slots.slot_store as slot_mod

    old_slot_conflict = slot_mod.SlotVersionConflict
    old_snapshot_error = slot_mod.SnapshotValidationError
    old_slot = slot_mod.Slot
    old_val_ack = slot_mod.ValidationAcknowledgement
    old_state_error = session_mod.StateContractError
    old_conversation_state = session_mod.ConversationState
    old_task_state = session_mod.TaskLifecycleState
    old_execution_state = session_mod.ExecutionControlState
    old_session_state = session_mod.SessionState
    old_persistence_error = exc_mod.TaskPersistenceError
    old_intent_conflict = exc_mod.IntentIdConflict
    old_id_reservation_error = exc_mod.IdReservationError
    state_errors = {
        name: getattr(exc_mod, name)
        for name in ("StatePersistenceError", "StateVersionConflict", "StateSnapshotValidationError", "StateSelectorError")
    }

    reloaded_exc = importlib.reload(exc_mod)
    reloaded_session = importlib.reload(session_mod)
    reloaded_slot = importlib.reload(slot_mod)

    assert reloaded_slot.SlotVersionConflict is old_slot_conflict
    assert reloaded_slot.SnapshotValidationError is old_snapshot_error
    assert reloaded_slot.Slot is old_slot
    assert reloaded_slot.ValidationAcknowledgement is old_val_ack
    assert reloaded_session.StateContractError is old_state_error
    assert reloaded_session.ConversationState is old_conversation_state
    assert reloaded_session.TaskLifecycleState is old_task_state
    assert reloaded_session.ExecutionControlState is old_execution_state
    assert reloaded_session.SessionState is old_session_state
    assert reloaded_exc.TaskPersistenceError is old_persistence_error
    assert reloaded_exc.IntentIdConflict is old_intent_conflict
    assert reloaded_exc.IdReservationError is old_id_reservation_error
    for name, old_type in state_errors.items():
        assert getattr(reloaded_exc, name) is old_type


def test_model_profile_reload_preserves_error_handlers():
    import src.extraction.model_profile as profiles
    error_types = {name: getattr(profiles, name) for name in (
        "ModelProfileError", "ModelProfileConfigError", "ModelProfileNotFoundError",
    )}
    importlib.reload(profiles)
    for name, old_type in error_types.items():
        assert getattr(profiles, name) is old_type


def test_builder_reload_preserves_commit_uncertain_handler():
    # Keep this direct module-reload probe isolated: production configuration
    # refresh no longer reloads code, and other tests patch cached builder types.
    import subprocess

    result = subprocess.run([sys.executable, "-c", """
import importlib
import src.dispatch.task_intent_builder as builder
from src.handlers.task_commit import TaskCommitUncertainError as handler_error
importlib.reload(builder)
assert builder.TaskCommitUncertainError is handler_error
"""], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_code_reload_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("SEAGENT_ENABLE_CODE_RELOAD", raising=False)
    with patch("src.hot_reload.perform_reload") as reload, patch("src.hot_reload.check_changed_files") as scan:
        assert maybe_auto_reload() is None
        with web_backend.app.test_client() as client:
            assert client.post("/api/dev/reload").status_code == 403
        scan.assert_not_called()
        reload.assert_not_called()
