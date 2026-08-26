"""
types 包 - 共享类型定义

集中存放跨模块复用的数据类、异常类、常量和类型别名，避免循环依赖。
"""

from .routing_types import (
    InteractionType,
    DialogueMode,
    VALID_INTERACTION_TYPES,
    VALID_DIALOGUE_MODES,
    VALID_EMERGENCY_ACTIONS,
    VALID_QUERY_INTENTS,
    IntentRoutingError,
    IntentRouteResult,
)

__all__ = [
    "InteractionType",
    "DialogueMode",
    "VALID_INTERACTION_TYPES",
    "VALID_DIALOGUE_MODES",
    "VALID_EMERGENCY_ACTIONS",
    "VALID_QUERY_INTENTS",
    "IntentRoutingError",
    "IntentRouteResult",
]
