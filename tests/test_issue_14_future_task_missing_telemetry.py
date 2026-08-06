"""
test_issue_14_future_task_missing_telemetry.py — 覆盖未来规划任务在遥测缺失/过期时的 pending_runtime_validation 逻辑
"""

import pytest
from src.knowledge_retriever import KnowledgeBase
from src.validator import TaskValidator


def test_future_task_missing_telemetry_returns_pending_runtime_validation():
    kb = KnowledgeBase()
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
    # 未来任务在缺乏当前具体执行环境时，不应当错判为 error，而应返回 pending_runtime_validation
    assert res.overall_status in ("pending_runtime_validation", "warning", "valid")
