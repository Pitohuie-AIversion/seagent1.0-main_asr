"""
task_intent_schema_validator.py — TaskIntent JSON 契约与 Schema 校验器 (v1 与 v2 双模态分发)
"""
from datetime import datetime
from typing import Any

from .id_sequence import validate_intent_id, validate_task_id, validate_task_id_for_task_type
from src.utils import (
    validate_uuid4,
    is_finite_number,
    is_exact_int,
)

TASK_ALLOWED_ROBOT_TYPES = {
    "pipeline_inspection": {"observation_rov", "auv"},
    "pipeline_burial": {"work_class_rov"},
    "tree_valve_operation": {"work_class_rov"},
    "valve_operation": {"work_class_rov"},
}
VALID_ROBOT_TYPES = {"observation_rov", "work_class_rov", "auv"}


def _is_exact_schema_version(val: Any, expected: int) -> bool:
    """严格判断值是否为精确整数类型且数值等于 expected (排除 bool、float 及 None)。"""
    return is_exact_int(val, expected)


def _parse_optional_task_time(value: Any, field_name: str) -> tuple[datetime | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, f"time.{field_name} must be an ISO-8601 string or null"
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")), None
    except ValueError:
        return None, f"time.{field_name} is not a valid ISO-8601 timestamp"


_is_finite_number = is_finite_number  # backwards compat alias


def _coordinate_error(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return f"{field_name} must be a coordinate object or null"
    lat = value.get("latitude", value.get("lat"))
    lon = value.get("longitude", value.get("lon"))
    if not _is_finite_number(lat) or not -90.0 <= float(lat) <= 90.0:
        return f"{field_name}.latitude must be finite and between -90 and 90"
    if not _is_finite_number(lon) or not -180.0 <= float(lon) <= 180.0:
        return f"{field_name}.longitude must be finite and between -180 and 180"
    return None


def _task_intent_payload_error(intent: dict) -> str | None:
    """Validate nested TaskIntent payload semantics shared by read and write paths."""
    priority = intent.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int) or priority not in range(1, 11):
        return "priority must be an integer between 1 and 10"

    time_info = intent.get("time")
    if not isinstance(time_info, dict):
        return "time must be an object"
    start_time, start_error = _parse_optional_task_time(time_info.get("start"), "start")
    if start_error:
        return start_error
    end_time, end_error = _parse_optional_task_time(time_info.get("end"), "end")
    if end_error:
        return end_error
    if start_time is not None and end_time is not None:
        try:
            if end_time <= start_time:
                return "time.end must be later than time.start"
        except TypeError:
            return "time.start and time.end must use compatible timezone forms"

    location = intent.get("location")
    if not isinstance(location, dict):
        return "location must be an object"
    oilfield = location.get("oilfield")
    if oilfield is not None and (not isinstance(oilfield, str) or not oilfield.strip()):
        return "location.oilfield must be a non-empty string or null"
    water_depth = location.get("water_depth_m")
    if water_depth is not None and (
        not _is_finite_number(water_depth) or float(water_depth) <= 0
    ):
        return "location.water_depth_m must be a finite positive number or null"

    task = intent.get("task")
    if not isinstance(task, dict):
        return "task must be an object"
    details = task.get("details")
    if not isinstance(details, dict):
        return "task.details must be an object"
    if intent.get("task_type") in {"valve_operation", "tree_valve_operation"}:
        if details.get("operation") not in (None, "insert", "withdraw"):
            return "task.details.operation must be insert or withdraw"
    for key in ("start_point", "end_point", "target"):
        error = _coordinate_error(details.get(key), f"task.details.{key}")
        if error:
            return error

    equipment = intent.get("equipment")
    if not isinstance(equipment, dict):
        return "equipment must be an object"
    payload = equipment.get("payload")
    if not isinstance(payload, list) or any(
        not isinstance(item, str) or not item.strip() for item in payload
    ):
        return "equipment.payload must be a list of non-empty strings"
    support_vessel = equipment.get("support_vessel")
    if not isinstance(support_vessel, dict):
        return "equipment.support_vessel must be an object"
    vessel_name = support_vessel.get("name")
    if vessel_name is not None and (
        not isinstance(vessel_name, str) or not vessel_name.strip()
    ):
        return "equipment.support_vessel.name must be a non-empty string or null"
    for field_name in ("latitude", "longitude"):
        value = support_vessel.get(field_name)
        if value is not None and not _is_finite_number(value):
            return f"equipment.support_vessel.{field_name} must be finite or null"

    if not isinstance(intent.get("conditions"), dict):
        return "conditions must be an object"
    return None


def validate_task_intent_v1(intent: dict) -> bool:
    """v1 历史 TaskIntent 结构校验：schema_version 缺失或等于精确整数 1，internal_id 与 task_id 必须同时不存在。"""
    if not isinstance(intent, dict):
        return False
    if "schema_version" in intent:
        if not _is_exact_schema_version(intent["schema_version"], 1):
            return False
    if "internal_id" in intent or "task_id" in intent:
        return False
    intent_id = intent.get("intent_id")
    if not validate_intent_id(intent_id):
        return False
    top_task_type = intent.get("task_type")
    if top_task_type not in TASK_ALLOWED_ROBOT_TYPES:
        return False
    priority = intent.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int):
        return False
    time_info = intent.get("time")
    if not isinstance(time_info, dict) or "start" not in time_info or "end" not in time_info:
        return False
    loc_info = intent.get("location")
    if not isinstance(loc_info, dict) or "oilfield" not in loc_info or "water_depth_m" not in loc_info:
        return False
    task_info = intent.get("task")
    if not isinstance(task_info, dict) or "type" not in task_info or "details" not in task_info:
        return False
    if task_info.get("type") != top_task_type:
        return False
    eq_info = intent.get("equipment")
    if not isinstance(eq_info, dict) or "robot_type" not in eq_info or "payload" not in eq_info or "support_vessel" not in eq_info:
        return False
    robot_type = eq_info.get("robot_type")
    allowed_robots = TASK_ALLOWED_ROBOT_TYPES.get(top_task_type, set())
    if robot_type not in allowed_robots:
        return False
    cond_info = intent.get("conditions")
    if not isinstance(cond_info, dict):
        return False
    return _task_intent_payload_error(intent) is None


def validate_task_intent_v2(intent: dict, task_schemas: dict | None = None) -> bool:
    """v2 TaskIntent 结构与类别编号权威校验器：schema_version 必须为精确整数 2，task_schemas 必传，internal_id (UUIDv4) 与 task_id (前缀匹配) 必填。"""
    if not isinstance(intent, dict):
        return False
    if not _is_exact_schema_version(intent.get("schema_version"), 2):
        return False
    if task_schemas is None:
        return False
    internal_id = intent.get("internal_id")
    if not validate_uuid4(internal_id):
        return False
    task_id = intent.get("task_id")
    if not validate_task_id(task_id):
        return False
    intent_id = intent.get("intent_id")
    if not validate_intent_id(intent_id):
        return False
    top_task_type = intent.get("task_type")
    if top_task_type not in TASK_ALLOWED_ROBOT_TYPES:
        return False
    rev_map = {"pipeline_inspection": "pipeline_inspection", "pipeline_burial": "pipeline_burial", "valve_operation": "tree_valve_operation"}
    task_type_key = intent.get("task_type_key") or rev_map.get(top_task_type, top_task_type)
    if not validate_task_id_for_task_type(task_id, task_type_key, task_schemas):
        return False
    priority = intent.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int):
        return False
    time_info = intent.get("time")
    if not isinstance(time_info, dict) or "start" not in time_info or "end" not in time_info:
        return False
    loc_info = intent.get("location")
    if not isinstance(loc_info, dict) or "oilfield" not in loc_info or "water_depth_m" not in loc_info:
        return False
    task_info = intent.get("task")
    if not isinstance(task_info, dict) or "type" not in task_info or "details" not in task_info:
        return False
    if task_info.get("type") != top_task_type:
        return False
    eq_info = intent.get("equipment")
    if not isinstance(eq_info, dict) or "robot_type" not in eq_info or "payload" not in eq_info or "support_vessel" not in eq_info:
        return False
    robot_type = eq_info.get("robot_type")
    allowed_robots = TASK_ALLOWED_ROBOT_TYPES.get(top_task_type, set())
    if robot_type not in allowed_robots:
        return False
    cond_info = intent.get("conditions")
    if not isinstance(cond_info, dict):
        return False
    return _task_intent_payload_error(intent) is None


def validate_task_intent(intent: Any, task_schemas: dict | None = None) -> bool:
    """权威完整 TaskIntent 结构与交叉约束校验器 (根据 schema_version 显式分派)"""
    if not isinstance(intent, dict):
        return False
    if "schema_version" not in intent:
        return validate_task_intent_v1(intent)
    ver = intent["schema_version"]
    if _is_exact_schema_version(ver, 1):
        return validate_task_intent_v1(intent)
    elif _is_exact_schema_version(ver, 2):
        return validate_task_intent_v2(intent, task_schemas)
    return False
