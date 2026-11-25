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
    sim = simulated_time.get_simulated_time()
    prev_time = sim.get_current_time()
    sim.set_current_time(
        datetime(2026, 7, 6, 9, 30, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    )
    try:
        yield
    finally:
        sim.set_current_time(prev_time)


def test_formats_current_simulated_time_for_user():
    context = time_context.get_time_context()

    assert context.datetime_text == "2026-07-06 09:30:15 CST"
    assert context.user_reply == "当前模拟时间是 2026-07-06 09:30:15 CST。"


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


@pytest.mark.parametrize(
    "query",
    [
        "现在几点？",
        "请问现在几点？",
        "今天是几号",
        "请问当前时间是多少",
        "当前时间是多少",
        "模拟时间",
        "系统时间",
        "系统时间是多少",
        "看一下当前时间",
        "告诉我几点",
        "查一下今天是几日",
    ],
)
def test_is_standalone_time_query_true(query):
    assert time_context.is_standalone_time_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "执行任务的时间",
        "现在可以下发巡检规划吗",
        "现在可以开始巡检吗？",
        "当前机器人的坐标",
        "当前机器人的水深是多少？",
        "油田水深是多少",
        "采油树现在有违规吗",
    ],
)
def test_is_standalone_time_query_false_task_words(query):
    assert not time_context.is_standalone_time_query(query)


@pytest.mark.parametrize("query", ["", "   ", "，。！？", "...", "？"])
def test_is_standalone_time_query_false_empty(query):
    assert not time_context.is_standalone_time_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "今天天气真好",
        "天气怎么样",
        "你好",
        "请问这是什么",
        "你在干嘛",
    ],
)
def test_is_standalone_time_query_false_no_time_words(query):
    assert not time_context.is_standalone_time_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "当前时间，你想干嘛",
        "现在几点，顺便帮我查一下天气",
        "今天是几号，我不知道",
        "告诉我当前时间和其他事情",
        "今天心情不错",
    ],
)
def test_is_standalone_time_query_false_non_matching(query):
    assert not time_context.is_standalone_time_query(query)
