"""
Regression tests for burial depth (埋设深度/开沟深度) isolation from water_depth (海水水深).
Ensures that trenching/burial depth cannot overwrite sea water depth slots.
"""

import pytest
from src.slots.candidate_resolver import CandidateResolver


@pytest.fixture
def resolver():
    return CandidateResolver(llm=None)


@pytest.fixture
def water_depth_field_def():
    return {
        "key": "water_depth",
        "type": "float",
        "allowed_values": [],
    }


def test_burial_depth_rejected_when_raw_key_is_burial(resolver, water_depth_field_def):
    """当模型抽取出的候选对象 raw_key 为'埋设深度'时，必须被判定为施工工艺参数而拒绝作为 water_depth。"""
    cand = {
        "canonical_key": "water_depth",
        "raw_key": "埋设深度",
        "raw_value": "1.5米",
        "normalized_value": 1.5,
        "confidence": 0.95,
    }
    resolved, unresolved_reason = resolver.resolve_candidate_value(
        candidate=cand,
        required_by_key={"water_depth": water_depth_field_def},
        allowed_keys={"water_depth"},
        current_state={"water_depth": 120.0},
        conversation_history=[],
        user_message="把埋设深度设为1.5米，并带上海底挖沟机。",
    )
    assert resolved is None, "埋设深度严禁解析为 water_depth"
    assert unresolved_reason is not None
    assert "埋设深度" in unresolved_reason or "施工工艺参数" in unresolved_reason


def test_burial_depth_rejected_from_user_message_context(resolver, water_depth_field_def):
    """即使模型将 raw_key 误填为'水深'，若数值来自用户原话中对'埋设深度'的指定，也必须被拦截。"""
    cand = {
        "canonical_key": "water_depth",
        "raw_key": "深度",
        "raw_value": "1.5",
        "normalized_value": 1.5,
        "confidence": 0.95,
    }
    resolved, unresolved_reason = resolver.resolve_candidate_value(
        candidate=cand,
        required_by_key={"water_depth": water_depth_field_def},
        allowed_keys={"water_depth"},
        current_state={"water_depth": 120.0},
        conversation_history=[],
        user_message="把埋设深度设为1.5米，并带上海底挖沟机。",
    )
    assert resolved is None, "来自用户原话'埋设深度设为1.5米'的数值不能覆写 water_depth"
    assert unresolved_reason is not None


def test_legitimate_water_depth_remains_intact(resolver, water_depth_field_def):
    """合法的海水水深输入必须正常解析。"""
    cand = {
        "canonical_key": "water_depth",
        "raw_key": "水深",
        "raw_value": "120米",
        "normalized_value": 120.0,
        "confidence": 0.95,
    }
    resolved, unresolved_reason = resolver.resolve_candidate_value(
        candidate=cand,
        required_by_key={"water_depth": water_depth_field_def},
        allowed_keys={"water_depth"},
        current_state={},
        conversation_history=[],
        user_message="计划在流花油田进行管缆埋设，水深120米。",
    )
    assert resolved is not None
    assert unresolved_reason is None
    assert resolved["normalized_value"] == 120.0
