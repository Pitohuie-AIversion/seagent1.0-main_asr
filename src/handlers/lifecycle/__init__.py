"""
src/handlers/lifecycle — 任务生命周期跃迁、执行控制与命令意图谓词子包
"""

from .task_transition import TaskTransitionManager
from .execution_control import ExecutionControlHandler
from .command_predicates import (
    is_business_identity_query,
    is_confirmation_only,
    is_final_publish_confirmation,
    is_ignore_warning,
    is_payload_modification_request,
    is_user_cancelled,
    is_user_confirmed,
    is_user_requested_modification,
)

__all__ = [
    "TaskTransitionManager",
    "ExecutionControlHandler",
    "is_business_identity_query",
    "is_confirmation_only",
    "is_final_publish_confirmation",
    "is_ignore_warning",
    "is_payload_modification_request",
    "is_user_cancelled",
    "is_user_confirmed",
    "is_user_requested_modification",
]
