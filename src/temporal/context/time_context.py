"""
time_context.py — 模拟时间上下文工具

集中处理用户时间查询和提示词中的时间展示，避免各模块重复拼装时间文本。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .simulated_time import get_current_datetime


DEFAULT_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class TimeContext:
    current: datetime

    @property
    def date_text(self) -> str:
        return self.current.strftime("%Y年%m月%d日（%A）")

    @property
    def datetime_text(self) -> str:
        return self.current.strftime("%Y-%m-%d %H:%M:%S %Z")

    @property
    def user_reply(self) -> str:
        return f"当前模拟时间是 {self.datetime_text}。"


def get_time_context() -> TimeContext:
    current = get_current_datetime()
    if current.tzinfo is None:
        current = current.replace(tzinfo=DEFAULT_TIMEZONE)
    else:
        current = current.astimezone(DEFAULT_TIMEZONE)
    return TimeContext(current=current.replace(microsecond=0))


def is_standalone_time_query(message: str) -> bool:
    """判断用户是否只是在询问当前模拟时间。"""
    text = _normalize_text(message)
    if not text:
        return False

    task_words = (
        "任务", "巡检", "管缆", "采油树", "rov", "机器人", "设备", "水深",
        "坐标", "油田", "井口", "开始", "执行", "规划", "发布", "下发",
        "违规", "约束", "可以吗", "能不能",
    )
    if any(word in text for word in task_words):
        return False

    time_words = (
        "时间", "几点", "几时", "日期", "今天", "现在", "当前",
        "模拟时间", "系统时间", "当前时间", "当前日期",
    )
    if not any(word in text for word in time_words):
        return False

    return re.fullmatch(
        r"(请问|帮我看看|告诉我|查询|查一下|看一下|现在|当前|模拟|系统|今天|的|是|多少|"
        r"什么|几号|几日|几点|几时|时间|日期|当前时间|模拟时间|系统时间)+",
        text,
    ) is not None


def _normalize_text(message: str) -> str:
    return re.sub(r"[\s，。！？?！、,.：:；;“”\"'（）()【】\[\]]+", "", message.strip().lower())
