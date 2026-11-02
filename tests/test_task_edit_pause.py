"""An explicit task-wide pause cannot be overridden by a model WRITE plan."""

import copy

import pytest

from tests.interaction_plan_support import make_plan, slot_candidate
from tests.test_payload_write_grounding_regressions import (
    TORQUE, extraction, mutation, valve,
)


@pytest.mark.parametrize("message", [
    "不要修改任务，水深改为320米，删除阀门扭矩工具。",
    "水深改为320米，删除阀门扭矩工具。先别修改。",
    "只是讨论，水深改为320米，删除阀门扭矩工具。",
    "不要修改任何任务参数，水深改为320米。",
])
def test_task_wide_pause_preserves_all_slots_and_phase(valve, message):
    manager, llm = valve
    before = copy.deepcopy(manager.slot_store.export_snapshot())
    before_state = copy.deepcopy(manager.task_state)
    before_phase = manager.phase
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction(
        [mutation("remove", [TORQUE])],
        candidates=[slot_candidate("water_depth", 320)],
    ))

    reply = manager.process(message)

    assert manager.slot_store.export_snapshot() == before
    assert manager.task_state == before_state
    assert manager.phase == before_phase
    assert "已删除" not in reply and "已更新" not in reply


@pytest.mark.parametrize("message", [
    "不要修改任务编号，水深改为320米。",
    "不要改时间，水深改为320米。",
    "先不发布，水深改为320米。",
])
def test_field_or_publication_pause_still_allows_independent_edits(valve, message):
    manager, llm = valve
    before = copy.deepcopy(manager.task_state)
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction(candidates=[slot_candidate("water_depth", 320)]))

    manager.process(message)

    # A first accepted write may also initialize derived identity/selection metadata.
    for key, value in {**before, "water_depth": 320}.items():
        assert manager.task_state[key] == value


@pytest.mark.parametrize("phase", ["collecting", "confirming", "blocked_soft", "blocked_hard"])
def test_pause_preserves_phase_and_bypasses_pending_selection(valve, monkeypatch, phase):
    manager, llm = valve
    manager.phase = phase
    before = copy.deepcopy(manager.slot_store.export_snapshot())
    refusal_counts = copy.deepcopy(manager._hard_refusal_counts)

    def unexpected_confirmation(*args, **kwargs):
        pytest.fail("Paused edits must not resolve a pending task selection")

    monkeypatch.setattr(manager, "_resolve_pending_oilfield_confirmation", unexpected_confirmation)
    llm.queue_plan(make_plan("WRITE", pending_action="confirm", warning_action="acknowledge"))

    manager.process("先别修改任务，重新选择携带工具。")

    assert manager.slot_store.export_snapshot() == before
    assert manager.phase == phase
    assert manager.editing_slot is None
    assert manager._hard_refusal_counts == refusal_counts


def test_pause_does_not_block_emergency_stop(valve):
    manager, llm = valve
    before = copy.deepcopy(manager.slot_store.export_snapshot())
    llm.queue_plan(make_plan("CONTROL", emergency_action="stop"))

    reply = manager.process("先别修改任务，停止机器人。")

    assert "当前任务尚未发布" in reply
    assert "任务草稿已保留" in reply
    assert manager.slot_store.export_snapshot() == before
