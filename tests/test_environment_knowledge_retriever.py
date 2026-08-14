"""tests/test_environment_knowledge_retriever.py — 海域环境与关联知识库检索测试套件"""

from unittest.mock import MagicMock
import pytest

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouteResult
from src.interaction_plan import InteractionPlan
from src.knowledge_retriever import KnowledgeBase


def test_generic_knowledge_qa_contains_environment_knowledge() -> None:
    kb = KnowledgeBase()
    evidence = kb.execute_typed_query("KNOWLEDGE_QA", "系统知识库包含哪些内容？", context={})

    assert evidence["found"] is True
    categories = {item.get("category") for item in evidence["results"] if isinstance(item, dict)}
    assert "oil_fields" in categories
    assert "forbidden_areas" in categories
    assert "dvl_bottom_lock_failure_areas" in categories
    assert "task_templates" in categories
    assert "constraints_rules" in categories
    assert "robot_classes_summary" in categories
    assert "cable_types" in categories
    assert "vessels" in categories


def test_oilfield_query_by_name_and_alias() -> None:
    kb = KnowledgeBase()

    # 1. 完整标准名称
    evidence1 = kb.execute_typed_query(
        "KNOWLEDGE_QA",
        "介绍一下流花11-1油田",
        context={"subject_type": "environment", "subject_text": "流花11-1油田"},
    )
    assert evidence1["query_type"] == "ENVIRONMENT_QUERY"
    assert evidence1["found"] is True
    details = next(item for item in evidence1["results"] if item.get("category") == "oil_field_details")
    oil_field = details["oil_field"]
    assert oil_field["id"] == "liuhua_11_1"
    assert oil_field["water_depth"] == 305
    assert oil_field["lat_range"] == [20.81, 20.82]

    # 2. 别名匹配（深海一号 / 陵水17-2）
    evidence2 = kb.execute_typed_query(
        "KNOWLEDGE_QA",
        "深海一号气田的水深是多少？",
        context={"subject_type": "environment"},
    )
    assert evidence2["query_type"] == "ENVIRONMENT_QUERY"
    assert evidence2["found"] is True
    details2 = next(item for item in evidence2["results"] if item.get("category") == "oil_field_details")
    assert details2["oil_field"]["id"] == "lingshui_17_2"
    assert details2["oil_field"]["maximum_reference_water_depth"] == 1587


def test_forbidden_area_query_by_name_and_alias() -> None:
    kb = KnowledgeBase()
    evidence = kb.execute_typed_query(
        "KNOWLEDGE_QA",
        "中华白海豚保护区能进去作业吗？",
        context={"subject_type": "environment"},
    )
    assert evidence["query_type"] == "ENVIRONMENT_QUERY"
    assert evidence["found"] is True
    details = next(item for item in evidence["results"] if item.get("category") == "forbidden_area_details")
    assert details["forbidden_area"]["id"] == "gd_pearl_river_dolphin_core"


def test_dvl_area_query() -> None:
    kb = KnowledgeBase()
    evidence = kb.execute_typed_query(
        "KNOWLEDGE_QA",
        "南海北部陆坡DVL风险区情况如何？",
        context={"subject_type": "environment"},
    )
    assert evidence["query_type"] == "ENVIRONMENT_QUERY"
    assert evidence["found"] is True
    details = next(item for item in evidence["results"] if item.get("category") == "dvl_area_details")
    assert details["dvl_area"]["id"] == "dvl_failure_south_china_sea_northern_slope"


def test_generic_oilfield_query() -> None:
    kb = KnowledgeBase()
    evidence = kb.execute_typed_query(
        "KNOWLEDGE_QA",
        "介绍一下油田",
        context={},
    )
    assert evidence["query_type"] == "ENVIRONMENT_QUERY"
    assert evidence["found"] is True
    summary = next(item for item in evidence["results"] if item.get("category") == "oil_fields_summary")
    oil_fields = summary["oil_fields"]
    assert len(oil_fields) >= 4
    names = [f["name"] for f in oil_fields]
    assert "流花11-1油田" in names
    assert "陆丰14-8油田" in names
    assert "文昌16-2油田" in names
    assert "陵水17-2气田" in names


def test_dialogue_manager_environment_knowledge_query_dispatch() -> None:
    mock_llm = MagicMock()
    mock_llm.chat.return_value = "流花11-1油田位于珠江口盆地，水深约305米。"
    mock_llm.filter_reply.side_effect = lambda text, *args, **kwargs: text
    dm = DialogueManager(llm=mock_llm)

    plan = InteractionPlan(
        schema_version=1,
        operation="READ",
        dialogue_mode="knowledge_qa",
        query_intent="ENVIRONMENT_QUERY",
        subject_type="environment",
        subject_text="流花11-1油田",
        relation="describe",
        source_policy="project_kb",
        needs_clarification=False,
        clarification_reason=None,
        emergency_action=None,
        confidence=0.95,
        reason_code="ENV_KB_QUERY",
    )
    route = IntentRouteResult(
        interaction_type="QUERY",
        confidence=0.95,
        reason="测试环境知识查询",
        query_intent="ENVIRONMENT_QUERY",
        dialogue_mode="knowledge_qa",
        interaction_plan=plan,
    )

    reply = dm._handle_non_task_route("介绍一下流花11-1油田", route, "req-123")
    assert "流花11-1" in reply
    # 状态不变性保持
    assert dm.phase == "collecting"
    assert dm.slot_store.version == 0


def test_dialogue_manager_environment_knowledge_fallback() -> None:
    dm = DialogueManager(llm=MagicMock())
    kb_evidence = {
        "query_type": "ENVIRONMENT_QUERY",
        "found": True,
        "results": [
            {
                "category": "oil_field_details",
                "oil_field": {
                    "name": "流花11-1油田",
                    "water_depth": 305,
                    "maximum_reference_water_depth": 330,
                    "seabed_type": "soft",
                    "notes": "海床表层以软泥沉积为主。",
                },
            }
        ],
    }
    fallback_text = dm._build_knowledge_fallback(kb_evidence)
    assert "流花11-1油田" in fallback_text
    assert "305" in fallback_text
