"""
tests/test_issue_14_validator_snapshot.py
针对 Issue #14 批次一与批次二的定向单元测试：
1. 严格单机状态快照接口 get_unit_state_snapshot
2. TaskValidator 结构化校验服务 (validate_task)
"""

import pytest
from src.knowledge_retriever import KnowledgeBase
from src.validator import TaskValidator, ValidationResult, Violation
from src.exceptions import StateSelectorError, StateSnapshotValidationError


@pytest.fixture
def kb(tmp_path):
    state_file = tmp_path / "state.yaml"
    import shutil, os
    shutil.copy("config/state.yaml", state_file)
    kb_inst = KnowledgeBase()
    kb_inst.state_info.file_path = str(state_file)
    return kb_inst


@pytest.fixture
def validator(kb):
    return TaskValidator(kb)


def test_get_unit_state_snapshot_strict(kb):
    """测试批次一：get_unit_state_snapshot 必须精确匹配 unit_id 且按 status_ref 读取。"""
    # 实体编号为 OBSROV--001
    snapshot = kb.get_unit_state_snapshot("OBSROV--001")
    assert isinstance(snapshot, dict)
    assert snapshot["unit_id"] == "OBSROV--001"
    assert snapshot["status_ref"] == "OBSROV-001"
    assert "state_version" in snapshot
    assert "updated_at" in snapshot
    assert "state" in snapshot

    # status_ref (OBSROV-001) 直接传入抛出 StateSelectorError（不再接受 status_ref 当作 unit_id 模糊入口）
    with pytest.raises(StateSelectorError):
        kb.get_unit_state_snapshot("OBSROV-001")

    # 不存在的 unit_id 抛出 StateSelectorError
    with pytest.raises(StateSelectorError):
        kb.get_unit_state_snapshot("NON_EXISTENT_UNIT_999")

    # 传入空字符串抛出 StateSelectorError
    with pytest.raises(StateSelectorError):
        kb.get_unit_state_snapshot("")


def test_turbidity_and_velocity_thresholds(kb, validator):
    """测试浑浊度 (C013/C014) 与流速 (C015/C016/C017) 的分级逻辑。"""
    # 模拟为 OBSROV-001 设置遥测状态
    kb.state_info.set_status("OBSROV-001", {"turbidity": 7, "current_velocity": 0.7})
    task_state = {
        "equipment_unit_id": "OBSROV--001",
        "task_type_key": "pipeline_inspection",
    }
    res = validator.validate_task(task_state)
    c_ids = {v.constraint_id for v in res.violations}
    assert "C013" in c_ids
    assert "C014" not in c_ids
    assert "C015" in c_ids
    assert "C016" not in c_ids
    assert "C017" not in c_ids
    assert res.overall_status == "blocked_soft"

    # turbidity = 15, velocity = 0.9
    kb.state_info.set_status("OBSROV-001", {"turbidity": 15, "current_velocity": 0.9})
    res = validator.validate_task(task_state)
    c_ids = {v.constraint_id for v in res.violations}
    assert "C013" not in c_ids
    assert "C014" in c_ids
    assert "C015" not in c_ids
    assert "C016" in c_ids
    assert "C017" not in c_ids

    # velocity = 1.3 -> 触发 C017 (hard)
    kb.state_info.set_status("OBSROV-001", {"turbidity": 3, "current_velocity": 1.3})
    res = validator.validate_task(task_state)
    c_ids = {v.constraint_id for v in res.violations}
    assert "C017" in c_ids
    assert res.overall_status == "blocked_hard"


def test_single_unit_isolation(kb, validator):
    """测试同一型号多台设备，只读取用户选择的 unit_id 的状态快照。"""
    kb.state_info.set_status("LROV--001", {"overall_status": "available", "current_velocity": 0.1})
    kb.state_info.set_status("LROV-002", {"overall_status": "available", "current_velocity": 1.5})  # LROV-002 超流速

    # 选择 LROV--001，应正常 valid
    task1 = {"equipment_unit_id": "LROV--001", "task_type_key": "pipeline_inspection"}
    res1 = validator.validate_task(task1)
    assert res1.overall_status == "valid"
    assert res1.state_snapshot["unit_id"] == "LROV--001"

    # 选择 LROV-002，应触发 blocked_hard
    task2 = {"equipment_unit_id": "LROV--002", "task_type_key": "pipeline_inspection"}
    res2 = validator.validate_task(task2)
    assert res2.overall_status == "blocked_hard"
    assert res2.state_snapshot["unit_id"] == "LROV--002"


def test_ambiguous_family_returns_validation_error(kb, validator):
    """当只提供 family/type 且对应多台单机时，无法唯一确定单机，应返回 validation_error。"""
    # 假设 observation_rov 在 fleet 中有多台单机
    task_state = {
        "equipment_family": "observation_rov",
        "task_type_key": "pipeline_inspection",
    }
    res = validator.validate_task(task_state)
    assert res.overall_status == "validation_error"
    assert res.error is not None
    assert res.error["code"] == "AMBIGUOUS_UNIT_SELECTOR"
    assert len(res.violations) > 0
    assert res.violations[0].constraint_id == "VAL_ERR"


def test_future_task_pending_runtime_validation(kb, validator):
    """未来执行的任务（start_time 晚于当前）不使用当前遥测流速阻断，标记为 pending_runtime_validation。"""
    kb.state_info.set_status("OBSROV-001", {"current_velocity": 1.5})  # 当前强流
    future_time = "2099-01-01T12:00:00"
    task_state = {
        "equipment_unit_id": "OBSROV--001",
        "task_type_key": "pipeline_inspection",
        "start_time": future_time,
    }
    res = validator.validate_task(task_state)
    assert res.overall_status == "pending_runtime_validation"
    # C017 不在当前违规列表中
    c_ids = {v.constraint_id for v in res.violations}
    assert "C017" not in c_ids
