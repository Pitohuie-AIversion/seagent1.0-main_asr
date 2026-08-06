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
    kb.state_info.state_file = state_file
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

    # 设置干净初始遥测状态
    kb.state_info.set_status("OBSROV-001", {"current_velocity": 0.2, "turbidity": 3})

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


def test_publish_revalidate_exception_fails_closed(monkeypatch, tmp_path):
    """测试发布前状态复核抛出异常时必须 fail closed (事务回滚，final 文件不存在，phase != done)。"""
    state_file = tmp_path / "state.yaml"
    import shutil
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.state_file = state_file

    task_dir = tmp_path / "task_intents"
    task_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.task_intent_builder.get_task_dir", lambda create=True: task_dir)

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
        "intent_id": "TI202608060002",
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
    dm.slot_store.slots["intent_id"].value = "TI202608060002"
    dm.slot_store.slots["intent_id"].status = "valid"
    dm.slot_store.slots["task_id"].value = "PI-20260806-002"
    dm.slot_store.slots["task_id"].status = "valid"
    dm._last_built_json = dict(dm.task_state)
    dm.phase = "confirming"

    kb.state_info.set_status("OBSROV-001", {"current_velocity": 0.2, "turbidity": 3})

    original_get_snapshot = kb.state_info.get_unit_state_snapshot
    call_count = 0
    def mock_broken_snapshot(unit_id):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise RuntimeError("IO Error reading robot state snapshot")
        return original_get_snapshot(unit_id)

    monkeypatch.setattr(kb.state_info, "get_unit_state_snapshot", mock_broken_snapshot)

    with pytest.raises(TaskPersistenceError) as exc_info:
        dm._handle_final_publish_confirmation("确认发布", request_id="req-race-failclosed")

    assert "fail closed" in str(exc_info.value) or "发布前单机状态复核失败" in str(exc_info.value)
    assert dm.phase != "done"
    final_file = task_dir / "task_intent_TI202608060002.json"
    assert not final_file.exists()


def test_publish_new_soft_warning_blocks_without_acknowledgement(monkeypatch, tmp_path):
    """测试状态变化产生新的 blocked_soft 且缺乏有效 acknowledgement 时必须阻断并更新 phase=blocked_soft。"""
    state_file = tmp_path / "state.yaml"
    import shutil
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.state_file = state_file

    task_dir = tmp_path / "task_intents"
    task_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.task_intent_builder.get_task_dir", lambda create=True: task_dir)

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
        "intent_id": "TI202608060003",
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
    dm.slot_store.slots["intent_id"].value = "TI202608060003"
    dm.slot_store.slots["intent_id"].status = "valid"
    dm.slot_store.slots["task_id"].value = "PI-20260806-003"
    dm.slot_store.slots["task_id"].status = "valid"
    dm._last_built_json = dict(dm.task_state)
    dm.phase = "confirming"

    kb.state_info.set_status("OBSROV-001", {"current_velocity": 0.2, "turbidity": 3})

    original_get_snapshot = kb.state_info.get_unit_state_snapshot
    call_count = 0
    def mock_soft_changed_snapshot(unit_id):
        nonlocal call_count
        call_count += 1
        snap = dict(original_get_snapshot(unit_id))
        if call_count >= 2:
            snap["state_version"] = snap["state_version"] + 1
            snap["state"] = dict(snap["state"])
            snap["state"]["turbidity"] = 15  # 触发 C014 软警告
        return snap

    monkeypatch.setattr(kb.state_info, "get_unit_state_snapshot", mock_soft_changed_snapshot)

    with pytest.raises(TaskPersistenceError):
        dm._handle_final_publish_confirmation("确认发布", request_id="req-race-soft")

    assert dm.phase == "blocked_soft"
    final_file = task_dir / "task_intent_TI202608060003.json"
    assert not final_file.exists()


def test_real_state_lock_race_with_barrier(tmp_path):
    """测试真实状态锁 guard_unit_state_version 与 set_status 并发互斥逻辑（使用 threading.Barrier，不使用 sleep）。"""
    state_file = tmp_path / "state.yaml"
    import shutil, threading
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.state_file = state_file

    current_snap = kb.state_info.get_unit_state_snapshot("OBSROV--001")
    current_ver = current_snap["state_version"]

    barrier = threading.Barrier(2)
    step = []

    def worker_guard():
        with kb.state_info.guard_unit_state_version("OBSROV--001", current_ver):
            step.append("guard_entered")
            barrier.wait(timeout=5)  # 通知并等待 worker_update 试图获取排他锁
            step.append("guard_holding")
        step.append("guard_exited")

    def worker_update():
        barrier.wait(timeout=5)  # 确保 worker_guard 已进入 guard
        kb.state_info.set_status("OBSROV-001", {"current_velocity": 0.5})
        step.append("update_finished")

    t1 = threading.Thread(target=worker_guard)
    t2 = threading.Thread(target=worker_update)

    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive()
    assert not t2.is_alive()
    # 状态更新必须在 guard 退出后才完成
    assert step == ["guard_entered", "guard_holding", "guard_exited", "update_finished"]
