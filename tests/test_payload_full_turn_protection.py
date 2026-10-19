"""Whole-turn and field-wide payload restrictions precede model proposals."""

import copy

import pytest

from tests.interaction_plan_support import make_plan, slot_candidate
from tests.test_payload_write_grounding_regressions import (
    HYDRAULIC, INITIAL, STEREO, TORQUE, TURBID,
    extraction, mutation, valve,
)


@pytest.mark.parametrize("message", [
    "不要改工具，水深改为320米。",
    "工具不要改，只是讨论删除阀门扭矩工具，水深改为320米。",
    "请先不要修改载荷，删除阀门扭矩工具，水深改为320米。",
    "删除阀门扭矩工具，载荷配置先别改，水深改为320米。",
    "水深改为320米，携带工具暂时不要调整。",
    "水深改为320米，只是讨论删除阀门扭矩工具。",
    "水深改为320米，只讨论新增双目水下成像系统。",
    "只讨论载荷，删除阀门扭矩工具，水深改为320米。",
])
@pytest.mark.parametrize("proposal", ["mutation", "candidate", "empty"])
def test_payload_pause_preserves_metadata_and_independent_depth(valve, message, proposal):
    dm, llm = valve
    before = copy.deepcopy(dm.slot_store.slots["payload"].to_dict())
    candidates = [slot_candidate("water_depth", 320)]
    if proposal == "candidate":
        candidates.append(slot_candidate("payload", [STEREO]))
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction(
        [mutation("remove", [TORQUE])] if proposal == "mutation" else [],
        candidates=candidates,
    ))

    reply = dm.process(message)

    assert dm.slot_store.slots["payload"].to_dict() == before
    assert dm.task_state["payload"] == INITIAL
    assert dm.task_state["water_depth"] == 320
    assert not any("携带工具" in line for line in reply.splitlines() if line.startswith("✅ 已记录："))
    assert "载荷操作失败" not in reply


@pytest.mark.parametrize("message,expected", [
    ("不要删除阀门扭矩工具，新增双目水下成像系统。", INITIAL + [STEREO]),
    ("不要改水深，删除阀门扭矩工具。", [HYDRAULIC, TURBID]),
    ("不要修改任务编号，删除阀门扭矩工具。", [HYDRAULIC, TURBID]),
    ("只是讨论水深，删除阀门扭矩工具。", [HYDRAULIC, TURBID]),
    ("保留阀门扭矩工具并新增双目水下成像系统。", INITIAL + [STEREO]),
    ("只是讨论删除阀门扭矩工具，新增双目水下成像系统。", INITIAL + [STEREO]),
])
def test_other_field_or_single_operation_negation_keeps_payload_consent(valve, message, expected):
    dm, llm = valve
    version = dm.slot_store.slots["payload"].version
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction([mutation("clear")]))

    dm.process(message)

    assert dm.task_state["payload"] == expected
    assert dm.slot_store.slots["payload"].version == version + 1
    assert not dm.slot_store.slots["payload"].validation_error
    assert dm.task_state["water_depth"] == 305


@pytest.mark.parametrize("message", [
    "只是讨论，不要修改任务，删除阀门扭矩工具。",
    "删除阀门扭矩工具，先别修改任务，只是讨论。",
    "确认选择携带工具：双目水下成像系统，先别修改任务。",
    "确认选择携带工具：双目水下成像系统，工具不要改。",
])
def test_direct_normalizer_honors_pause_before_card_or_clause_recovery(valve, message):
    dm, _ = valve
    dm.editing_slot = "payload"
    before = copy.deepcopy(dm.slot_store.slots["payload"].to_dict())
    proposed = extraction([mutation("remove", [TORQUE])], candidates=[slot_candidate("payload", [STEREO])])

    dm._normalize_payload_list_mutations(proposed, message, dm.slot_store.slots)

    assert proposed["list_mutations"] == []
    assert proposed["slot_candidates"] == []
    assert dm.slot_store.slots["payload"].to_dict() == before


@pytest.mark.parametrize("add_independently", [False, True])
def test_discussion_keep_clause_does_not_split_into_authorized_edit(valve, add_independently):
    dm, llm = valve
    before = copy.deepcopy(dm.slot_store.slots["payload"].to_dict())
    message = "水深改为320米，只是讨论保留阀门扭矩工具并删除液压飞线插拔工具"
    if add_independently:
        message += "，新增双目水下成像系统"
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction([mutation("remove", [HYDRAULIC])], candidates=[slot_candidate("water_depth", 320)]))

    dm.process(message + "。")

    assert dm.task_state["payload"] == INITIAL + ([STEREO] if add_independently else [])
    assert dm.task_state["water_depth"] == 320
    if not add_independently:
        assert dm.slot_store.slots["payload"].to_dict() == before
    else:
        assert dm.slot_store.slots["payload"].version == before["version"] + 1
        assert not dm.slot_store.slots["payload"].validation_error


@pytest.mark.parametrize("ending", ["？", "。"])
@pytest.mark.parametrize("proposal", ["mutation", "candidate"])
def test_comma_question_does_not_delete_payload(valve, ending, proposal):
    dm, llm = valve
    before = copy.deepcopy(dm.slot_store.slots["payload"].to_dict())
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction(
        [mutation("remove", [TORQUE])] if proposal == "mutation" else [],
        candidates=[slot_candidate("payload", [STEREO])] if proposal == "candidate" else [],
    ))

    reply = dm.process("删除阀门扭矩工具，可以吗" + ending)

    assert dm.slot_store.slots["payload"].to_dict() == before
    assert not any("携带工具" in line for line in reply.splitlines() if line.startswith("✅ 已记录："))


@pytest.mark.parametrize("message,expected,depth", [
    ("删除阀门扭矩工具，可以吗？新增双目水下成像系统。", INITIAL + [STEREO], 305),
    ("删除阀门扭矩工具，可以吗？水深改为320米。", INITIAL, 320),
    ("水深可以改为320米吗？删除阀门扭矩工具。", [HYDRAULIC, TURBID], 305),
])
def test_question_boundary_keeps_independent_affirmative_edit(valve, message, expected, depth):
    dm, llm = valve
    before = copy.deepcopy(dm.slot_store.slots["payload"].to_dict())
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction(
        [mutation("remove", [TORQUE])],
        candidates=[slot_candidate("water_depth", 320)] if depth == 320 else [],
    ))

    dm.process(message)

    assert dm.task_state["payload"] == expected
    assert dm.task_state["water_depth"] == depth
    if expected == INITIAL:
        assert dm.slot_store.slots["payload"].to_dict() == before
    else:
        assert dm.slot_store.slots["payload"].version == before["version"] + 1
        assert not dm.slot_store.slots["payload"].validation_error
