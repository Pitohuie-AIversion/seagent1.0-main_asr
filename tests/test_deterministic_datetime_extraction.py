import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.extractor import ParameterExtractor


FIXED_NOW = datetime(2026, 7, 29, 18, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class EmptyExtractionLLM:
    def __init__(self, result=None):
        self.result = result if result is not None else {
            "slot_candidates": [],
            "unresolved": [],
        }

    def extract_json(self, messages, max_tokens=800):
        return self.result


class DeterministicDatetimeExtractionTest(unittest.TestCase):
    def setUp(self):
        self.required = [
            {"key": "start_time", "label": "任务开始时间", "type": "datetime"},
            {"key": "end_time", "label": "任务结束时间", "type": "datetime"},
        ]

    def extract(self, message, result=None):
        extractor = ParameterExtractor(EmptyExtractionLLM(result))
        with patch("src.simulated_time.get_current_datetime", return_value=FIXED_NOW):
            return extractor.extract_updates(
                message,
                current_state={"task_type_key": "pipeline_inspection"},
                task_type_key="pipeline_inspection",
                required=self.required,
                conversation_history=[],
            )

    def values_by_key(self, message, result=None):
        extracted = self.extract(message, result)
        return {
            candidate["canonical_key"]: candidate["normalized_value"]
            for candidate in extracted["slot_candidates"]
        }

    def test_recovers_labelled_relative_times_when_llm_omits_them(self):
        values = self.values_by_key("开始时间五小时后，结束时间现在")

        self.assertEqual(values["start_time"], "2026-07-29T23:00:00")
        self.assertEqual(values["end_time"], "2026-07-29T18:00:00")

    def test_recovers_labelled_iso_times_when_llm_omits_them(self):
        values = self.values_by_key(
            "开始时间2020-01-01T08:00:00，结束时间2020-01-01T13:00:00"
        )

        self.assertEqual(values["start_time"], "2020-01-01T08:00:00")
        self.assertEqual(values["end_time"], "2020-01-01T13:00:00")

    def test_does_not_guess_unlabelled_or_ambiguous_time(self):
        self.assertEqual(self.values_by_key("尽快开始，稍后结束"), {})
        self.assertEqual(self.values_by_key("2020-01-01T08:00:00"), {})
        self.assertEqual(self.values_by_key("开始时间一二小时后"), {})

    def test_existing_llm_time_candidate_is_not_overridden(self):
        result = {
            "slot_candidates": [
                {
                    "raw_key": "开始时间",
                    "canonical_key": "start_time",
                    "raw_value": "明天上午九点",
                    "normalized_value": "2026-07-30T09:00:00",
                    "confidence": 0.95,
                }
            ],
            "unresolved": [],
        }
        values = self.values_by_key("开始时间五小时后，结束时间现在", result)

        self.assertEqual(values["start_time"], "2026-07-30T09:00:00")
        self.assertEqual(values["end_time"], "2026-07-29T18:00:00")


if __name__ == "__main__":
    unittest.main()
