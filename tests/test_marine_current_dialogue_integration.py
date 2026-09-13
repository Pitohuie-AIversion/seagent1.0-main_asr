"""End-to-end integration test verifying SEAgent dialogue state and Marine Current MCP bridge."""

from datetime import datetime, timedelta, timezone
import pytest
import yaml

from seagent_marine_current.contracts import CurrentQuery
from seagent_marine_current.integration import (
    MarineCurrentBridge,
    extract_current_query,
)
from seagent_marine_current.provider import SyntheticCopernicusProvider


@pytest.fixture
def oilfield_kb():
    from pathlib import Path
    kb_path = Path(__file__).resolve().parents[1] / "config" / "oilfield.yaml"
    if kb_path.exists():
        with open(kb_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {"oil_fields": []}


def test_dialogue_state_extracts_query_and_evaluates_window(oilfield_kb):
    """Test that SEAgent task state can automatically extract query, cache it, and evaluate window."""
    base_time = datetime(2026, 9, 14, 8, 0, 0, tzinfo=timezone.utc)

    # 1. Simulate DialogueManager slot collection
    task_state = {
        "task_type_key": "pipeline_inspection",
        "oilfield_name": "流花11-1油田",
        "water_depth": 305.0,
        "start_time": base_time.isoformat(),
        "end_time": (base_time + timedelta(hours=48)).isoformat(),
        "equipment_type": "通用工作级深海机器人250HP",
        "equipment_unit_id": "WROV-250-001",
    }

    # 2. Extract CurrentQuery using oilfield knowledge base
    query = extract_current_query(task_state, oilfield_kb=oilfield_kb)
    assert query is not None
    assert 20.80 <= query.latitude <= 20.83
    assert 115.70 <= query.longitude <= 115.75
    assert query.operation_depth_m == 305.0

    # 3. Simulate forecast retrieval and bridge caching
    provider = SyntheticCopernicusProvider(base_speed_mps=0.45)
    forecast_data = provider.fetch(query)

    bridge = MarineCurrentBridge(default_current_limit_mps=0.5)
    bridge.update_cache(forecast_data)
    assert bridge.is_cache_valid_for(query)

    # 4. Unrelated slot change (e.g. payload or remarks) preserves cached forecast
    task_state["payload"] = ["高精度双目视觉", "激光标尺"]
    task_state["remarks"] = "注意避让悬垂线缆"
    q_after_unrelated = extract_current_query(task_state, oilfield_kb=oilfield_kb)
    assert q_after_unrelated is not None
    assert bridge.is_cache_valid_for(q_after_unrelated)

    # 5. Fixed-window CHECK evaluation
    check_res = bridge.evaluate_task_window(task_state, current_limit_mps=0.5)
    assert check_res is not None
    assert check_res.status in ("AVAILABLE", "UNAVAILABLE")
    assert check_res.basis == "interpolated_model_current_only"
    assert check_res.execution_authorized is False

    # 6. Alternative window SEARCH
    search_res = bridge.search_task_windows(
        task_state,
        duration_hours=4.0,
        search_range_hours=24.0,
        current_limit_mps=0.5,
    )
    assert search_res is not None
    assert search_res.status in ("AVAILABLE", "UNAVAILABLE")

    # 7. Time slot change invalidates fingerprint
    task_state["start_time"] = (base_time + timedelta(hours=24)).isoformat()
    task_state["end_time"] = (base_time + timedelta(hours=30)).isoformat()
    q_shifted = extract_current_query(task_state, oilfield_kb=oilfield_kb)
    assert q_shifted is not None
    assert not bridge.is_cache_valid_for(q_shifted)
