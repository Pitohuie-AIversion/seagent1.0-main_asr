# -*- coding: utf-8 -*-
"""
test_coord_and_payload_normalization_fix.py

验证以下两项缺陷修复：
1. 坐标解析器 parse_coord_value 对 JSON 字符串、Python dict 字符串以及纯空格分隔裸值的正确解析；
2. 列表规范化与载荷处理中对顿号/逗号未切分子项的自动展平，以及对已选定机器人出厂自带载荷（onboard_payloads）的容错过滤。
"""

import pytest
from src.extraction.coord_parser import parse_coord_value
from src.extraction.normalizer import FieldNormalizer
from src.knowledge_retriever import KnowledgeBase
from src.slots.slot_store import SlotStore, Slot
from src.dispatch.output_builder import OutputBuilder
from src.dialogue_manager import DialogueManager


class TestCoordParserJsonAndSpaces:
    def test_parse_coord_value_json_string(self):
        # JSON 格式字符串（包含双引号）
        res = parse_coord_value('{"lat": 11.2, "lon": 113.4}')
        assert res == {"lat": 11.2, "lon": 113.4}

        res_full = parse_coord_value('{"latitude": 11.2, "longitude": 113.4}')
        assert res_full == {"lat": 11.2, "lon": 113.4}

    def test_parse_coord_value_python_dict_string(self):
        # Python repr 字典格式（包含单引号）
        res = parse_coord_value("{'lat': 11.2, 'lon': 113.4}")
        assert res == {"lat": 11.2, "lon": 113.4}

    def test_parse_coord_value_bare_space_separated(self):
        # 空格分隔的裸数字对
        res = parse_coord_value("11.2 113.4")
        assert res == {"lat": 11.2, "lon": 113.4}

        res_neg = parse_coord_value(" -11.2   113.4 ")
        assert res_neg == {"lat": -11.2, "lon": 113.4}

    def test_parse_coord_value_dict(self):
        # 直接传入 dict
        res = parse_coord_value({"lat": 11.2, "lon": 113.4})
        assert res == {"lat": 11.2, "lon": 113.4}

    def test_parse_coord_value_invalid(self):
        assert parse_coord_value("invalid text") is None
        assert parse_coord_value("2026-08-20 15:00") is None
        assert parse_coord_value("") is None
        assert parse_coord_value(None) is None


class TestPayloadListNormalizationAndOnboardFiltering:
    def test_field_normalizer_flattens_delimiter_separated_list_items(self):
        fn = FieldNormalizer()
        allowed = ["侧扫声呐", "侧扫声呐系统", "USBL辅助定位模块", "厚度检测传感器", "溶解氧传感器"]

        # 输入为包含单个顿号连接长字符串的列表
        raw1 = ["侧扫声呐、侧扫声呐系统、USBL辅助定位模块、厚度检测传感器、溶解氧传感器"]
        res1 = fn.normalize(raw1, allowed, "list")
        assert res1 == allowed

        # 输入为混合列表
        raw2 = ["侧扫声呐", "侧扫声呐系统、USBL辅助定位模块", "厚度检测传感器,溶解氧传感器"]
        res2 = fn.normalize(raw2, allowed, "list")
        assert res2 == allowed

    def test_normalize_payload_list_mutations_converts_direct_candidate_and_flattens(self):
        kb = KnowledgeBase()
        dm = DialogueManager(llm=None, kb=kb)

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "payload",
                    "raw_value": "前视声呐系统、侧扫声呐、侧扫声呐系统、USBL辅助定位模块、厚度检测传感器、溶解氧传感器",
                    "normalized_value": "前视声呐系统、侧扫声呐、侧扫声呐系统、USBL辅助定位模块、厚度检测传感器、溶解氧传感器",
                    "confidence": 0.95,
                }
            ],
            "list_mutations": [],
        }

        # 即使 user_message 无特定增减指令且当前无已生效载荷，也应被转换为 set 变异，且子项被自动展平
        dm._normalize_payload_list_mutations(
            extraction_res,
            user_message="前视声呐系统、侧扫声呐、侧扫声呐系统、USBL辅助定位模块、厚度检测传感器、溶解氧传感器",
            current_slots={},
        )

        assert len(extraction_res["slot_candidates"]) == 0
        mutations = extraction_res.get("list_mutations", [])
        assert len(mutations) == 1
        assert mutations[0]["field"] == "payload"
        assert mutations[0]["operation"] == "set"
        items = mutations[0]["items"]
        assert len(items) == 6
        assert "前视声呐系统" in items
        assert "侧扫声呐" in items
        assert "溶解氧传感器" in items

    def test_apply_list_mutation_skips_robot_onboard_payloads(self):
        kb = KnowledgeBase()
        store = SlotStore(kb)

        schema_field = {"key": "payload", "allowed_values_ref": "payload_options.pipeline_inspection"}
        # AUV-324CC 自带 前视声呐系统
        new_slots = {
            "task_type_key": Slot(slot_name="task_type_key", value="pipeline_inspection", value_type="string", status="valid"),
            "equipment_type": Slot(slot_name="equipment_type", value="水下无人自主航行器 324CC", value_type="string", status="valid"),
            "payload": Slot(slot_name="payload", value=[], value_type="list", status="missing"),
        }

        mutation = {
            "field": "payload",
            "operation": "set",
            "items": ["前视声呐系统、侧扫声呐、侧扫声呐系统、USBL辅助定位模块、厚度检测传感器、溶解氧传感器"],
            "raw_text": "前视声呐系统、侧扫声呐、侧扫声呐系统、USBL辅助定位模块、厚度检测传感器、溶解氧传感器",
        }

        res = store.apply_list_mutation(
            new_slots=new_slots,
            mutation=mutation,
            required_schema=[schema_field],
        )

        assert res.get("success") is True, f"Mutation failed with error: {res.get('error')}"
        payload_val = new_slots["payload"].value
        assert isinstance(payload_val, list)
        # 出厂自带的前视声呐系统应被自动跳过，不报非法
        assert "前视声呐系统" not in payload_val
        # 其余合法支持载荷成功写入
        assert "侧扫声呐" in payload_val
        assert "侧扫声呐系统" in payload_val
        assert "USBL辅助定位模块" in payload_val
        assert "厚度检测传感器" in payload_val
        assert "溶解氧传感器" in payload_val
        assert new_slots["payload"].status in ("candidate", "valid")
        assert new_slots["payload"].validation_error is None

