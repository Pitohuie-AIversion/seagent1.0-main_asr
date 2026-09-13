"""End-to-end integration test verifying SEAgent dialogue state and Marine Current MCP bridge.

Covers verification scenarios:
- Rule 6: operation_depth vs water_depth strict separation.
- Rule 11 & 14: Deterministic current limit from robot profile (no LLM guessing) + SafetyMargin.
- Rule 13 & 15: Slot candidate_value workflow (never directly overwrite formal value).
- Rule 24 & 25: Ordinary chat does not trigger Marine, ASR task enters same pipeline.
"""

from datetime import datetime, timedelta, timezone
import pytest
import yaml

from src.slot_store import Slot
from seagent_marine_current.contracts import CurrentQuery
from seagent_marine_current.integration import (
    MarineCurrentBridge,
    apply_candidate_window_to_slots,
    confirm_candidate_window_in_slots,
    extract_current_query,
    get_deterministic_current_limit,
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

    # 1. Simulate DialogueManager slot collection for seabed task
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

    # 5. Fixed-window CHECK evaluation:
    # 5a. Default formal call MUST reject synthetic forecast as NOT_EVALUABLE
    check_res_rejected = bridge.evaluate_task_window(task_state, current_limit_mps=0.5, allow_synthetic_for_testing=False)
    assert check_res_rejected is not None
    assert check_res_rejected.status == "NOT_EVALUABLE"
    assert check_res_rejected.reason_code == "SYNTHETIC_DATA_NOT_ALLOWED"

    # 5b. Explicit test authorization allows algorithm validation
    check_res = bridge.evaluate_task_window(task_state, current_limit_mps=0.5, allow_synthetic_for_testing=True)
    assert check_res is not None
    assert check_res.status in ("AVAILABLE", "UNAVAILABLE")
    assert check_res.basis == "interpolated_model_current_only"
    assert check_res.execution_authorized is False

    # 6. Alternative window SEARCH
    search_res_rejected = bridge.search_task_windows(
        task_state,
        duration_hours=4.0,
        search_range_hours=24.0,
        current_limit_mps=0.5,
        allow_synthetic_for_testing=False,
    )
    assert search_res_rejected is not None
    assert search_res_rejected.status == "NOT_EVALUABLE"
    assert search_res_rejected.reason_code == "SYNTHETIC_DATA_NOT_ALLOWED"

    search_res = bridge.search_task_windows(
        task_state,
        duration_hours=4.0,
        search_range_hours=24.0,
        current_limit_mps=0.5,
        allow_synthetic_for_testing=True,
    )
    assert search_res is not None
    assert search_res.status in ("AVAILABLE", "UNAVAILABLE")
    if search_res.available_windows:
        cand = search_res.available_windows[0]
        assert hasattr(cand, "safety_margin")
        assert cand.safety_margin >= 0.0

    # 7. Time slot change invalidates fingerprint
    task_state["start_time"] = (base_time + timedelta(hours=24)).isoformat()
    task_state["end_time"] = (base_time + timedelta(hours=30)).isoformat()
    q_shifted = extract_current_query(task_state, oilfield_kb=oilfield_kb)
    assert q_shifted is not None
    assert not bridge.is_cache_valid_for(q_shifted)


def test_operation_depth_and_water_depth_strict_separation():
    """Rule 6: operation_depth and water_depth must be strictly separated.

    Mid-water task with missing operation_depth must NOT guess water_depth.
    """
    base_time = datetime(2026, 9, 14, 8, 0, 0, tzinfo=timezone.utc)
    midwater_task = {
        "task_type_key": "water_column_environmental_sampling",
        "latitude": 20.5,
        "longitude": 115.2,
        "water_depth": 500.0,  # seabed depth is 500m
        "start_time": base_time.isoformat(),
        "end_time": (base_time + timedelta(hours=12)).isoformat(),
    }
    # Should be None because operation_depth is missing and this is not a seabed task
    query = extract_current_query(midwater_task)
    assert query is None, "Mid-water tasks must NOT default operation_depth = water_depth"

    # Now provide explicit operation_depth
    midwater_task["operation_depth"] = 120.0
    query_with_op_depth = extract_current_query(midwater_task)
    assert query_with_op_depth is not None
    assert query_with_op_depth.operation_depth_m == 120.0


def test_deterministic_current_limit_lookup():
    """Rule 14: Never allow LLM to guess current limit; fail-closed if unknown."""
    known_task = {
        "equipment_type": "通用工作级深海机器人250HP",
        "equipment_unit_id": "WROV-250-001",
    }
    limit = get_deterministic_current_limit(known_task)
    assert limit == 1.5

    unknown_task = {
        "equipment_type": "CustomPrototypeROV_X99",
    }
    unknown_limit = get_deterministic_current_limit(unknown_task)
    assert unknown_limit is None, "Unknown robot must fail closed with None limit"


def test_slot_candidate_value_workflow():
    """Rule 15: SEARCH result modifies candidate_value, never formal value directly.

    Formal value only updated after explicit confirmation, incrementing version.
    """
    original_start = "2026-09-14T08:00:00+00:00"
    original_end = "2026-09-14T12:00:00+00:00"

    slots = {
        "start_time": Slot(
            slot_name="start_time",
            value=original_start,
            status="valid",
            version=1,
        ),
        "end_time": Slot(
            slot_name="end_time",
            value=original_end,
            status="valid",
            version=1,
        ),
    }

    class MockWindow:
        start_time = datetime(2026, 9, 14, 14, 0, 0, tzinfo=timezone.utc)
        end_time = datetime(2026, 9, 14, 18, 0, 0, tzinfo=timezone.utc)

    # 1. Apply candidate window
    applied = apply_candidate_window_to_slots(slots, MockWindow())
    assert applied is True
    # Formal value must NOT change
    assert slots["start_time"].value == original_start
    assert slots["end_time"].value == original_end
    # Candidate value updated
    assert slots["start_time"].candidate_value == MockWindow.start_time.isoformat()
    assert slots["end_time"].candidate_value == MockWindow.end_time.isoformat()
    assert slots["start_time"].status == "candidate"
    assert slots["start_time"].version == 1

    # 2. User confirms candidate window
    promoted = confirm_candidate_window_in_slots(slots)
    assert promoted is True
    assert slots["start_time"].value == MockWindow.start_time.isoformat()
    assert slots["end_time"].value == MockWindow.end_time.isoformat()
    assert slots["start_time"].candidate_value is None
    assert slots["end_time"].candidate_value is None
    assert slots["start_time"].status == "valid"
    assert slots["start_time"].version == 2


def test_ordinary_chat_does_not_trigger_marine_forecast():
    """Rule 24 & 25: Non-task chat does not trigger marine forecast; ASR follows same routing."""
    # Ordinary chat input
    chat_state = {
        "user_message": "你好，请问今天天气怎么样？",
    }
    query = extract_current_query(chat_state, is_task_intent=False)
    assert query is None, "Ordinary chat must never trigger Marine forecast"

    # ASR transcribed text that forms a valid future task
    base_time = datetime(2026, 9, 15, 0, 0, 0, tzinfo=timezone.utc)
    asr_task_state = {
        "source": "asr_transcription",
        "task_type_key": "pipeline_inspection",
        "latitude": 20.0,
        "longitude": 114.0,
        "operation_depth": 80.0,
        "start_time": base_time.isoformat(),
        "end_time": (base_time + timedelta(hours=24)).isoformat(),
    }
    asr_query = extract_current_query(asr_task_state, is_task_intent=True)
    assert asr_query is not None
    assert asr_query.latitude == 20.0
    assert asr_query.operation_depth_m == 80.0
