import os
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
    maybe_auto_reload,
    perform_reload,
)


def test_force_reload_success():
    """测试强制热重载能够成功执行并返回重载模块列表"""
    with patch("importlib.reload", side_effect=lambda m: m):
        res = force_reload()
        assert res["ok"] is True
        assert "src.prompts" in res["reloaded_modules"]
        assert "src.dialogue_manager" in res["reloaded_modules"]


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


def test_api_dev_reload_endpoint():
    """测试 /api/dev/reload 接口返回正确 JSON"""
    with patch("importlib.reload", side_effect=lambda m: m):
        with web_backend.app.test_client() as client:
            resp = client.get("/api/dev/reload")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert "src.dialogue_manager" in data["reloaded_modules"]
