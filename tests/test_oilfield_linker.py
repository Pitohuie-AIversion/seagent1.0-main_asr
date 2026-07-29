from pathlib import Path
import importlib.util
import sys
import unittest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "src" / "oilfield_linker.py"
spec = importlib.util.spec_from_file_location("oilfield_linker", MODULE_PATH)
oilfield_linker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = oilfield_linker
spec.loader.exec_module(oilfield_linker)
OilfieldEntityLinker = oilfield_linker.OilfieldEntityLinker


class OilfieldEntityLinkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = yaml.safe_load((PROJECT_ROOT / "config" / "environment.yaml").read_text(encoding="utf-8"))
        constraints = yaml.safe_load(
            (PROJECT_ROOT / "config" / "constraints.yaml").read_text(encoding="utf-8")
        )["constraints"]
        cls.linker = OilfieldEntityLinker(env, constraints)

    def test_link_asr_error_liuhua_with_digits(self):
        match = self.linker.link("硫化11-1油田")
        self.assertEqual(match.status, "accepted")
        self.assertEqual(match.standard_name, "流花11-1油田")

    def test_link_asr_error_liuhua_with_chinese_digits(self):
        match = self.linker.link("硫化十一杠一油田")
        self.assertEqual(match.status, "accepted")
        self.assertEqual(match.standard_name, "流花11-1油田")

    def test_link_standard_liuhua_with_chinese_digits(self):
        match = self.linker.link("流花十一杠一油田")
        self.assertEqual(match.status, "accepted")
        self.assertEqual(match.standard_name, "流花11-1油田")

    def test_link_asr_error_lingshui_with_digits(self):
        match = self.linker.link("临水17-2")
        self.assertEqual(match.status, "accepted")
        self.assertEqual(match.standard_name, "陵水17-2油田")

    def test_link_asr_error_lingshui_with_chinese_digits(self):
        match = self.linker.link("临水十七杠二油田")
        self.assertEqual(match.status, "accepted")
        self.assertEqual(match.standard_name, "陵水17-2油田")

    def test_coords_can_disambiguate_name_without_digits(self):
        match = self.linker.link("硫化油田", {"lat": 20.815, "lon": 115.735})
        self.assertEqual(match.status, "accepted")
        self.assertEqual(match.standard_name, "流花11-1油田")

    def test_unmatched_name_is_not_accepted(self):
        match = self.linker.link("乱说油田")
        self.assertEqual(match.status, "unmatched")
        self.assertIsNone(match.standard_name)

    def test_unknown_name_with_chinese_digits_is_not_accepted(self):
        match = self.linker.link("乱说十一杠一油田")
        self.assertNotEqual(match.status, "accepted")
        self.assertIsNone(match.standard_name)

    def test_context_defaults_use_oilfield_reference(self):
        result = self.linker.evaluate_context(entity_id="liuhua_11_1")

        self.assertEqual(result.oilfield_name, "流花11-1油田")
        self.assertEqual(result.defaults["oilfield_coordinates"], {"lat": 20.815, "lon": 115.735})
        self.assertEqual(result.defaults["water_depth"], 305)
        self.assertEqual(result.violations, [])
        self.assertEqual(result.coordinate_status, "not_provided")
        self.assertEqual(result.depth_status, "not_provided")

    def test_context_flags_coordinate_outside_reference_range_as_soft(self):
        result = self.linker.evaluate_context(
            entity_id="liuhua_11_1",
            coordinates={"lat": 21.5, "lon": 116.0},
        )

        self.assertEqual(len(result.violations), 1)
        self.assertEqual(result.violations[0]["constraint_id"], "C028")
        self.assertEqual(result.violations[0]["severity"], "soft")
        self.assertEqual(result.coordinate_status, "mismatched")

    def test_context_flags_depth_above_reference_as_hard(self):
        result = self.linker.evaluate_context(
            entity_id="liuhua_11_1",
            water_depth=350,
        )

        self.assertEqual(len(result.violations), 1)
        self.assertEqual(result.violations[0]["constraint_id"], "C029")
        self.assertEqual(result.violations[0]["severity"], "hard")
        self.assertEqual(result.depth_status, "exceeded_reference")


if __name__ == "__main__":
    unittest.main()
