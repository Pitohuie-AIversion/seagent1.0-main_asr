"""tests/test_environment_knowledge_retriever.py — 海域环境与关联知识库检索测试套件"""

from unittest.mock import MagicMock
import pytest

from src.dialogue_manager import DialogueManager
from src.session.intent_router import IntentRouteResult
from src.session.interaction_plan import InteractionPlan
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
    assert "软泥海床" in fallback_text
    assert "soft" not in fallback_text


def test_grounded_catalog_introductions_no_yaml_packaging() -> None:
    dm = DialogueManager(llm=MagicMock())

    # 1. 油气田列表介绍不暴露 海床：soft 或 物理校验上限
    oilfield_intro = dm._build_grounded_oilfield_catalog_introduction()
    assert "流花11-1油田" in oilfield_intro
    assert "软泥海床" in oilfield_intro
    assert "海床：soft" not in oilfield_intro
    assert "物理校验上限" not in oilfield_intro

    # 2. 任务目录介绍不暴露 (pipeline_inspection) 或 workclass_rov 键名
    task_intro = dm._build_grounded_task_catalog_introduction()
    assert "(pipeline_inspection)" not in task_intro
    assert "workclass_rov" not in task_intro
    assert "适用装备" in task_intro


def test_environment_status_reply_telemetry_translation() -> None:
    from src.knowledge_retriever import format_telemetry_value
    assert format_telemetry_value("high") == "高"
    assert format_telemetry_value("strong") == "强"
    assert format_telemetry_value("available") == "可用"

    dm = DialogueManager(llm=MagicMock())
    state_dict = {
        "obstacle_density": "high",
        "mothership_support": "strong",
        "overall_status": "available",
        "update_timestamp": "2026-09-01T10:00:00+08:00",
        "version": 10,
    }
    reply = dm._build_environment_status_reply("WROV-250-001", state_dict)
    assert "障碍物密度：高" in reply
    assert "母船支援：强" in reply
    assert "机器人状态：可用" in reply
    assert "high" not in reply
    assert "strong" not in reply
    assert "available" not in reply


def test_ordinal_guided_catalog_referential_query() -> None:
    mock_llm = MagicMock()
    mock_llm.chat.return_value = "管缆埋设机器人包含履带式海底重载作业机器人和特种工作级深海机器人。"
    mock_llm.filter_reply.side_effect = lambda text, *args, **kwargs: text

    dm = DialogueManager(llm=mock_llm)
    route = IntentRouteResult(
        interaction_type="QUERY",
        confidence=0.95,
        reason="测试查询",
        query_intent="DEVICE_CAPABILITY",
        dialogue_mode="knowledge_qa",
        interaction_plan=None,
    )

    # 1. 助手呈现机器人清单
    reply1 = dm._handle_non_task_route("当前支持哪些机器人", route, "req-001")
    assert "1. **管缆埋设机器人**" in reply1
    assert len(dm._last_visible_catalog_items) == 4

    # 2. 用户追问 "介绍下第一种"
    reply2 = dm._handle_non_task_route("介绍下第一种", route, "req-002")
    assert "您好，SEAgent 水下多智能体任务决策系统已就绪" not in reply2
    assert ("管缆埋设" in reply2 or "履带" in reply2)


def test_pronoun_context_carryover_and_no_public_identity_overtrigger() -> None:
    mock_llm = MagicMock()
    mock_llm.chat.return_value = "匹配结果说明"
    mock_llm.filter_reply.side_effect = lambda text, *args, **kwargs: text

    dm = DialogueManager(llm=mock_llm)

    # 1. 机器人代词追问 ("它的最大水深是多少")
    r_route = IntentRouteResult(
        interaction_type="QUERY",
        confidence=0.95,
        reason="测试机器人代词追问",
        query_intent="DEVICE_CAPABILITY",
        dialogue_mode="knowledge_qa",
        interaction_plan=None,
    )
    _ = dm._handle_non_task_route("介绍下金牛座一号", r_route, "req-r1")
    assert dm._last_discussed_robot in ("金牛座", "金牛座001", "金牛座一号")

    r_reply2 = dm._handle_non_task_route("它的最大水深是多少？", r_route, "req-r2")
    assert "您好，SEAgent 水下多智能体任务决策系统已就绪" not in r_reply2
    assert "1600" in str(mock_llm.chat.call_args)

    # 2. 油田代词追问 ("它的海床类型是什么")
    o_route = IntentRouteResult(
        interaction_type="QUERY",
        confidence=0.95,
        reason="测试油田代词追问",
        query_intent="ENVIRONMENT_QUERY",
        dialogue_mode="knowledge_qa",
        interaction_plan=None,
    )
    _ = dm._handle_non_task_route("介绍下流花11-1油田", o_route, "req-o1")
    assert dm._last_discussed_oilfield == "流花11-1油田"

    o_reply2 = dm._handle_non_task_route("它的海床类型是什么？", o_route, "req-o2")
    assert "您好，SEAgent 水下多智能体任务决策系统已就绪" not in o_reply2
    assert ("305" in str(mock_llm.chat.call_args) or "soft" in str(mock_llm.chat.call_args) or "软" in str(mock_llm.chat.call_args))




