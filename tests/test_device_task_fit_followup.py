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
