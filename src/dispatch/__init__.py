"""
dispatch - SEAgent 任务意图构建、适配器与发布派发域

Public classes and functions are loaded on demand so lightweight path/sequence imports
do not trigger the entire dispatch or model dependencies stack.
"""

from importlib import import_module as _import_module
from typing import TYPE_CHECKING

from .result_paths import (
    get_history_dir,
    get_result_dir,
    get_task_dir,
)

if TYPE_CHECKING:
    from .id_sequence import (
        next_daily_id,
        next_daily_task_id,
        peek_daily_task_id,
        validate_intent_id,
        validate_task_id,
        validate_task_id_for_task_type,
        validate_task_prefix,
    )
    from .output_builder import OutputBuilder
    from .task_capability_adapter import TaskCapabilityAdapter
    from .task_dispatch import save_dispatch_history
    from .task_intent_builder import (
        TaskIntentBuilder,
        TaskPublishLock,
    )

_LAZY_EXPORTS = {
    "TaskIntentBuilder": ".task_intent_builder",
    "TaskPublishLock": ".task_intent_builder",
    "save_dispatch_history": ".task_dispatch",
    "TaskCapabilityAdapter": ".task_capability_adapter",
    "next_daily_id": ".id_sequence",
    "next_daily_task_id": ".id_sequence",
    "peek_daily_task_id": ".id_sequence",
    "validate_intent_id": ".id_sequence",
    "validate_task_id": ".id_sequence",
    "validate_task_id_for_task_type": ".id_sequence",
    "validate_task_prefix": ".id_sequence",
    "OutputBuilder": ".output_builder",
}

__all__ = [
    "get_history_dir",
    "get_result_dir",
    "get_task_dir",
    "TaskIntentBuilder",
    "TaskPublishLock",
    "save_dispatch_history",
    "TaskCapabilityAdapter",
    "next_daily_id",
    "next_daily_task_id",
    "peek_daily_task_id",
    "validate_intent_id",
    "validate_task_id",
    "validate_task_id_for_task_type",
    "validate_task_prefix",
    "OutputBuilder",
]


def __getattr__(name: str):
    if name in _LAZY_EXPORTS:
        mod = _import_module(_LAZY_EXPORTS[name], __name__)
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
