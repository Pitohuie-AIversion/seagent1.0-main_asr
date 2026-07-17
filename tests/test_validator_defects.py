import sys
from pathlib import Path
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.validator import TaskValidator, Violation
from src.simulated_time import get_simulated_time


class FakeKnowledgeBaseForDefects:
    def __init__(self):
        self.constraints = [
            {
                "id": "C008",
                "name": "禁入区约束",
                "applies_to": ["all"],
                "check_type": "forbidden_area",
                "violation_message": "禁入区内无法作业。",
                "severity": "hard"
            },
            {
                "id": "C010",
                "name": "DVL底锁失效高风险",
                "applies_to": ["all"],
                "check_type": "dvl_high_risk",
                "violation_message": "DVL底锁失效风险高。",
                "severity": "soft"
            },
            {
                "id": "C009",
                "name": "海床质地约束",
                "applies_to": ["all"],
                "check_type": "seabed_compatibility",
                "violation_message": "海床质地不匹配。",
                "severity": "hard"
            },
            {
                "id": "C015",
                "name": "流速状态监测-中",
                "applies_to": ["all"],
                "check_type": "current_velocity",
                "violation_message": "流速中等 {current_velocity}",
                "severity": "soft"
            },
            {
                "id": "C016",
                "name": "流速状态监测-高",
                "applies_to": ["all"],
                "check_type": "current_velocity",
                "violation_message": "流速较高 {current_velocity}",
                "severity": "soft"
            },
            {
                "id": "C017",
                "name": "流速状态监测-禁止",
                "applies_to": ["all"],
                "check_type": "current_velocity",
                "violation_message": "流速超限 {current_velocity}",
                "severity": "hard"
            },
            {
                "id": "C019",
                "name": "环境信息已过期",
                "applies_to": ["all"],
                "check_type": "state_timestamp",
                "violation_message": "环境信息已过期 {update_timestamp}",
                "severity": "soft"
            }
        ]
        self.rov = {
            "full_name": "sealien_inspection",
            "category": "observation",
            "max_depth_m": 600,
            "forbidden_seabed": ["soft"]
        }
        self.robot_state = {
            "current_velocity": 0.3,
            "update_timestamp": "2026-06-30T17:30:00+08:00",
            "overall_status": "available",
        }
        # A map of coords string to environment info dict
        self.env_map = {}

    def get_constraints(self):
        return self.constraints

    def get_rov(self, name):
        return self.rov

    def get_vessel(self, vessel_id):
        return None

    def get_environment_info_dict(self, coords):
        key = (coords.get("lat"), coords.get("lon"))
        return self.env_map.get(key, {"forbidden": False, "dvl_risk": False, "seabed_type": "hard"})

    def get_robot_state_dict(self, name):
        return self.robot_state


class TaskValidatorDefectsTest(unittest.TestCase):
    def setUp(self):
        self.kb = FakeKnowledgeBaseForDefects()
        self.validator = TaskValidator(self.kb)
        get_simulated_time().set_current_time(
            datetime(2026, 6, 30, 17, 38, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )

    def test_past_start_time_triggers_dynamic_checks(self):
        # Setting task start time 2 minutes in the past (within grace)
        # We also trigger a dynamic warning (e.g. flow velocity 0.6)
        self.kb.robot_state["current_velocity"] = 0.6
        task_state = {
            "start_time": "2026-06-30T17:36:00",
            "equipment_name": "sealien_inspection"
        }
        violations = self.validator.validate(task_state)
        # Should NOT bypass dynamic checks, so we should receive C015 warning
        v_ids = [v.constraint_id for v in violations]
        self.assertIn("C015", v_ids, "Dynamic check C015 was bypassed for a past start time!")

    def test_multiple_coordinates_do_not_short_circuit(self):
        # Set up coordinates: start_point is safe, end_point is forbidden
        safe_coord = {"lat": 19.8, "lon": 113.5}
        forbidden_coord = {"lat": 20.5, "lon": 114.5}
        
        self.kb.env_map[(19.8, 113.5)] = {"forbidden": False, "dvl_risk": False, "seabed_type": "hard"}
        self.kb.env_map[(20.5, 114.5)] = {"forbidden": True, "dvl_risk": False, "seabed_type": "hard"}

        task_state = {
            "start_point": safe_coord,
            "end_point": forbidden_coord,
            "equipment_name": "sealien_inspection"
        }
        violations = self.validator.validate(task_state)
        v_ids = [v.constraint_id for v in violations]
        self.assertIn("C008", v_ids, "Forbidden end_point was ignored due to short-circuiting!")

    def test_timezone_aware_timestamp_parsed_correctly(self):
        # update_timestamp is "2026-06-30T17:30:00+08:00"
        # Current simulated time is "2026-06-30T17:38:00+08:00" (diff is 8 mins)
        # This is within 1 hour, so no C019 violation should occur
        self.kb.robot_state["update_timestamp"] = "2026-06-30T17:30:00+08:00"
        task_state = {
            "start_time": "2026-06-30T17:38:00",
            "equipment_name": "sealien_inspection"
        }
        violations = self.validator.validate(task_state)
        v_ids = [v.constraint_id for v in violations]
        self.assertNotIn("C019", v_ids, "Timezone aware timestamp caused incorrect C019 violation!")

    def test_incremental_validation_triggers_on_start_time_change(self):
        # We start with a future task (no dynamic checks)
        # Then, we simulate a start_time update to "now", which should trigger C015 since current_velocity = 0.6
        self.kb.robot_state["current_velocity"] = 0.6
        task_state = {
            "start_time": "2026-06-30T17:38:00",
            "equipment_name": "sealien_inspection"
        }
        violations = self.validator.validate_for_fields(task_state, changed_fields={"start_time"})
        v_ids = [v.constraint_id for v in violations]
        self.assertIn("C015", v_ids, "Changing start_time did not trigger dynamic checks during incremental validation!")

    def test_mutually_exclusive_velocity_ranges(self):
        task_state = {
            "start_time": "2026-06-30T17:38:00",
            "equipment_name": "sealien_inspection"
        }
        
        # Velocity = 0.6 -> C015 only
        self.kb.robot_state["current_velocity"] = 0.6
        v_ids = [v.constraint_id for v in self.validator.validate(task_state)]
        self.assertIn("C015", v_ids)
        self.assertNotIn("C016", v_ids)
        self.assertNotIn("C017", v_ids)

        # Velocity = 1.0 -> C016 only
        self.kb.robot_state["current_velocity"] = 1.0
        v_ids = [v.constraint_id for v in self.validator.validate(task_state)]
        self.assertNotIn("C015", v_ids)
        self.assertIn("C016", v_ids)
        self.assertNotIn("C017", v_ids)

        # Velocity = 1.5 -> C017 only
        self.kb.robot_state["current_velocity"] = 1.5
        v_ids = [v.constraint_id for v in self.validator.validate(task_state)]
        self.assertNotIn("C015", v_ids)
        self.assertNotIn("C016", v_ids)
        self.assertIn("C017", v_ids)


if __name__ == "__main__":
    unittest.main()
