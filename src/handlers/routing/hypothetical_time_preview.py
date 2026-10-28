"""Deterministic READ-only previews of a single proposed task time window.

The caller owns READ/WRITE routing. This module accepts only a complete time
proposal, reuses the task time grammar and range engine, and never emits writes.
"""
from __future__ import annotations

from datetime import datetime
import re
import unicodedata
from zoneinfo import ZoneInfo

from src.temporal.relative_time_parser import DURATION_VALUE_PATTERN
from src.temporal.temporal_parser import TemporalParser
from src.temporal.time_range_engine import parse_time_range


_LOCAL_ZONE = ZoneInfo("Asia/Shanghai")
_HYPOTHETICAL = re.compile(
    r"^(?:请问\s*)?(?:如果|假如|假设|假定|要是|倘若|若)\s*(?P<body>.+?)"
    r"\s*(?:的话)?\s*[,，]?\s*"
    r"(?:会有什么影响|会有何影响|会不会有影响|有什么影响|会怎么样|会怎样|怎么样|怎样"
    r"|什么时候结束|何时结束|几点结束|结束时间是多少|可以吗|可行吗|呢)\s*[?？。!！]*$"
)
_TIME_CUE = re.compile(rf"{DURATION_VALUE_PATTERN}|(?:今天|明天|后天|\d{{4}}年).*(?:点|[:：])")


def _local_datetime(value: object) -> datetime | None:
    result = TemporalParser.parse_state_datetime(value)
    if result is not None and result.tzinfo is not None:
        result = result.astimezone(_LOCAL_ZONE).replace(tzinfo=None)
    return result


def _duration_text(seconds: float) -> str:
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    return "".join(f"{amount:g}{unit}" for amount, unit in (
        (days, "天"), (hours, "小时"), (minutes, "分钟"), (seconds, "秒"),
    ) if amount) or "0秒"


def _clock_text(value: datetime) -> str:
    return value.isoformat(sep=" ")


def _saved_summary(start: datetime | None, end: datetime | None) -> str:
    parts = [f"开始 {_clock_text(start)}" if start else "开始时间未提供",
             f"结束 {_clock_text(end)}" if end else "结束时间未提供"]
    if start and end and end > start:
        parts.append("持续" + _duration_text((end - start).total_seconds()))
    return "当前已保存：" + "；".join(parts) + "。"


def _proposal_fields(body: str) -> dict[str, str] | None:
    # Remove only grammatical connectors, never a subject, a negation, or an
    # alternative. Every remaining character must belong to a time statement
    # or the scanner's explicit task prefix; substring matches are insufficient.
    body = body.replace("、", ",")
    body = re.sub(r"^((?:(?:任务|作业|把|将)\s*)*)(?:改成|改为|设为|调整为)\s*", r"\1", body)
    body = re.sub(
        r"(持续时间|持续时长|时长|持续)\s*(?:修改为|设置为|调整为|改成|改为|设为)\s*", r"\1为", body,
    )
    statements = TemporalParser._explicit_time_statements(body)
    fields: dict[str, str] = {}
    cursor = 0
    for statement in statements:
        if statement["kind"] not in {"endpoint", "duration"}:
            return None
        gap = body[cursor:statement["start"]]
        if not re.fullmatch(r"(?:(?:任务|作业|把|将|请)|[\s,，;；。])*", gap):
            return None
        cursor = statement["end"]
        if statement["kind"] == "duration":
            key = "duration"
        else:
            key = "start" if statement["label"] in {
                "开始时间", "起始时间", "开工时间", "开始", "开工",
            } else "end"
        if key in fields:
            return None
        fields[key] = statement["value"]
    if not fields or body[cursor:].strip(" \t\n,，;；。"):
        return None
    return fields


def build_hypothetical_time_reply(user_message: str, task_state: dict) -> str | None:
    """Return a complete grounded answer, or None for an unrelated READ turn."""
    message = unicodedata.normalize("NFKC", user_message).strip()
    match = _HYPOTHETICAL.fullmatch(message)
    if not match or not _TIME_CUE.search(match["body"]):
        return None
    if not re.search(r"开始|开工|持续|时长|结束|完工|收工", match["body"]):
        return None

    from src.temporal.simulated_time import get_current_datetime
    now = _local_datetime(get_current_datetime()).replace(microsecond=0)
    previous_start = _local_datetime(task_state.get("start_time"))
    previous_end = _local_datetime(task_state.get("end_time"))
    saved = _saved_summary(previous_start, previous_end)
    fields = _proposal_fields(match["body"])
    result = None
    if fields:
        try:
            result = parse_time_range(
                fields.get("start"), fields.get("duration"), fields.get("end"),
                base_dt=now, previous_start=previous_start, previous_end=previous_end,
            )
        except (ValueError, OverflowError, TypeError):
            # An invalid or unrepresentable proposal must not fall through to
            # model arithmetic or claim that the stored task was changed.
            result = None
    if result is None or not result.success:
        return (
            "这段描述无法唯一确定一个完整的任务时间方案，暂不能给出确定的假设起止时间。"
            "请明确一个任务开始时间及持续时长（或结束时间），并区分其他设备的时间。\n"
            f"{saved}\n本轮仅分析，未修改任务。"
        )

    start, end = result.start_time.value, result.end_time.value
    duration = (end - start).total_seconds()
    until_end = (end - now).total_seconds()
    relative_end = (
        f"距当前时间{_duration_text(until_end)}" if until_end >= 0
        else f"比当前时间早{_duration_text(-until_end)}"
    )
    inheritance = []
    if "start" not in fields:
        inheritance.append("沿用已保存的开始时间")
    if "duration" not in fields and "end" not in fields:
        inheritance.append("沿用已保存的持续时长")
    assumption = "（" + "，".join(inheritance) + "）" if inheritance else ""
    lines = [
        "本轮仅作假设计算，未修改任务。",
        f"计算基准：北京时间 {_clock_text(now)}。",
        f"假设方案{assumption}：开始 {_clock_text(start)}；持续{_duration_text(duration)}；"
        f"结束 {_clock_text(end)}（{relative_end}）。",
        saved,
    ]
    if previous_start and previous_end and previous_end > previous_start:
        previous_duration = (previous_end - previous_start).total_seconds()
        if duration != previous_duration:
            direction = "增加" if duration > previous_duration else "减少"
            lines.append(f"假设时长由{_duration_text(previous_duration)}变为{_duration_text(duration)}，"
                         f"{direction}{_duration_text(abs(duration - previous_duration))}。")
    lines.append("这里只计算时间变化；设备可用性和作业条件需按假设时间段另行校验，尚未据此判定能否执行。")
    return "\n".join(lines)
