import unittest

from src.extractor import ParameterExtractor
from src.interaction_plan import has_write_evidence
from src.knowledge_retriever import KnowledgeBase
from src.output_builder import OutputBuilder


class EmptyExtractionLLM:
    def __init__(self, result=None):
        self.result = result if result is not None else {
            "slot_candidates": [],
            "unresolved": [],
        }

    def extract_json(self, messages, max_tokens=800):
        self.messages = messages
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


class TaskTypeCatalogExtractionTest(unittest.TestCase):
    def setUp(self):
        self.catalog = [
            {
                "task_type_key": "pipeline_inspection",
                "display_name": "管缆巡检",
                "task_type_values": ["管缆巡检"],
            },
            {
                "task_type_key": "tree_valve_operation",
                "display_name": "采油树控制面板阀门插拔",
                "task_type_values": ["采油树控制面板插入", "采油树控制面板拔出"],
            },
        ]

    def test_knowledge_base_builds_task_type_catalog_from_schema(self):
        catalog = KnowledgeBase().get_task_type_catalog()
        tree = next(item for item in catalog if item["task_type_key"] == "tree_valve_operation")

        self.assertEqual(tree["display_name"], "采油树控制面板阀门插拔")
        self.assertEqual(
            tree["task_type_values"],
            ["采油树控制面板插入", "采油树控制面板拔出"],
        )

    def test_stage1_prompt_includes_template_display_name_and_values(self):
        llm = EmptyExtractionLLM()
        ParameterExtractor(llm).extract_updates(
            "我想做采油树面板",
            current_state={},
            task_type_key=None,
            task_type_catalog=self.catalog,
            conversation_history=[],
        )

        system_prompt = llm.messages[0]["content"]
        self.assertIn("tree_valve_operation", system_prompt)
        self.assertIn("display_name: 采油树控制面板阀门插拔", system_prompt)
        self.assertIn("task_type_values: 采油树控制面板插入 / 采油树控制面板拔出", system_prompt)

    def test_single_value_template_auto_fills_task_type_from_schema(self):
        llm = EmptyExtractionLLM(
            {
                "slot_candidates": [
                    {
                        "raw_key": "作业类型标识",
                        "canonical_key": "task_type_key",
                        "raw_value": "管缆巡检",
                        "normalized_value": "pipeline_inspection",
                        "confidence": 0.95,
                    }
                ],
                "unresolved": [],
            }
        )

        result = ParameterExtractor(llm).extract_updates(
            "我要做管缆巡检",
            current_state={},
            task_type_key=None,
            task_type_catalog=self.catalog,
            conversation_history=[],
        )

        values = {
            cand["canonical_key"]: cand["normalized_value"]
            for cand in result["slot_candidates"]
        }
        self.assertEqual(values["task_type_key"], "pipeline_inspection")
        self.assertEqual(values["task_type"], "管缆巡检")

    def test_multi_value_template_keeps_task_type_missing_when_semantics_are_ambiguous(self):
        llm = EmptyExtractionLLM(
            {
                "slot_candidates": [
                    {
                        "raw_key": "作业类型标识",
                        "canonical_key": "task_type_key",
                        "raw_value": "采油树面板",
                        "normalized_value": "tree_valve_operation",
                        "confidence": 0.95,
                    }
                ],
                "unresolved": [],
            }
        )

        result = ParameterExtractor(llm).extract_updates(
            "我想做采油树面板",
            current_state={},
            task_type_key=None,
            task_type_catalog=self.catalog,
            conversation_history=[],
        )

        values = {
            cand["canonical_key"]: cand["normalized_value"]
            for cand in result["slot_candidates"]
        }
        self.assertEqual(values["task_type_key"], "tree_valve_operation")
        self.assertNotIn("task_type", values)

    def test_invalid_task_type_value_is_rejected_against_template_values(self):
        llm = EmptyExtractionLLM(
            {
                "slot_candidates": [
                    {
                        "raw_key": "作业类型标识",
                        "canonical_key": "task_type_key",
                        "raw_value": "采油树面板",
                        "normalized_value": "tree_valve_operation",
                        "confidence": 0.95,
                    },
                    {
                        "raw_key": "任务类型",
                        "canonical_key": "task_type",
                        "raw_value": "采油树控制面板阀门插拔",
                        "normalized_value": "采油树控制面板阀门插拔",
                        "confidence": 0.95,
                    },
                ],
                "unresolved": [],
            }
        )

        result = ParameterExtractor(llm).extract_updates(
            "我想做采油树面板",
            current_state={},
            task_type_key=None,
            task_type_catalog=self.catalog,
            conversation_history=[],
        )

        values = {
            cand["canonical_key"]: cand["normalized_value"]
            for cand in result["slot_candidates"]
        }
        self.assertEqual(values["task_type_key"], "tree_valve_operation")
        self.assertNotIn("task_type", values)

    def test_exact_task_type_value_reply_fills_task_type_even_when_llm_omits_it(self):
        llm = EmptyExtractionLLM(
            {
                "slot_candidates": [],
                "unresolved": [],
            }
        )
        required = [
            {
                "key": "task_type",
                "label": "任务类型（插入/拔出）",
                "type": "tasktype",
                "allowed_values": ["采油树控制面板插入", "采油树控制面板拔出"],
            }
        ]

        result = ParameterExtractor(llm).extract_updates(
            "采油树控制面板插入",
            current_state={"task_type_key": "tree_valve_operation"},
            task_type_key="tree_valve_operation",
            task_type_catalog=self.catalog,
            required=required,
            conversation_history=[],
        )

        self.assertEqual(
            result["slot_candidates"],
            [
                {
                    "raw_key": "任务类型（插入/拔出）",
                    "canonical_key": "task_type",
                    "raw_value": "采油树控制面板插入",
                    "normalized_value": "采油树控制面板插入",
                    "confidence": 1.0,
                    "resolution_method": "canonical_exact",
                }
            ],
        )

    def test_task_type_key_only_candidate_for_exact_leaf_value_also_fills_task_type(self):
        llm = EmptyExtractionLLM(
            {
                "slot_candidates": [
                    {
                        "raw_key": "作业类型标识",
                        "canonical_key": "task_type_key",
                        "raw_value": "采油树控制面板插入",
                        "normalized_value": "tree_valve_operation",
                        "confidence": 0.95,
                    }
                ],
                "unresolved": [],
            }
        )
        required = [
            {
                "key": "task_type",
                "label": "任务类型（插入/拔出）",
                "type": "tasktype",
                "allowed_values": ["采油树控制面板插入", "采油树控制面板拔出"],
            }
        ]

        result = ParameterExtractor(llm).extract_updates(
            "采油树控制面板插入",
            current_state={"task_type_key": "tree_valve_operation"},
            task_type_key="tree_valve_operation",
            task_type_catalog=self.catalog,
            required=required,
            conversation_history=[],
        )

        values = {
            cand["canonical_key"]: cand["normalized_value"]
            for cand in result["slot_candidates"]
        }
        self.assertEqual(values["task_type_key"], "tree_valve_operation")
        self.assertEqual(values["task_type"], "采油树控制面板插入")

    def test_output_builder_does_not_default_first_task_type_value(self):
        builder = OutputBuilder(KnowledgeBase())

        value = builder._extract_field(
            "task_type",
            "tasktype",
            {"key": "task_type", "type": "tasktype"},
            {},
            "tree_valve_operation",
        )

        self.assertIsNone(value)


class WriteEvidenceBoundaryTest(unittest.TestCase):
    def test_llm_write_routes_without_business_keyword_gate(self):
        self.assertTrue(
            has_write_evidence(
                "我想做采油树面板",
                task_state={},
                plan_candidate={"operation": "WRITE", "confidence": 0.95},
            )
        )

    def test_business_terms_alone_do_not_create_write_evidence(self):
        self.assertFalse(
            has_write_evidence(
                "采油树 管缆 阀门",
                task_state={},
                plan_candidate=None,
            )
        )

    def test_hypothetical_question_blocks_write_even_with_write_plan(self):
        self.assertFalse(
            has_write_evidence(
                "如果水深改成500米会有什么影响？",
                task_state={"task_type_key": "pipeline_inspection"},
                plan_candidate={"operation": "WRITE", "confidence": 0.95},
            )
        )


if __name__ == "__main__":
    unittest.main()
