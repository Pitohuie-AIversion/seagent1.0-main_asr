"""
src/state/runtime_checker.py - 机器人运行时可用性与遥测健康检查模块

职责：
1. 布尔状态标记严格解析（parse_bool）
2. 状态时效性与时间戳漂移防护（TTL, Skew check）
3. 多源状态指标严格交叉审计（online/busy/connection/health/overall_status）
4. Fail-Closed 决策闭环（任何离线、忙碌、过期或格式损坏指标均可靠阻断）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from src.temporal.simulated_time import get_current_datetime

ROBOT_STATE_MAX_AGE_SECONDS: int = 30 * 60
TELEMETRY_MAX_FUTURE_SKEW_SECONDS: int = 5 * 60

OFFLINE_STATUSES = frozenset({"offline", "disconnected"})
BUSY_STATUSES = frozenset({
    "busy", "working", "operating", "executing", "unavailable", "maintenance", "fault"
})
AVAILABLE_STATUSES = frozenset({"available", "idle", "ready"})


def parse_bool(val: Any) -> bool | None:
    """严格解析布尔值或字符串形式的布尔标志 ("true"/"false"/"1"/"0" 等)，避免 Python 字符串 truthiness 错误。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        if val in (1, 1.0):
            return True
        if val in (0, 0.0):
            return False
    if isinstance(val, str):
        clean = val.strip().lower()
        if clean in ("true", "1", "yes", "online"):
            return True
        if clean in ("false", "0", "no", "offline"):
            return False
    return None


def inspect_robot_availability(
    unit_id: str,
    *,
    matched_unit: Optional[Dict[str, Any]],
    state: Optional[Dict[str, Any]],
    max_age_seconds: int = ROBOT_STATE_MAX_AGE_SECONDS,
    now_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """核心可用性判定算法：对机器人机队注册与持久化遥测快照进行一致性及可用性检查。

    检查项：
    1. 编号有效性与 Registry 注册存在性
    2. 实时快照记录存在性
    3. is_online / is_busy 显式指标检查
    4. overall_status / status / work_status / task_status 等多源指标一致性
    5. 时间戳解析、未来时钟漂移防护与 TTL 存活超时校验
    """
    if now_dt is None:
        now_dt = get_current_datetime()
    checked_at_str = now_dt.isoformat(timespec="seconds")

    if not isinstance(unit_id, str) or not unit_id.strip():
        return {
            "available": False,
            "reason_code": "UNIT_NOT_FOUND",
            "message": "无法发布任务：未指定有效的机器人编号。",
            "unit_id": str(unit_id or ""),
            "checked_at": checked_at_str,
            "state_updated_at": None,
        }

    clean_unit_id = unit_id.strip()

    # 1. 检查 matched_unit 是否已从 Registry 成功匹配
    if matched_unit is None:
        return {
            "available": False,
            "reason_code": "UNIT_NOT_FOUND",
            "message": f"无法发布任务：机器人 {clean_unit_id} 未在系统中注册。",
            "unit_id": clean_unit_id,
            "checked_at": checked_at_str,
            "state_updated_at": None,
        }

    # 2. 检查对应机器人状态记录是否存在
    if not isinstance(state, dict):
        return {
            "available": False,
            "reason_code": "STATE_NOT_FOUND",
            "message": (
                f"无法发布任务：机器人 {clean_unit_id} 状态记录不存在，无法确认当前可用性。\n"
                "请检查设备配置或刷新设备状态后重新确认发布。"
            ),
            "unit_id": clean_unit_id,
            "checked_at": checked_at_str,
            "state_updated_at": None,
        }

    state_updated_at_str = state.get("updated_at") or state.get("update_timestamp")

    # 3. 校验 is_online 显式布尔字段
    if "is_online" in state:
        online_bool = parse_bool(state["is_online"])
        if online_bool is False:
            return {
                "available": False,
                "reason_code": "OFFLINE",
                "message": (
                    f"无法发布任务：机器人 {clean_unit_id} 当前离线。\n"
                    "请更换机器人，或在设备恢复在线后重新确认发布。"
                ),
                "unit_id": clean_unit_id,
                "checked_at": checked_at_str,
                "state_updated_at": str(state_updated_at_str) if state_updated_at_str else None,
            }
        elif online_bool is None:
            return {
                "available": False,
                "reason_code": "INVALID_STATE_DATA",
                "message": (
                    f"无法发布任务：机器人 {clean_unit_id} is_online 指标无法解析为有效布尔值。\n"
                    "请刷新设备状态后重新确认发布。"
                ),
                "unit_id": clean_unit_id,
                "checked_at": checked_at_str,
                "state_updated_at": str(state_updated_at_str) if state_updated_at_str else None,
            }

    # 4. 校验 is_busy 显式布尔字段
    if "is_busy" in state:
        busy_bool = parse_bool(state["is_busy"])
        if busy_bool is True:
            return {
                "available": False,
                "reason_code": "BUSY",
                "message": (
                    f"无法发布任务：机器人 {clean_unit_id} 当前正在执行其他任务（处于忙碌状态）。\n"
                    "请更换机器人，或在设备空闲后重新确认发布。"
                ),
                "unit_id": clean_unit_id,
                "checked_at": checked_at_str,
                "state_updated_at": str(state_updated_at_str) if state_updated_at_str else None,
            }
        elif busy_bool is None:
            return {
                "available": False,
                "reason_code": "INVALID_STATE_DATA",
                "message": (
                    f"无法发布任务：机器人 {clean_unit_id} is_busy 指标无法解析为有效布尔值。\n"
                    "请刷新设备状态后重新确认发布。"
                ),
                "unit_id": clean_unit_id,
                "checked_at": checked_at_str,
                "state_updated_at": str(state_updated_at_str) if state_updated_at_str else None,
            }

    # 5. 收集所有存在的状态指标字段
    status_entries = []
    for key in (
        "overall_status", "status", "work_status", "task_status",
        "connection_status", "online_status", "busy",
    ):
        if key in state and state[key] is not None:
            status_entries.append((key, state[key]))

    if not status_entries and "is_online" not in state and "is_busy" not in state:
        return {
            "available": False,
            "reason_code": "INVALID_STATE_DATA",
            "message": (
                f"无法发布任务：机器人 {clean_unit_id} 缺少状态指标 (overall_status)。\n"
                "请刷新设备状态后重新确认发布。"
            ),
            "unit_id": clean_unit_id,
            "checked_at": checked_at_str,
            "state_updated_at": str(state_updated_at_str) if state_updated_at_str else None,
        }

    # 6. 遍历所有状态字段，任何一个负面/矛盾信号即阻断 (Fail-Closed)
    has_positive_available = False
    for key, raw_val in status_entries:
        parsed_bool = parse_bool(raw_val)
        if key in ("connection_status", "online_status"):
            if parsed_bool is False:
                return {
                    "available": False,
                    "reason_code": "OFFLINE",
                    "message": (
                        f"无法发布任务：机器人 {clean_unit_id} 当前离线。\n"
                        "请更换机器人，或在设备恢复在线后重新确认发布。"
                    ),
                    "unit_id": clean_unit_id,
                    "checked_at": checked_at_str,
                    "state_updated_at": str(state_updated_at_str) if state_updated_at_str else None,
                }
            elif parsed_bool is True:
                has_positive_available = True
                continue

        if key == "busy":
            if parsed_bool is True:
                return {
                    "available": False,
                    "reason_code": "BUSY",
                    "message": (
                        f"无法发布任务：机器人 {clean_unit_id} 当前正在执行其他任务（处于忙碌状态）。\n"
                        "请更换机器人，或在设备空闲后重新确认发布。"
                    ),
                    "unit_id": clean_unit_id,
                    "checked_at": checked_at_str,
                    "state_updated_at": str(state_updated_at_str) if state_updated_at_str else None,
                }
            elif parsed_bool is False:
                has_positive_available = True
                continue

        val_str = str(raw_val).strip().lower()
        if val_str in OFFLINE_STATUSES:
            return {
                "available": False,
                "reason_code": "OFFLINE",
                "message": (
                    f"无法发布任务：机器人 {clean_unit_id} 当前离线。\n"
                    "请更换机器人，或在设备恢复在线后重新确认发布。"
                ),
                "unit_id": clean_unit_id,
                "checked_at": checked_at_str,
                "state_updated_at": str(state_updated_at_str) if state_updated_at_str else None,
            }

        if val_str in BUSY_STATUSES:
            return {
                "available": False,
                "reason_code": "BUSY",
                "message": (
                    f"无法发布任务：机器人 {clean_unit_id} 当前正在执行其他任务（处于忙碌状态）。\n"
                    "请更换机器人，或在设备空闲后重新确认发布。"
                ),
                "unit_id": clean_unit_id,
                "checked_at": checked_at_str,
                "state_updated_at": str(state_updated_at_str) if state_updated_at_str else None,
            }

        if val_str in AVAILABLE_STATUSES:
            has_positive_available = True
        else:
            return {
                "available": False,
                "reason_code": "INVALID_STATE_DATA",
                "message": (
                    f"无法发布任务：机器人 {clean_unit_id} 状态字段 {key} 的值 '{raw_val}' 无法识别。\n"
                    "请刷新设备状态后重新确认发布。"
                ),
                "unit_id": clean_unit_id,
                "checked_at": checked_at_str,
                "state_updated_at": str(state_updated_at_str) if state_updated_at_str else None,
            }

    if not has_positive_available and ("is_online" not in state or parse_bool(state["is_online"]) is not True):
        return {
            "available": False,
            "reason_code": "INVALID_STATE_DATA",
            "message": (
                f"无法发布任务：机器人 {clean_unit_id} 缺少明确可用的状态指标。\n"
                "请刷新设备状态后重新确认发布。"
            ),
            "unit_id": clean_unit_id,
            "checked_at": checked_at_str,
            "state_updated_at": str(state_updated_at_str) if state_updated_at_str else None,
        }

    # 7. 存活性 TTL 与未来时间戳漂移校验
    if not state_updated_at_str:
        return {
            "available": False,
            "reason_code": "STATE_EXPIRED",
            "message": (
                f"无法发布任务：机器人 {clean_unit_id} 状态信息已过期，无法确认当前可用性。\n"
                "请刷新设备状态后重新确认发布。"
            ),
            "unit_id": clean_unit_id,
            "checked_at": checked_at_str,
            "state_updated_at": None,
        }

    try:
        updated_dt = datetime.fromisoformat(str(state_updated_at_str))
        if updated_dt.tzinfo is None:
            updated_dt = updated_dt.replace(tzinfo=now_dt.tzinfo)
        else:
            updated_dt = updated_dt.astimezone(now_dt.tzinfo)

        age_seconds = (now_dt - updated_dt).total_seconds()
        if age_seconds < -TELEMETRY_MAX_FUTURE_SKEW_SECONDS:
            return {
                "available": False,
                "reason_code": "INVALID_STATE_DATA",
                "message": (
                    f"无法发布任务：机器人 {clean_unit_id} 状态时间戳明显晚于系统时间，"
                    "无法确认当前可用性。\n请校准设备时钟并刷新状态后重新确认发布。"
                ),
                "unit_id": clean_unit_id,
                "checked_at": checked_at_str,
                "state_updated_at": str(state_updated_at_str),
            }
        if age_seconds > max_age_seconds:
            return {
                "available": False,
                "reason_code": "STATE_EXPIRED",
                "message": (
                    f"无法发布任务：机器人 {clean_unit_id} 状态信息已过期，无法确认当前可用性。\n"
                    "请刷新设备状态后重新确认发布。"
                ),
                "unit_id": clean_unit_id,
                "checked_at": checked_at_str,
                "state_updated_at": str(state_updated_at_str),
            }
    except Exception:
        return {
            "available": False,
            "reason_code": "INVALID_STATE_DATA",
            "message": (
                f"无法发布任务：机器人 {clean_unit_id} 状态格式无法解析。\n"
                "请刷新设备状态后重新确认发布。"
            ),
            "unit_id": clean_unit_id,
            "checked_at": checked_at_str,
            "state_updated_at": str(state_updated_at_str),
        }

    return {
        "available": True,
        "reason_code": "AVAILABLE",
        "message": f"机器人 {clean_unit_id} 当前在线且空闲可用。",
        "unit_id": clean_unit_id,
        "checked_at": checked_at_str,
        "state_updated_at": str(state_updated_at_str),
    }
