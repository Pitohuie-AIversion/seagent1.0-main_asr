"""
test_issue_14_publish_state_version_race.py — 覆盖发布前状态版本发生变更触发重新校验与防 TOCTOU 竞争
"""

import pytest
from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.exceptions import TaskPersistenceError, TaskRollbackError


def test_publish_revalidates_when_state_version_changes(monkeypatch, tmp_path):
    state_file = tmp_path / "state.yaml"
    import shutil
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.file_path = str(state_file)
    dm = DialogueManager(kb=kb)
    from src.simulated_time import get_current_datetime
    now_str = get_current_datetime().isoformat(timespec="seconds")
    dm.task_state = {
        "task_type": "海底管道巡检",
        "task_type_key": "pipeline_inspection",
        "cable_type": "海底油气管道",
        "equipment_unit_id": "OBSROV--001",
        "start_time": now_str,
        "end_time": "2099-01-01T18:00:00+08:00",
        "water_depth": 300,
        "support_vessel": "深海一号",
        "start_point": {"lat": 20.0, "lon": 110.0},
        "end_point": {"lat": 20.1, "lon": 110.1},
        "intent_id": "TI202608060001",
    }
    req_schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
    dm.slot_store.init_task_slots(req_schema)
    for k, v in dm.task_state.items():
        if k in dm.slot_store.slots:
            dm.slot_store.slots[k].value = v
            dm.slot_store.slots[k].status = "valid"
    for slot_name, slot in dm.slot_store.slots.items():
        if slot.status != "valid":
            slot.status = "valid"
            slot.value = {} if slot_name == "equipment_specification" else "auto_populated"
    dm.slot_store.slots["intent_id"].value = "TI202608060001"
    dm.slot_store.slots["intent_id"].status = "valid"
    dm.slot_store.slots["task_id"].value = "PI-20260806-001"
    dm.slot_store.slots["task_id"].status = "valid"
    dm._last_built_json = dict(dm.task_state)
    dm.phase = "confirming"

    # 首次校验
    val_res = dm._refresh_validation(purpose="publish")
    assert val_res.overall_status in ("valid", "warning", "pending_runtime_validation")

    # 模拟遥测在确认过程中变更了 state_version 并引入严重超流速阻断
    original_get_snapshot = kb.state_info.get_unit_state_snapshot
    call_count = 0
    def mock_changed_snapshot(unit_id):
        nonlocal call_count
        call_count += 1
        snap = dict(original_get_snapshot(unit_id))
        if call_count >= 2:
            snap["state_version"] = snap["state_version"] + 10
            snap["state"] = dict(snap["state"])
            snap["state"]["current_velocity"] = 5.0 # 超高流速，触发 C014/C017
        return snap

    monkeypatch.setattr(kb.state_info, "get_unit_state_snapshot", mock_changed_snapshot)
    monkeypatch.setattr(kb, "get_unit_state_snapshot", mock_changed_snapshot)

    # 执行确认发布，应被检测到 state_version 变更为更高流速后重新校验并阻断
    with pytest.raises((TaskPersistenceError, TaskRollbackError)) as exc_info:
        dm._handle_final_publish_confirmation("确认发布", request_id="req-race-1")

    assert "状态遥测在确认发布过程中发生变更" in str(exc_info.value)
