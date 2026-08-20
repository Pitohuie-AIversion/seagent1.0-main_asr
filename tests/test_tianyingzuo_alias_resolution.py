import unittest
from src.extractor import ParameterExtractor

class TestTianyingzuoAliasResolution(unittest.TestCase):
    def setUp(self):
        self.field_def = {
            "key": "equipment_family",
            "allowed_values": ["轻型工作级深海机器人", "观察级深海机器人"],
            "alias_mappings": {
                "天鹰座": "轻型工作级深海机器人",
                "轻型工作级ROV": "轻型工作级深海机器人",
                "观察级ROV": "观察级深海机器人",
            }
        }

    def test_direct_tianyingzuo(self):
        res = ParameterExtractor._match_alias_value("天鹰座", self.field_def)
        self.assertEqual(res, "轻型工作级深海机器人")

    def test_colloquial_select_tianyingzuo(self):
        res = ParameterExtractor._match_alias_value("选择天鹰座", self.field_def)
        self.assertEqual(res, "轻型工作级深海机器人")

    def test_colloquial_want_use_tianyingzuo(self):
        res = ParameterExtractor._match_alias_value("我要使用天鹰座", self.field_def)
        self.assertEqual(res, "轻型工作级深海机器人")

    def test_tianyingzuo_rov_suffix(self):
        res = ParameterExtractor._match_alias_value("天鹰座ROV", self.field_def)
        self.assertEqual(res, "轻型工作级深海机器人")

if __name__ == "__main__":
    unittest.main()
