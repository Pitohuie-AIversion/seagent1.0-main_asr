import unittest
import tempfile
from pathlib import Path

import yaml

from tests.run_accumulation_integration_tests import (
    FORBIDDEN_PIPELINE_START,
    LIUHUA_COORDINATES,
    LINGSHUI_COORDINATES,
    SAFE_PIPELINE_END,
    SAFE_PIPELINE_START,
    build_burial_task,
    build_pipeline_task,
    build_robot_state,
    preserve_file_bytes,
    verify_collected_unit,
    build_tree_task,
)


ROOT = Path(__file__).resolve().parents[1]


def parse_point(value):
    latitude, longitude = value.strip("()").split(",")
    return float(latitude), float(longitude)


def contains(area, point):
    latitude, longitude = point
    return (
        area["lat_range"][0] <= latitude <= area["lat_range"][1]
        and area["lon_range"][0] <= longitude <= area["lon_range"][1]
    )


class AccumulationFixtureConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (ROOT / "config/environment.yaml").open(encoding="utf-8") as stream:
            cls.environment = yaml.safe_load(stream)
        with (ROOT / "config/robot_fleet.yaml").open(encoding="utf-8") as stream:
            cls.fleet = yaml.safe_load(stream)

    def test_safe_pipeline_avoids_warning_areas(self):
        warning_areas = (
            self.environment["forbidden_areas"]
            + self.environment["dvl_bottom_lock_failure_areas"]
        )
        for point_text in (SAFE_PIPELINE_START, SAFE_PIPELINE_END):
            point = parse_point(point_text)
            self.assertFalse(
                any(contains(area, point) for area in warning_areas),
                f"safe pipeline point {point_text} overlaps a warning area",
            )

    def test_forbidden_pipeline_really_hits_forbidden_area(self):
        point = parse_point(FORBIDDEN_PIPELINE_START)
        self.assertTrue(
            any(contains(area, point) for area in self.environment["forbidden_areas"])
        )

    def test_oilfield_coordinates_match_seabed_baseline(self):
        oilfields = {item["id"]: item for item in self.environment["oil_fields"]}
        for field_id, point_text, seabed in (
            ("lingshui_17_2", LINGSHUI_COORDINATES, "soft"),
            ("liuhua_11_1", LIUHUA_COORDINATES, "hard"),
        ):
            field = oilfields[field_id]
            self.assertTrue(contains(field, parse_point(point_text)))
            self.assertEqual(field["seabed_type"], seabed)

    def test_factories_use_current_fleet_units(self):
        unit_ids = {item["unit_id"] for item in self.fleet["fleet_units"]}
        status_refs = {item["status_ref"] for item in self.fleet["fleet_units"]}
        self.assertIn("OBSROV--001", unit_ids)
        self.assertIn("WROV-250-001", unit_ids)
        self.assertIn("OBSROV-001", status_refs)
        self.assertIn("WROV-250-001", status_refs)
        self.assertIn("AUV-324cc-001", status_refs)
        self.assertIn("TOWED-1500-001", status_refs)
        self.assertIn("SPECIAL-600-001", status_refs)
        self.assertIn("LROV--001", status_refs)
        self.assertIn("OBSROV--001", build_pipeline_task())
        self.assertIn("管缆类型为海底油气管道", build_pipeline_task())
        self.assertIn("WROV-250-001", build_tree_task())
        self.assertIn(
            "TOWED-1500-001",
            build_burial_task(
                equipment_model="拖曳式海底重载作业机器人 1500HP",
                unit_id="TOWED-1500-001",
            ),
        )
        self.assertEqual(build_robot_state("now", turbidity=9)["turbidity"], 9)

    def test_unit_verifier_can_separate_resolution_from_completion(self):
        incomplete = {
            "collected": {"equipment_unit_id": "LROV--001"},
            "missing": ["cable_type"],
        }
        resolved_only = verify_collected_unit("LROV--001", require_complete=False)
        completed = verify_collected_unit("LROV--001")
        self.assertTrue(resolved_only(0, incomplete)[0])
        self.assertFalse(completed(0, incomplete)[0])
        self.assertTrue(
            completed(1, {**incomplete, "missing": []})[0]
        )

    def test_state_file_guard_restores_existing_file_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.yaml"
            original = b"robots:\n  OBSROV-001:\n    current_velocity: 0.3\n"
            state_file.write_bytes(original)
            with preserve_file_bytes(state_file):
                state_file.write_text("robots: {}\n", encoding="utf-8")
            self.assertEqual(original, state_file.read_bytes())

    def test_state_file_guard_removes_file_created_inside_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.yaml"
            with preserve_file_bytes(state_file):
                state_file.write_text("robots: {}\n", encoding="utf-8")
            self.assertFalse(state_file.exists())

    def test_manual_docs_do_not_restore_stale_baselines(self):
        stale_tokens = (
            "localhost:8888",
            "localhost:6006",
            "2026-06-",
            "(19.8,113.5)",
            "(20.5,114.2)",
            "设备名称sealien_",
            "C011",
            '"robot_name":"sealien_',
        )
        for name in ("测试集05.md", "测试机.md"):
            text = (ROOT / "tests/test_accumulation" / name).read_text(encoding="utf-8")
            for token in stale_tokens:
                self.assertNotIn(token, text, f"{name} still contains {token}")


if __name__ == "__main__":
    unittest.main()
