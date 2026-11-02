from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import importlib
import sys
import types
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("src")
pkg.__path__ = [str(PROJECT_ROOT / "src")]
sys.modules.setdefault("src", pkg)

simulated_time = importlib.import_module("src.temporal.simulated_time")
time_context = importlib.import_module("src.temporal.time_context")


class TimeContextTest(unittest.TestCase):
    def setUp(self):
        simulated_time.get_simulated_time().set_current_time(
            datetime(2026, 7, 6, 9, 30, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        )

    def test_formats_current_simulated_time_for_user(self):
        context = time_context.get_time_context()

        self.assertEqual(context.datetime_text, "2026-07-06 09:30:15 CST")
        self.assertEqual(context.user_reply, "当前模拟时间是 2026-07-06 09:30:15 CST。")

    def test_is_standalone_time_query_true(self):
        queries = [
            "现在几点？",
            "今天是几号",
            "请问当前时间是多少",
            "模拟时间",
            "系统时间是多少",
            "看一下当前时间",
            "告诉我几点",
            "查一下今天是几日",
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertTrue(time_context.is_standalone_time_query(query))

    def test_is_standalone_time_query_false_task_words(self):
        queries = [
            "执行任务的时间",
            "现在可以下发巡检规划吗",
            "当前机器人的坐标",
            "油田水深是多少",
            "采油树现在有违规吗",
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertFalse(time_context.is_standalone_time_query(query))

    def test_is_standalone_time_query_false_empty(self):
        queries = ["", "   ", "，。！？", "...", "？"]
        for query in queries:
            with self.subTest(query=query):
                self.assertFalse(time_context.is_standalone_time_query(query))

    def test_is_standalone_time_query_false_no_time_words(self):
        queries = [
            "天气怎么样",
            "你好",
            "请问这是什么",
            "你在干嘛",
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertFalse(time_context.is_standalone_time_query(query))

    def test_is_standalone_time_query_false_regex_mismatch(self):
        queries = [
            "现在几点，顺便帮我查一下天气",
            "今天是几号，我不知道",
            "告诉我当前时间和其他事情",
            "今天心情不错",
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertFalse(time_context.is_standalone_time_query(query))


if __name__ == "__main__":
    unittest.main()
