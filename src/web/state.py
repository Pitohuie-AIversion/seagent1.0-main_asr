"""
src/web/state.py - Web 后端全局状态管理与共享组件
集中维护会话管理器、只读模型单例、锁、Token鉴权与全局辅助工具。
"""

import json
import logging
import os
import re
import threading
from functools import wraps
from pathlib import Path
from typing import Any

from flask import current_app, jsonify, request

from session import Session
from src.dialogue_manager import DialogueManager

logger = logging.getLogger(__name__)

# ========== 核心路径配置 ==========
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = ROOT_DIR / "config"
FRONTEND_DIR = ROOT_DIR / "frontend"

# ---------- 全局只读资源（所有会话共享）----------
_shared_llm = None       # LLMClient 实例
_shared_kb = None        # KnowledgeBase 实例
_shared_asr = None       # ASRService 实例
_shared_mcp_bridge = None

# ---------- 会话管理器 ----------
_sessions_manager: dict[str, DialogueManager] = {}
_sessions_lock = threading.Lock()

_sessions: dict[str, Any] = {}           # 兼容原有的 Session 对象（用于前端展示）
_sess_lock = threading.Lock()

# ---------- 翻译缓存 ----------
_translation_cache: dict[str, str] = {}
_translation_cache_lock = threading.Lock()


def init_manager(dialogue_manager: Any) -> None:
    """在启动时由 run.py 调用，注入完整的 DialogueManager 实例，
    并从中提取只读的 llm 和 kb 供所有会话复用。
    """
    global _shared_llm, _shared_kb
    _shared_llm = dialogue_manager.llm
    _shared_kb = dialogue_manager.kb


def init_asr_service(asr_service: Any) -> None:
    """注入全局 ASR 语音识别服务实例"""
    global _shared_asr
    _shared_asr = asr_service


def init_mcp_bridge_service(bridge_service: Any) -> None:
    """初始化并注入全局 MCP 桥接服务实例"""
    global _shared_mcp_bridge
    _shared_mcp_bridge = bridge_service


def get_mcp_bridge() -> Any:
    """获取全局 MCP 桥接服务实例"""
    return _shared_mcp_bridge


def get_or_create_manager(sid: str) -> DialogueManager:
    """获取或创建会话专属的 DialogueManager 实例"""
    with _sessions_lock:
        if sid not in _sessions_manager:
            _sessions_manager[sid] = DialogueManager(_shared_llm, _shared_kb, session_id=sid)
        return _sessions_manager[sid]


def print_status(manager: DialogueManager) -> None:
    """每轮对话后打印结构化任务状态面板"""
    status = manager.get_status()

    phase_labels = {
        "collecting":   "收集中",
        "validating":   "约束校验中",
        "confirming":   "待用户确认",
        "done":         "✅ 已完成",
        "rejected":     "❌ 已拒绝",
    }
    phase_value = status.get("workflow_phase") or status["phase"]
    phase_str = phase_labels.get(phase_value, phase_value)
    mode_str  = "🚨 紧急模式" if status["mode"] == "emergency" else "普通模式"

    print()
    print("┌─ 任务状态 " + "─" * 48)
    print(f"│ 阶段：{phase_str}　模式：{mode_str}")
    print("├─ 已提取字段（规范化结果）" + "─" * 33)

    if status["filled"]:
        for key, info in status["filled"].items():
            val = info["value"]
            if isinstance(val, dict):
                val_str = ", ".join(f"{k}={v}" for k, v in val.items())
            elif isinstance(val, list):
                val_str = " / ".join(str(x) for x in val)
            else:
                val_str = str(val)
            print(f"│  ✓ {info['label']:<18} {val_str}")
    else:
        print("│  （暂无）")

    print("├─ 待补充字段 " + "─" * 45)
    if status["missing"]:
        for m in status["missing"]:
            allowed = m.get("allowed_values", [])
            if allowed:
                print(f"│  ✗ {m['label']:<18} 可选：{allowed}")
            else:
                print(f"│  ✗ {m['label']}")
    else:
        print("│  （无缺失，所有必填字段已收集 ✓）")

    if status["whitelisted_soft"]:
        print("├─ 已忽略的 Soft 警告 " + "─" * 37)
        for cid in status["whitelisted_soft"]:
            print(f"│  ~ [{cid}]")

    print("└" + "─" * 58)
    print()


# ========== 访问控制与 Token 鉴权 ==========

def _load_api_tokens() -> list[str]:
    """从环境变量读取控制面 Token，未配置时返回空列表表示关闭鉴权。"""
    raw_tokens = (
        os.getenv("SEAGENT_API_TOKENS", "")
        or os.getenv("API_TOKENS", "")
        or os.getenv("SEAGENT_API_TOKEN", "")
        or os.getenv("API_TOKEN", "")
    ).strip()
    if not raw_tokens:
        return []

    tokens = []
    for token in re.split(r"[,\s]+", raw_tokens):
        token = token.strip()
        if token:
            tokens.append(token)
    return tokens


def _token_from_request() -> str:
    authorization = request.headers.get("Authorization", "").strip()
    if authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ").strip()
    return request.headers.get("X-API-Token", "").strip()


def _require_api_token(view_fn):
    """当且仅当配置了 API token 时，对路由进行鉴权。"""

    @wraps(view_fn)
    def wrapped(*args, **kwargs):
        allowed_tokens = current_app.config.get("SEAGENT_API_TOKENS", [])
        if not allowed_tokens:
            return view_fn(*args, **kwargs)

        provided = _token_from_request()
        if provided in allowed_tokens:
            return view_fn(*args, **kwargs)

        response = jsonify({
            "code": 401,
            "error": "Unauthorized",
            "msg": "缺少有效 API token",
        })
        response.status_code = 401
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    return wrapped


# ========== 日志过滤器 ==========
class EndpointFilter(logging.Filter):
    """过滤包含 '/api/time/current' 的访问日志"""
    def filter(self, record):
        msg = record.getMessage()
        if '/api/time/current' in msg:
            return False
        return True


# ========== 统一服务钩子与依赖注入（解耦顶层 monkeypatch 依赖）==========
_service_hooks: dict[str, Any] = {}


def register_service_hook(name: str, service_callable: Any) -> None:
    """注册或覆盖后端服务钩子"""
    _service_hooks[name] = service_callable


def get_service_symbol(name: str, fallback: Any) -> Any:
    """解析服务符号：优先读取显式注册的 hook，次级读取 web_backend 的动态 monkeypatch，最后使用 fallback。"""
    if name in _service_hooks:
        return _service_hooks[name]
    import sys
    backend = sys.modules.get("web_backend")
    if backend is not None and hasattr(backend, name):
        symbol = getattr(backend, name)
        if symbol is not fallback:
            return symbol
    return fallback
