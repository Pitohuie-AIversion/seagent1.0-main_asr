import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.extractor import ParameterExtractor


class ContextualSelectionLLM:
    def __init__(self):
        self.calls = []

    def extract_json(self, messages, max_tokens=800):
        self.calls.append(messages)
        system = messages[0]["content"]
        if "受约束的候选语义解析器" in system:
            return {
                "matched": True,
                "canonical_key": "equipment_type",
                "canonical_value": "观察级深海机器人",
                "confidence": 0.98,
                "reason": "最近回复列出了候选，用户选择第一个。",
            }
        return {"slot_candidates": [], "unresolved": []}


class ContextualPayloadSelectionLLM:
    def __init__(self):
        self.calls = []

    def extract_json(self, messages, max_tokens=800):
        self.calls.append(messages)
        system = messages[0]["content"]
        if "受约束的候选语义解析器" in system:
            return {
                "matched": True,
                "canonical_key": "payload",
                "canonical_value": "激光标尺",
                "confidence": 0.96,
                "reason": "用户选择载荷候选中的第一个。",
            }
        return {"slot_candidates": [], "unresolved": []}


class ContextualSelectionExtractorTest(unittest.TestCase):
    def test_empty_primary_extraction_still_runs_constrained_contextual_resolution(self):
        extractor = ParameterExtractor(ContextualSelectionLLM())
        required = [
            {
                "key": "equipment_type",
                "label": "作业设备型号",
                "type": "string",
                "allowed_values": ["观察级深海机器人", "轻型工作级深海机器人"],
            }
        ]
        history = [
            {
                "role": "assistant",
                "content": "请选择作业设备型号：1. 观察级深海机器人 2. 轻型工作级深海机器人",
            }
        ]

        result = extractor.extract_updates(
            user_message="选择第一个",
            current_state={"task_type_key": "pipeline_inspection"},
            task_type_key="pipeline_inspection",
            required=required,
            conversation_history=history,
        )

        self.assertEqual(result["unresolved"], [])
        self.assertEqual(
            result["slot_candidates"],
            [
                {
                    "raw_key": "作业设备型号",
                    "canonical_key": "equipment_type",
                    "raw_value": "选择第一个",
                    "normalized_value": "观察级深海机器人",
                    "confidence": 0.98,
                    "resolution_method": "llm_contextual_selection",
                }
            ],
        )
        self.assertEqual(len(extractor.llm.calls), 2)

    def test_empty_primary_extraction_contextual_resolution_handles_list_fields(self):
        extractor = ParameterExtractor(ContextualPayloadSelectionLLM())
        required = [
            {
                "key": "payload",
                "label": "携带工具",
                "type": "list",
                "allowed_values": ["激光标尺", "机械式声呐"],
            }
        ]
        history = [
            {
                "role": "assistant",
                "content": "请选择携带工具：1. 激光标尺 2. 机械式声呐",
            }
        ]

        result = extractor.extract_updates(
            user_message="选第一个",
            current_state={
                "task_type_key": "pipeline_inspection",
                "equipment_type": "轻型工作级深海机器人",
            },
            task_type_key="pipeline_inspection",
            required=required,
            conversation_history=history,
        )

        self.assertEqual(result["unresolved"], [])
        self.assertEqual(result["slot_candidates"][0]["canonical_key"], "payload")
        self.assertEqual(result["slot_candidates"][0]["normalized_value"], ["激光标尺"])
        self.assertEqual(result["slot_candidates"][0]["resolution_method"], "llm_contextual_selection")


if __name__ == "__main__":
    unittest.main()
