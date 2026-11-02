"""Real manager/router regressions for device suitability follow-up questions."""

from copy import deepcopy

import pytest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slots.slot_store import Slot
from tests.interaction_plan_support import ScriptedLLM, make_plan


def make_manager(*, task="tree_valve_operation", depth=305, selected=True, plan=None):
    llm = ScriptedLLM(default_plan=plan or make_plan(
        "READ", query_intent="KNOWLEDGE_QA", subject_type="unknown",
        relation="unknown", source_policy="project_kb",
    ))
    manager = DialogueManager(llm, KnowledgeBase())
    state = {
        "task_type_key": task,
        "task_type": "采油树控制面板插入" if task == "tree_valve_operation" else "管缆巡检",
        "water_depth": depth,
    }
    if selected:
        state.update({
            "equipment_family": "通用工作级深海机器人",
            "equipment_type": "通用工作级深海机器人 250HP",
            "equipment_unit_id": "WROV-250-001",
        })
    for key, value in state.items():
        manager.slot_store.slots[key] = Slot(
            slot_name=key, value_type="number" if key == "water_depth" else "string",
            value=value, status="valid", source="user_input",
        )
    manager.task_state = state
    manager._last_built_json = deepcopy(state)
    return manager


def ask_read_only(manager, message):
    before = deepcopy((manager.slot_store.export_snapshot(), manager.task_state,
                       manager._last_built_json, manager._last_missing, manager.phase))
    reply = manager.process(message)
    after = (manager.slot_store.export_snapshot(), manager.task_state,
             manager._last_built_json, manager._last_missing, manager.phase)
    assert after == before
    assert manager.llm.extract_calls == []
    assert manager.final_result is None
    return reply


@pytest.mark.parametrize("message", [
    "这台机器人为什么适合当前任务？",
    "为什么推荐这台机器人？如果我改用观察级机器人可以吗？先解释一下，不要改任务。",
    "当前选定的设备是否适用于这项作业？",
])
def test_device_suitability_does_not_become_task_catalog(message):
    manager = make_manager()
    reply = ask_read_only(manager, message)
    assert "本系统当前支持以下水下作业任务类型" not in reply
    assert "采油树" in reply
    if "观察级" in message:
        assert "观察级" in reply
        assert "不能" in reply or "不支持" in reply
    else:
        assert "通用工作级" in reply
        assert "305" in reply
        assert "3000" in reply


def test_explicit_incompatible_device_and_task_override_selected_context():
    manager = make_manager(task="pipeline_inspection")
    reply = ask_read_only(manager, "观察级机器人能做采油树阀门操作吗？")
    assert "观察级" in reply and "采油树" in reply
    assert "不能" in reply or "不支持" in reply
    assert "本系统当前支持以下" not in reply


def test_selected_device_does_not_imply_depth_suitability():
    reply = ask_read_only(make_manager(depth=4000), "这台机器人为什么适合当前任务？")
    assert "4000" in reply and "3000" in reply
    assert "超出" in reply


def test_unselected_device_reference_asks_for_device():
    reply = ask_read_only(make_manager(selected=False), "这台机器人为什么适合当前任务？")
    assert "尚未选定" in reply or "请说明" in reply
    assert "本系统当前支持以下" not in reply


def test_unknown_named_device_does_not_fall_back_to_selected_device():
    reply = ask_read_only(make_manager(), "测试未知机器人能做采油树阀门任务吗？")
    assert "未找到" in reply or "请说明" in reply
    assert "通用工作级深海机器人 250HP】具备" not in reply


@pytest.mark.parametrize("message", ["系统支持哪些任务？", "你能做什么工作？", "介绍一下支持的任务类型"])
def test_actual_task_catalog_questions_remain_catalogs(message):
    reply = ask_read_only(make_manager(), message)
    assert "本系统当前支持以下水下作业任务类型" in reply
    assert all(name in reply for name in ("管缆巡检", "管缆埋设", "采油树"))


def test_device_subject_plan_still_reaches_grounded_task_fit():
    manager = make_manager(plan=make_plan(
        "READ", query_intent="DEVICE_CAPABILITY", subject_type="device",
        subject_text="这台机器人", relation="capabilities", source_policy="project_kb",
    ))
    reply = ask_read_only(manager, "说明所选机器人与当前任务的适配依据。")
    assert "通用工作级" in reply and "采油树" in reply
    assert "305" in reply and "3000" in reply


def test_unrelated_device_depth_question_keeps_knowledge_query_path():
    manager = make_manager(plan=make_plan(
        "READ", query_intent="DEVICE_CAPABILITY", subject_type="device",
        subject_text="通用工作级深海机器人 250HP", relation="capabilities",
        source_policy="project_kb",
    ))
    reply = ask_read_only(manager, "这台机器人能否在4000米水深作业？")
    assert "4000" in reply and "无法满足" in reply
    assert "草稿水深 305" not in reply


@pytest.mark.parametrize(("message", "subject_type", "subject_text"), [
    ("观察级机器人能做哪些任务？", "device_class", "观察级机器人"),
    ("这台机器人能做什么任务？", "device", "这台机器人"),
])
def test_device_task_enumeration_is_not_replaced_by_current_task_fit(message, subject_type, subject_text):
    manager = make_manager(plan=make_plan(
        "READ", query_intent="DEVICE_CAPABILITY", subject_type=subject_type,
        subject_text=subject_text, relation="list", source_policy="project_kb",
    ))
    manager.llm.default_reply = "根据设备知识库回答其全部适用任务。"
    reply = ask_read_only(manager, message)
    assert reply == manager.llm.default_reply
    assert manager.llm.chat_calls
    evidence = manager.llm.chat_calls[-1][0]["content"]
    assert "device_class_describe" in evidence or "device_check" in evidence
    if "观察级" in message:
        assert "supported_tasks" in evidence and "管缆巡检" in evidence


def test_tool_question_about_current_task_keeps_tool_query_path():
    manager = make_manager(plan=make_plan(
        "READ", query_intent="TOOL_QUERY", subject_type="payload",
        subject_text="阀门扭矩工具", relation="supports", source_policy="project_kb",
    ))
    manager.llm.default_reply = "查询阀门扭矩工具的配置与用途。"
    reply = ask_read_only(manager, "这台机器人能否携带阀门扭矩工具完成当前任务？")
    assert reply == manager.llm.default_reply
    assert manager.llm.chat_calls
    assert "阀门扭矩工具" in manager.llm.chat_calls[-1][0]["content"]


@pytest.mark.parametrize("plan_relation", ["unknown", "recommend"])
def test_tree_task_device_options_use_current_task_without_selecting(plan_relation):
    manager = make_manager(plan=make_plan(
        "READ", query_intent="KNOWLEDGE_QA", subject_type="unknown",
        relation=plan_relation, source_policy="project_kb",
    ))
    reply = ask_read_only(manager, "这个插入任务适合用哪种机器人，为什么？先介绍一下，不要修改任务参数。")
    assert "通用工作级深海机器人 250HP" in reply
    assert "305" in reply and "3000" in reply
    assert "未找到该设备" not in reply
    assert "水下无人自主航行器" not in reply
    assert "未选择或修改机器人" in reply


@pytest.mark.parametrize("depth,excluded", [(100, []), (1000, ["履带式海底重载作业机器人 1600HP", "拖曳式海底重载作业机器人 1500HP"])])
def test_burial_device_recommendation_filters_capability_and_depth(depth, excluded):
    manager = make_manager(task="pipeline_burial", depth=depth, selected=False)
    manager.task_state["task_type"] = "管缆埋设"
    reply = ask_read_only(manager, f"目前有哪些机器人能完成这个{depth}米水深的管缆埋设任务？你建议选哪一台，为什么？先不要替我选。")
    assert "特种工作级深海机器人" in reply
    assert "通用工作级深海机器人" not in reply
    assert "观察级深海机器人" not in reply
    assert "水下无人自主航行器" not in reply
    assert str(depth) in reply
    for name in excluded:
        assert name not in reply


def test_task_device_options_report_no_candidate_when_depth_is_impossible():
    manager = make_manager(depth=9000, selected=False)
    reply = ask_read_only(manager, "这个任务有哪些机器人可以执行？")
    assert "没有满足静态能力条件的机器人" in reply
    assert "9000" in reply


@pytest.mark.parametrize("verb", ["完成", "执行"])
def test_explicit_unit_can_perform_question_stays_grounded(verb):
    manager = make_manager()
    reply = ask_read_only(manager, f"通用工作级001能{verb}这个拔出任务吗？先说明理由，不要修改任务。")
    assert "通用工作级深海机器人 250HP" in reply
    assert "3000" in reply and "305" in reply
    assert "静态能力适配说明" in reply
    assert "实际执行仍需" in reply
    assert manager.llm.chat_calls == []


@pytest.mark.parametrize('message', [
    '请解释 DVL 定位失锁、浑浊度、海流与水下作业安全的关系，并结合系统中的油田环境、禁入区、载荷和支持船规则说明。只查询知识，不创建或修改任何任务。',
    '请解释支持船和载荷如何影响作业安全，不修改任何任务。',
])
def test_explanations_use_knowledge_synthesis_instead_of_catalog(message):
    manager = make_manager(plan=make_plan('READ', query_intent='KNOWLEDGE_QA',
        subject_type='system_rule', relation='describe', source_policy='project_kb'))
    manager.llm.default_reply = 'DVL 定位、海流、支持船和载荷需要一起校验。'
    assert ask_read_only(manager, message) == manager.llm.default_reply
    assert manager.llm.chat_calls



def test_mixed_project_question_cannot_bypass_evidence_with_general_domain_plan(monkeypatch):
    from unittest.mock import Mock
    manager = make_manager(plan=make_plan('READ', query_intent='KNOWLEDGE_QA',
        subject_type='general_concept', relation='describe', source_policy='general_domain'))
    query = Mock(wraps=manager.kb.execute_typed_query)
    monkeypatch.setattr(manager.kb, 'execute_typed_query', query)
    manager.llm.default_reply = '项目 C010 是 DVL 风险软警告，不能据此声称自动上浮。'
    reply = ask_read_only(manager, '请解释DVL失锁，并结合系统中的油田、载荷和支持船规则说明。')
    assert reply == manager.llm.default_reply
    query.assert_called_once()
    assert query.call_args.args[0] == 'KNOWLEDGE_QA'
    assert query.call_args.kwargs['context']['source_policy'] == 'hybrid'
    prompt = manager.llm.chat_calls[-1][0]['content']
    assert 'constraints_rules' in prompt and 'C010' in prompt
    assert 'severity' in prompt and 'soft' in prompt



def test_specific_payload_requirement_question_reaches_model_answer():
    manager = make_manager(plan=make_plan('READ', query_intent='KNOWLEDGE_QA',
        subject_type='system_rule', relation='describe', source_policy='hybrid'))
    manager.llm.default_reply = '支持的载荷属于候选，不能据此断言任务必须另带电液机械臂。'
    reply = ask_read_only(manager, '管缆巡检必须携带侧扫声呐或成像声呐吗？采油树阀门作业必须另带电液机械臂吗？请结合本系统的任务模板和载荷规则说明，不创建或修改任务。')
    assert manager.llm.default_reply in reply
    assert '不代表已通过全部准入检查' in reply
    assert manager.llm.chat_calls


def test_rule_catalog_uses_configured_names_and_severity_only():
    manager = make_manager(plan=make_plan('READ', query_intent='KNOWLEDGE_QA',
        subject_type='system_rule', relation='list', source_policy='project_kb'))
    reply = ask_read_only(manager, '系统有哪些硬约束和软警告？')
    for rule in manager.kb.constraints:
        if rule.get('enabled', True):
            assert rule['id'] in reply and rule['name'] in reply
    assert reply.index('[C010]') > reply.index('软警告：')
    assert '3.0 节' not in reply and '0.5 米' not in reply and '电力配额' not in reply


def test_payload_requirement_answer_cannot_claim_admission_from_capability_only():
    manager = make_manager(plan=make_plan('READ', query_intent='KNOWLEDGE_QA',
        subject_type='system_rule', relation='describe', source_policy='hybrid'))
    manager.llm.default_reply = '机器人自带多功能液压机械臂。只要具备该能力，即可满足任务准入。'
    reply = ask_read_only(manager, '采油树阀门作业必须另带电液机械臂吗？请结合本系统的规则说明。')
    assert '机器人自带多功能液压机械臂。' in reply
    assert '即可满足任务准入' not in reply
    assert '不代表已通过全部准入检查' in reply
    assert '明确确认' in reply and '执行协议支持' in reply
    assert manager.llm.chat_calls
