"""
web_backend.py - Web 后端主控与服务统一外观（Facade）
支持多会话隔离：每个 session_id 拥有独立的 DialogueManager 实例，
共享只读模型（LLMClient, KnowledgeBase）。
底层路由已模块化下沉至 src/web/ 蓝图体系，此处提供统一 Flask 应用装配与 100% 向后兼容导出。
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any
from flask import Flask

import src.web.state as state
from session import Session
from src.dialogue_manager import DialogueManager
from src.history_manager import save_conversation, list_history, load_history
from src.ui_state_builder import build_frontend_ui_state
from src.web import register_blueprints
from src.web.routes_asr import (
    _allowed_audio_extensions,
    _asr_api_config,
    _asr_direct_to_llm,
    _config_bool,
    _is_allowed_audio,
    _load_asr_api_config,
    api_asr,
    asr_bp,
)
from src.web.routes_chat import (
    _dispatch_ros2_on_done_transition,
    _persist_and_dispatch_done_transition,
    api_chat,
    api_chat_stream,
    api_reset,
    chat_bp,
    get_session_state,
)
from src.web.routes_dev import (
    auto_hot_reload_check,
    dev_bp,
    dev_reload_events,
    dev_reload_events_stream,
    manual_dev_reload,
)
from src.web.routes_mcp import (
    _persist_active_gateway,
    dispatch_mcp_task,
    get_mcp_status,
    mcp_bp,
    mcp_ctrl_task,
    mcp_gateway,
    mcp_task_manage,
)
from src.web.routes_pages import (
    dashboard,
    disable_static_cache_after_request as _disable_static_cache_after_request,
    favicon,
    index,
    pages_bp,
)
from src.web.routes_robot import (
    get_telemetry_snapshot,
    robot_bp,
    set_robot_state_info,
)
from src.web.routes_time_history import (
    api_history_list,
    api_history_load,
    get_current_time,
    set_current_time,
    time_history_bp,
)
from src.web.routes_translate import (
    TRANSLATION_CACHE_FILE,
    TRANSLATION_CHUNK_SIZE,
    TRANSLATION_MAX_INPUT_CHARS,
    TRANSLATION_MAX_TOKENS,
    TRANSLATION_USE_CACHE,
    _CJK_RE,
    _get_cache_key,
    _is_dirty_translation,
    _split_into_chunks,
    _translate_single_chunk,
    _translate_text_internal,
    _translation_cache,
    _translation_cache_file,
    _translation_cache_lock,
    _validate_translation_quality,
    api_translate,
    translate_bp,
)
import types

from src.web.state import (
    CONFIG_DIR,
    FRONTEND_DIR,
    EndpointFilter,
    _load_api_tokens,
    _require_api_token,
    _sess_lock,
    _sessions,
    _sessions_lock,
    _sessions_manager,
    _shared_asr,
    _shared_kb,
    _shared_llm,
    _shared_mcp_bridge,
    _token_from_request,
    _translation_cache,
    _translation_cache_lock,
    get_mcp_bridge,
    get_or_create_manager,
    init_asr_service,
    init_manager,
    init_mcp_bridge_service,
    print_status,
)

logger = logging.getLogger(__name__)

# ========== 核心 Flask 应用初始化 ==========
app = Flask(
    __name__,
    template_folder=str(FRONTEND_DIR),
    static_folder=str(FRONTEND_DIR),
    static_url_path="/static",
)

app.config["MAX_CONTENT_LENGTH"] = int(_asr_api_config.get("max_upload_mb", 25)) * 1024 * 1024
app.config["SEAGENT_API_TOKENS"] = _load_api_tokens()

# 批量挂载业务模块蓝图
register_blueprints(app)

# ========== 日志过滤器：屏蔽 /api/time/current 的访问日志 ==========
werkzeug_logger = logging.getLogger("werkzeug")
for f in werkzeug_logger.filters[:]:
    if isinstance(f, EndpointFilter):
        werkzeug_logger.removeFilter(f)
werkzeug_logger.addFilter(EndpointFilter())


# ========== 针对外部/测试用例直接访问与覆写全局对象的透明代理 ==========
_STATE_SYMBOLS = {
    "_shared_llm",
    "_shared_kb",
    "_shared_asr",
    "_shared_mcp_bridge",
    "_sessions_manager",
    "_sessions_lock",
    "_sessions",
    "_sess_lock",
    "_translation_cache",
    "_translation_cache_lock",
    "init_manager",
    "init_asr_service",
    "init_mcp_bridge_service",
    "get_mcp_bridge",
    "get_or_create_manager",
}


class _WebBackendModule(types.ModuleType):
    """自定义 ModuleType，保证通过 web_backend 访问与覆写的全局符号与 src.web.state 实时同步，
    同时作为原生 ModuleType 实例，100% 兼容 unittest.mock.patch.object、__dict__ 检查、
    delattr 及 inspect 等反射操作。
    """

    def __getattribute__(self, name: str) -> Any:
        if name in _STATE_SYMBOLS:
            if hasattr(state, name):
                return getattr(state, name)
        return super().__getattribute__(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _STATE_SYMBOLS:
            setattr(state, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _STATE_SYMBOLS:
            try:
                delattr(state, name)
            except AttributeError:
                pass
        try:
            super().__delattr__(name)
        except AttributeError:
            pass


sys.modules[__name__].__class__ = _WebBackendModule
