"""
SEAgent Core Source Package

Public classes are loaded on demand so utility imports do not load model runtimes.
"""

from importlib import import_module as _import_module
from typing import TYPE_CHECKING

from .constants import (
    FIELD_LABELS,
    HARD_REFUSAL_LIMIT,
    OILFIELD_CONTEXT_FIELDS,
    ROBOT_CASCADE_FIELDS,
    SOFT_IGNORE_KEYWORDS,
    TASK_TRANSITION_NON_INHERITED_FIELDS,
)
from .simulated_time import (
    get_current_date,
    get_current_datetime,
    get_current_timestamp,
    get_simulated_time,
)

if TYPE_CHECKING:
    from .dialogue_manager import DialogueManager
    from .dialogue_snapshot import DialogueSnapshotManager
    from .intent_router import IntentRouter
    from .knowledge_retriever import KnowledgeBase
    from .llm_client import LLMClient
    from .normalizer import FieldNormalizer
    from .output_builder import OutputBuilder
    from .slot_store import Slot, SlotStore
    from .task_intent_builder import TaskIntentBuilder, TaskPublishLock
    from .validator import TaskValidator, ValidationResult, Violation


_LAZY_EXPORTS = {
    "DialogueManager": ".dialogue_manager",
    "DialogueSnapshotManager": ".dialogue_snapshot",
    "IntentRouter": ".intent_router",
    "KnowledgeBase": ".knowledge_retriever",
    "LLMClient": ".llm_client",
    "FieldNormalizer": ".normalizer",
    "OutputBuilder": ".output_builder",
    "Slot": ".slot_store",
    "SlotStore": ".slot_store",
    "TaskIntentBuilder": ".task_intent_builder",
    "TaskPublishLock": ".task_intent_builder",
    "TaskValidator": ".validator",
    "ValidationResult": ".validator",
    "Violation": ".validator",
}


def __getattr__(name: str):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(_import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | _LAZY_EXPORTS.keys())

__all__ = [
    "DialogueManager",
    "DialogueSnapshotManager",
    "LLMClient",
    "KnowledgeBase",
    "OutputBuilder",
    "FieldNormalizer",
    "SlotStore",
    "Slot",
    "TaskValidator",
    "ValidationResult",
    "Violation",
    "TaskIntentBuilder",
    "TaskPublishLock",
    "IntentRouter",
    "FIELD_LABELS",

    "ROBOT_CASCADE_FIELDS",
    "OILFIELD_CONTEXT_FIELDS",
    "TASK_TRANSITION_NON_INHERITED_FIELDS",
    "SOFT_IGNORE_KEYWORDS",
    "HARD_REFUSAL_LIMIT",
    "get_current_datetime",
    "get_current_timestamp",
    "get_current_date",
    "get_simulated_time",
]
