"""
tests/test_extractor_submodules.py — 抽取层解耦子模块专有单元测试套件

测试对象：
1. TemporalParser (src/temporal/temporal_parser.py): 相对时间语义探测、区间物化与时长算术
2. CandidateResolver (src/slots/candidate_resolver.py): 序数匹配、口语清洗、别名模糊匹配与数值规范化
3. ParameterExtractor (src/extraction/extractor.py): 门面调度、子模块协作与向后兼容代理契约
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from src.slots.candidate_resolver import CandidateResolver
from src.extraction.extractor import (
    ParameterExtractor,
    _build_task_type_rules,
)
from src.temporal.temporal_parser import TemporalParser

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class TestTemporalParser(unittest.TestCase):
    """测试 TemporalParser 时间关系抽取与物化引擎"""

    def setUp(self):
        self.parser = TemporalParser()

    def test_has_date_semantics(self):
        self.assertTrue(self.parser.has_date_semantics("明天上午九点开始作业"))
        self.assertTrue(self.parser.has_date_semantics("下周三下午三点"))
        self.assertTrue(self.parser.has_date_semantics("2026-08-01开始"))
        self.assertFalse(self.parser.has_date_semantics("水深大约三百米"))
        self.assertFalse(self.parser.has_date_semantics(""))

    def test_looks_like_iso_datetime(self):
        self.assertTrue(self.parser.looks_like_iso_datetime("2026-07-28T12:00:00"))
        self.assertTrue(self.parser.looks_like_iso_datetime("2026-07-28T12:00:00+08:00"))
        self.assertFalse(self.parser.looks_like_iso_datetime("2026-07-28 12:00:00"))
        self.assertFalse(self.parser.looks_like_iso_datetime("明天上午"))
        self.assertFalse(self.parser.looks_like_iso_datetime(None))

    def test_mentions_end_time_keep(self):
        self.assertTrue(self.parser.mentions_end_time_keep("推迟1小时开始，结束时间保持不变"))
        self.assertTrue(self.parser.mentions_end_time_keep("截止时间不变"))
        self.assertFalse(self.parser.mentions_end_time_keep("正常作业2小时"))

    def test_extract_duration_delta_text(self):
        text = "任务推迟开始，时长再增加2小时"
        self.assertIsNotNone(self.parser.extract_duration_delta_text(text))
        self.assertIsNone(self.parser.extract_duration_delta_text("作业持续3小时"))

    def test_parse_state_datetime(self):
        dt = self.parser.parse_state_datetime("2026-07-28T12:00:00")
        self.assertEqual(2026, dt.year)
        self.assertEqual(12, dt.hour)

        # 兼容带 Z 结尾
        dt_z = self.parser.parse_state_datetime("2026-07-28T12:00:00Z")
        self.assertIsNotNone(dt_z)

        # 兼容 dict 结构
        dt_dict = self.parser.parse_state_datetime({"normalized_value": "2026-07-28T12:00:00"})
        self.assertIsNotNone(dt_dict)

        self.assertIsNone(self.parser.parse_state_datetime(None))
        self.assertIsNone(self.parser.parse_state_datetime("null"))

    def test_materialize_time_relation_duration(self):
        # 验证由持续时间推导 end_time
        candidates = [
            {
                "raw_key": "开始时间",
                "canonical_key": "start_time",
                "raw_value": "2026-07-28T10:00:00",
                "normalized_value": "2026-07-28T10:00:00",
            }
        ]
        relation = {
            "has_duration": True,
            "duration_seconds": 7200,
            "raw_text": "干2小时",
            "target": "duration",
            "action": "SET",
        }
        res, unresolved = self.parser.materialize_time_relation(
            candidates,
            relation,
            current_state={},
            allowed_keys={"start_time", "end_time"},
            user_message="干2小时",
        )
        self.assertEqual([], unresolved)
        end_cand = next((c for c in res if c.get("canonical_key") == "end_time"), None)
        self.assertIsNotNone(end_cand)
        self.assertIn("2026-07-28T12:00:00", end_cand["normalized_value"])


class TestCandidateResolver(unittest.TestCase):
    """测试 CandidateResolver 实体候选消歧、口语清洗与匹配器"""

    def setUp(self):
        self.resolver = CandidateResolver()

    def test_strip_colloquial_prefixes(self):
        self.assertEqual("海洋石油681", self.resolver.strip_colloquial_prefixes("把支持船改成海洋石油681"))
        self.assertEqual("OBSROV-75-001", self.resolver.strip_colloquial_prefixes("我要选择OBSROV-75-001"))
        self.assertEqual("300米", self.resolver.strip_colloquial_prefixes("把水深调整为300米"))

    def test_strip_colloquial_suffixes(self):
        self.assertEqual("海洋石油681", self.resolver.strip_colloquial_suffixes("海洋石油681号船"))
        self.assertEqual("崖城13-1", self.resolver.strip_colloquial_suffixes("崖城13-1油田"))

    def test_match_allowed_value(self):
        allowed = ["海洋石油681", "海洋石油708", "德丰号"]
        # 精确匹配
        self.assertEqual("海洋石油681", self.resolver.match_allowed_value("海洋石油681", allowed))
        # 口语前后缀清洗后匹配
        self.assertEqual("海洋石油681", self.resolver.match_allowed_value("把船只改成海洋石油681号船", allowed))
        # 子串容错匹配
        self.assertEqual("海洋石油708", self.resolver.match_allowed_value("708", allowed))

    def test_match_alias_value(self):
        field_def = {
            "alias_mappings": {
                "天鹰座": "通用工作级深海机器人 250HP",
                "天鹰座ROV": "通用工作级深海机器人 250HP",
                "海马号": "轻型作业级深海机器人 150HP",
            }
        }
        # 别名精确匹配
        self.assertEqual(
            "通用工作级深海机器人 250HP",
            self.resolver.match_alias_value("天鹰座", field_def),
        )
        # 口语前缀清洗匹配
        self.assertEqual(
            "通用工作级深海机器人 250HP",
            self.resolver.match_alias_value("选择天鹰座", field_def),
        )

    def test_extract_and_match_numbered_options(self):
        history = [
            {"role": "user", "content": "有哪些可用ROV？"},
            {"role": "assistant", "content": "为您推荐以下设备：\n1. 观察级深海机器人 75HP\n2. 通用工作级深海机器人 250HP"},
        ]
        options = self.resolver.extract_numbered_options_from_assistant_message(history)
        self.assertEqual(["观察级深海机器人 75HP", "通用工作级深海机器人 250HP"], options)

        # 匹配用户选择序号
        self.assertEqual("观察级深海机器人 75HP", self.resolver.match_numbered_option_by_user_input("1", options))
        self.assertEqual("通用工作级深海机器人 250HP", self.resolver.match_numbered_option_by_user_input("选第2个", options))
        self.assertIsNone(self.resolver.match_numbered_option_by_user_input("5", options))

    def test_clean_numeric_candidate(self):
        field_def = {"type": "integer"}
        # 中文数字
        cand = {"normalized_value": "三百米", "raw_value": "三百米"}
        cleaned = self.resolver.clean_numeric_candidate(cand, "water_depth", field_def)
        self.assertEqual(300, cleaned["normalized_value"])

        # 纯阿拉伯数字加单位
        cand = {"normalized_value": "450m", "raw_value": "450m"}
        cleaned = self.resolver.clean_numeric_candidate(cand, "water_depth", field_def)
        self.assertEqual(450, cleaned["normalized_value"])

        # 时长单位换算：2.5小时 -> 9000秒
        cand_dur = {"normalized_value": "2.5小时", "raw_value": "2.5小时"}
        cleaned_dur = self.resolver.clean_numeric_candidate(cand_dur, "duration", {"type": "integer"})
        self.assertEqual(9000, cleaned_dur["normalized_value"])


class TestParameterExtractorFacade(unittest.TestCase):
    """测试 ParameterExtractor 门面调度器与向后兼容方法代理"""

    def setUp(self):
        self.mock_llm = MagicMock()
        self.extractor = ParameterExtractor(self.mock_llm)

    def test_submodule_instantiation(self):
        self.assertIsInstance(self.extractor.temporal_parser, TemporalParser)
        self.assertIsInstance(self.extractor.candidate_resolver, CandidateResolver)
        self.assertIs(self.extractor.temporal_parser.llm, self.mock_llm)
        self.assertIs(self.extractor.candidate_resolver.llm, self.mock_llm)

    def test_build_task_type_rules(self):
        task_map = {
            "管缆巡检": "pipeline_inspection",
            "采油树操作": "tree_valve_operation",
        }
        rules = _build_task_type_rules(task_map)
        self.assertIn("pipeline_inspection", rules)
        self.assertIn("tree_valve_operation", rules)

    def test_allowed_candidate_keys(self):
        # 初始未选定 task_type_key
        keys = self.extractor._allowed_candidate_keys(None, [])
        self.assertEqual({"task_type", "task_type_key", "emergency_mode"}, keys)

        # 选定 task_type_key 后
        required = [{"key": "water_depth"}, {"key": "support_vessel"}]
        keys_expanded = self.extractor._allowed_candidate_keys("pipeline_inspection", required)
        self.assertIn("water_depth", keys_expanded)
        self.assertIn("support_vessel", keys_expanded)
        self.assertIn("task_type", keys_expanded)

    def test_delegated_method_proxies(self):
        # 验证代理在 ParameterExtractor 实例与类对象上均可正常工作
        self.assertEqual("海洋石油681", ParameterExtractor._strip_colloquial_prefixes("把船改成海洋石油681"))
        self.assertEqual("OBSROV", self.extractor._strip_colloquial_prefixes("选择OBSROV"))

        self.assertTrue(ParameterExtractor._has_date_semantics("明天下午两点"))
        self.assertTrue(self.extractor._has_date_semantics("今天开始"))


if __name__ == "__main__":
    unittest.main()
