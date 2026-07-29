import unittest

from src.extractor import ParameterExtractor


class EmptyExtractionLLM:
    def __init__(self, result=None):
        self.result = result if result is not None else {
            "slot_candidates": [],
            "unresolved": [],
        }

    def extract_json(self, messages, max_tokens=800):
        return self.result


class DeterministicEnumExtractionTest(unittest.TestCase):
    def setUp(self):
        self.required = [
            {
                "key": "cable_type",
                "label": "管缆类型",
                "type": "string",
                "allowed_values": ["海底油气管道", "电力电缆", "光纤通信缆"],
            }
        ]

    def extract(self, message, result=None):
        extractor = ParameterExtractor(EmptyExtractionLLM(result))
        return extractor.extract_updates(
            message,
            current_state={"task_type_key": "pipeline_inspection"},
            task_type_key="pipeline_inspection",
            required=self.required,
            conversation_history=[],
        )

    def test_explicit_allowed_value_fills_llm_omission(self):
        result = self.extract("管缆类型为海底油气管道")

        self.assertEqual(
            result["slot_candidates"],
            [
                {
                    "raw_key": "管缆类型",
                    "canonical_key": "cable_type",
                    "raw_value": "海底油气管道",
                    "normalized_value": "海底油气管道",
                    "confidence": 1.0,
                    "resolution_method": "canonical_exact",
                }
            ],
        )

    def test_last_explicit_allowed_value_wins(self):
        result = self.extract("原来是电力电缆，现在改为海底油气管道")

        self.assertEqual(
            result["slot_candidates"][0]["normalized_value"],
            "海底油气管道",
        )

    def test_unknown_value_is_not_added(self):
        result = self.extract("管缆类型为未知复合缆")

        self.assertEqual(result["slot_candidates"], [])

    def test_existing_llm_candidate_is_not_duplicated_or_overridden(self):
        result = self.extract(
            "管缆类型为海底油气管道",
            {
                "slot_candidates": [
                    {
                        "raw_key": "管缆类型",
                        "canonical_key": "cable_type",
                        "raw_value": "电力电缆",
                        "normalized_value": "电力电缆",
                        "confidence": 0.9,
                    }
                ],
                "unresolved": [],
            },
        )

        self.assertEqual(len(result["slot_candidates"]), 1)
        self.assertEqual(
            result["slot_candidates"][0]["normalized_value"],
            "电力电缆",
        )

    def test_invalid_llm_payload_still_allows_exact_schema_recovery(self):
        result = self.extract("管缆类型为海底油气管道", result=[])

        self.assertEqual(
            result["slot_candidates"][0]["normalized_value"],
            "海底油气管道",
        )


if __name__ == "__main__":
    unittest.main()
