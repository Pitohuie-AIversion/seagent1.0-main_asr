"""模型语义权威第二阶段：仅保留确定性安全底线。"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.dialogue_manager import DialogueManager
from src.extractor import ParameterExtractor
from src.interaction_plan import validate_interaction_plan
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.model_profile import ModelRole
from src.output_builder import OutputBuilder
from src.prompts import RESPONDER_SYSTEM
from src.task_request_guard import analyze_task_request
from src.ui_state_builder import _compute_actions, _compute_read_only
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)


def test_read_plan_tolerates_noncritical_metadata_and_low_confidence() -> None:
    plan = validate_interaction_plan(
        {
            "schema_version": 1,
            "operation": "READ",
            "dialogue_mode": "task_collection",
            "query_intent": "OPEN_ENDED_EXPLANATION",
            "subject_type": "navigation_algorithm",
            "relation": "explain",
            "source_policy": "project_config",
            "confidence": 0.35,
        }
    )

    assert plan.operation == "READ"
    assert plan.dialogue_mode == "knowledge_qa"
    assert plan.query_intent == "KNOWLEDGE_QA"
    assert plan.subject_type == "unknown"
    assert plan.relation == "unknown"
    assert plan.source_policy == "none"
    assert plan.confidence == 0.35


def test_operation_only_plan_derives_safe_defaults() -> None:
    plan = validate_interaction_plan({"operation": "READ"})

    assert plan.operation == "READ"
    assert plan.dialogue_mode == "knowledge_qa"
    assert plan.query_intent == "GENERAL_CHAT"
    assert plan.needs_clarification is False


def test_low_confidence_write_still_requires_clarification() -> None:
    plan = validate_interaction_plan(
        {"operation": "WRITE", "confidence": 0.35}
    )

    assert plan.operation == "CLARIFY"
    assert plan.reason_code == "LOW_CONFIDENCE_CLARIFY"


def test_general_chat_uses_general_reasoning_role() -> None:
    llm = MagicMock(spec=LLMClient)
    llm.chat.return_value = "可以，我们从系统工程角度分析。"
    llm.filter_reply.side_effect = lambda reply, **_: reply
    dm = DialogueManager(llm, KnowledgeBase())

    reply = dm._handle_general_chat(
        "解释一下卡尔曼滤波为什么适合水下导航",
        validate_interaction_plan(make_plan("READ")).to_intent_route_result(),
    )

    assert "系统工程" in reply
    assert llm.chat.call_args.kwargs["role"] == ModelRole.GENERAL_REASONING


def test_general_domain_question_skips_project_kb_dump() -> None:
    llm = MagicMock(spec=LLMClient)
    llm.chat.return_value = "卡尔曼滤波通过预测与观测更新融合状态估计。"
    llm.filter_reply.side_effect = lambda reply, **_: reply
    dm = DialogueManager(llm, KnowledgeBase())
    dm.kb.execute_typed_query = MagicMock(
        side_effect=AssertionError("通用领域问题不应注入项目知识库")
    )
    route = validate_interaction_plan(
        make_plan(
            "READ",
            query_intent="KNOWLEDGE_QA",
            subject_type="general_concept",
            relation="describe",
            source_policy="general_domain",
        )
    ).to_intent_route_result()

    reply = dm._handle_knowledge_query("解释一下卡尔曼滤波", route)

    assert "预测与观测" in reply
    dm.kb.execute_typed_query.assert_not_called()
    assert llm.chat.call_args.kwargs["role"] == ModelRole.GENERAL_REASONING


def test_clarification_returns_the_model_reason() -> None:
    llm = MagicMock(spec=LLMClient)
    dm = DialogueManager(llm, KnowledgeBase())
    route = validate_interaction_plan(
        make_plan(
            "CLARIFY",
            clarification_reason="请确认你是要暂停已发布任务，还是保留任务草稿。",
        )
    ).to_intent_route_result()

    reply = dm._handle_clarification("先停一下", route)

    assert "暂停已发布任务" in reply
    llm.chat.assert_not_called()


def test_fuzzy_candidate_can_be_semantically_mapped_inside_allowed_domain() -> None:
    llm = MagicMock(spec=LLMClient)
    llm.extract_json.side_effect = [
        {
            "slot_candidates": [
                slot_candidate(
                    "equipment_class",
                    "适合长航程自主巡检的那个",
                    raw_value="就用不用系缆、能自己跑远距离的",
                )
            ],
            "list_mutations": [],
            "time_relation": None,
            "unresolved": [],
        },
        {
            "matched": True,
            "canonical_key": "equipment_class",
            "canonical_value": "AUV",
            "confidence": 0.86,
            "reason": "描述对应自主长航程平台",
        },
    ]
    extractor = DialogueManager(llm, KnowledgeBase()).extractor

    result = extractor.extract_updates(
        "就用不用系缆、能自己跑远距离的",
        current_state={"task_type_key": "pipeline_inspection"},
        task_type_key="pipeline_inspection",
        required=[
            {
                "key": "equipment_class",
                "label": "机器人类别",
                "type": "string",
                "allowed_values": ["观察级ROV", "AUV"],
            }
        ],
        conversation_history=[],
    )

    assert result["slot_candidates"][0]["normalized_value"] == "AUV"
    assert result["slot_candidates"][0]["resolution_method"] == "llm_semantic"


def test_fuzzy_recommendation_uses_semantic_choice_not_list_order() -> None:
    llm = ScriptedLLM()
    dm = DialogueManager(llm, KnowledgeBase())
    dm.task_state = {
        "task_type": "管缆巡检",
        "task_type_key": "pipeline_inspection",
    }
    dm._last_missing = [
        {
            "key": "equipment_class",
            "label": "机器人类别",
            "allowed_values": ["观察级ROV", "AUV"],
        }
    ]
    dm.extractor.resolve_allowed_candidate = MagicMock(return_value="AUV")
    route = validate_interaction_plan(
        make_plan(
            "READ",
            query_intent="KNOWLEDGE_QA",
            subject_type="device_class",
            subject_text="适合长航程自主巡检的",
            relation="recommend",
            source_policy="project_kb",
        )
    ).to_intent_route_result()

    reply = dm._build_grounded_recommendation(route)

    assert "明确推荐机器人类别【AUV】" in reply
    assert "建议选择【观察级ROV】" not in reply


def test_exact_planner_recommendation_is_rechecked_against_user_preference() -> None:
    dm = DialogueManager(ScriptedLLM(), KnowledgeBase())
    dm.task_state = {
        "task_type": "管缆巡检",
        "task_type_key": "pipeline_inspection",
    }
    # Runtime missing fields come from OutputBuilder.build(), which intentionally
    # carries only the validation domain rather than the richer semantic catalog.
    dm._last_missing = [
        {
            "key": "equipment_class",
            "label": "机器人类别",
            "type": "string",
            "allowed_values": ["观察级ROV", "AUV"],
        }
    ]
    dm.extractor.resolve_allowed_candidate = MagicMock(return_value="AUV")
    route = validate_interaction_plan(
        make_plan(
            "READ",
            query_intent="KNOWLEDGE_QA",
            subject_type="device_class",
            subject_text="观察级ROV",
            relation="recommend",
            source_policy="project_kb",
        )
    ).to_intent_route_result()

    reply = dm._build_grounded_recommendation(
        route,
        user_message="推荐适合长航程自主巡检的机器人类别",
    )

    assert "明确推荐机器人类别【AUV】" in reply
    resolver_input = dm.extractor.resolve_allowed_candidate.call_args.args[0]
    resolver_field = dm.extractor.resolve_allowed_candidate.call_args.args[2]
    assert "长航程自主巡检" in resolver_input
    assert "观察级ROV" not in resolver_input
    assert resolver_field["allowed_values"] == ["观察级ROV", "AUV"]
    auv_evidence = next(
        item
        for item in resolver_field["candidate_evidence"]
        if item["canonical_value"] == "AUV"
    )
    assert "长航程" in auv_evidence["description"]
    assert "自主" in auv_evidence["description"]


def test_unresolved_multi_candidate_recommendation_does_not_pick_first() -> None:
    dm = DialogueManager(ScriptedLLM(), KnowledgeBase())
    dm.task_state = {"task_type_key": "pipeline_inspection"}
    dm._last_missing = [
        {
            "key": "equipment_class",
            "label": "机器人类别",
            "allowed_values": ["观察级ROV", "AUV"],
        }
    ]
    dm.extractor.resolve_allowed_candidate = MagicMock(return_value=None)
    route = validate_interaction_plan(
        make_plan(
            "READ",
            query_intent="KNOWLEDGE_QA",
            subject_type="device_class",
            subject_text="随便推荐一个",
            relation="recommend",
            source_policy="project_kb",
        )
    ).to_intent_route_result()

    reply = dm._build_grounded_recommendation(route)

    assert "观察级ROV" in reply and "AUV" in reply
    assert "建议选择【观察级ROV】" not in reply
    assert "请补充偏好" in reply


def test_compound_guard_only_blocks_multiple_supported_tasks() -> None:
    supported = KnowledgeBase().get_all_task_type_values()

    intelligent_assistance = analyze_task_request(
        "创建管缆巡检任务，另外帮我选择合适的机器人并解释风险",
        supported,
    )
    multiple_tasks = analyze_task_request(
        "创建管缆巡检，同时安排采油树控制面板插入",
        supported,
    )

    assert intelligent_assistance.should_block is False
    assert multiple_tasks.should_block is True


def test_terminal_task_remains_read_only_but_conversation_can_continue() -> None:
    actions = _compute_actions("done", "task_collection")

    assert actions["can_send"] is True
    assert actions["can_modify"] is False
    assert _compute_read_only("done") is True


def test_terminal_task_write_enters_safe_new_draft_pipeline() -> None:
    llm = ScriptedLLM(
        plans=[make_plan("WRITE")],
        extractions=[
            extraction_result(slot_candidate("water_depth", 300.0))
        ],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    dm.phase = "done"

    reply = dm.process("把水深改成三百米")

    assert "已正式确认发布" in reply
    assert "无法就地修改参数" in reply
    assert dm.phase == "done"


def test_robot_class_candidates_include_authoritative_semantic_evidence() -> None:
    required = OutputBuilder(KnowledgeBase()).get_required(
        "pipeline_inspection",
        task_state={"task_type_key": "pipeline_inspection"},
    )
    class_field = next(item for item in required if item["key"] == "equipment_class")
    auv = next(
        item
        for item in class_field["candidate_evidence"]
        if item["canonical_value"] == "AUV"
    )

    assert "AUV" in auv["aliases"]
    assert "长航程" in auv["description"]
    assert "自主" in auv["description"]


def test_candidate_resolver_treats_unique_preference_evidence_as_sufficient() -> None:
    llm = MagicMock(spec=LLMClient)
    llm.extract_json.return_value = {
        "matched": False,
        "canonical_key": None,
        "canonical_value": None,
        "confidence": 0.5,
        "reason": "test",
    }
    extractor = ParameterExtractor(llm)

    extractor.resolve_allowed_candidate(
        "适合长距离自主巡检",
        "equipment_class",
        {
            "key": "equipment_class",
            "label": "机器人类别",
            "allowed_values": ["观察级ROV", "AUV"],
            "candidate_evidence": [
                {"canonical_value": "观察级ROV", "description": "实时操控"},
                {"canonical_value": "AUV", "description": "长航程自主作业"},
            ],
        },
    )

    system_prompt = llm.extract_json.call_args.args[0][0]["content"]
    assert "某个候选的证据明确覆盖" in system_prompt
    assert "没有提供全部任务参数而拒绝推荐" in system_prompt
    assert "不能生成 allowed_values 之外的值" in system_prompt


def test_generic_rov_acronym_resolves_inside_single_feasible_task_domain() -> None:
    dm = DialogueManager(ScriptedLLM(), KnowledgeBase())
    dm.task_state = {
        "task_type": "管缆巡检",
        "task_type_key": "pipeline_inspection",
    }

    resolved = dict(dm._resolve_project_robot_classes("比较 AUV 和 ROV 的取舍"))

    assert resolved["auv"] == "AUV"
    assert resolved["observation_rov"] == "观察级ROV"


def test_task_responder_must_answer_read_only_part_of_mixed_write() -> None:
    assert "同时包含任务写入和解释、比较或风险咨询" in RESPONDER_SYSTEM
    assert "不得把建议描述成已写入" in RESPONDER_SYSTEM


def test_device_class_comparison_uses_project_evidence_and_llm() -> None:
    llm = MagicMock(spec=LLMClient)
    llm.chat.return_value = "AUV适合长航程自主覆盖；观察级ROV适合实时操控与精细观察。"
    llm.filter_reply.side_effect = lambda reply, **_: reply
    dm = DialogueManager(llm, KnowledgeBase())
    dm.task_state = {
        "task_type": "管缆巡检",
        "task_type_key": "pipeline_inspection",
    }
    route = validate_interaction_plan(
        make_plan(
            "READ",
            query_intent="DEVICE_CAPABILITY",
            subject_type="device_class",
            subject_text="AUV 和 ROV",
            relation="compare",
            source_policy="project_kb",
        )
    ).to_intent_route_result()

    reply = dm._build_grounded_device_class_answer(
        "比较 AUV 和 ROV 做长航程巡检的取舍",
        route,
    )

    assert "长航程" in reply and "实时操控" in reply
    system_prompt = llm.chat.call_args.args[0][0]["content"]
    assert "观察级ROV" in system_prompt
    assert "AUV" in system_prompt
    assert llm.chat.call_args.kwargs["role"] == ModelRole.KNOWLEDGE_QA
