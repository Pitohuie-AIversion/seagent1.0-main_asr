"""
tests/test_payload_multivalue.py — Payload 多值一致性强化测试

验证场景：
多个 payload 载荷不能因为 key 重复而被覆盖，必须完整进入 SlotStore / output_builder 并去重呈现。
例如：
输入："携带机械臂、高清摄像机、声呐、探测工具"
输出 JSON 中 payload 保持完整 list:
[
  "机械臂",
  "高清摄像机",
  "声呐",
  "探测工具"
]
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.output_builder import OutputBuilder
from src.knowledge_retriever import KnowledgeBase
from src.extractor import ParameterExtractor


class FakeLLMForPayload:
    def extract_json(self, messages, max_tokens=800):
        # 模拟模型从 "携带机械臂、高清摄像机、声呐、探测工具" 中提取候选
        return {
            "slot_candidates": [
                {"raw_key": "携带工具", "canonical_key": "payload", "raw_value": "机械臂", "normalized_value": "机械臂", "confidence": 0.95},
                {"raw_key": "携带工具", "canonical_key": "payload", "raw_value": "高清摄像机", "normalized_value": "高清摄像机", "confidence": 0.95},
                {"raw_key": "携带工具", "canonical_key": "payload", "raw_value": "声呐", "normalized_value": "声呐", "confidence": 0.95},
                {"raw_key": "携带工具", "canonical_key": "payload", "raw_value": "探测工具", "normalized_value": "探测工具", "confidence": 0.95},
            ],
            "unresolved": []
        }

    def chat(self, messages, **kwargs):
        return "OK"

    def filter_reply(self, reply):
        return reply


class PayloadMultiValueTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.output_builder = OutputBuilder(self.kb)
        self.llm = FakeLLMForPayload()
        self.extractor = ParameterExtractor(self.llm)

    def test_output_builder_list_extraction(self):
        # 测试 output_builder 对 list 类型的解析与过滤去重
        field_def = {"key": "payload", "label": "携带工具", "type": "list"}
        task_state = {
            "payload": ["机械臂", "高清摄像机", "声呐", "探测工具"]
        }
        extracted = self.output_builder._extract_field(
            key="payload",
            ftype="list",
            field_def=field_def,
            task_state=task_state,
            task_type_key="pipeline_inspection"
        )
        self.assertIsInstance(extracted, list)
        self.assertEqual(len(extracted), 4)
        self.assertEqual(extracted, ["机械臂", "高清摄像机", "声呐", "探测工具"])

    def test_output_builder_single_string_to_list_coercion(self):
        # 兼容单元素字符串输入
        field_def = {"key": "payload", "label": "携带工具", "type": "list"}
        task_state = {"payload": "机械臂"}
        extracted = self.output_builder._extract_field(
            key="payload",
            ftype="list",
            field_def=field_def,
            task_state=task_state,
            task_type_key="pipeline_inspection"
        )
        self.assertEqual(extracted, ["机械臂"])

    def test_extractor_multi_payload_preservation(self):
        # 测试 ParameterExtractor 针对 payload 多值 candidate 不会被覆盖
        current_state = {}
        required = [
            {"key": "payload", "label": "携带工具", "type": "list", "allowed_values": ["机械臂", "高清摄像机", "声呐", "探测工具"]}
        ]
        res = self.extractor.extract_updates(
            user_message="携带机械臂、高清摄像机、声呐、探测工具",
            current_state=current_state,
            task_type_key="pipeline_inspection",
            required=required
        )
        candidates = res.get("slot_candidates", [])
        payload_cands = [c for c in candidates if c.get("canonical_key") == "payload"]
        self.assertTrue(len(payload_cands) >= 1)
        values = payload_cands[0].get("normalized_value")
        self.assertIsInstance(values, list)
        self.assertIn("机械臂", values)
        self.assertIn("高清摄像机", values)
        self.assertIn("声呐", values)
        self.assertIn("探测工具", values)


if __name__ == "__main__":
    unittest.main()
