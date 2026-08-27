"""
hot_reload.py - 业务逻辑与配置热重载管理器

支持在 vLLM/ASR 模型常驻显存的情况下，动态重载 src/ 业务代码和 config/ 配置，
无需重启 Python 进程或重新加载大模型权重。
"""

import importlib
import logging
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 监控目录
BASE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = BASE_DIR / "src"
CONFIG_DIR = BASE_DIR / "config"

# 需要热重载的模块列表（按依赖拓扑顺序排序）
RELOAD_MODULE_ORDER = [
    "src.exceptions",
    "src.result_paths",
    "src.simulated_time",
    "src.environment_info",
    "src.state_info",
    "src.asr_normalizer",
    "src.coord_parser",
    "src.prompts",
    "src.oilfield_linker",
    "src.model_profile",
    "src.session_state",
    "src.session_state_shadow",
    "src.knowledge_retriever",
    "src.normalizer",
    "src.validator",
    "src.extractor",
    "src.slot_store",
    "src.constraint_checker",
    "src.task_intent_builder",
    "src.dialogue_manager",
    "src.ui_state_builder",
    "src.history_manager",
    "src.asr_service",
]

_reload_lock = threading.Lock()
_file_mtimes: Dict[str, float] = {}
_initialized = False
_reload_events = deque(maxlen=50)
_reload_event_seq = 0


def _public_changed_file_names(changed_files: Optional[List[str]]) -> List[str]:
    names = []
    for item in changed_files or []:
        try:
            names.append(Path(str(item)).name)
        except Exception:
            names.append(str(item))
    return names


def _record_reload_event(
    *,
    ok: bool,
    message: str,
    changed_files: Optional[List[str]],
    reloaded_modules: List[str],
    refreshed_sessions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    global _reload_event_seq
    _reload_event_seq += 1
    event = {
        "event_id": _reload_event_seq,
        "ok": bool(ok),
        "message": message,
        "changed_files": _public_changed_file_names(changed_files),
        "reloaded_modules": list(reloaded_modules),
        "reloaded_modules_count": len(reloaded_modules),
        "refreshed_sessions": list(refreshed_sessions),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _reload_events.append(event)
    return event


def get_reload_events(after_event_id: int = 0) -> List[Dict[str, Any]]:
    try:
        cursor = int(after_event_id)
    except (TypeError, ValueError):
        cursor = 0
    return [
        dict(event)
        for event in list(_reload_events)
        if int(event.get("event_id", 0)) > cursor
    ]


def _scan_monitored_files() -> Dict[str, float]:
    """扫描 src/ 和 config/ 下的所有代码和配置文件 mtime"""
    current_mtimes = {}
    
    # 扫描 src
    if SRC_DIR.exists():
        for p in SRC_DIR.rglob("*.py"):
            try:
                current_mtimes[str(p.resolve())] = p.stat().st_mtime
            except OSError:
                pass

    # 扫描 config
    if CONFIG_DIR.exists():
        for p in CONFIG_DIR.rglob("*.yaml"):
            try:
                current_mtimes[str(p.resolve())] = p.stat().st_mtime
            except OSError:
                pass
        for p in CONFIG_DIR.rglob("*.yml"):
            try:
                current_mtimes[str(p.resolve())] = p.stat().st_mtime
            except OSError:
                pass

    return current_mtimes


def check_changed_files() -> List[str]:
    """检查自上次扫描以来发生变动的文件列表"""
    global _file_mtimes, _initialized
    current_mtimes = _scan_monitored_files()

    if not _initialized:
        _file_mtimes = current_mtimes
        _initialized = True
        return []

    changed = []
    for fpath, mtime in current_mtimes.items():
        old_mtime = _file_mtimes.get(fpath)
        if old_mtime is None or mtime > old_mtime:
            changed.append(fpath)

    # 检查是否有删除的文件
    for fpath in list(_file_mtimes.keys()):
        if fpath not in current_mtimes:
            changed.append(fpath)

    return changed


def perform_reload(changed_files: Optional[List[str]] = None) -> Tuple[bool, str, List[str]]:
    """
    执行业务模块热重载与会话状态平滑迁移。
    返回: (是否成功, 提示信息, 重载模块列表)
    """
    global _file_mtimes
    start_time = time.perf_counter()
    reloaded_mods = []
    refreshed_sessions: List[Dict[str, Any]] = []

    with _reload_lock:
        try:
            # 1. 按顺序 reload 模块
            for mod_name in RELOAD_MODULE_ORDER:
                if mod_name in sys.modules:
                    mod = sys.modules[mod_name]
                    importlib.reload(mod)
                    reloaded_mods.append(mod_name)
                else:
                    try:
                        mod = importlib.import_module(mod_name)
                        reloaded_mods.append(mod_name)
                    except Exception:
                        pass

            # 2. 刷新 web_backend 模块内的引用
            if "web_backend" in sys.modules:
                web_mod = sys.modules["web_backend"]
                
                # 重新挂载核心类与函数
                new_kb_cls = None
                if "src.knowledge_retriever" in sys.modules:
                    new_kb_cls = sys.modules["src.knowledge_retriever"].KnowledgeBase
                    web_mod._shared_kb = new_kb_cls()
                if "src.dialogue_manager" in sys.modules:
                    web_mod.DialogueManager = sys.modules["src.dialogue_manager"].DialogueManager
                if "src.asr_normalizer" in sys.modules:
                    web_mod.normalize_terminology = sys.modules["src.asr_normalizer"].normalize_terminology
                if "src.ui_state_builder" in sys.modules:
                    web_mod.build_frontend_ui_state = sys.modules["src.ui_state_builder"].build_frontend_ui_state
                if "src.history_manager" in sys.modules:
                    web_mod.save_conversation = sys.modules["src.history_manager"].save_conversation
                    web_mod.list_history = sys.modules["src.history_manager"].list_history
                    web_mod.load_history = sys.modules["src.history_manager"].load_history

                # 3. 平滑迁移活跃会话的 DialogueManager 实例（保持会话状态）
                if hasattr(web_mod, "_sessions_lock") and hasattr(web_mod, "_sessions_manager"):
                    with web_mod._sessions_lock:
                        new_dm_cls = sys.modules["src.dialogue_manager"].DialogueManager
                        for sid, old_mgr in list(web_mod._sessions_manager.items()):
                            try:
                                snap = old_mgr.export_snapshot()
                                new_mgr = new_dm_cls(
                                    web_mod._shared_llm,
                                    web_mod._shared_kb,
                                    session_id=sid,
                                )
                                new_mgr.load_snapshot(snap)
                                if new_kb_cls and hasattr(new_mgr, "kb"):
                                    new_mgr.kb.__class__ = new_kb_cls
                                if hasattr(new_mgr, "refresh_external_state_constraints"):
                                    try:
                                        refresh = new_mgr.refresh_external_state_constraints()
                                        if isinstance(refresh, dict) and refresh.get("refreshed"):
                                            refreshed_sessions.append({
                                                "session_id": sid,
                                                "phase": refresh.get("phase"),
                                                "hard_violations": refresh.get("hard_violations", 0),
                                                "soft_violations": refresh.get("soft_violations", 0),
                                            })
                                    except Exception as exc:
                                        logger.warning("[Hot-Reload] 会话 %s 强刷新外部约束失败: %s", sid, exc)
                                web_mod._sessions_manager[sid] = new_mgr
                            except Exception as err:
                                logger.warning(
                                    "[Hot-Reload] 会话 %s 迁移失败，已重置为空会话: %s",
                                    sid,
                                    err,
                                )
                                new_mgr = new_dm_cls(
                                    web_mod._shared_llm,
                                    web_mod._shared_kb,
                                    session_id=sid,
                                )
                                if new_kb_cls and hasattr(new_mgr, "kb"):
                                    new_mgr.kb.__class__ = new_kb_cls
                                web_mod._sessions_manager[sid] = new_mgr

            # 更新记录的文件时间戳
            _file_mtimes = _scan_monitored_files()
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            msg = f"热重载成功，耗时 {elapsed_ms:.1f}ms，重载模块: {len(reloaded_mods)} 个"
            _record_reload_event(
                ok=True,
                message=msg,
                changed_files=changed_files,
                reloaded_modules=reloaded_mods,
                refreshed_sessions=refreshed_sessions,
            )
            print(f"\n🔄 [Hot-Reload] ✅ {msg}")
            return True, msg, reloaded_mods

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            err_msg = f"热重载失败 (保留上一稳定版本): {exc}"
            logger.error("[Hot-Reload] %s", err_msg, exc_info=True)
            _record_reload_event(
                ok=False,
                message=err_msg,
                changed_files=changed_files,
                reloaded_modules=reloaded_mods,
                refreshed_sessions=refreshed_sessions,
            )
            print(f"\n❌ [Hot-Reload] {err_msg}")
            return False, err_msg, reloaded_mods


def maybe_auto_reload() -> Optional[Tuple[bool, str]]:
    """
    检查文件变动并自动执行热重载（若有变更）。
    通常在 Flask @app.before_request 中调用。
    """
    # 允许通过环境变量禁用热重载（如测试环境）
    if os.environ.get("DISABLE_HOT_RELOAD") == "1":
        return None

    changed = check_changed_files()
    if not changed:
        return None

    changed_names = [Path(p).name for p in changed[:3]]
    if len(changed) > 3:
        changed_names.append(f"...共{len(changed)}个文件")
    print(f"\n🔄 [Hot-Reload] 检测到文件更新: {', '.join(changed_names)}")

    success, msg, _ = perform_reload(changed_files=changed)
    return success, msg


def force_reload() -> Dict[str, Any]:
    """手动强制重载所有业务模块"""
    success, msg, reloaded = perform_reload()
    return {
        "ok": success,
        "msg": msg,
        "reloaded_modules": reloaded,
    }
