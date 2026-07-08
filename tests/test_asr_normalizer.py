from pathlib import Path
import importlib.util
import sys
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "asr_normalizer.py"
spec = importlib.util.spec_from_file_location("asr_normalizer", MODULE_PATH)
asr_normalizer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = asr_normalizer
spec.loader.exec_module(asr_normalizer)
normalize_terminology = asr_normalizer.normalize_terminology


class AsrNormalizerTest(unittest.TestCase):
    def test_normalize_tree_term(self):
        result = normalize_terminology("我现在采油数控制面板插入")
        self.assertEqual(result["corrected_text"], "我现在采油树控制面板插入")
        self.assertTrue(result["normalization_changed"])

    def test_no_rewrite_correct_tree_term(self):
        result = normalize_terminology("我现在采油树控制面板插入")
        self.assertEqual(result["corrected_text"], "我现在采油树控制面板插入")
        self.assertFalse(result["normalization_changed"])

    def test_no_action_rewrite(self):
        result = normalize_terminology("我现在采油数控制面板插入")
        self.assertEqual(result["corrected_text"], "我现在采油树控制面板插入")
        self.assertIn("插入", result["corrected_text"])
        self.assertEqual(result["replacements"][0]["source"], "采油数")
        self.assertEqual(result["replacements"][0]["target"], "采油树")

    def test_valve_context(self):
        result = normalize_terminology("目标阀门主阀、逆阀")
        self.assertEqual(result["corrected_text"], "目标阀门主阀、翼阀")

    def test_no_valve_context(self):
        result = normalize_terminology("这个方向逆风比较严重")
        self.assertEqual(result["corrected_text"], "这个方向逆风比较严重")
        self.assertFalse(result["normalization_changed"])

    def test_oilfield_context(self):
        result = normalize_terminology("临水17-2油田")
        self.assertEqual(result["corrected_text"], "陵水17-2油田")

    def test_pipeline_terms(self):
        result = normalize_terminology("管蓝巡检，携带多波束声纳")
        self.assertEqual(result["corrected_text"], "管缆巡检，携带多波束声呐")

    def test_tree_type_asr_history_no_longer_rewrites_to_vertical(self):
        result = normalize_terminology("历史采油树控制面板插入")
        self.assertEqual(result["corrected_text"], "历史采油树控制面板插入")
        self.assertFalse(result["normalization_changed"])

    def test_tree_type_asr_lishi_no_longer_rewrites_to_vertical(self):
        result = normalize_terminology("力士采油树控制面板插入")
        self.assertEqual(result["corrected_text"], "力士采油树控制面板插入")
        self.assertFalse(result["normalization_changed"])

    def test_no_rewrite_history_record(self):
        result = normalize_terminology("查看历史记录")
        self.assertEqual(result["corrected_text"], "查看历史记录")
        self.assertFalse(result["normalization_changed"])

    def test_do_not_replace_standard_pipeline_term(self):
        result = normalize_terminology("管缆巡检")
        self.assertEqual(result["corrected_text"], "管缆巡检")
        self.assertFalse(result["normalization_changed"])

    def test_coordinate_direction_tokyo_to_east_longitude(self):
        result = normalize_terminology("北纬19.8度，东京113.5度")
        self.assertEqual(result["corrected_text"], "北纬19.8度，东经113.5度")
        self.assertTrue(result["normalization_changed"])
        self.assertEqual(result["replacements"][0]["source"], "东京")
        self.assertEqual(result["replacements"][0]["target"], "东经")

    def test_coordinate_direction_tokyo_metropolis_to_east_longitude(self):
        result = normalize_terminology("起始点北纬十九点八度，东京都一百一十三点五度")
        self.assertEqual(result["corrected_text"], "起始点北纬十九点八度，东经一百一十三点五度")
        self.assertTrue(result["normalization_changed"])

    def test_coordinate_direction_with_longer_context_window(self):
        result = normalize_terminology("北纬19.8度，任务区域东京113.5度")
        self.assertEqual(result["corrected_text"], "北纬19.8度，任务区域东经113.5度")
        self.assertTrue(result["normalization_changed"])

    def test_no_rewrite_tokyo_without_coordinate_context(self):
        result = normalize_terminology("东京天气多少度")
        self.assertEqual(result["corrected_text"], "东京天气多少度")
        self.assertFalse(result["normalization_changed"])

    def test_no_rewrite_tokyo_place_context(self):
        result = normalize_terminology("查看东京附近的记录")
        self.assertEqual(result["corrected_text"], "查看东京附近的记录")
        self.assertFalse(result["normalization_changed"])


if __name__ == "__main__":
    unittest.main()
