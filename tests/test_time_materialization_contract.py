"""Time edits preserve caller data and candidate provenance across all stages."""

from copy import deepcopy
from datetime import datetime

import pytest

from src.temporal.temporal_parser import TemporalParser


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(
        "src.temporal.simulated_time.get_current_datetime",
        lambda: datetime(2026, 10, 20, 9),
    )


@pytest.mark.parametrize("message,rejected", [
    ("开始时间推迟半小时，结束时间不变。", False),
    ("开始时间推迟半小时，开始时间不变。", True),
    ("持续时间再减少5小时，开始时间不变。", True),
])
def test_time_edit_preserves_inputs_and_last_candidate_provenance(message, rejected):
    state = {"start_time": "2026-10-21T09:00:00", "end_time": "2026-10-21T13:00:00"}
    candidates = [
        {"canonical_key": "water_depth", "normalized_value": 320, "evidence": {"source": "user"}},
        {"canonical_key": "start_time", "raw_value": "推迟半小时",
         "normalized_value": "2026-10-21T08:00:00", "confidence": 0.2, "candidate_id": "earlier"},
        {"canonical_key": "end_time", "raw_value": "结束时间不变",
         "normalized_value": "2026-10-21T14:00:00", "confidence": 0.8, "candidate_id": "end"},
        {"canonical_key": "start_time", "raw_value": "推迟半小时",
         "normalized_value": "2026-10-21T08:30:00", "confidence": 0.9, "candidate_id": "latest"},
    ]
    relation = {"has_duration": True, "action": "ADD", "target": "start_time",
                "raw_text": "半小时", "duration_seconds": 1800, "confidence": 0.7}
    original = deepcopy((candidates, relation, state))

    result, unresolved = TemporalParser.materialize_time_relation(
        candidates, relation, state, {"start_time", "end_time", "water_depth"}, message,
    )

    assert (candidates, relation, state) == original
    assert result[0] == candidates[0]
    if rejected:
        assert unresolved
        assert result == [candidates[0]]
    else:
        assert unresolved == []
        assert [item["canonical_key"] for item in result] == ["water_depth", "start_time", "end_time"]
        assert result[1] == {
            **candidates[-1], "raw_key": "start_time", "raw_value": "开始时间推迟半小时",
            "normalized_value": "2026-10-21T09:30:00", "resolution_method": "time_endpoint_shifted",
        }
        assert result[2] == {
            **candidates[2], "raw_key": "end_time",
            "normalized_value": "2026-10-21T13:00:00", "resolution_method": "end_time_unchanged",
        }
