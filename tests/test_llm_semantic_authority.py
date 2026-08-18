"""ADR-005：模型语义权威与确定性安全执行边界。"""

from unittest.mock import MagicMock

import src.llm_client as llm_client_module
from src.dialogue_manager import DialogueManager
from src.extractor import EXTRACTION_SYSTEM, ParameterExtractor
from src.intent_router import INTENT_ROUTER_SYSTEM, IntentRouteResult, IntentRouter
from src.interaction_plan import (
    VALID_EMERGENCY_ACTIONS,
    VALID_PENDING_ACTIONS,
    VALID_RELATIONS,
    VALID_WARNING_ACTIONS,
    validate_interaction_plan,
)
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.prompts import build_responder_messages
from src.validator import Violation
from tests.interaction_plan_support import (
    ScriptedLLM,
    empty_extraction,
    extraction_result,
    make_plan,
    slot_candidate,
)


def _plan(operation: str, dialogue_mode: str, confidence: float = 0.9) -> dict:
    return {
        "schema_version": 1,
        "operation": operation,
        "dialogue_mode": dialogue_mode,
        "query_intent": "GENERAL_CHAT" if operation == "READ" else None,
        "subject_type": "task" if operation == "WRITE" else "general_concept",
        "subject_text": None,
        "relation": "filled_fields" if operation == "WRITE" else "describe",
        "source_policy": "session_state" if operation == "WRITE" else "general_domain",
        "needs_clarification": False,
        "clarification_reason": None,
        "emergency_action": None,
        "pending_action": None,
        "warning_action": None,
        "confidence": confidence,
        "reason_code": "TEST_MODEL_PLAN",
    }


def _router_with_model_result(result: dict) -> tuple[IntentRouter, MagicMock]:
    llm = MagicMock(spec=LLMClient)
    llm.classify_interaction.return_value = result
    return IntentRouter(llm), llm


def test_native_router_schema_matches_runtime_emergency_actions() -> None:
    schema_values = set(
        llm_client_module.INTERACTION_PLAN_JSON_SCHEMA["properties"]
        ["emergency_action"]["enum"]
    )
    schema_values.discard(None)
    assert schema_values == VALID_EMERGENCY_ACTIONS


def test_native_router_schema_matches_runtime_pending_actions() -> None:
    schema_values = set(
        llm_client_module.INTERACTION_PLAN_JSON_SCHEMA["properties"]
        ["pending_action"]["enum"]
    )
    schema_values.discard(None)
    assert schema_values == VALID_PENDING_ACTIONS

def test_native_router_schema_matches_runtime_warning_actions() -> None:
    schema_values = set(
        llm_client_module.INTERACTION_PLAN_JSON_SCHEMA["properties"]
        ["warning_action"]["enum"]
    )
    schema_values.discard(None)
    assert schema_values == VALID_WARNING_ACTIONS


def test_recommendation_is_a_first_class_validated_relation() -> None:
    candidate = _plan("READ", "knowledge_qa")
    candidate.update(
        {
            "subject_type": "device_class",
            "subject_text": "观察级ROV",
            "relation": "recommend",
            "source_policy": "project_kb",
        }
    )

    plan = validate_interaction_plan(candidate)

    assert "recommend" in VALID_RELATIONS
    assert plan.operation == "READ"
    assert plan.relation == "recommend"


def test_router_receives_typed_allowed_options_for_pending_fields() -> None:
    router, llm = _router_with_model_result(
        {
            **_plan("READ", "knowledge_qa"),
            "subject_type": "device_class",
            "subject_text": "观察级ROV",
            "relation": "recommend",
            "source_policy": "project_kb",
        }
    )

    router.route(
        "从合法机器人类别中推荐一个，但先不要修改任务",
        conversation_history=[],
        task_state={"task_type_key": "pipeline_inspection"},
        phase="collecting",
        expected_slots=["equipment_class"],
        expected_slot_options=[
            {
                "key": "equipment_class",
                "label": "机器人类别",
                "allowed_values": ["观察级ROV", "AUV"],
            }
        ],
    )

    prompt = llm.classify_interaction.call_args.args[0][-1]["content"]
    assert '"expected_slot_options"' in prompt
    assert '"equipment_class"' in prompt
    assert '"观察级ROV"' in prompt
    assert '"AUV"' in prompt



def test_pending_action_is_a_validated_write_side_effect() -> None:
    candidate = _plan("WRITE", "task_collection")
    candidate["pending_action"] = "reject"
    assert validate_interaction_plan(candidate).pending_action == "reject"

    invalid = _plan("READ", "knowledge_qa")
    invalid["pending_action"] = "reject"
    validated = validate_interaction_plan(invalid)
    assert validated.operation == "READ"
    assert validated.pending_action is None

def test_warning_action_is_a_validated_write_side_effect() -> None:
    candidate = _plan("WRITE", "task_collection")
    candidate["warning_action"] = "acknowledge"
    assert validate_interaction_plan(candidate).warning_action == "acknowledge"

    invalid = _plan("READ", "knowledge_qa")
    invalid["warning_action"] = "acknowledge"
    validated = validate_interaction_plan(invalid)
    assert validated.operation == "READ"
    assert validated.warning_action is None


def test_blocked_soft_uses_model_warning_action_without_phrase_gate() -> None:
    llm = ScriptedLLM(
        plans=[make_plan("WRITE", warning_action="acknowledge")],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    dm.phase = "blocked_soft"
    before = dm.slot_store.export_snapshot()

    dm._handle_soft_warning_confirmation = MagicMock(
        return_value="已记录软警告确认，任务尚未发布。"
    )
    reply = dm.process("我理解你刚才说的定位风险，就按当前方案接着办吧")

    dm._handle_soft_warning_confirmation.assert_called_once()
    assert reply == "已记录软警告确认，任务尚未发布。"
    # 警告动作必须经过一次无候选抽取，防止真实模型把字段更新误标成 acknowledge。
    assert len(llm.extract_calls) == 1
    assert dm.slot_store.export_snapshot() == before


def test_blocked_soft_deterministic_ignore_warning_fast_path() -> None:
    """当大模型路由输出 warning_action: null 时，用户显式输入‘忽略警告’依然能成功触发软警告记录。"""
    llm = ScriptedLLM(
        plans=[make_plan("WRITE", warning_action=None)],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    dm.phase = "blocked_soft"

    dm._handle_soft_warning_confirmation = MagicMock(
        return_value="已记录软警告确认，任务尚未发布。"
    )
    reply = dm.process("忽略警告")

    dm._handle_soft_warning_confirmation.assert_called_once()
    assert reply == "已记录软警告确认，任务尚未发布。"


def test_blocked_hard_rejects_ignore_warning() -> None:
    """在 blocked_hard 阶段，用户输入‘忽略警告’应被坚决拒绝，阻止硬阻断绕过。"""
    llm = ScriptedLLM(
        plans=[make_plan("WRITE", warning_action="acknowledge")],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    dm.phase = "blocked_hard"

    reply = dm.process("忽略警告")

    assert "硬性约束不能通过确认或忽略警告绕过" in reply
    assert dm.phase == "blocked_hard"


def test_state_timestamp_whitelist_preserved_across_slot_updates() -> None:
    """验证 check_type='state_timestamp' (如 C019) 警告在同一环境观察值下，跨槽位补充时不会因 task_version 跳变而再次跳出。"""
    from src.validator import Violation
    from src.slot_store import ValidationAcknowledgement

    llm = ScriptedLLM()
    dm = DialogueManager(llm, KnowledgeBase())
    v = Violation(
        constraint_id="C019",
        constraint_name="环境信息已过期",
        message="环境信息最后更新于 2026-08-12T16:00:09+08:00，已超出有效时间范围",
        severity="soft",
        related_fields=[],
        check_type="state_timestamp",
        observed_value="2026-08-12T16:00:09+08:00",
    )

    ack = ValidationAcknowledgement(
        constraint_id="C019",
        acknowledged_at="2026-08-13T12:00:00",
        task_version=1,
        validation_version=1,
        validation_fingerprint="old_fp",
        status_ref="",
        state_version=0,
        field="",
        value="2026-08-12T16:00:09+08:00",
    )
    dm.slot_store.validation_acknowledgements.append(ack)

    # 此时假设任务更新了其他槽位，task_version 从 1 变成了 2
    mock_res = MagicMock()
    mock_res.task_version = 2
    mock_res.validation_version = 2
    mock_res.validation_fingerprint = "new_fp"
    mock_res.state_snapshot = {}
    dm.slot_store.validation_result = mock_res

    assert dm._is_whitelisted(v) is True




def test_warning_action_cannot_preempt_valid_task_update() -> None:
    """错误的警告动作不得吞掉同轮可验证的字段更新。"""
    llm = ScriptedLLM(
        plans=[make_plan("WRITE", warning_action="acknowledge")],
        extractions=[
            extraction_result(
                slot_candidate("water_depth", 500.0, raw_value="五百米")
            )
        ],
        replies=["已更新水深参数。"],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    schema = dm.builder.get_schema("pipeline_inspection", "normal")
    dm.slot_store.init_task_slots(schema)
    slots, unresolved, version = dm.slot_store.snapshot()
    slots["task_type"].value = "管缆巡检"
    slots["task_type"].status = "valid"
    slots["task_type_key"].value = "pipeline_inspection"
    slots["task_type_key"].status = "valid"
    dm.slot_store.commit_transaction(
        slots,
        unresolved,
        expected_version=version,
    )
    dm.task_state = dm.slot_store.get_task_state()
    dm.phase = "blocked_soft"
    version_before = dm.slot_store.version
    dm._handle_soft_warning_confirmation = MagicMock(
        return_value="不应处理警告确认"
    )

    reply = dm.process("继续补充任务参数")

    assert len(llm.extract_calls) == 1
    assert dm.slot_store.version == version_before + 1
    assert dm.slot_store.get_task_state()["water_depth"] == 500.0
    dm._handle_soft_warning_confirmation.assert_not_called()
    assert "水深（米）：500.0" in reply
    assert "警告都已经处理完成" not in reply


def test_completed_update_that_resolves_soft_block_enters_confirmation() -> None:
    llm = ScriptedLLM(
        plans=[make_plan("WRITE")],
        extractions=[
            extraction_result(
                slot_candidate("water_depth", 120.0, raw_value="一百二十米")
            )
        ],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    schema = dm.builder.get_schema("pipeline_inspection", "normal")
    dm.slot_store.init_task_slots(schema)
    slots, unresolved, version = dm.slot_store.snapshot()
    complete_values = {
        "task_type": "管缆巡检",
        "task_type_key": "pipeline_inspection",
        "start_time": "2026-08-13T10:00:00",
        "end_time": "2026-08-13T15:00:00",
        "cable_type": "海底油气管道",
        "start_point": {"lat": 17.6, "lon": 111.0},
        "end_point": {"lat": 17.7, "lon": 111.1},
        "water_depth": 100.0,
        "equipment_class": "observation_rov",
        "equipment_family": "观察级深海机器人",
        "equipment_type": "观察级深海机器人 75HP",
        "equipment_unit_id": "OBSROV-75-001",
        "payload": ["腐蚀检测探头"],
        "support_vessel": "海洋石油681",
    }
    for key, value in complete_values.items():
        slots[key].value = value
        slots[key].status = "valid"
    dm.slot_store.commit_transaction(slots, unresolved, expected_version=version)
    dm.task_state = dm.slot_store.get_task_state()
    dm.phase = "blocked_soft"
    dm._run_constraint_check = MagicMock(
        side_effect=lambda changed, *args, **kwargs: (
            setattr(dm, "phase", "collecting")
            or {"type": "none", "violations": [], "hard_refusal_counts": {}}
        )
    )

    reply = dm.process("水深调整为一百二十米")

    assert dm.phase == "confirming"
    assert dm.slot_store.get_task_state()["water_depth"] == 120.0
    assert "所有必填字段已收集完成" in reply


def test_soft_warnings_without_robot_snapshot_can_be_acknowledged_once() -> None:
    """时间/位置软警告不依赖机器人遥测，空 snapshot 也必须可确认。"""
    llm = ScriptedLLM(
        plans=[make_plan("WRITE", warning_action="acknowledge")],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    dm.task_state.update(
        {
            "task_type": "管缆巡检",
            "task_type_key": "pipeline_inspection",
            # 使用确定性的久远过去时间，避免其他时间测试修改全局模拟时钟后
            # 让 C030 是否触发依赖执行顺序。
            "start_time": "2000-01-01T19:50:42",
            "end_time": "2000-01-01T23:50:42",
            "start_point": {"lat": 20.5, "lon": 113.0},
            "end_point": {"lat": 19.8, "lon": 113.6},
        }
    )
    validation = dm._refresh_validation(purpose="interactive")
    assert validation.state_snapshot is None
    warning_ids = {
        violation.constraint_id
        for violation in validation.violations
        if violation.severity == "soft"
    }
    assert {"C030", "C010"}.issubset(warning_ids)
    dm.phase = "blocked_soft"
    dm._blocking_violations = [
        violation
        for violation in validation.violations
        if violation.severity == "soft"
    ]

    reply = dm.process("风险我已知晓，按当前方案继续")

    assert dm.phase != "blocked_soft"
    assert "仍有未确认的软警告" not in reply
    current = dm.slot_store.validation_result
    valid_ack_ids = {
        acknowledgement.constraint_id
        for acknowledgement in dm._get_valid_acknowledgements(current)
    }
    assert warning_ids <= valid_ack_ids
    assert len(llm.extract_calls) == 1


def test_blocked_hard_rejects_model_warning_acknowledgement() -> None:
    llm = ScriptedLLM(
        plans=[make_plan("WRITE", warning_action="acknowledge")],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    dm.phase = "blocked_hard"

    reply = dm.process("风险我都接受，继续执行")

    assert dm.phase == "blocked_hard"
    assert "约束" in reply and "不能" in reply
    assert llm.extract_calls == []



def test_model_write_is_not_vetoed_by_missing_keywords() -> None:
    result = validate_interaction_plan(
        _plan("WRITE", "task_collection"),
        user_message="那就照你刚才说的做",
        context={"expected_slots": [], "task_state": {}},
    )

    assert result.operation == "WRITE"


def test_explicit_task_phrase_still_uses_model_semantics() -> None:
    router, llm = _router_with_model_result(_plan("WRITE", "task_collection"))

    result = router.route(
        "创建一个管缆巡检任务",
        conversation_history=[],
        task_state={},
        phase="collecting",
    )

    assert llm.classify_interaction.call_count == 1
    llm.extract_json.assert_not_called()
    assert result.interaction_type == "WRITE"


def test_model_read_is_not_overridden_by_expected_slots() -> None:
    router, _ = _router_with_model_result(_plan("READ", "knowledge_qa"))

    result = router.route(
        "今天天气不错啊",
        conversation_history=[],
        task_state={"task_type_key": "pipeline_inspection"},
        phase="collecting",
        expected_slots=["water_depth"],
    )

    assert result.interaction_type == "QUERY"
    assert result.dialogue_mode == "knowledge_qa"


def test_llm_failure_clarifies_instead_of_guessing_write() -> None:
    llm = MagicMock(spec=LLMClient)
    llm.classify_interaction.side_effect = RuntimeError("router unavailable")
    router = IntentRouter(llm)

    result = router.route(
        "水深改成五百米",
        conversation_history=[],
        task_state={"task_type_key": "pipeline_inspection"},
        phase="collecting",
        expected_slots=["water_depth"],
    )

    assert result.interaction_type == "QUERY"
    assert result.query_intent == "CLARIFICATION"


def test_missing_operation_is_not_inferred_from_task_context() -> None:
    candidate = _plan("READ", "knowledge_qa")
    candidate.pop("operation")

    result = validate_interaction_plan(
        candidate,
        user_message="随便聊两句吧",
        context={"has_task": True, "expected_slots": ["water_depth"]},
    )

    assert result.operation == "CLARIFY"


def test_low_confidence_read_remains_safe_and_side_effect_free() -> None:
    result = validate_interaction_plan(
        _plan("READ", "knowledge_qa", confidence=0.4),
        user_message="介绍一下机器人",
    )

    assert result.operation == "READ"
    assert result.confidence == 0.4


def test_generate_json_enables_native_vllm_json_constraint(monkeypatch) -> None:
    captured: dict = {}

    class FakeStructuredOutputsParams:
        def __init__(self, *, json_object: bool | None = None, json: dict | None = None) -> None:
            self.json_object = json_object
            self.json = json

    class FakeSamplingParams:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    tokenizer = MagicMock()
    tokenizer.apply_chat_template.return_value = "prompt"
    engine = MagicMock()
    engine.generate.return_value = [
        MagicMock(outputs=[MagicMock(text='{"operation":"READ"}')])
    ]
    monkeypatch.setattr(llm_client_module, "SamplingParams", FakeSamplingParams)
    monkeypatch.setattr(
        llm_client_module,
        "StructuredOutputsParams",
        FakeStructuredOutputsParams,
    )
    client = LLMClient(engine, tokenizer)

    result = client.generate_json([{"role": "user", "content": "route"}])

    assert result == {"operation": "READ"}
    assert captured["structured_outputs"].json_object is True
    assert captured["temperature"] == 0.1

    captured.clear()
    engine.generate.return_value = [
        MagicMock(
            outputs=[
                MagicMock(text='{"slot_candidates":[],"list_mutations":[],"unresolved":["unknown"]}')
            ]
        )
    ]
    extraction = client.extract_slots([{"role": "user", "content": "extract"}])
    assert extraction["unresolved"] == ["unknown"]
    assert captured["structured_outputs"].json == llm_client_module.SLOT_EXTRACTION_JSON_SCHEMA


def test_read_plan_is_read_only_in_dialogue_manager() -> None:
    router, llm = _router_with_model_result(_plan("READ", "knowledge_qa"))
    llm.chat.return_value = "我们可以继续聊这个问题。"
    llm.filter_reply.side_effect = lambda text, *args, **kwargs: text
    dm = DialogueManager(llm, KnowledgeBase())
    dm.intent_router = router
    before_version = dm.slot_store.version
    before_snapshot = dm.slot_store.export_snapshot()
    dm.extractor.extract_updates = MagicMock()

    reply = dm.process("有任务草稿时也聊聊天气")

    assert reply
    dm.extractor.extract_updates.assert_not_called()
    assert dm.slot_store.version == before_version
    assert dm.slot_store.export_snapshot() == before_snapshot


def test_generic_knowledge_query_with_device_subject_uses_variant_evidence() -> None:
    kb = KnowledgeBase()

    evidence = kb.execute_typed_query(
        "KNOWLEDGE_QA",
        "给我介绍一下它",
        context={
            "subject_type": "device",
            "subject_text": "观察级深海机器人 75HP",
            "relation": "describe",
        },
    )

    assert evidence["requested_query_type"] == "KNOWLEDGE_QA"
    assert evidence["query_type"] == "DEVICE_CAPABILITY"
    assert evidence["matched_entity"] == "variant:observation_rov_75hp"
    assert [item["full_name"] for item in evidence["results"]] == [
        "观察级深海机器人 75HP"
    ]


def test_device_alias_in_generic_read_is_grounded_without_phrase_routing() -> None:
    kb = KnowledgeBase()

    for message in (
        "先别改任务，给我介绍一下观察级深海机器人 75HP。",
        "能说说观察级深海机器人 75HP 吗？",
    ):
        evidence = kb.execute_typed_query("KNOWLEDGE_QA", message, context={})

        assert evidence["query_type"] == "DEVICE_CAPABILITY"
        assert evidence["matched_entity"] == "variant:observation_rov_75hp"
        assert evidence["results"][0]["full_name"] == "观察级深海机器人 75HP"


def test_realtime_device_status_is_not_rewritten_as_static_capability() -> None:
    evidence = KnowledgeBase().execute_typed_query(
        "KNOWLEDGE_QA",
        "观察级深海机器人 75HP 现在状态怎么样？",
        context={
            "subject_type": "device",
            "subject_text": "观察级深海机器人 75HP",
            "relation": "status",
            "source_policy": "realtime_state",
        },
    )

    assert evidence["query_type"] == "KNOWLEDGE_QA"
    assert evidence["requested_query_type"] == "KNOWLEDGE_QA"


def test_structured_non_device_subject_keeps_general_knowledge_evidence() -> None:
    evidence = KnowledgeBase().execute_typed_query(
        "KNOWLEDGE_QA",
        "观察级深海机器人 75HP 为什么不能绕过硬约束？",
        context={
            "subject_type": "system_rule",
            "subject_text": "硬约束",
            "relation": "procedure",
            "source_policy": "project_kb",
        },
    )

    assert evidence["query_type"] == "KNOWLEDGE_QA"
    assert any(
        item.get("category") == "constraints_rules"
        for item in evidence["results"]
    )


def test_dialogue_manager_passes_structured_read_subject_to_retriever() -> None:
    llm = ScriptedLLM(
        plans=[
            make_plan(
                "READ",
                query_intent="KNOWLEDGE_QA",
                subject_type="device",
                subject_text="观察级深海机器人 75HP",
                relation="describe",
                source_policy="project_kb",
            )
        ],
        replies=["观察级深海机器人 75HP 的最大作业水深为 600 米。"],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    before = dm.slot_store.export_snapshot()
    version = dm.slot_store.version

    reply = dm.process("给我介绍一下它")

    system_prompt = llm.chat_calls[0][0]["content"]
    assert '"query_type": "DEVICE_CAPABILITY"' in system_prompt
    assert '"matched_entity": "variant:observation_rov_75hp"' in system_prompt
    assert "观察级深海机器人 75HP" in reply
    assert dm.slot_store.version == version
    assert dm.slot_store.export_snapshot() == before


def test_build_responder_messages_filters_internal_metadata_slots() -> None:
    built_json = {
        "task_type": "采油树控制面板插入",
        "task_type_key": "tree_valve_operation",
        "water_depth": 100.0,
        "oilfield_name": "流花11-1油田",
        "oilfield_match_evidence": ["名称包含匹配“流花11-1油田”"],
        "oilfield_match_candidates": [{"id": "liuhua_11_1", "name": "流花11-1油田", "evidence": ["拼音匹配"]}],
        "raw_oilfield_name": "流花",
        "oilfield_match_status": "accepted",
        "oilfield_match_confidence": 0.82,
    }
    messages = build_responder_messages(
        task_state=built_json,
        built_json=built_json,
        missing_fields=[],
        mode="normal",
        phase="confirming",
        knowledge_context="",
        constraint_context={"type": "none"},
        conversation_history=[],
        latest_user_message="确认发布",
        ROV2type={},
        support_task=["采油树控制面板插入"],
    )
    system_content = messages[0]["content"]
    assert "oilfield_name" in system_content
    assert "oilfield_match_evidence" not in system_content
    assert "oilfield_match_candidates" not in system_content
    assert "raw_oilfield_name" not in system_content
    assert "oilfield_match_status" not in system_content
    assert "oilfield_match_confidence" not in system_content



def test_empty_commit_result_is_explicitly_grounded_for_responder() -> None:
    messages = build_responder_messages(
        task_state={"task_type_key": "pipeline_inspection"},
        built_json={"task_type_key": "pipeline_inspection"},
        missing_fields=[],
        mode="normal",
        phase="collecting",
        knowledge_context="",
        constraint_context={"type": "none"},
        conversation_history=[],
        latest_user_message="水深改成五百米",
        ROV2type={},
        support_task=["管缆巡检"],
        accepted_updates={},
        unresolved_inputs=["水深改成五百米"],
    )

    turn_message = messages[-1]["content"]
    assert "已提交字段更新：\n{}" in turn_message
    assert "未解析内容" in turn_message
    assert "水深改成五百米" in turn_message
    assert "本轮未写入任何字段" in turn_message


def test_write_responder_keeps_original_mixed_question_and_commit_result() -> None:
    messages = build_responder_messages(
        task_state={"task_type_key": "pipeline_inspection", "water_depth": 500.0},
        built_json={"task_type_key": "pipeline_inspection", "water_depth": 500.0},
        missing_fields=[],
        mode="normal",
        phase="collecting",
        knowledge_context="",
        constraint_context={"type": "none"},
        conversation_history=[],
        latest_user_message="水深改成五百米，顺便说明风险",
        ROV2type={},
        support_task=["管缆巡检"],
        accepted_updates={"water_depth": 500.0},
        unresolved_inputs=[],
    )

    turn_message = messages[-1]["content"]
    assert "【用户本轮原始请求】" in turn_message
    assert "顺便说明风险" in turn_message
    assert "【本轮后端处理结果】" in turn_message
    assert '"water_depth": 500.0' in turn_message


def test_model_cannot_claim_success_when_no_update_was_committed() -> None:
    reply = DialogueManager._ground_write_reply(
        "水深已经设置为五百米。",
        accepted_updates={},
        unresolved_inputs=["水深超出允许范围"],
    )

    # accepted_updates 为空时，应退化为兼底模式，不得原样输出 LLM 谎称成功的语句
    assert "已经设置" not in reply
    assert "未写入任务状态" in reply
    assert "水深超出允许范围" in reply


def test_partial_commit_reply_is_derived_from_committed_fields_only() -> None:
    reply = DialogueManager._ground_write_reply(
        "水深和支持船均已设置，任务参数已完整。",
        accepted_updates={"support_vessel": "海洋石油681"},
        unresolved_inputs=["水深：超出允许范围"],
        missing_fields=[{"key": "water_depth", "label": "水深（米）"}],
    )

    # 新格式：字段摘要使用 “label：value” 而非 “label=value”
    assert "支持船编号：海洋石油681" in reply
    # LLM 回复保留，但既然 accepted_updates 非空，主体语句应在回复中
    assert "水深：超出允许范围" in reply
    assert "仍需补充：水深（米）" in reply


def test_write_with_no_candidates_cannot_return_model_success_claim() -> None:
    llm = ScriptedLLM(
        plans=[make_plan("WRITE")],
        extractions=[empty_extraction()],
        replies=["任务已创建，指令已经下发。"],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    before = dm.slot_store.export_snapshot()
    version = dm.slot_store.version

    reply = dm.process("按上下文继续处理")

    assert dm.slot_store.version == version
    assert dm.slot_store.export_snapshot() == before
    assert "已创建" not in reply
    assert "已下发" not in reply
    assert "未写入任务状态" in reply


def test_invalid_water_depth_candidate_does_not_replace_valid_fact() -> None:
    llm = ScriptedLLM(
        plans=[make_plan("WRITE")],
        extractions=[
            extraction_result(
                slot_candidate(
                    "water_depth",
                    -500.0,
                    raw_value="负五百米",
                )
            )
        ],
        replies=["水深已经设置为负五百米。"],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    schema = dm.builder.get_schema("pipeline_inspection", "normal")
    dm.slot_store.init_task_slots(schema)
    slots, unresolved, version = dm.slot_store.snapshot()
    slots["task_type"].value = "管缆巡检"
    slots["task_type"].status = "valid"
    slots["task_type_key"].value = "pipeline_inspection"
    slots["task_type_key"].status = "valid"
    slots["water_depth"].value = 500.0
    slots["water_depth"].status = "valid"
    dm.slot_store.commit_transaction(slots, unresolved, expected_version=version)
    dm.task_state = dm.slot_store.get_task_state()

    reply = dm.process("使用一个不合法的水深候选")

    water_depth = dm.slot_store.slots["water_depth"]
    assert water_depth.value == 500.0
    assert water_depth.candidate_value == -500.0
    assert water_depth.status == "conflict"
    assert "大于 0" in water_depth.validation_error
    assert "water_depth" not in dm.slot_store.get_task_state()
    assert "已经设置" not in reply
    assert "未写入任务状态" in reply
    assert "大于 0" in reply


def test_extractor_empty_candidates_are_not_reparsed_by_regex() -> None:
    llm = MagicMock(spec=LLMClient)
    llm.extract_json.return_value = {
        "slot_candidates": [],
        "list_mutations": [],
        "unresolved": [],
    }
    extractor = ParameterExtractor(llm)

    result = extractor.extract_updates(
        "深度别太大，三百米吧",
        current_state={"task_type_key": "pipeline_inspection"},
        task_type_key="pipeline_inspection",
        required=[
            {
                "key": "water_depth",
                "label": "水深",
                "type": "number",
            }
        ],
        conversation_history=[],
    )

    assert result["slot_candidates"] == []


def test_extractor_budget_can_return_a_complete_multi_field_turn() -> None:
    """完整任务可能包含十余候选，结构化输出预算不能沿用短分类预算。"""
    llm = MagicMock(spec=LLMClient)
    llm.extract_json.return_value = empty_extraction()
    extractor = ParameterExtractor(llm)

    extractor.extract_updates(
        "一次提供时间、区域、水深、设备、载荷和支持船",
        current_state={"task_type_key": "pipeline_inspection"},
        task_type_key="pipeline_inspection",
        required=[{"key": "water_depth", "label": "水深", "type": "number"}],
        conversation_history=[],
    )

    assert llm.extract_json.call_args.kwargs["max_tokens"] >= 1600


def test_warning_side_effect_probe_allows_empty_without_hiding_field_updates() -> None:
    llm = MagicMock(spec=LLMClient)
    llm.extract_json.return_value = empty_extraction()
    extractor = ParameterExtractor(llm)

    extractor.extract_updates(
        "按当前风险继续",
        current_state={"task_type_key": "pipeline_inspection"},
        task_type_key="pipeline_inspection",
        required=[{"key": "water_depth", "label": "水深", "type": "number"}],
        conversation_history=[],
        allow_empty_for_side_effect=True,
    )

    prompt = llm.extract_json.call_args.args[0][0]["content"]
    assert "仍须优先抽取最新用户消息中的全部任务字段" in prompt
    assert "若确实没有任何任务字段" in prompt
    assert "允许返回空 slot_candidates" in prompt


def test_extractor_always_receives_recent_context() -> None:
    llm = MagicMock(spec=LLMClient)
    llm.extract_json.return_value = {
        "slot_candidates": [],
        "list_mutations": [],
        "unresolved": [],
    }
    extractor = ParameterExtractor(llm)
    history = [
        {"role": "assistant", "content": "我建议选第二台机器人。"},
        {"role": "user", "content": "为什么？"},
        {"role": "assistant", "content": "它的能力更匹配当前任务。"},
    ]

    extractor.extract_updates(
        "那就照你刚才说的做",
        current_state={"task_type_key": "pipeline_inspection"},
        task_type_key="pipeline_inspection",
        required=[],
        conversation_history=history,
    )

    messages = llm.extract_json.call_args.args[0]
    assert messages[1:-1] == history


def test_recommendation_question_and_acceptance_have_distinct_model_contracts() -> None:
    """推荐问答保持只读；随后接受才授权从最近助手建议生成候选。"""
    assert "询问推荐本身属于 READ" in INTENT_ROUTER_SYSTEM
    assert "接受上一轮助手明确给出的单一推荐才属于 WRITE" in INTENT_ROUTER_SYSTEM
    assert "用户本轮明确接受上一轮助手给出的单一推荐" in EXTRACTION_SYSTEM
    assert "可以从紧邻的上一条 assistant 消息复制被接受的推荐值" in EXTRACTION_SYSTEM
    assert "最新用户消息是本轮候选值的唯一文本来源" not in EXTRACTION_SYSTEM


def test_confirmed_assistant_recommendation_commits_after_schema_validation() -> None:
    """确认推荐仍必须走字段白名单、标准值归一化和 SlotStore 事务。"""
    llm = ScriptedLLM(
        plans=[make_plan("WRITE")],
        extractions=[
            extraction_result(
                slot_candidate(
                    "equipment_class",
                    "观察级ROV",
                    raw_key="上一轮设备推荐",
                    raw_value="确认",
                )
            )
        ],
        replies=["已按刚才的推荐选择观察级ROV。"],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    schema = dm.builder.get_schema("pipeline_inspection", "normal")
    dm.slot_store.init_task_slots(schema)
    slots, unresolved, version = dm.slot_store.snapshot()
    slots["task_type"].value = "管缆巡检"
    slots["task_type"].status = "valid"
    slots["task_type_key"].value = "pipeline_inspection"
    slots["task_type_key"].status = "valid"
    dm.slot_store.commit_transaction(
        slots,
        unresolved,
        expected_version=version,
    )
    dm.task_state = dm.slot_store.get_task_state()
    dm.conversation_history.extend(
        [
            {"role": "user", "content": "观察级ROV和AUV哪个更合适？"},
            {
                "role": "assistant",
                "content": "根据当前任务能力约束，我明确推荐观察级ROV。",
            },
        ]
    )
    version_before = dm.slot_store.version

    reply = dm.process("确认")

    assert dm.slot_store.version == version_before + 1
    assert dm.slot_store.get_task_state()["equipment_class"] == "observation_rov"
    assert "观察级ROV" in reply
    assert len(llm.extract_calls) == 1
    extraction_messages = llm.extract_calls[0]
    assert extraction_messages[-2]["role"] == "assistant"
    assert "明确推荐观察级ROV" in extraction_messages[-2]["content"]
    assert extraction_messages[-1] == {"role": "user", "content": "确认"}


def _seed_pipeline_inspection_task(dm: DialogueManager) -> None:
    schema = dm.builder.get_schema("pipeline_inspection", "normal")
    dm.slot_store.init_task_slots(schema)
    slots, unresolved, version = dm.slot_store.snapshot()
    slots["task_type"].value = "管缆巡检"
    slots["task_type"].status = "valid"
    slots["task_type_key"].value = "pipeline_inspection"
    slots["task_type_key"].status = "valid"
    dm.slot_store.commit_transaction(slots, unresolved, expected_version=version)
    dm._rebuild_cache(commit_derived=False)


def test_grounded_class_recommendation_is_single_read_only_candidate() -> None:
    llm = ScriptedLLM(
        plans=[
            make_plan(
                "READ",
                query_intent="DEVICE_CAPABILITY",
                subject_type="device_class",
                subject_text="观察级ROV",
                relation="recommend",
                source_policy="project_kb",
            )
        ],
        replies=["不应调用自由回答模型"],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    _seed_pipeline_inspection_task(dm)
    before_version = dm.slot_store.version
    before_snapshot = dm.slot_store.export_snapshot()

    reply = dm.process("从合法机器人类别中明确只推荐一个，但先不要修改任务")

    assert "明确推荐机器人类别【观察级ROV】" in reply
    assert "本轮仅提供建议，尚未写入任务" in reply
    assert "轻型工作级深海机器人" not in reply
    assert "AUV" not in reply
    assert llm.chat_calls == []
    assert dm.slot_store.version == before_version
    assert dm.slot_store.export_snapshot() == before_snapshot


def test_accepting_class_recommendation_cannot_write_family_or_variant() -> None:
    llm = ScriptedLLM(
        plans=[
            make_plan(
                "READ",
                query_intent="DEVICE_CAPABILITY",
                subject_type="device_class",
                subject_text="观察级ROV",
                relation="recommend",
                source_policy="project_kb",
            ),
            make_plan(
                "WRITE",
                subject_type="device_class",
                subject_text="观察级ROV",
                relation="recommend",
                source_policy="project_kb",
            ),
        ],
        extractions=[
            extraction_result(
                slot_candidate("equipment_class", "观察级ROV", raw_value="确认"),
                slot_candidate(
                    "equipment_family",
                    "轻型工作级深海机器人",
                    raw_value="确认",
                ),
                slot_candidate(
                    "equipment_type",
                    "轻型工作级深海机器人 150HP",
                    raw_value="确认",
                ),
            )
        ],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    _seed_pipeline_inspection_task(dm)

    recommendation = dm.process("请只推荐一个合法机器人类别，不要修改任务")
    version_before = dm.slot_store.version
    reply = dm.process("那就按你刚才推荐的选")
    state = dm.slot_store.get_task_state()

    assert "明确推荐机器人类别【观察级ROV】" in recommendation
    assert state["equipment_class"] == "observation_rov"
    assert state.get("equipment_family") is None
    assert state.get("equipment_type") is None
    assert dm.slot_store.version == version_before + 1
    assert dm.phase != "blocked_hard"
    assert "机器人类别：观察级ROV" in reply


def test_device_class_comparison_uses_project_configuration_only() -> None:
    llm = ScriptedLLM(
        plans=[
            make_plan(
                "READ",
                query_intent="DEVICE_CAPABILITY",
                subject_type="device_class",
                subject_text="观察级ROV和AUV",
                relation="compare",
                source_policy="project_kb",
            )
        ],
        replies=["不应调用自由回答模型"],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    before = dm.slot_store.export_snapshot()

    reply = dm.process("观察级ROV和AUV分别适合什么任务？只回答，不创建任务。")

    assert "观察级ROV" in reply
    assert "AUV" in reply
    assert "管缆巡检" in reply
    assert "依据项目配置" in reply
    assert "通用工程常识" not in reply
    assert llm.chat_calls == []
    assert dm.slot_store.export_snapshot() == before


def test_natural_write_phrase_commits_model_extracted_value() -> None:
    llm = MagicMock(spec=LLMClient)
    llm.classify_interaction.return_value = _plan("WRITE", "task_collection")
    llm.extract_json.return_value = {
        "slot_candidates": [
            {
                "raw_key": "深度",
                "canonical_key": "water_depth",
                "raw_value": "三百米",
                "normalized_value": 300.0,
                "confidence": 0.95,
            }
        ],
        "list_mutations": [],
        "unresolved": [],
    }
    llm.chat.return_value = "已根据提交结果继续规划。"
    llm.filter_reply.side_effect = lambda text, *args, **kwargs: text
    dm = DialogueManager(llm, KnowledgeBase())
    schema = dm.builder.get_schema("pipeline_inspection", "normal")
    dm.slot_store.init_task_slots(schema)
    slots, unresolved, version = dm.slot_store.snapshot()
    slots["task_type"].value = "管缆巡检"
    slots["task_type"].status = "valid"
    slots["task_type_key"].value = "pipeline_inspection"
    slots["task_type_key"].status = "valid"
    dm.slot_store.commit_transaction(
        slots,
        unresolved,
        expected_version=version,
    )
    dm.task_state = dm.slot_store.get_task_state()

    dm.process("深度别太大，三百米吧")

    assert dm.slot_store.get_task_state()["water_depth"] == 300.0
    route_messages = llm.classify_interaction.call_args.args[0]
    assert "expected_slots" in route_messages[-1]["content"]


def test_dedicated_temporal_model_recovers_omitted_duration_relation() -> None:
    llm = MagicMock(spec=LLMClient)
    llm.extract_json.return_value = {
        "slot_candidates": [
            slot_candidate(
                "start_time",
                "2026-08-13T14:00:00+08:00",
                raw_key="开始时间",
                raw_value="下午两点",
            )
        ],
        "list_mutations": [],
        "time_relation": None,
        "unresolved": [],
    }
    llm.extract_temporal_relation.return_value = {
        "has_duration": True,
        "duration_seconds": 18000,
        "raw_text": "持续五小时",
        "confidence": 0.96,
    }
    extractor = ParameterExtractor(llm)

    result = extractor.extract_updates(
        "下午两点开始，持续五小时。",
        current_state={"task_type_key": "pipeline_inspection"},
        task_type_key="pipeline_inspection",
        required=[
            {"key": "start_time", "type": "datetime"},
            {"key": "end_time", "type": "datetime"},
        ],
    )

    candidates = {item["canonical_key"]: item for item in result["slot_candidates"]}
    assert candidates["end_time"]["normalized_value"] == "2026-08-13T19:00:00+08:00"
    llm.extract_temporal_relation.assert_called_once()


def test_duration_relation_derives_end_time_without_text_parsing() -> None:
    llm = ScriptedLLM(
        extractions=[
            {
                "slot_candidates": [
                    slot_candidate(
                        "start_time",
                        "2026-08-13T14:00:00+08:00",
                        raw_key="开始时间",
                        raw_value="下午两点",
                    )
                ],
                "list_mutations": [],
                "time_relation": {
                    "duration_seconds": 18000,
                    "raw_text": "五小时左右",
                    "confidence": 0.95,
                },
                "unresolved": [],
            }
        ]
    )
    extractor = ParameterExtractor(llm)

    result = extractor.extract_updates(
        "时间定在下午两点，五小时左右。",
        current_state={"task_type_key": "pipeline_inspection"},
        task_type_key="pipeline_inspection",
        required=[
            {"key": "start_time", "type": "datetime"},
            {"key": "end_time", "type": "datetime"},
        ],
    )

    candidates = {
        item["canonical_key"]: item
        for item in result["slot_candidates"]
    }
    assert candidates["start_time"]["normalized_value"] == "2026-08-13T14:00:00+08:00"
    assert candidates["end_time"]["normalized_value"] == "2026-08-13T19:00:00+08:00"
    assert candidates["end_time"]["resolution_method"] == "duration_arithmetic"
    assert result["unresolved"] == []


def test_duration_relation_without_start_time_is_unresolved() -> None:
    llm = ScriptedLLM(
        extractions=[
            {
                "slot_candidates": [],
                "list_mutations": [],
                "time_relation": {
                    "duration_seconds": 18000,
                    "raw_text": "持续五小时",
                    "confidence": 0.95,
                },
                "unresolved": [],
            }
        ]
    )
    extractor = ParameterExtractor(llm)

    result = extractor.extract_updates(
        "持续五小时。",
        current_state={"task_type_key": "pipeline_inspection"},
        task_type_key="pipeline_inspection",
        required=[{"key": "end_time", "type": "datetime"}],
    )

    assert result["slot_candidates"] == []
    assert result["unresolved"] == ["持续五小时：缺少开始时间，无法计算结束时间。"]


def test_rov_description_uses_structured_model_list_without_task_keyword_filter() -> None:
    llm = MagicMock(spec=LLMClient)
    llm.extract_json.return_value = ["rov-2"]
    extractor = ParameterExtractor(llm)
    rovs = [
        {
            "model": "rov-1",
            "full_name": "一号",
            "category": "observation",
            "max_depth_m": 300,
            "brief": "观察",
        },
        {
            "model": "rov-2",
            "full_name": "二号",
            "category": "work",
            "max_depth_m": 1000,
            "brief": "作业",
        },
    ]

    result = extractor.resolve_rov_description("选第二台", rovs, "arbitrary_task")

    assert result == [rovs[1]]
    kwargs = llm.extract_json.call_args.kwargs
    assert kwargs["json_schema"]["items"]["enum"] == ["rov-1", "rov-2"]
    prompt = llm.extract_json.call_args.args[0][0]["content"]
    assert "arbitrary_task" not in prompt


def test_invalid_control_protocol_demotes_to_read_only_clarification() -> None:
    route = IntentRouteResult(
        interaction_type="QUERY",
        confidence=0.9,
        reason="invalid model action",
        dialogue_mode="emergency_intervention",
        emergency_action="destroy",
    )

    assert route.dialogue_mode == "knowledge_qa"
    assert route.query_intent == "CLARIFICATION"
    assert route.emergency_action is None


def test_ground_write_reply_deduplicates_existing_recorded_summary() -> None:
    # 当大模型已经在回复中输出了 ✅ 已记录 和 仍需补充 时，不应重复追加
    model_reply = (
        "收到，已确认您计划使用观察级 ROV执行本次任务。\n"
        "✅ 已记录：机器人类别：观察级 ROV。\n"
        "仍需补充：作业机器人系列、作业设备型号。"
    )
    reply = DialogueManager._ground_write_reply(
        model_reply,
        accepted_updates={"equipment_class": "observation_rov"},
        unresolved_inputs=[],
        missing_fields=[
            {"key": "equipment_family", "label": "作业机器人系列"},
            {"key": "equipment_type", "label": "作业设备型号"},
        ],
        display_updates={"equipment_class": "观察级 ROV"},
    )
    # 验证没有出现两次 ✅ 已记录 或 两次 仍需补充
    assert reply.count("✅ 已记录") == 1
    assert reply.count("仍需补充") == 1


def test_ensure_constraint_details_deduplicates_paraphrased_warning() -> None:
    dm = DialogueManager(MagicMock(), KnowledgeBase())
    violation = Violation(
        constraint_id="C010",
        constraint_name="DVL底锁失效高风险",
        message="当前区域DVL底锁失效风险高，定位/导航能力可能不稳定。谨慎依赖DVL进行悬浮或精确定位。",
        severity="soft",
        related_fields=["start_point"],
    )
    constraint_context = {"type": "soft", "violations": [violation]}

    # 大模型回复中包含空格和自然语言格式的软警告
    model_reply = (
        "系统检测到以下环境风险：\n"
        "- DVL 底锁失效高风险：当前区域 DVL 底锁失效风险高，定位/导航能力可能不稳定。"
    )
    result = dm._ensure_constraint_details(model_reply, constraint_context)
    # 不应再在末尾追加重复的 [C010] 警告块
    assert result == model_reply


def test_duration_relation_corrects_chinese_two_and_half_hours() -> None:
    # 模拟大模型将“两个半小时”误换算为 5400 秒 (1.5小时)
    llm = ScriptedLLM(
        extractions=[
            {
                "slot_candidates": [
                    slot_candidate(
                        "start_time",
                        "2026-08-14T17:30:00",
                        raw_key="开始时间",
                        raw_value="下午五点半",
                    ),
                    # 模拟模型自行心算出错生成了 19:00:00
                    slot_candidate(
                        "end_time",
                        "2026-08-14T19:00:00",
                        raw_key="结束时间",
                        raw_value="晚上七点",
                    ),
                ],
                "list_mutations": [],
                "time_relation": {
                    "duration_seconds": 5400,  # 模型误算
                    "raw_text": "持续两个半小时",
                    "confidence": 0.95,
                },
                "unresolved": [],
            }
        ]
    )
    extractor = ParameterExtractor(llm)

    result = extractor.extract_updates(
        "任务今天下午五点半开始 持续两个半小时",
        current_state={"task_type_key": "pipeline_inspection"},
        task_type_key="pipeline_inspection",
        required=[
            {"key": "start_time", "type": "datetime"},
            {"key": "end_time", "type": "datetime"},
        ],
    )

    candidates = {
        item["canonical_key"]: item
        for item in result["slot_candidates"]
    }
    assert candidates["start_time"]["normalized_value"] == "2026-08-14T17:30:00"
    # 规则解析纠偏：两个半小时应为 2.5h (9000s)，17:30 + 2.5h = 20:00:00
    assert candidates["end_time"]["normalized_value"] == "2026-08-14T20:00:00"
    assert candidates["end_time"]["resolution_method"] == "duration_arithmetic"
    assert result["unresolved"] == []


def test_cross_day_end_time_auto_correction() -> None:
    from src.simulated_time import get_simulated_time
    from datetime import datetime
    from zoneinfo import ZoneInfo
    get_simulated_time().set_current_time(datetime(2026, 8, 18, 10, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    try:
        # 模拟模型输出了今晚23:00到凌晨02:00，但模型将 end_time 的日期写成了同日 2026-08-18 (小于 start_time)
        llm = ScriptedLLM(
            extractions=[
                {
                    "slot_candidates": [
                        slot_candidate(
                            "start_time",
                            "2026-08-18T23:00:00",
                            raw_key="开始时间",
                            raw_value="晚上11点",
                        ),
                        slot_candidate(
                            "end_time",
                            "2026-08-18T02:00:00",  # 模型误算填成了同日
                            raw_key="结束时间",
                            raw_value="凌晨2点",
                        ),
                    ],
                    "list_mutations": [],
                    "time_relation": None,
                    "unresolved": [],
                }
            ]
        )
        extractor = ParameterExtractor(llm)

        result = extractor.extract_updates(
            "今晚11点开始，凌晨2点结束",
            current_state={"task_type_key": "pipeline_inspection"},
            task_type_key="pipeline_inspection",
            required=[
                {"key": "start_time", "type": "datetime"},
                {"key": "end_time", "type": "datetime"},
            ],
        )

        candidates = {
            item["canonical_key"]: item
            for item in result["slot_candidates"]
        }
        assert candidates["start_time"]["normalized_value"] == "2026-08-18T23:00:00"
        # Python 时间库自动识别跨夜并对 end_time 增加 1 天：2026-08-19T02:00:00
        assert candidates["end_time"]["normalized_value"] == "2026-08-19T02:00:00"
        assert candidates["end_time"]["resolution_method"] == "cross_day_auto_corrected"
    finally:
        get_simulated_time().reset()


def test_change_start_time_inherits_duration() -> None:
    from src.simulated_time import get_simulated_time
    from datetime import datetime
    from zoneinfo import ZoneInfo
    get_simulated_time().set_current_time(datetime(2026, 8, 18, 10, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    try:
        # 场景：旧状态 start_time 10:00，end_time 12:00 (隐式持续2小时)
        # 本轮修改 start_time 为晚上 23点，且未提供 end_time (或说明持续时间不变)
        llm = ScriptedLLM(
            extractions=[
                {
                    "slot_candidates": [
                        slot_candidate(
                            "start_time",
                            "2026-08-18T23:00:00",
                            raw_key="开始时间",
                            raw_value="晚上11点",
                        ),
                    ],
                    "list_mutations": [],
                    "time_relation": {
                        "has_duration": True,
                        "duration_seconds": None,
                        "raw_text": "持续时间不变",
                        "confidence": 0.95,
                    },
                    "unresolved": [],
                }
            ]
        )
        extractor = ParameterExtractor(llm)

        current_state = {
            "task_type_key": "pipeline_inspection",
            "start_time": "2026-08-18T10:00:00",
            "end_time": "2026-08-18T12:00:00",  # 原时长 2 小时
        }

        result = extractor.extract_updates(
            "修改开始时间为晚上11点，持续时间不变",
            current_state=current_state,
            task_type_key="pipeline_inspection",
            required=[
                {"key": "start_time", "type": "datetime"},
                {"key": "end_time", "type": "datetime"},
            ],
        )

        candidates = {
            item["canonical_key"]: item
            for item in result["slot_candidates"]
        }
        assert candidates["start_time"]["normalized_value"] == "2026-08-18T23:00:00"
        # Python 时间库自动继承 2 小时时长，并以新的 start_time 加算得出跨天的 2026-08-19T01:00:00
        assert candidates["end_time"]["normalized_value"] == "2026-08-19T01:00:00"
        assert candidates["end_time"]["resolution_method"] == "duration_arithmetic"
    finally:
        get_simulated_time().reset()


def test_change_start_time_inherits_duration_after_many_turns() -> None:
    from src.simulated_time import get_simulated_time
    from datetime import datetime
    from zoneinfo import ZoneInfo
    get_simulated_time().set_current_time(datetime(2026, 8, 18, 10, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    try:
        # 模拟经过了 10 轮 (20 条消息) 的无关对话后，旧的对话历史中完全没有 10:00 和 2小时 的文字
        irrelevant_history = []
        for i in range(10):
            irrelevant_history.append({"role": "user", "content": f"咨询问题 {i}"})
            irrelevant_history.append({"role": "assistant", "content": f"回答问题 {i}"})

        llm = ScriptedLLM(
            extractions=[
                {
                    "slot_candidates": [
                        slot_candidate(
                            "start_time",
                            "2026-08-18T23:00:00",
                            raw_key="开始时间",
                            raw_value="晚上11点",
                        ),
                    ],
                    "list_mutations": [],
                    "time_relation": None,  # 即使模型没有返回 time_relation
                    "unresolved": [],
                }
            ]
        )
        extractor = ParameterExtractor(llm)

        current_state = {
            "task_type_key": "pipeline_inspection",
            "start_time": "2026-08-18T10:00:00",
            "end_time": "2026-08-18T12:00:00",  # 旧数据库槽位里保存了 2 小时时长
        }

        result = extractor.extract_updates(
            "把开始时间改成晚上11点",
            current_state=current_state,
            task_type_key="pipeline_inspection",
            required=[
                {"key": "start_time", "type": "datetime"},
                {"key": "end_time", "type": "datetime"},
            ],
            conversation_history=irrelevant_history,  # 20 条无关对话
        )

        candidates = {
            item["canonical_key"]: item
            for item in result["slot_candidates"]
        }
        assert candidates["start_time"]["normalized_value"] == "2026-08-18T23:00:00"
        # 后端 Python 代码从 current_state 自动继承 2 小时，算出 2026-08-19T01:00:00！
        assert candidates["end_time"]["normalized_value"] == "2026-08-19T01:00:00"
        assert candidates["end_time"]["resolution_method"] == "duration_arithmetic"
    finally:
        get_simulated_time().reset()


def test_august_31_explicit_date_and_duration() -> None:
    # 场景：用户说“任务从8月31号早上6点开始，任务持续8个小时”
    # 模拟大模型在 start_time 误选了当前日期 (2026-08-18)，但 raw_value 保留了 "8月31号早上6点"
    llm = ScriptedLLM(
        extractions=[
            {
                "slot_candidates": [
                    slot_candidate(
                        "start_time",
                        "2026-08-18T06:00:00",  # 模型误选了当天
                        raw_key="开始时间",
                        raw_value="8月31号早上6点",
                    ),
                ],
                "list_mutations": [],
                "time_relation": {
                    "has_duration": True,
                    "duration_seconds": 28800,  # 8小时
                    "raw_text": "持续8个小时",
                    "confidence": 0.95,
                },
                "unresolved": [],
            }
        ]
    )
    extractor = ParameterExtractor(llm)

    result = extractor.extract_updates(
        "任务从8月31号早上6点开始，任务持续8个小时",
        current_state={"task_type_key": "pipeline_inspection"},
        task_type_key="pipeline_inspection",
        required=[
            {"key": "start_time", "type": "datetime"},
            {"key": "end_time", "type": "datetime"},
        ],
    )

    candidates = {
        item["canonical_key"]: item
        for item in result["slot_candidates"]
    }
    # 1. 验证 Python 后端相对/绝对时间解析器覆盖模型误选，确定性算出 2026-08-31T06:00:00
    assert candidates["start_time"]["normalized_value"] == "2026-08-31T06:00:00"
    assert candidates["start_time"]["resolution_method"] == "relative_date_parsed"
    # 2. 验证 Python 后端根据 2026-08-31T06:00:00 + 8小时得出 2026-08-31T14:00:00
    assert candidates["end_time"]["normalized_value"] == "2026-08-31T14:00:00"
    assert candidates["end_time"]["resolution_method"] == "duration_arithmetic"


def test_august_31_truncated_raw_value_recovers_from_full_user_message() -> None:
    # 场景：大模型 LLM 将 raw_value 截断为 "早上6点"（丢失了 8月31号），但用户原话为 "任务从8月31号早上6点开始，任务持续12个小时"
    llm = ScriptedLLM(
        extractions=[
            {
                "slot_candidates": [
                    slot_candidate(
                        "start_time",
                        "2026-08-18T06:00:00",  # 模型误算当天
                        raw_key="开始时间",
                        raw_value="早上6点",  # 截断的 raw_value
                    ),
                ],
                "list_mutations": [],
                "time_relation": {
                    "has_duration": True,
                    "duration_seconds": 43200,  # 12小时
                    "raw_text": "持续12个小时",
                    "confidence": 0.95,
                },
                "unresolved": [],
            }
        ]
    )
    extractor = ParameterExtractor(llm)

    result = extractor.extract_updates(
        "任务从8月31号早上6点开始，任务持续12个小时",
        current_state={"task_type_key": "pipeline_inspection"},
        task_type_key="pipeline_inspection",
        required=[
            {"key": "start_time", "type": "datetime"},
            {"key": "end_time", "type": "datetime"},
        ],
    )

    candidates = {
        item["canonical_key"]: item
        for item in result["slot_candidates"]
    }
    # 1. 验证即使 LLM 给出的 raw_value 只有 "早上6点"，后端依然从整句中救出 "8月31号"，得出 2026-08-31T06:00:00
    assert candidates["start_time"]["normalized_value"] == "2026-08-31T06:00:00"
    # 2. 验证 06:00 + 12h 确定性计算得 2026-08-31T18:00:00
    assert candidates["end_time"]["normalized_value"] == "2026-08-31T18:00:00"





