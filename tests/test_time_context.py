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

simulated_time = importlib.import_module("src.simulated_time")
time_context = importlib.import_module("src.time_context")


class TimeContextTest(unittest.TestCase):
    def setUp(self):
        simulated_time.get_simulated_time().set_current_time(
            datetime(2026, 7, 6, 9, 30, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        )

    def test_formats_current_simulated_time_for_user(self):
        context = time_context.get_time_context()

        self.assertEqual(context.datetime_text, "2026-07-06 09:30:15 CST")
        self.assertEqual(context.user_reply, "当前模拟时间是 2026-07-06 09:30:15 CST。")

    def test_recognizes_standalone_time_queries(self):
        self.assertTrue(time_context.is_standalone_time_query("现在几点？"))
        self.assertTrue(time_context.is_standalone_time_query("当前模拟时间是多少"))
        self.assertTrue(time_context.is_standalone_time_query("今天几号"))

    def test_keeps_task_requests_in_normal_dialogue_flow(self):
        self.assertFalse(time_context.is_standalone_time_query("现在开始管缆巡检"))
        self.assertFalse(time_context.is_standalone_time_query("如果现在开始，会不会违规？"))
        self.assertFalse(time_context.is_standalone_time_query("明天上午开始可以吗"))


if __name__ == "__main__":
    unittest.main()
