"""
session - SEAgent 对话会话状态、交互计划与路由域

Public classes are loaded on demand so lightweight imports do not trigger heavy lifecycle chains.
"""

from importlib import import_module as _import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dialogue_snapshot import DialogueSnapshotManager
    from .history_manager import save_conversation
    from .intent_router import IntentRouter
    from .session_state import (
        SessionState,
        StateContractError,
    )
    from .ui_state_builder import build_frontend_ui_state

_LAZY_EXPORTS = {
    "DialogueSnapshotManager": ".dialogue_snapshot",
    "save_conversation": ".history_manager",
    "IntentRouter": ".intent_router",
    "SessionState": ".session_state",
    "StateContractError": ".session_state",
    "build_frontend_ui_state": ".ui_state_builder",
}

__all__ = [
    "DialogueSnapshotManager",
    "save_conversation",
    "IntentRouter",
    "SessionState",
    "StateContractError",
    "build_frontend_ui_state",
]


def __getattr__(name: str):
    if name in _LAZY_EXPORTS:
        mod = _import_module(_LAZY_EXPORTS[name], __name__)
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
