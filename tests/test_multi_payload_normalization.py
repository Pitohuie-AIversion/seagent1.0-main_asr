# -*- coding: utf-8 -*-
"""
test_multi_payload_normalization.py

针对 P1 缺陷：验证输入多个载荷（如“双目水下成像系统和机械扫描声呐”）时，
规范化层与 SlotStore 能够完整聚合保存多载荷，不会截断单项。
"""

from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager


class TestMultiPayloadNormalization:
    """测试多载荷规范化与 SlotStore 保存完整性。"""

    def test_normalize_payload_list_mutations_aggregates_all_candidates(self):
        kb = KnowledgeBase()
        dm = DialogueManager(llm=None, kb=kb)

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "payload",
                    "raw_value": "双目水下成像系统",
                    "normalized_value": "双目水下成像系统",
                    "confidence": 0.95,
                },
                {
                    "canonical_key": "payload",
                    "raw_value": "机械扫描声呐",
                    "normalized_value": "机械扫描声呐",
                    "confidence": 0.98,
                },
            ],
            "list_mutations": [],
        }

        # 调用规范化方法
        dm._normalize_payload_list_mutations(
            extraction_res,
            user_message="配置双目水下成像系统和机械扫描声呐",
            current_slots={},
        )

        mutations = extraction_res.get("list_mutations", [])
        assert len(mutations) == 1
        payload_mutation = mutations[0]
        assert payload_mutation["field"] == "payload"

        items = payload_mutation.get("items", [])
        assert "双目水下成像系统" in items, f"缺少双目水下成像系统，实际得到: {items}"
        assert "机械扫描声呐" in items, f"缺少机械扫描声呐，实际得到: {items}"
        assert len(items) == 2

    def test_normalize_payload_list_mutations_deduplicates_overlapping_items(self):
        kb = KnowledgeBase()
        dm = DialogueManager(llm=None, kb=kb)

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "payload",
                    "raw_value": "前视声呐系统",
                    "normalized_value": "前视声呐系统",
                    "confidence": 0.95,
                },
                {
                    "canonical_key": "payload",
                    "raw_value": "前视声呐系统",
                    "normalized_value": "前视声呐系统",
                    "confidence": 0.92,
                },
            ],
            "list_mutations": [],
        }

        dm._normalize_payload_list_mutations(
            extraction_res,
            user_message="带上前视声呐系统",
            current_slots={},
        )

        mutations = extraction_res.get("list_mutations", [])
        assert len(mutations) == 1
        items = mutations[0].get("items", [])
        assert items == ["前视声呐系统"]

    def test_normalize_python_stringified_list_in_field_normalizer(self):
        from src.extraction.normalizer import FieldNormalizer

        fn = FieldNormalizer()
        allowed = [
            "轻型多功能液压机械臂",
            "机械臂末端夹爪 / 夹具",
            "单目水下成像系统",
            "云台摄像机",
            "前视声呐系统",
            "厚度检测传感器",
            "水质传感器",
            "TSS管缆跟踪系统",
        ]
        python_repr = "['轻型多功能液压机械臂', '机械臂末端夹爪 / 夹具', '单目水下成像系统', '云台摄像机', '前视声呐系统', '厚度检测传感器', '水质传感器', 'TSS管缆跟踪系统']"

        res = fn.normalize(python_repr, allowed, "list")
        assert res == allowed, f"Expected {allowed}, got {res}"
