"""Read-only freshness evidence shared by dialogue and telemetry presentation."""

from __future__ import annotations

from datetime import datetime
import re
from zoneinfo import ZoneInfo

from src.state.runtime_checker import inspect_state_timestamp
from src.temporal.simulated_time import get_current_datetime


def assess_telemetry_freshness(
    state_snapshot_or_state: dict | None, *, now: datetime | None = None,
) -> dict:
    """Classify evidence age; this does not perform or override admission checks."""
    source = state_snapshot_or_state if isinstance(state_snapshot_or_state, dict) else {}
    state = source.get("state", source)
    state = state if isinstance(state, dict) else {}
    timestamp = (
        state.get("updated_at") or state.get("update_timestamp")
        or state.get("update_at") or source.get("updated_at")
    )
    now = now or get_current_datetime()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    inspection = inspect_state_timestamp(timestamp, now_dt=now)
    reason = inspection["timestamp_error"]
    status = "fresh" if reason is None else ("invalid" if reason in ("invalid", "future") else reason)
    messages = {
        "fresh": "遥测时间有效；设备健康与作业准入仍须依据实际指标和校验结果。",
        "stale": "遥测数据已过期，无法确认当前海况和设备健康，请刷新状态信息。",
        "missing": "遥测更新时间未提供，无法确认当前海况和设备健康，请刷新状态信息。",
        "invalid": "遥测时间格式异常，无法确认当前海况和设备健康，请刷新状态信息。",
    }
    message = messages[status]
    if reason == "future":
        message = "遥测时间明显晚于系统时间，无法确认当前海况和设备健康，请校准时钟并刷新状态信息。"
    return {
        "status": status, "usable": status == "fresh",
        "updated_at": str(timestamp) if timestamp else None,
        "age_seconds": inspection["age_seconds"], "message": message,
    }


def format_telemetry_unavailable(
    equipment: str, state_snapshot_or_state: dict | None,
    *, freshness: dict | None = None,
) -> str:
    source = state_snapshot_or_state if isinstance(state_snapshot_or_state, dict) else {}
    state = source.get("state", source)
    state = state if isinstance(state, dict) else {}
    freshness = freshness or assess_telemetry_freshness(source)
    lines = [f"机器人 {equipment}：{freshness['message']}"]
    if freshness["updated_at"]:
        lines.append(f"- 最后更新时间：{freshness['updated_at']}")
    version = source.get("state_version", state.get("version"))
    if version is not None:
        lines.append(f"- 状态版本号：{version}")
    lines.append("当前设备健康与海况：未知。历史快照不能作为当前正常、可用或安全的依据。")
    return "\n".join(lines)


_TELEMETRY_SUBJECT = re.compile(
    r"(?:遥测|水文|海况|环境核验|环境信息|状态快照|状态版本|更新时间|"
    r"总体状态|整体状态|机器人状态|设备状态|设备健康|子系统|"
    r"(?:机器人|设备).*?(?:在线|空闲|可用|健康)|"
    r"海流|水流|流速|浑浊|浊度|能见度|障碍物|母船支援|母船支持|"
    r"推进器|定深(?:能力|系统)|视觉(?:系统|状态)|声呐(?:系统|状态)|"
    r"生存状态|通信状态|脐带缆(?:连接|状态)|机械臂状态|末端(?:工具|执行器)状态)"
)
_CURRENT_CLAIM = re.compile(r"[:：|]|当前|实时|正常|可用|异常|较高|偏高|超出|符合|已过期|核验看板")


def ground_telemetry_reply(
    reply: str, constraint_context: dict | None, *, task_state: dict | None = None,
) -> str:
    """Remove unsupported live telemetry claims while retaining task receipts/advice."""
    context = constraint_context or {}
    deferred = context.get("runtime_validation_deferred") or any(
        (item.get("check_type", "") if isinstance(item, dict) else getattr(item, "check_type", "")) == "future_task_runtime_notice"
        for item in context.get("violations", [])
    )
    snapshot = context.get("state_snapshot")
    if not deferred and not isinstance(snapshot, dict):
        return reply
    freshness = assess_telemetry_freshness(snapshot)
    if not deferred and freshness["usable"]:
        return reply

    lines = []
    in_telemetry_table = False
    removed_claim = False
    for line in str(reply or "").splitlines():
        if re.search(r"(?:遥测|环境|状态).*?(?:看板|汇报|校核摘要)", line):
            in_telemetry_table = True
            removed_claim = True
            continue
        if in_telemetry_table and line.lstrip().startswith("|"):
            continue
        if line.strip() and not _TELEMETRY_SUBJECT.search(line):
            in_telemetry_table = False
        # Conditional engineering advice is not a claim about present telemetry.
        conditional = re.search(r"(?:如果|若|假如|当.*时)", line) and not re.search(r"当前|实时|现有", line)
        if _TELEMETRY_SUBJECT.search(line) and (_CURRENT_CLAIM.search(line) or line.lstrip().startswith("#")) and not conditional:
            removed_claim = True
            continue
        lines.append(line)
    if not removed_claim and not deferred and context.get("type", "none") == "none":
        return reply
    cleaned = "\n".join(lines).strip()
    if deferred:
        notice = "这是未来排期任务，运行时环境与遥测核验尚未进行，需在执行窗口期前重新核验；不能据此判断设备故障或保证设备健康。"
    else:
        unit = (snapshot or {}).get("unit_id") or (task_state or {}).get("equipment_unit_id") or "所选设备"
        notice = format_telemetry_unavailable(unit, snapshot, freshness=freshness)
        if context.get("violations"):
            notice += "\n已有遥测相关校验提示基于该历史快照，不能据此断言当前海况；校验与确认要求仍然有效。"
    return f"{cleaned}\n\n{notice}".strip()
