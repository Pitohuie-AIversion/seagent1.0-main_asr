import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.knowledge_retriever import KnowledgeBase
from src.validator import TaskValidator


NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
SAFE_POINT = {"lat": 17.60, "lon": 111.00}
FORBIDDEN_POINT = {"lat": 20.40, "lon": 109.85}
DVL_RISK_POINT = {"lat": 20.50, "lon": 113.00}
SOFT_SEABED_POINT = {"lat": 17.52, "lon": 110.15}


def base_robot_state(**overrides):
    state = {
        "current_velocity": 0.3,
        "turbidity": 3,
        "obstacle_density": "low",
        "mothership_support": "strong",
        "update_timestamp": NOW.isoformat(),
        "confidence": 0.95,
        "overall_status": "available",
        "survival_status": "normal",
        "thruster_status": "normal",
        "depth_keeping_status": "normal",
        "sonar_status": "normal",
        "vision_status": "normal",
        "arm_status": "normal",
        "end_effector_status": "normal",
        "acoustic_comms_status": "normal",
        "tether_connection_status": "normal",
    }
    state.update(overrides)
    return state


def base_task(**overrides):
    task = {
        "task_type_key": "pipeline_inspection",
        "equipment_unit_id": "OBSROV-75-001",
        "water_depth": 300,
        "support_vessel": "海洋石油681",
        "start_point": copy.deepcopy(SAFE_POINT),
        "end_point": {"lat": 17.70, "lon": 111.10},
        "start_time": NOW.isoformat(),
    }
    task.update(overrides)
    return task


# Every active constraint must have a direct trigger. The key set is checked
# against config/constraints.yaml so a newly enabled rule cannot silently land
# without a deterministic test scenario.
CONSTRAINT_TRIGGER_MATRIX = {
    "C001": {
        "task": {
            "task_type_key": "pipeline_inspection",
            "equipment_unit_id": None,
            "equipment_type": "通用工作级深海机器人 250HP",
        },
    },
    "C002": {
        "task": {
            "task_type_key": "tree_valve_operation",
            "equipment_unit_id": None,
            "equipment_type": "观察级深海机器人 75HP",
        },
    },
    "C004": {"task": {"water_depth": 601}},
    "C030": {"task": {"start_time": (NOW - timedelta(minutes=6)).isoformat()}},
    "C031": {"task": {"end_time": (NOW - timedelta(seconds=1)).isoformat()}},
    "C032": {"task": {"start_time": (NOW + timedelta(hours=2)).isoformat()}},
    "C007": {"task": {"support_vessel": "海洋石油708"}},
    "C008": {"task": {"start_point": FORBIDDEN_POINT}},
    "C009": {
        "task": {
            "task_type_key": "pipeline_burial",
            "equipment_unit_id": "CRAWLER-1600-001",
            "equipment_type": "履带式海底重载作业机器人 1600HP",
            "start_point": SOFT_SEABED_POINT,
        },
    },
    "C010": {"task": {"start_point": DVL_RISK_POINT}},
    "C011": {"state": {"obstacle_density": "high"}},
    "C012": {"state": {"mothership_support": "weak"}},
    "C013": {"state": {"turbidity": 6}},
    "C014": {"state": {"turbidity": 11}},
    "C015": {"state": {"current_velocity": 0.500001}},
    "C016": {"state": {"current_velocity": 0.800001}},
    "C017": {"state": {"current_velocity": 1.200001}},
    "C018": {"state": {"confidence": 0.49}},
    "C019": {"state": {"update_timestamp": (NOW - timedelta(seconds=601)).isoformat()}},
    "C020": {"state": {"overall_status": "unavailable"}},
    "C021": {"state": {"survival_status": "abnormal"}},
    "C022": {"state": {"thruster_status": "abnormal"}},
    "C023": {"state": {"depth_keeping_status": "abnormal"}},
    "C024": {"state": {"sonar_status": "abnormal"}},
    "C025": {"state": {"vision_status": "abnormal"}},
    "C026": {"state": {"arm_status": "abnormal"}},
    "C027": {"state": {"tether_connection_status": "abnormal"}},
    "C028": {
        "task": {
            "task_type_key": "tree_valve_operation",
            "oilfield_name": "流花11-1油田",
            "coordinates": {"lat": 10.0, "lon": 10.0},
        }
    },
    "C029": {
        "task": {
            "task_type_key": "tree_valve_operation",
            "oilfield_name": "陵水17-2气田",
            "water_depth": 3000,
        }
    },
}


class ConstraintCoverageMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()
        cls.validator = TaskValidator(cls.kb)

    def validate(self, *, task=None, state=None):
        candidate = base_task(**(task or {}))
        robot_state = base_robot_state(**(state or {}))
        unit_id = candidate.get("equipment_unit_id")
        if not unit_id and candidate.get("equipment_type"):
            res_unit = self.kb.resolve_robot_unit(candidate.get("equipment_type"), candidate.get("task_type_key"))
            if res_unit:
                unit_id = res_unit.get("unit_id") or (res_unit.get("unit", {}).get("unit_id") if isinstance(res_unit.get("unit"), dict) else None)
        if not unit_id:
            unit_id = "OBSROV-75-001"

        matched = None
        for u in self.kb.robot_fleet.get("fleet_units", []):
            if u.get("unit_id") == unit_id or u.get("status_ref") == unit_id:
                matched = u
                break
        status_ref = (matched.get("status_ref") if matched else None) or unit_id

        fake_snapshot = {
            "unit_id": unit_id,
            "status_ref": status_ref,
            "state_version": 1,
            "store_version": 1,
            "updated_at": NOW.isoformat(),
            "state": robot_state,
        }
        with (
            patch.object(self.kb, "get_robot_state_dict", return_value=robot_state),
            patch.object(self.kb, "get_unit_state_snapshot", return_value=fake_snapshot),
            patch("src.validator.get_current_datetime", return_value=NOW),
        ):
            res = self.validator.validate(candidate)

            if candidate.get("oilfield_name"):
                from src.oilfield_linker import OilfieldEntityLinker
                linker = OilfieldEntityLinker(self.kb.environment, self.kb.constraints)
                match = linker.link(candidate.get("oilfield_name"))
                if match and match.entity_id:
                    context_res = linker.evaluate_context(
                        entity_id=match.entity_id,
                        coordinates=candidate.get("coordinates"),
                        water_depth=candidate.get("water_depth"),
                    )
                    if context_res and context_res.issues:
                        from src.validator import Violation
                        for issue in context_res.issues:
                            res.append(
                                Violation(
                                    constraint_id=issue.constraint_id,
                                    constraint_name=issue.constraint_name,
                                    check_type=issue.check_type,
                                    severity=issue.severity,
                                    message=issue.message,
                                    related_fields=list(issue.related_fields),
                                )
                            )
            return res

    def assert_constraint_ids(self, expected, *, task=None, state=None):
        actual = {violation.constraint_id for violation in self.validate(task=task, state=state)}
        self.assertEqual(set(expected), actual)

    def test_matrix_covers_every_active_constraint(self):
        active_ids = {constraint["id"] for constraint in self.kb.get_constraints()}
        self.assertEqual(
            active_ids,
            set(CONSTRAINT_TRIGGER_MATRIX),
            "Every active constraint needs a direct trigger scenario",
        )

    def test_current_constraint_id_and_hard_block_copy(self):
        constraints = {item["id"]: item for item in self.kb.get_constraints()}
        self.assertIn("C011", constraints)
        self.assertNotIn(
            "确认船只调度后再发布",
            constraints["C007"]["violation_message"],
        )
        self.assertIn("更新为可用", constraints["C007"]["violation_message"])

    def test_each_matrix_scenario_triggers_its_constraint_and_severity(self):
        constraints = {item["id"]: item for item in self.kb.get_constraints()}
        for constraint_id, scenario in CONSTRAINT_TRIGGER_MATRIX.items():
            with self.subTest(constraint_id=constraint_id):
                violations = self.validate(**scenario)
                matching = [item for item in violations if item.constraint_id == constraint_id]
                self.assertEqual(1, len(matching), f"{constraint_id} did not trigger")
                self.assertEqual(constraints[constraint_id]["severity"], matching[0].severity)

    def test_current_velocity_exact_boundaries(self):
        cases = (
            (0.5, set()),
            (0.500001, {"C015"}),
            (0.8, {"C015"}),
            (0.800001, {"C016"}),
            (1.2, {"C016"}),
            (1.200001, {"C017"}),
        )
        for velocity, expected in cases:
            with self.subTest(velocity=velocity):
                actual = {
                    item.constraint_id
                    for item in self.validate(state={"current_velocity": velocity})
                    if item.constraint_id in {"C015", "C016", "C017"}
                }
                self.assertEqual(expected, actual)

    def test_turbidity_exact_boundaries(self):
        cases = (
            (5, set()),
            (5.000001, {"C013"}),
            (10, {"C013"}),
            (10.000001, {"C014"}),
        )
        for turbidity, expected in cases:
            with self.subTest(turbidity=turbidity):
                actual = {
                    item.constraint_id
                    for item in self.validate(state={"turbidity": turbidity})
                    if item.constraint_id in {"C013", "C014"}
                }
                self.assertEqual(expected, actual)

    def test_state_timestamp_exact_boundary(self):
        for age_seconds, expected in ((600, set()), (601, {"C019"})):
            with self.subTest(age_seconds=age_seconds):
                actual = {
                    item.constraint_id
                    for item in self.validate(
                        state={
                            "update_timestamp": (
                                NOW - timedelta(seconds=age_seconds)
                            ).isoformat()
                        }
                    )
                    if item.constraint_id == "C019"
                }
                self.assertEqual(expected, actual)

    def test_invalid_state_timestamp_fails_closed(self):
        violations = self.validate(state={"update_timestamp": "not-a-timestamp"})
        self.assertEqual(["VAL_ERR"], [item.constraint_id for item in violations])
        self.assertEqual("hard", violations[0].severity)

    def test_current_velocity_thresholds_are_configuration_driven(self):
        constraint = copy.deepcopy(
            next(item for item in self.kb.get_constraints() if item["id"] == "C015")
        )
        constraint["id"] = "CUSTOM_CURRENT_RANGE"
        constraint["thresholds"] = {
            "min_exclusive": 0.6,
            "max_inclusive": 0.7,
        }
        snapshot = {"state": base_robot_state(current_velocity=0.65)}

        with patch.object(
            self.validator,
            "_is_task_start_now",
            return_value=True,
        ):
            violation = self.validator._check_one(
                constraint,
                "current_velocity",
                base_task(),
                None,
                None,
                None,
                None,
                snapshot,
            )

        self.assertIsNotNone(violation)
        self.assertEqual("CUSTOM_CURRENT_RANGE", violation.constraint_id)
        self.assertEqual(0.7, violation.threshold)

    def test_every_fleet_variant_resolves_for_its_supported_task(self):
        expected_tasks = {
            "CRAWLER-1600-001": "pipeline_burial",
            "TOWED-1500-001": "pipeline_burial",
            "SPECIAL-600-001": "pipeline_burial",
            "WROV-250-001": "tree_valve_operation",
            "LROV-150-001": "pipeline_inspection",
            "LROV-150-002": "pipeline_inspection",
            "OBSROV-75-001": "pipeline_inspection",
            "AUV-324cc-001": "pipeline_inspection",
        }
        fleet_ids = {unit["unit_id"] for unit in self.kb.robot_fleet["fleet_units"]}
        self.assertEqual(fleet_ids, set(expected_tasks))
        for unit_id, task_type in expected_tasks.items():
            with self.subTest(unit_id=unit_id):
                resolved = self.kb.resolve_robot_unit(unit_id, task_type)
                self.assertIsNotNone(resolved)
                self.assertTrue(self.kb.robot_matches_task(resolved["robot"], task_type))


if __name__ == "__main__":
    unittest.main()
