"""
src/dispatch/validation — 任务意图 Schema 规范校验与能力适配子包
包含 v1/v2 TaskIntent 契约验证、字段合法性断言与机器人载荷工具适配器。
"""

from .task_intent_schema_validator import (
    TASK_ALLOWED_ROBOT_TYPES,
    VALID_ROBOT_TYPES,
    _is_exact_schema_version,
    _parse_optional_task_time,
    _is_finite_number,
    _coordinate_error,
    _task_intent_payload_error,
    validate_task_intent_v1,
    validate_task_intent_v2,
    validate_task_intent,
)
from .task_capability_adapter import TaskCapabilityAdapter

__all__ = [
    "TASK_ALLOWED_ROBOT_TYPES",
    "VALID_ROBOT_TYPES",
    "_is_exact_schema_version",
    "_parse_optional_task_time",
    "_is_finite_number",
    "_coordinate_error",
    "_task_intent_payload_error",
    "validate_task_intent_v1",
    "validate_task_intent_v2",
    "validate_task_intent",
    "TaskCapabilityAdapter",
]
