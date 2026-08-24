import unittest
from src.coord_parser import format_coord_display, format_slot_display_value
from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase


class TestNaturalLanguageFormatting(unittest.TestCase):
    def test_format_coord_display_dict(self):
        coord_dict = {"lat": 19.8, "lon": 113.2}
        result = format_coord_display(coord_dict)
        self.assertEqual(result, "北纬 19.8 度，东经 113.2 度")

    def test_format_coord_display_negative(self):
        coord_dict = {"lat": -19.8, "lon": -113.2}
        result = format_coord_display(coord_dict)
        self.assertEqual(result, "南纬 19.8 度，西经 113.2 度")

    def test_format_coord_display_json_string(self):
        json_str = '{"lat":19.9,"lon":113.6}'
        result = format_coord_display(json_str)
        self.assertEqual(result, "北纬 19.9 度，东经 113.6 度")

    def test_format_coord_display_already_formatted(self):
        formatted = "北纬 19.8 度，东经 113.2 度"
        result = format_coord_display(formatted)
        self.assertEqual(result, "北纬 19.8 度，东经 113.2 度")

    def test_format_slot_display_value_water_depth(self):
        self.assertEqual(format_slot_display_value("water_depth", 130.0), "130 米")
        self.assertEqual(format_slot_display_value("water_depth", "130.0"), "130 米")
        self.assertEqual(format_slot_display_value("water_depth", "130.5"), "130.5 米")
        self.assertEqual(format_slot_display_value("water_depth", "130米"), "130米")

    def test_format_slot_display_value_list(self):
        val = ["高清水下摄像机", "成像声呐"]
        self.assertEqual(format_slot_display_value("payload", val), "高清水下摄像机、成像声呐")

    def test_dialogue_manager_fact_anchor_suffix(self):
        kb = KnowledgeBase()
        dm = DialogueManager(kb)
        accepted_updates = {
            "start_point": {"lat": 19.8, "lon": 113.2},
            "end_point": {"lat": 19.9, "lon": 113.6},
            "water_depth": 130.0,
        }
        display_updates = dm._get_committed_update_display_values(accepted_updates)
        self.assertEqual(display_updates["start_point"], "北纬 19.8 度，东经 113.2 度")
        self.assertEqual(display_updates["end_point"], "北纬 19.9 度，东经 113.6 度")
        self.assertEqual(display_updates["water_depth"], "130 米")

        suffix = dm._ground_write_reply(
            model_reply="已为您记录相关参数。",
            accepted_updates=accepted_updates,
            unresolved_inputs=[],
            missing_fields=[],
            display_updates=display_updates,
        )
        self.assertIn("起始点经纬度：北纬 19.8 度，东经 113.2 度", suffix)
        self.assertIn("结束点经纬度：北纬 19.9 度，东经 113.6 度", suffix)
        self.assertIn("水深（米）：130 米", suffix)
        self.assertNotIn('{"lat":19.8,"lon":113.2}', suffix)
        self.assertNotIn('{"lat":19.9,"lon":113.6}', suffix)


if __name__ == "__main__":
    unittest.main()
