"""Mixed payload commands retain their own question and condition scope."""

import copy

import pytest

from src.dialogue_manager import DialogueManager
from tests.interaction_plan_support import extraction_result, make_plan
from tests.test_payload_write_grounding_regressions import INITIAL, mutation, valve


LASER = "激光标尺"


def process(manager, llm, message, model_proposal):
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction_result(
        list_mutations=[mutation("clear")] if model_proposal == "wrong-clear" else [],
    ))
    return manager.process(message)


def assert_payload_snapshot(manager, llm):
    restored = DialogueManager(llm=llm, kb=manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert restored.slot_store.slots["payload"].to_dict() == manager.slot_store.slots["payload"].to_dict()


@pytest.mark.parametrize("message", [
    "添加激光标尺，这个机器人支持哪些工具？",
    "添加激光标尺，当前水深是多少？",
    "添加激光标尺，这个设备型号是什么？",
    "此前，载荷设置为阀门扭矩工具，现在添加激光标尺，当前水深是多少？",
    "如果需要，删除阀门扭矩工具。现在添加激光标尺。",
    "添加激光标尺，如果需要，删除阀门扭矩工具。",
])
@pytest.mark.parametrize("model_proposal", ["empty", "wrong-clear"])
def test_independent_queries_and_conditions_do_not_erase_affirmative_payload(
    valve, message, model_proposal,
):
    manager, llm = valve
    before_depth = copy.deepcopy(manager.slot_store.slots["water_depth"].to_dict())
    version = manager.slot_store.slots["payload"].version

    process(manager, llm, message, model_proposal)

    slot = manager.slot_store.slots["payload"]
    assert manager.task_state["payload"] == INITIAL + [LASER]
    assert slot.version == version + 1
    assert slot.status == "valid"
    assert slot.validation_error is None
    assert manager.slot_store.slots["water_depth"].to_dict() == before_depth
    assert_payload_snapshot(manager, llm)


@pytest.mark.parametrize("message", [
    "如果需要，删除阀门扭矩工具。",
    "如果机器人支持，添加激光标尺。",
    "如果需要，现在删除阀门扭矩工具。",
    "删除阀门扭矩工具，可以吗？",
    "添加激光标尺，这个机器人支持激光标尺吗？",
    "保留阀门扭矩工具并删除液压飞线插拔工具吗？",
])
@pytest.mark.parametrize("model_proposal", ["empty", "wrong-clear"])
def test_dependent_questions_and_conditional_commands_preserve_payload_metadata(
    valve, message, model_proposal,
):
    manager, llm = valve
    before = copy.deepcopy(manager.slot_store.slots["payload"].to_dict())

    process(manager, llm, message, model_proposal)

    assert manager.slot_store.slots["payload"].to_dict() == before
    assert_payload_snapshot(manager, llm)


@pytest.mark.parametrize("model_proposal", ["empty", "wrong-clear"])
def test_later_conditional_turn_and_other_field_cancellation_preserve_saved_payload(valve, model_proposal):
    manager, llm = valve
    process(manager, llm, "添加激光标尺，水深改为320米，取消这个修改。", model_proposal)
    assert manager.task_state["payload"] == INITIAL + [LASER]
    assert manager.task_state["water_depth"] == 305
    before = copy.deepcopy(manager.slot_store.slots["payload"].to_dict())

    process(manager, llm, "如果需要，删除阀门扭矩工具。", model_proposal)

    assert manager.slot_store.slots["payload"].to_dict() == before
    assert_payload_snapshot(manager, llm)
