"""Payload recovery respects withdrawal, historical context and list operations."""

import copy

import pytest

from src.dialogue_manager import DialogueManager
from tests.interaction_plan_support import extraction_result, make_plan, slot_candidate
from tests.test_payload_write_grounding_regressions import (
    HYDRAULIC, INITIAL, STEREO, TORQUE, TURBID, mutation, valve,
)


LASER = "激光标尺"


def proposal(kind, *, depth=False):
    candidates = [slot_candidate("water_depth", 320)] if depth else []
    if kind == "candidate":
        candidates.append(slot_candidate("payload", [LASER]))
    return extraction_result(
        *candidates, list_mutations=[mutation("clear")] if kind == "mutation" else [],
    )


def assert_snapshot_payload(manager, llm, expected):
    restored = DialogueManager(llm=llm, kb=manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert restored.slot_store.slots["payload"].to_dict() == expected


@pytest.mark.parametrize("message", [
    "删除阀门扭矩工具，取消这个修改。",
    "取消载荷修改，删除阀门扭矩工具。",
    "删除阀门扭矩工具，撤销载荷修改。",
    "昨天配备激光标尺。",
    "昨天，配备激光标尺。",
    "此前载荷为阀门扭矩工具。",
    "此前，载荷设置为阀门扭矩工具。",
    "不要配备激光标尺。",
    "配备激光标尺，可以吗？",
    "之前保留阀门扭矩工具并删除液压飞线插拔工具。",
])
@pytest.mark.parametrize("model_proposal", ["empty", "mutation", "candidate"])
def test_nonassertive_payload_preserves_metadata_and_independent_depth(valve, message, model_proposal):
    manager, llm = valve
    before = copy.deepcopy(manager.slot_store.slots["payload"].to_dict())
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(proposal(model_proposal, depth=True))

    manager.process(message + "水深改为320米。")

    assert manager.slot_store.slots["payload"].to_dict() == before
    assert manager.task_state["payload"] == INITIAL
    assert manager.task_state["water_depth"] == 320
    assert_snapshot_payload(manager, llm, before)


@pytest.mark.parametrize("message,expected", [
    ("配备激光标尺。", INITIAL + [LASER]),
    ("装载激光标尺。", INITIAL + [LASER]),
    ("搭载激光标尺。", INITIAL + [LASER]),
    ("载荷设置为双目水下成像系统，添加阀门扭矩工具。", [STEREO, TORQUE]),
    ("载荷改成双目水下成像系统，配备激光标尺。", [STEREO, LASER]),
    ("载荷设置为双目水下成像系统，阀门扭矩工具，配备激光标尺。", [STEREO, TORQUE, LASER]),
    ("昨天删除阀门扭矩工具，新增双目水下成像系统。", INITIAL + [STEREO]),
    ("新增双目水下成像系统，昨天删除阀门扭矩工具。", INITIAL + [STEREO]),
    ("此前，载荷设置为阀门扭矩工具，现在配备激光标尺。", INITIAL + [LASER]),
    ("昨天，配备阀门扭矩工具。现在配备激光标尺。", INITIAL + [LASER]),
    ("此前，载荷设置为阀门扭矩工具，当前配备激光标尺。", INITIAL + [LASER]),
    ("此前，载荷设置为阀门扭矩工具，本次配备激光标尺。", INITIAL + [LASER]),
    ("取消删除阀门扭矩工具，新增双目水下成像系统。", INITIAL + [STEREO]),
    ("删除阀门扭矩工具，取消这个修改，新增双目水下成像系统。", INITIAL + [STEREO]),
])
@pytest.mark.parametrize("model_proposal", ["empty", "mutation", "candidate"])
def test_affirmative_payload_operations_have_one_consistent_interpretation(
    valve, message, expected, model_proposal,
):
    manager, llm = valve
    version = manager.slot_store.slots["payload"].version
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(proposal(model_proposal))

    manager.process(message)

    slot = manager.slot_store.slots["payload"]
    assert manager.task_state["payload"] == expected
    assert slot.status == "valid"
    assert slot.validation_error is None
    assert slot.version == version + 1
    assert_snapshot_payload(manager, llm, slot.to_dict())


def test_cancel_of_another_field_does_not_undo_payload_addition(valve):
    manager, llm = valve
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(proposal("empty"))

    manager.process("配备激光标尺，水深改为320米，取消这个修改。")

    assert manager.task_state["payload"] == INITIAL + [LASER]


def test_replacing_full_list_then_removing_item_keeps_statement_order(valve):
    manager, llm = valve
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(proposal("mutation"))

    manager.process("载荷设置为阀门扭矩工具和双目水下成像系统，删除阀门扭矩工具。")

    assert manager.task_state["payload"] == [STEREO]
    assert HYDRAULIC not in manager.task_state["payload"]
    assert TURBID not in manager.task_state["payload"]


@pytest.mark.parametrize("message,expected", [
    ("带激光标尺。", INITIAL + [LASER]),
    ("配激光标尺。", INITIAL + [LASER]),
    ("不要配激光标尺。", INITIAL),
    ("别配激光标尺。", INITIAL),
    ("带激光标尺吗？", INITIAL),
    ("配激光标尺，可以吗？", INITIAL),
    ("不要带阀门扭矩工具。", [HYDRAULIC, TURBID]),
])
def test_short_payload_commands_keep_prior_recovery_and_negation_semantics(valve, message, expected):
    manager, llm = valve
    before = copy.deepcopy(manager.slot_store.slots["payload"].to_dict())
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(proposal("empty"))

    manager.process(message)

    assert manager.task_state["payload"] == expected
    if expected == INITIAL:
        assert manager.slot_store.slots["payload"].to_dict() == before
    else:
        assert manager.slot_store.slots["payload"].status == "valid"
        assert manager.slot_store.slots["payload"].validation_error is None
