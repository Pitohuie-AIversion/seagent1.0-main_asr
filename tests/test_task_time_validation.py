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
            },
            {
                "id": "C031",
                "name": "任务结束时间必须晚于任务开始时间",
                "applies_to": ["all"],
                "check_type": "end_time_after_start_time",
                "violation_message": (
                    "任务结束时间 {end_time} 不得早于或等于任务开始时间 {start_time}，"
                    "请修改任务时间窗口后再发布任务。"
                ),
                "severity": "hard",
            },
            {
                "id": "C032",
                "name": "未来任务环境与遥测延后校验提示",
                "applies_to": ["all"],
                "check_type": "future_task_runtime_notice",
                "violation_message": (
                    "任务计划开始时间为 {start_time}，已识别为未来排期任务。"
                    "当前海流、浑浊度及机器人实时遥测已跳过即时强校验，"
                    "系统将在任务执行窗口期前（运行时）再行自动核验与设备状态绑定。"
                ),
                "severity": "soft",
            },
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

    def test_immediate_future_start_time_within_window_does_not_trigger_c032(self):
        violations = self.validator.validate({"start_time": "2026-06-30T17:48:00"})

        self.assertEqual(violations, [])

    def test_future_start_time_triggers_c032_soft_notice(self):
        violations = self.validator.validate({"start_time": "2026-06-30T18:38:00"})

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].constraint_id, "C032")
        self.assertEqual(violations[0].severity, "soft")
        self.assertIn("2026-06-30 18:38:00", violations[0].message)
        self.assertIn("未来排期任务", violations[0].message)

    def test_incremental_validation_checks_soft_time_constraint_when_start_time_changes(self):
        violations = self.validator.validate_for_fields(
            {"start_time": "2026-06-30T17:31:59"},
            changed_fields={"start_time"},
        )

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].constraint_id, "C030")
        self.assertEqual(violations[0].severity, "soft")

    def test_incremental_validation_triggers_c032_on_future_start_time_change(self):
        violations = self.validator.validate_for_fields(
            {"start_time": "2026-06-30T19:00:00"},
            changed_fields={"start_time"},
        )

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].constraint_id, "C032")
        self.assertEqual(violations[0].severity, "soft")

    def test_rejects_end_time_not_after_start_time(self):
        for end_time in ("2026-06-30T17:38:00", "2026-06-30T17:37:59"):
            with self.subTest(end_time=end_time):
                violations = self.validator.validate({
                    "start_time": "2026-06-30T17:38:00",
                    "end_time": end_time,
                })

                self.assertEqual(len(violations), 1)
                self.assertEqual(violations[0].constraint_id, "C031")
                self.assertEqual(violations[0].severity, "hard")
                self.assertEqual(
                    violations[0].related_fields,
                    ["start_time", "end_time"],
                )

    def test_incremental_validation_rechecks_time_order_from_either_field(self):
        task = {
            "start_time": "2026-06-30T17:38:00",
            "end_time": "2026-06-30T17:00:00",
        }
        for changed_field in ("start_time", "end_time"):
            with self.subTest(changed_field=changed_field):
                violations = self.validator.validate_for_fields(
                    task,
                    changed_fields={changed_field},
                )
                self.assertEqual(
                    [violation.constraint_id for violation in violations],
                    ["C031"],
                )

    def test_future_start_time_with_invalid_end_time_returns_both_c031_and_c032(self):
        violations = self.validator.validate({
            "start_time": "2026-06-30T19:00:00",
            "end_time": "2026-06-30T18:00:00",
        })
        self.assertEqual(
            {violation.constraint_id for violation in violations},
            {"C031", "C032"},
        )


if __name__ == "__main__":
    unittest.main()
