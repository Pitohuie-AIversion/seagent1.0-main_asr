"""
test_issue_14_future_task_missing_telemetry.py — 覆盖未来规划任务在遥测缺失/过期时的 pending_runtime_validation 逻辑
"""

import pytest
from src.knowledge_retriever import KnowledgeBase
from src.validator import TaskValidator


def test_future_task_missing_telemetry_returns_pending_runtime_validation(tmp_path):
    state_file = tmp_path / "state.yaml"
    import shutil
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.state_file = state_file

    # 明确删除目标 status_ref ("OBSROV-001") 的状态记录
    snap = kb.state_info._load_state_unlocked()
    if "robots" in snap and "OBSROV-001" in snap["robots"]:
        del snap["robots"]["OBSROV-001"]
        kb.state_info._save_state_unlocked(snap)

    validator = TaskValidator(kb)

    # 提交一个未来两周的任务
    task_state = {
        "task_type_key": "pipeline_inspection",
        "equipment_unit_id": "OBSROV--001",
        "start_time": "2026-08-20T10:00:00+08:00",
        "end_time": "2026-08-20T18:00:00+08:00",
        "water_depth": 300,
    }

    res = validator.validate_task(task_state, purpose="interactive")
    # 未来任务在缺乏当前具体执行环境时，不应当错判为 error，而应精确返回 pending_runtime_validation
    assert res.overall_status == "pending_runtime_validation"
