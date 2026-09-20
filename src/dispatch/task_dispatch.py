"""Execution gate shared by automatic, manual, and non-Web task dispatch."""
from __future__ import annotations

import copy
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any

from src.temporal.simulated_time import get_current_datetime
from src.session.history_manager import save_conversation

BEIJING_TZ = timezone(timedelta(hours=8))


def save_dispatch_history(manager: Any, save_fn=save_conversation) -> str:
    """Archive the conversation together with its latest dispatch outcome."""
    return save_fn(
        session_id=manager.session_id, conversation_history=manager.conversation_history,
        task_state=manager.task_state, built_json=manager._last_built_json,
        mode=manager.mode, phase=manager.phase, intent_id=manager.task_state.get("intent_id"),
        slot_store=manager.slot_store, dialogue_mode=manager.dialogue_mode,
        last_mode_transition=manager.last_mode_transition,
        mode_transition_history=manager.mode_transition_history,
        control_state=manager.control_state, last_control_request=manager.last_control_request,
        ros2_dispatch=getattr(manager, "ros2_dispatch", None),
        pending_published_intent=getattr(manager, "_pending_published_intent", None),
    )


def _task_time(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=BEIJING_TZ) if parsed.tzinfo is None else parsed


def _result(manager: Any, state: str, message: str, **details: Any) -> dict:
    result = {"state": state, "message": message,
              "retry_allowed": state != "SENT", **details}
    manager.ros2_dispatch = copy.deepcopy(result)
    return result


def _sent(manager: Any, task_id: int) -> dict:
    manager.dispatched_ros2_task_id = task_id
    return _result(manager, "SENT", "任务已写入 ROS 2 传输，等待机器人遥测确认。",
                   task_id=task_id, task_id_hex=f"0x{task_id:X}")


def dispatch_completed_task(manager: Any, bridge: Any) -> dict:
    """Check a finalized intent at execution time; never schedule a background send.

    Callers serialize access with the session lock. Future tasks remain archived
    until an explicit retry at/after their start time passes fresh validation.
    Stored display state is never authority to suppress or authorize a send.
    """
    intent = getattr(manager, "final_result", None)
    if manager.phase != "done" or not isinstance(intent, dict) or not intent:
        return _result(manager, "BLOCKED", "当前会话尚无已确认归档的任务。", retry_allowed=False)
    try:
        record = None
        if bridge is not None:
            reader = getattr(bridge, "get_dispatch_record", None)
            record = reader(intent) if callable(reader) else None
            if isinstance(record, dict) and record.get("dispatch_state") == "SENT":
                return _sent(manager, int(record["task_id"]))

        previous = getattr(manager, "ros2_dispatch", None) or {}
        if previous.get("reason") == "legacy_history" and not isinstance(record, dict):
            return _result(manager, "UNKNOWN", "历史任务缺少可核验的发送记录，未重复发送；请核对机器人任务记录。",
                           reason="legacy_history")

        now = get_current_datetime()
        if now.tzinfo is None:
            now = now.replace(tzinfo=BEIJING_TZ)
        time_info = intent.get("time") or {}
        start, end = _task_time(time_info.get("start")), _task_time(time_info.get("end"))
        if start is not None and now < start:
            return _result(manager, "SCHEDULED", "任务已保存，尚未到计划开始时间；到期后请检查执行条件并下发。",
                           scheduled_for=start.isoformat())
        if end is not None and now >= end:
            return _result(manager, "BLOCKED", "任务的计划结束时间已过，请创建新任务。", retry_allowed=False)

        validator = getattr(manager, "validator", None)
        task_state = getattr(manager, "task_state", None)
        store = getattr(manager, "slot_store", None)
        if validator is None or not isinstance(task_state, dict) or store is None:
            return _result(manager, "BLOCKED", "缺少可核验的任务状态，无法完成执行前校验。", retry_allowed=False)
        validation = validator.validate_task(task_state, task_version=store.version,
                                             purpose="runtime_execution")
        violations = validation.violations or []
        if (validation.overall_status not in {"valid", "warning", "blocked_soft"}
                or any(v.severity == "hard" for v in violations)):
            return _result(manager, "BLOCKED", "执行前校验未通过，任务尚未下发。",
                           error="；".join(v.message for v in violations) or validation.overall_status)
        acknowledged = manager._get_valid_acknowledgements(validation)
        acknowledged_ids = {ack.constraint_id for ack in acknowledged}
        unacknowledged = [v for v in violations if v.severity == "soft" and v.constraint_id not in acknowledged_ids]
        if unacknowledged:
            return _result(manager, "BLOCKED", "执行条件出现尚未确认的告警，请重新确认任务条件。",
                           error="；".join(v.message for v in unacknowledged))
        if bridge is None or not bridge.is_healthy():
            return _result(manager, "FAILED", "任务已保存，ROS 2 桥接服务未连接。", error="ROS 2 MCP 桥接服务未连接")

        unit_id = task_state.get("equipment_unit_id")
        snapshot = validation.state_snapshot or {}
        state_info = getattr(getattr(manager, "kb", None), "state_info", None)
        guard = (state_info.guard_unit_state_version(str(unit_id), snapshot["state_version"])
                 if unit_id and state_info is not None and "state_version" in snapshot else nullcontext())
        with guard:
            task_id = bridge.dispatch_intent(intent)
        return _sent(manager, task_id)
    except Exception as exc:
        from mcp.core.bridge_service import DispatchOutcomeUnknown
        if isinstance(exc, DispatchOutcomeUnknown):
            return _result(manager, "UNKNOWN", str(exc), task_id=exc.task_id,
                           task_id_hex=f"0x{exc.task_id:X}", error=str(exc))
        return _result(manager, "FAILED", "任务已保存，执行检查或下发失败。", error=str(exc))
