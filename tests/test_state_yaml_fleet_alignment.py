import unittest
from datetime import datetime
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class StateYamlFleetAlignmentTest(unittest.TestCase):
    def test_state_yaml_contains_live_status_for_every_fleet_unit_status_ref(self):
        with (ROOT / "config/robot_fleet.yaml").open(encoding="utf-8") as stream:
            fleet = yaml.safe_load(stream)
        with (ROOT / "config/state.yaml").open(encoding="utf-8") as stream:
            state = yaml.safe_load(stream)

        refs = {
            unit["status_ref"]
            for unit in fleet["fleet_units"]
            if isinstance(unit, dict) and unit.get("status_ref")
        }
        robots = state.get("robots", {})

        self.assertEqual(set(robots), refs)
        self.assertIsInstance(state.get("store_version"), int)

        for ref in sorted(refs):
            robot_state = robots[ref]
            self.assertIsInstance(robot_state.get("version"), int, ref)
            self.assertGreaterEqual(robot_state["version"], 0, ref)
            self.assertIn(robot_state.get("overall_status"), {"available", "idle", "ready"}, ref)
            self.assertFalse(robot_state.get("is_busy"), ref)
            self.assertTrue(robot_state.get("is_online"), ref)
            self.assertIsInstance(robot_state.get("updated_at"), str, ref)
            datetime.fromisoformat(robot_state["updated_at"])
            self.assertEqual(robot_state.get("update_timestamp"), robot_state["updated_at"], ref)


if __name__ == "__main__":
    unittest.main()
