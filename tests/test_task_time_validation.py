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
validator_module = importlib.import_module("src.validator")
get_simulated_time = simulated_time.get_simulated_time
TaskValidator = validator_module.TaskValidator


class FakeKnowledgeBase:
    def get_constraints(self):
        return [
            {
                "id": "C030",
                "name": "任务开始时间不能早于当前时间",
                "applies_to": ["all"],
                "check_type": "start_time_not_in_past",
                "violation_message": (
                    "任务开始时间 {start_time} 早于当前系统时间 {current_time}，"
                    "可能表示任务开始时间已过期。请确认是否继续，或将任务开始时间修改为当前时间之后。"
                ),
                "severity": "soft",
            }
        ]

    def get_rov(self, equipment):
        return None


class TaskTimeValidationTest(unittest.TestCase):
    def setUp(self):
        get_simulated_time().set_current_time(
            datetime(2026, 6, 30, 17, 38, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        self.validator = TaskValidator(FakeKnowledgeBase())

    def test_allows_start_time_slightly_before_current_time(self):
        violations = self.validator.validate({"start_time": "2026-06-30T17:37:59"})

        self.assertEqual(violations, [])

    def test_warns_when_start_time_exceeds_past_grace_window(self):
        violations = self.validator.validate({"start_time": "2026-06-30T17:31:59"})

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].constraint_id, "C030")
        self.assertEqual(violations[0].severity, "soft")
        self.assertIn("2026-06-30 17:31:59", violations[0].message)
        self.assertIn("2026-06-30 17:38:00", violations[0].message)

    def test_allows_start_time_equal_to_current_time(self):
        violations = self.validator.validate({"start_time": "2026-06-30T17:38:00"})

        self.assertEqual(violations, [])

    def test_allows_future_start_time(self):
        violations = self.validator.validate({"start_time": "2026-06-30T17:48:00"})

        self.assertEqual(violations, [])

    def test_incremental_validation_checks_soft_time_constraint_when_start_time_changes(self):
        violations = self.validator.validate_for_fields(
            {"start_time": "2026-06-30T17:31:59"},
            changed_fields={"start_time"},
        )

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].constraint_id, "C030")
        self.assertEqual(violations[0].severity, "soft")


if __name__ == "__main__":
    unittest.main()
