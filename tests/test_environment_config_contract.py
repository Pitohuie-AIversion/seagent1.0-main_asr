import copy
from pathlib import Path
import unittest

import yaml

from src.knowledge_retriever import KnowledgeBase
from src.oilfield_linker import OilfieldEntityLinker


ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_PATH = ROOT / "config" / "oilfield.yaml"


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(
                f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class EnvironmentConfigContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = yaml.load(
            ENVIRONMENT_PATH.read_text(encoding="utf-8"),
            Loader=_UniqueKeyLoader,
        )
        cls.constraints = KnowledgeBase().constraints
        cls.linker = OilfieldEntityLinker(cls.environment, cls.constraints)

    def test_metadata_is_machine_readable_and_current(self):
        metadata = self.environment["metadata"]
        self.assertEqual(2, metadata["schema_version"])
        self.assertEqual("2026-08-13", metadata["last_updated"])
        self.assertEqual("internal_simulation", metadata["authority"])

    def test_oilfield_depth_defaults_and_hard_limits_are_distinct(self):
        for field in self.environment["oil_fields"]:
            with self.subTest(field=field["id"]):
                default_depth = field["water_depth"]
                maximum_depth = field["maximum_reference_water_depth"]
                self.assertIsInstance(default_depth, (int, float))
                self.assertIsInstance(maximum_depth, (int, float))
                self.assertGreater(default_depth, 0)
                self.assertGreaterEqual(maximum_depth, default_depth)
                provenance = field["provenance"]
                self.assertTrue(provenance["water_depth_source_url"].startswith("https://"))
                self.assertIn(
                    provenance["coordinates_status"],
                    {"verified", "internal_simulation"},
                )
                self.assertIn(
                    provenance["seabed_status"],
                    {"verified", "internal_simulation"},
                )

    def test_ids_names_aliases_and_coordinate_ranges_are_valid(self):
        for section in (
            "oil_fields",
            "forbidden_areas",
            "dvl_bottom_lock_failure_areas",
        ):
            rows = self.environment[section]
            self.assertEqual(len(rows), len({row["id"] for row in rows}))
            self.assertEqual(len(rows), len({row["name"] for row in rows}))
            aliases = [alias for row in rows for alias in row["aliases"]]
            self.assertEqual(len(aliases), len(set(aliases)))
            for row in rows:
                with self.subTest(section=section, row=row["id"]):
                    for key, lower, upper in (
                        ("lat_range", -90, 90),
                        ("lon_range", -180, 180),
                    ):
                        values = row[key]
                        self.assertEqual(2, len(values))
                        self.assertTrue(
                            all(
                                isinstance(value, (int, float))
                                and not isinstance(value, bool)
                                for value in values
                            )
                        )
                        self.assertLessEqual(lower, values[0])
                        self.assertLessEqual(values[0], values[1])
                        self.assertLessEqual(values[1], upper)

    def test_lufeng_record_contains_only_lufeng_provenance(self):
        lufeng = next(
            field
            for field in self.environment["oil_fields"]
            if field["id"] == "lufeng_14_8"
        )
        serialized = yaml.safe_dump(lufeng, allow_unicode=True)
        self.assertIn("陆丰", serialized)
        self.assertNotIn("惠州32-5", serialized)
        self.assertNotIn("113m", serialized)

    def test_lingshui_known_field_depth_does_not_false_block(self):
        within = self.linker.evaluate_context(
            entity_id="lingshui_17_2",
            water_depth=1587,
        )
        exceeded = self.linker.evaluate_context(
            entity_id="lingshui_17_2",
            water_depth=1588,
        )

        self.assertEqual("within_reference", within.depth_status)
        self.assertEqual(1587, within.maximum_reference_water_depth)
        self.assertFalse(within.issues)
        self.assertEqual("exceeded_reference", exceeded.depth_status)
        self.assertEqual(["C029"], [issue.constraint_id for issue in exceeded.issues])
        self.assertIn("1587", exceeded.issues[0].message)
        self.assertNotIn("{", exceeded.issues[0].message)

    def test_missing_or_inverted_hard_depth_limit_fails_closed(self):
        missing = copy.deepcopy(self.environment)
        del missing["oil_fields"][0]["maximum_reference_water_depth"]
        with self.assertRaisesRegex(ValueError, "缺少最大参考水深"):
            OilfieldEntityLinker(missing, self.constraints).evaluate_context(
                entity_id="liuhua_11_1",
                water_depth=305,
            )

        inverted = copy.deepcopy(self.environment)
        inverted["oil_fields"][0]["maximum_reference_water_depth"] = 304
        with self.assertRaisesRegex(ValueError, "不能小于默认参考水深"):
            OilfieldEntityLinker(inverted, self.constraints).evaluate_context(
                entity_id="liuhua_11_1",
                water_depth=305,
            )

    def test_map_areas_declare_non_authoritative_boundary_basis(self):
        for section in ("forbidden_areas", "dvl_bottom_lock_failure_areas"):
            for area in self.environment[section]:
                with self.subTest(section=section, area=area["id"]):
                    self.assertEqual(
                        "internal_simulation",
                        area["provenance"]["authority"],
                    )
                    self.assertTrue(area["provenance"]["boundary_basis"])

    def test_every_configured_area_center_is_queryable(self):
        env_info = KnowledgeBase().env_info
        for field in self.environment["oil_fields"]:
            lat = sum(field["lat_range"]) / 2
            lon = sum(field["lon_range"]) / 2
            with self.subTest(section="oil_fields", area=field["id"]):
                self.assertEqual(
                    field["seabed_type"],
                    env_info.get_physical_seabed_type(lat, lon),
                )
        for area in self.environment["forbidden_areas"]:
            lat = sum(area["lat_range"]) / 2
            lon = sum(area["lon_range"]) / 2
            with self.subTest(section="forbidden_areas", area=area["id"]):
                self.assertTrue(env_info.get_geometry_forbidden(lat, lon))
        for area in self.environment["dvl_bottom_lock_failure_areas"]:
            lat = sum(area["lat_range"]) / 2
            lon = sum(area["lon_range"]) / 2
            with self.subTest(section="dvl_areas", area=area["id"]):
                self.assertTrue(env_info.get_semantic_dvl_risk(lat, lon))

    def test_dvl_warning_notes_do_not_claim_certain_failure(self):
        pearl_river = next(
            area
            for area in self.environment["dvl_bottom_lock_failure_areas"]
            if area["id"] == "dvl_failure_pearl_river_mouth_basin"
        )
        self.assertIn("不作为DVL失效确定区域", pearl_river["notes"])


if __name__ == "__main__":
    unittest.main()
