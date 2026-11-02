"""Expired snapshots must never acquire live-health semantics in the UI."""

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.session.ui_state_builder import _build_constraint_state


@pytest.mark.parametrize("age, status", [(1800, "fresh"), (1801, "stale"), (-301, "invalid")])
def test_ui_projects_freshness_without_changing_validation_or_snapshot(monkeypatch, age, status):
    now = datetime(2026, 10, 21, 14, 15, 54, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("src.telemetry_evidence.get_current_datetime", lambda: now)
    validation = {
        "overall_status": "valid", "task_version": 3, "validation_version": 4,
        "state_snapshot": {
            "unit_id": "AUV-324cc-001", "state_version": 1,
            "updated_at": (now - timedelta(seconds=age)).isoformat(),
            "state": {"overall_status": "available", "thruster_status": "normal"},
        },
        "violations": [],
    }
    before = deepcopy(validation)
    manager = SimpleNamespace(slot_store=SimpleNamespace(
        validation_result=validation, validation_acknowledgements=[], get_slot_snapshot=lambda: {},
    ))

    result = _build_constraint_state(manager)

    assert result["telemetry_freshness"]["status"] == status
    assert result["telemetry_freshness"]["usable"] is (status == "fresh")
    assert result["overall_status"] == "valid"
    assert result["state_snapshot"] == before["state_snapshot"]
    assert validation == before


def test_ui_without_snapshot_reports_unknown_telemetry():
    manager = SimpleNamespace(slot_store=SimpleNamespace(
        validation_result={"overall_status": "valid"}, validation_acknowledgements=[],
    ))
    result = _build_constraint_state(manager)
    assert result["telemetry_freshness"]["usable"] is False
    assert result["telemetry_freshness"]["status"] == "missing"


@pytest.mark.parametrize("status", ["pending_runtime_validation", "blocked_soft", "warning", "blocked_hard"])
def test_deferred_evidence_survives_other_violations_overriding_overall_status(status):
    manager = SimpleNamespace(slot_store=SimpleNamespace(
        validation_result={
            "overall_status": status,
            "violations": [
                {"constraint_id": "C032", "severity": "soft", "check_type": "future_task_runtime_notice",
                 "message": "未来任务等待执行时核验"},
                {"constraint_id": "C-STATIC", "severity": "soft", "message": "另一静态约束"},
            ],
        },
        validation_acknowledgements=[],
    ))
    result = _build_constraint_state(manager)
    assert result["runtime_validation_deferred"] is True
    assert result["overall_status"] == status
