"""Task time withdrawals and ranges use the scope of their own statement."""

import copy
from datetime import datetime

import pytest

from src.dialogue_manager import DialogueManager
from tests.interaction_plan_support import extraction_result, slot_candidate
from tests.test_explicit_time_field_recovery import extract, times
from tests.test_temporal_scope_followup import STATE, manager_factory


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(
        "src.temporal.simulated_time.get_current_datetime",
        lambda: datetime(2026, 10, 20, 9),
    )


WITHDRAWALS = [
    "开始时间改为2026年10月21日10:00，取消这个修改，水深是否320米。",
    "开始时间改为2026年10月21日10:00，取消开始时间修改，水深是否320米。",
    "水深是否320米，开始时间改为2026年10月21日10:00，撤销这个修改。",
    "明天上午8点到10点，取消这个修改。",
    "任务时间为明天上午10点到下午2点，撤销这个修改。",
]

CURRENT_EDITS = [
    (
        "昨天，开始时间改为2026年10月21日10:00，取消这个修改。"
        "现在开始时间改为2026年10月21日11:00。",
        {"start_time": "2026-10-21T11:00:00", "end_time": "2026-10-21T14:00:00"},
    ),
    (
        "若有需要，取消开始时间修改。现在开始时间改为2026年10月21日10:00。",
        {"start_time": "2026-10-21T10:00:00", "end_time": "2026-10-21T13:00:00"},
    ),
    (
        "取消开始时间修改，可以吗？现在开始时间改为2026年10月21日10:00。",
        {"start_time": "2026-10-21T10:00:00", "end_time": "2026-10-21T13:00:00"},
    ),
]

RANGES = [
    "不要改水深，任务时间为明天上午10点到下午2点。",
    "任务时间为明天上午10点到下午2点，水深是多少？",
    "水深是多少？任务时间为明天上午10点到下午2点。",
    "明天上午10点到下午2点，水深改为320米，取消这个修改。",
]
RANGE_TIMES = {"start_time": "2026-10-21T10:00:00", "end_time": "2026-10-21T14:00:00"}


def proposed_times(model_has_value):
    return [
        slot_candidate("start_time", "2026-10-21T10:00:00", raw_value="2026年10月21日10:00"),
        slot_candidate("end_time", "2026-10-21T13:00:00", raw_value="2026年10月21日10:00"),
    ] if model_has_value else []


@pytest.mark.parametrize("message", WITHDRAWALS)
@pytest.mark.parametrize("model_has_value", [False, True])
def test_other_field_question_cannot_disable_time_withdrawal(message, model_has_value):
    result = extract(message, proposed_times(model_has_value), state=STATE)
    assert times(result) == {}
    assert result["unresolved"] == []


@pytest.mark.parametrize("message,expected", CURRENT_EDITS)
def test_nonasserted_withdrawal_cannot_cancel_current_instruction(message, expected):
    result = extract(message, state=STATE)
    assert times(result) == expected
    assert result["unresolved"] == []


@pytest.mark.parametrize("message", RANGES)
def test_range_is_not_lost_to_other_field_negation_or_question(message):
    result = extract(message, state=STATE)
    assert times(result) == RANGE_TIMES
    assert result["unresolved"] == []


@pytest.mark.parametrize("message", [
    "任务时间为明天上午10点到下午2点，可以吗？",
    "任务时间为明天上午10点到下午2点？",
    "昨天，任务时间为明天上午10点到下午2点。",
    "若有需要，任务时间为明天上午10点到下午2点。",
    "不要把任务时间改为明天上午10点到下午2点。",
])
@pytest.mark.parametrize("model_has_value", [False, True])
def test_rejected_range_cannot_be_restored_by_model_candidates(message, model_has_value):
    candidates = [
        slot_candidate("start_time", RANGE_TIMES["start_time"], raw_value="明天上午10点"),
        slot_candidate("end_time", RANGE_TIMES["end_time"], raw_value="下午2点"),
    ] if model_has_value else []
    result = extract(message, candidates, state=STATE)
    assert times(result) == {}
    assert result["unresolved"] == []


@pytest.mark.parametrize("message", WITHDRAWALS)
@pytest.mark.parametrize("model_has_value", [False, True])
def test_withdrawal_preserves_slot_metadata_while_other_field_can_change(
    manager_factory, message, model_has_value,
):
    manager, llm = manager_factory()
    before = {key: copy.deepcopy(manager.slot_store.slots[key].to_dict()) for key in STATE}
    llm.queue_extraction(extraction_result(*proposed_times(model_has_value)))

    manager.process(message + "水深改为320米。")

    assert {key: manager.slot_store.slots[key].to_dict() for key in STATE} == before
    assert manager.task_state["water_depth"] == 320
    restored = DialogueManager(llm, manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert {key: restored.slot_store.slots[key].to_dict() for key in STATE} == before


@pytest.mark.parametrize("message,expected", CURRENT_EDITS + [(message, RANGE_TIMES) for message in RANGES])
def test_current_time_edit_survives_full_commit_and_snapshot(manager_factory, message, expected):
    manager, llm = manager_factory()
    llm.queue_extraction(extraction_result())

    manager.process(message)

    assert {key: manager.task_state[key] for key in STATE} == expected
    assert all(manager.slot_store.slots[key].status == "valid" for key in STATE)
    restored = DialogueManager(llm, manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert restored.slot_store.get_task_state() == manager.slot_store.get_task_state()
