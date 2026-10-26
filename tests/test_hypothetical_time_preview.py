"""READ time proposals use arithmetic while preserving the saved task evidence."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import ScriptedLLM, make_plan


NOW = datetime(2026, 10, 20, 20, 0, 37, tzinfo=timezone(timedelta(hours=8)))
SAVED = {"start_time": "2026-10-20T22:00:37", "end_time": "2026-10-21T01:00:37"}
WRONG_REPLY = "当前配置下持续6小时，结束时间为6小时后。"


@pytest.fixture(params=[(False, False), (True, False), (True, True)], ids=["legacy", "patch-v2", "norm-v2"])
def manager(request, monkeypatch, tmp_path):
    patch, norm = request.param
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm)
    monkeypatch.setattr("src.temporal.simulated_time.get_current_datetime", lambda: NOW)
    llm = ScriptedLLM(default_plan=make_plan("READ", query_intent="KNOWLEDGE_QA"), default_reply=WRONG_REPLY)
    dm = DialogueManager(llm, KnowledgeBase())
    dm.slot_store.init_task_slots(dm.builder.get_schema("pipeline_inspection", "normal"))
    slots = dm.slot_store.clone_slots()
    for key, value in {"intent_id": "TI2026102001", "task_type_key": "pipeline_inspection",
                       "task_type": "管缆巡检", **SAVED}.items():
        slots[key].value, slots[key].status = value, "valid"
    dm.slot_store.commit_transaction(slots, [], request_id="seed-time-preview")
    dm._rebuild_cache()
    dm.phase = "collecting"
    return dm


def read_without_mutation(dm, message):
    before = copy.deepcopy((dm.slot_store.export_snapshot(), dm.task_state,
                            dm._last_built_json, dm._last_missing, dm.phase, dm.mode))
    reply = dm.process(message)
    assert (dm.slot_store.export_snapshot(), dm.task_state,
            dm._last_built_json, dm._last_missing, dm.phase, dm.mode) == before
    assert dm.llm.extract_calls == []
    assert dm.conversation_history[-1] == {"role": "assistant", "content": reply}
    restored = DialogueManager(ScriptedLLM(), dm.kb)
    restored.load_snapshot(dm.export_snapshot())
    assert restored.slot_store.export_snapshot() == before[0]
    assert restored.task_state == before[1]
    return reply


def test_live_question_computes_eleven_hours_without_changing_saved_three_hours(manager):
    reply = read_without_mutation(manager, "如果改成5小时后开始、持续6小时，会有什么影响？")
    assert "2026-10-21 01:00:37" in reply
    assert "2026-10-21 07:00:37" in reply
    assert "11小时" in reply
    assert "当前已保存" in reply and "3小时" in reply
    assert "假设方案" in reply and "未修改" in reply
    assert WRONG_REPLY not in reply
    assert manager.llm.chat_calls == []


@pytest.mark.parametrize("message,start,end,extra", [
    ("若改成半小时后开始、持续一个半小时，会怎样？",
     "2026-10-20 20:30:37", "2026-10-20 22:00:37", "1小时30分钟"),
    ("假设任务1小时30分钟后开始持续2.5小时会怎样？",
     "2026-10-20 21:30:37", "2026-10-21 00:00:37", "2小时30分钟"),
    ("如果开始时间改为5小时后，会怎样？",
     "2026-10-21 01:00:37", "2026-10-21 04:00:37", "沿用已保存的持续时长"),
    ("如果持续时间改为6小时，会有什么影响？",
     "2026-10-20 22:00:37", "2026-10-21 04:00:37", "沿用已保存的开始时间"),
    ("如果开始时间为明天下午3点，持续2小时，会有什么影响？",
     "2026-10-21 15:00:00", "2026-10-21 17:00:00", "2小时"),
    ("如果持续6小时，5小时后开始，会怎样？",
     "2026-10-21 01:00:37", "2026-10-21 07:00:37", "11小时"),
])
def test_single_proposal_reuses_duration_and_range_arithmetic(manager, message, start, end, extra):
    reply = read_without_mutation(manager, message)
    assert start in reply and end in reply and extra in reply
    assert "假设方案" in reply and "未修改" in reply
    assert "当前已保存" in reply and "3小时" in reply
    assert manager.llm.chat_calls == []


@pytest.mark.parametrize("message", [
    "如果5小时后或6小时后开始、持续6小时，会有什么影响？",
    "如果5至6小时后开始、持续6小时，会有什么影响？",
    "如果任务5小时后开始、任务6小时后开始、持续6小时，会有什么影响？",
    "如果任务5小时后开始、持续6小时、持续7小时，会有什么影响？",
    "如果不要5小时后开始、持续6小时，会有什么影响？",
    "如果摄像机5小时后开始自检、持续6小时，会有什么影响？",
    "如果任务5小时后开始、摄像机持续6小时，会有什么影响？",
    "如果任务5小时后开始、持续6小时、6小时后结束，会有什么影响？",
    "如果开始时间为明天9点、持续6小时，会有什么影响？",
    "如果5小时后开始、持续0小时，会有什么影响？",
])
def test_uncertain_negative_other_subject_and_conflicting_proposals_are_not_guessed(manager, message):
    reply = read_without_mutation(manager, message)
    assert "无法唯一确定" in reply and "未修改" in reply
    assert "当前已保存" in reply and "3小时" in reply
    assert "2026-10-21 07:00:37" not in reply
    assert WRONG_REPLY not in reply
    assert manager.llm.chat_calls == []


def test_write_command_keeps_existing_temporal_commit_path(manager, monkeypatch):
    def unexpected_preview(*args, **kwargs):
        raise AssertionError("WRITE must not invoke a READ time preview")
    monkeypatch.setattr("src.handlers.conversation_router.build_hypothetical_time_reply", unexpected_preview)
    manager.llm.queue_plan(make_plan("WRITE"))
    manager.process("把开始时间改成5小时后，持续6小时。")
    assert manager.task_state["start_time"] == "2026-10-21T01:00:37"
    assert manager.task_state["end_time"] == "2026-10-21T07:00:37"
    assert manager.llm.extract_calls


def test_unrelated_read_keeps_knowledge_answer_path(manager):
    manager.llm.default_reply = "水下导航的一般知识说明。"
    reply = read_without_mutation(manager, "介绍水下导航的一般原理")
    assert reply == manager.llm.default_reply
    assert manager.llm.chat_calls


def test_clarification_plan_does_not_gain_a_computed_proposal(manager, monkeypatch):
    def unexpected_preview(*args, **kwargs):
        raise AssertionError("Only READ may invoke a time preview")
    monkeypatch.setattr("src.handlers.conversation_router.build_hypothetical_time_reply", unexpected_preview)
    manager.llm.queue_plan(make_plan("CLARIFY"))
    reply = read_without_mutation(manager, "如果改成5小时后开始、持续6小时，会有什么影响？")
    assert "假设方案" not in reply
