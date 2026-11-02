from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import importlib
import sys
import types
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("src")
pkg.__path__ = [str(PROJECT_ROOT / "src")]
sys.modules.setdefault("src", pkg)

simulated_time = importlib.import_module("src.temporal.simulated_time")
time_context = importlib.import_module("src.temporal.time_context")


@pytest.fixture(autouse=True)
def setup_simulated_time():
    simulated_time.get_simulated_time().set_current_time(
        datetime(2026, 7, 6, 9, 30, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    )


def test_time_context_properties():
    dt = datetime(2026, 7, 6, 9, 30, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    tc = time_context.TimeContext(current=dt)

    assert tc.date_text == "2026年07月06日（Monday）"
    assert tc.datetime_text == "2026-07-06 09:30:15 CST"
    assert tc.user_reply == "当前模拟时间是 2026-07-06 09:30:15 CST。"


def test_get_time_context_default_tzinfo():
    simulated_time.get_simulated_time().set_current_time(
        datetime(2026, 7, 6, 9, 30, 15)
    )
    tc = time_context.get_time_context()

    assert tc.current.tzinfo == ZoneInfo("Asia/Shanghai")
    assert tc.current.year == 2026
    assert tc.current.month == 7
    assert tc.current.day == 6
    assert tc.current.hour == 9
    assert tc.current.minute == 30
    assert tc.current.second == 15
    assert tc.current.microsecond == 0


def test_get_time_context_convert_tzinfo():
    simulated_time.get_simulated_time().set_current_time(
        datetime(2026, 7, 6, 9, 30, 15, tzinfo=timezone.utc)
    )
    tc = time_context.get_time_context()

    assert tc.current.tzinfo == ZoneInfo("Asia/Shanghai")
    # 09:30 UTC -> 17:30 Asia/Shanghai
    assert tc.current.hour == 17
    assert tc.current.microsecond == 0


def test_is_standalone_time_query_empty():
    assert not time_context.is_standalone_time_query("")
    assert not time_context.is_standalone_time_query("   ")


def test_is_standalone_time_query_task_words():
    assert not time_context.is_standalone_time_query("现在可以开始巡检吗？")
    assert not time_context.is_standalone_time_query("当前机器人的水深是多少？")


def test_is_standalone_time_query_no_time_words():
    assert not time_context.is_standalone_time_query("今天天气真好")


def test_is_standalone_time_query_pure_time():
    assert time_context.is_standalone_time_query("请问现在几点？")
    assert time_context.is_standalone_time_query("当前时间是多少")
    assert time_context.is_standalone_time_query("今天是几号")
    assert time_context.is_standalone_time_query("系统时间")


def test_is_standalone_time_query_non_matching():
    assert not time_context.is_standalone_time_query("当前时间，你想干嘛")
