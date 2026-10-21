"""Execution gate shared by automatic, manual, and non-Web task dispatch."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any

from src.temporal.simulated_time import get_current_datetime
from src.session.history_manager import save_conversation
from src.dispatch.execution_state import (
    intent_execution_guard, read_execution_control, write_execution_control,
)

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
        runtime_execution_review=getattr(manager, "runtime_execution_review", None),
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


def is_runtime_risk_confirmation(message: str) -> bool:
    return bool(re.fullmatch(
        r"\s*(?:确认(?:当前)?执行风险|接受执行风险|忽略警告)\s*[。！!]*\s*", message or "",
    ))


def runtime_validation_review(manager: Any):
    """Refresh execution evidence without reopening or changing an archived plan."""
    validation = manager.validator.validate_task(
        manager.task_state, task_version=manager.slot_store.version, purpose="runtime_execution",
    )
    manager.slot_store.validation_result = validation
    violations = validation.violations or []
    hard = validation.overall_status not in {"valid", "warning", "blocked_soft"} or any(
        v.severity == "hard" for v in violations
    )
    soft = [v for v in violations if v.severity == "soft"]
    # Interactive/publication acknowledgements are reusable only when their
    # actual evidence is identical; a matching field name is insufficient.
    acknowledged = {
        ack.constraint_id for ack in manager._get_valid_acknowledgements(validation)
        if ack.task_version == validation.task_version
        and ack.validation_fingerprint == validation.validation_fingerprint
        and any(v.constraint_id == ack.constraint_id
                and ack.value == getattr(v, "observed_value", None) for v in soft)
    }
    review = {
        "status": "blocked_hard" if hard else "pending",
        "intent_id": manager.final_result.get("intent_id"),
        "task_version": validation.task_version,
        "validation_fingerprint": validation.validation_fingerprint,
        "state_snapshot": copy.deepcopy(validation.state_snapshot),
        "violations": [v.to_dict() for v in violations],
    }
    evidence = {key: value for key, value in review.items() if key != "status"}
    review["review_token"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    previous = getattr(manager, "runtime_execution_review", None)
    if isinstance(previous, dict) and previous.get("status") == "acknowledged":
        if previous.get("review_token") != review["review_token"]:
            acknowledged = set()
    unacknowledged = [v for v in soft if v.constraint_id not in acknowledged]
    if not hard and not unacknowledged:
        review["status"] = "acknowledged"
    manager.runtime_execution_review = review if (hard or soft) else None
    return validation, hard, unacknowledged


def dispatch_completed_task(manager: Any, bridge: Any, *, resume_paused: bool = False) -> dict:
    """Check a finalized intent at execution time; never schedule a background send.

    Callers serialize access with the session lock. Future tasks remain archived
    until an explicit retry at/after their start time passes fresh validation.
    Stored display state is never authority to suppress or authorize a send.
    """
    intent = getattr(manager, "final_result", None)
    if manager.phase != "done" or not isinstance(intent, dict) or not intent:
        return _result(manager, "BLOCKED", "当前会话尚无已确认归档的任务。", retry_allowed=False)
    try:
        with intent_execution_guard(intent) as control_path:
            control = read_execution_control(control_path, intent)
            if control and control["dispatch_blocked"]:
                request = control["request"]
                reviewed_control = getattr(manager, "last_control_request", None) == request
                setter = getattr(manager, "_set_execution_control_state", None)
                if callable(setter):
                    setter(f"{request['action']}_requested", request, reason="intent_control_reconciled")
                else:
                    manager.control_state = f"{request['action']}_requested"
                    manager.last_control_request = copy.deepcopy(request)
                manager.runtime_execution_review = None
                is_pause = request.get("outcome") == "not_sent_paused"
                if is_pause and resume_paused and reviewed_control:
                    resumed = dict(request, outcome="resumed", message="已明确恢复下发，正在重新校验执行条件。")
                    write_execution_control(control_path, intent, resumed, blocked=False)
                    if callable(setter):
                        setter(f"{request['action']}_requested", resumed, reason="explicit_dispatch_resume")
                    else:
                        manager.last_control_request = copy.deepcopy(resumed)
                else:
                    message = request.get("message") or "该任务已收到停止或取消请求，禁止继续下发。"
                    return _result(manager, "BLOCKED", message,
                                   reason="execution_control", retry_allowed=is_pause)
            return _dispatch_completed_task_locked(manager, bridge)
    except Exception as exc:
        return _result(manager, "FAILED", "执行控制记录无法核验，未下发任务。",
                       error=str(exc), retry_allowed=False)


def _dispatch_completed_task_locked(manager: Any, bridge: Any) -> dict:
    intent = manager.final_result
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

        from mcp.core.sealien_protocol import ProtocolValidationError, validate_valve_operation
        try:
            validate_valve_operation(intent)
        except ProtocolValidationError as exc:
            return _result(manager, "BLOCKED", f"任务计划已保存。{exc}",
                           reason="unsupported_task_operation", error=str(exc), retry_allowed=False)

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
        validation, hard, unacknowledged = runtime_validation_review(manager)
        violations = validation.violations or []
        if hard:
            return _result(manager, "BLOCKED", "执行前校验未通过，任务尚未下发。",
                           error="；".join(v.message for v in violations) or validation.overall_status)
        if unacknowledged:
            return _result(manager, "BLOCKED", "执行条件出现尚未确认的告警。请核对后回复“确认执行风险”，再检查并下发。",
                           reason="runtime_soft_warning", error="；".join(v.message for v in unacknowledged))
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
