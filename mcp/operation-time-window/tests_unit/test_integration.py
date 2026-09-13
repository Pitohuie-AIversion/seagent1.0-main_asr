"""Unit tests for SEAgent integration bridge and session forecast caching."""

from datetime import datetime, timedelta, timezone
import pytest

from seagent_marine_current.contracts import CurrentQuery
from seagent_marine_current.integration import (
    MarineCurrentBridge,
    extract_current_query,
    parse_datetime_safe,
)
from seagent_marine_current.provider import SyntheticCopernicusProvider


def test_parse_datetime_safe():
    dt_iso = parse_datetime_safe("2026-09-14T08:00:00+08:00")
    assert dt_iso is not None
    assert dt_iso.hour == 8

    dt_space = parse_datetime_safe("2026-09-14 08:00:00")
    assert dt_space is not None

    assert parse_datetime_safe(None) is None
    assert parse_datetime_safe("invalid_time_str") is None


def test_extract_current_query_with_coordinates():
    state = {
        "start_time": "2026-09-14 08:00:00",
        "end_time": "2026-09-15 08:00:00",
        "coordinates": [20.5, 114.2],
        "operation_depth": 120.0,
    }
    q = extract_current_query(state)
    assert q is not None
    assert q.latitude == 20.5
    assert q.longitude == 114.2
    assert q.operation_depth_m == 120.0


def test_extract_current_query_with_oilfield_kb_lookup():
    kb = {
        "oil_fields": [
            {
                "name": "流花11-1油田",
                "aliases": ["流花11-1"],
                "lat_range": [20.81, 20.82],
                "lon_range": [115.73, 115.74],
            }
        ]
    }
    state = {
        "task_type_key": "pipeline_inspection",
        "start_time": "2026-09-14 08:00:00",
        "end_time": "2026-09-15 08:00:00",
        "oilfield_name": "流花11-1油田",
        "water_depth": 300.0,
    }
    q = extract_current_query(state, oilfield_kb=kb)
    assert q is not None
    assert 20.81 <= q.latitude <= 20.82
    assert 115.73 <= q.longitude <= 115.74
    assert q.operation_depth_m == 300.0


def test_extract_current_query_incomplete_returns_none():
    # Missing end_time
    state = {
        "start_time": "2026-09-14 08:00:00",
        "coordinates": [20.5, 114.2],
        "operation_depth": 120.0,
    }
    assert extract_current_query(state) is None


def test_marine_current_bridge_caching_and_evaluation(base_time):
    bridge = MarineCurrentBridge(default_current_limit_mps=0.5)

    q = CurrentQuery(
        latitude=19.6,
        longitude=113.0,
        operation_depth_m=130.0,
        start_time=base_time,
        end_time=base_time + timedelta(hours=48),
    )
    provider = SyntheticCopernicusProvider(base_speed_mps=0.4)
    forecast = provider.fetch(q)

    # Initial state: no cache
    assert bridge.cached_forecast is None
    assert not bridge.is_cache_valid_for(q)

    # Cache update
    bridge.update_cache(forecast)
    assert bridge.is_cache_valid_for(q)

    # Same query evaluates successfully
    task_state = {
        "start_time": base_time.isoformat(),
        "end_time": (base_time + timedelta(hours=4)).isoformat(),
        "operation_depth": 130.0,
    }
    res_check = bridge.evaluate_task_window(task_state)
    assert res_check is not None
    assert res_check.status in ("AVAILABLE", "UNAVAILABLE")

    # Mutated query invalidates fingerprint
    q_mutated = CurrentQuery(
        latitude=19.7,
        longitude=113.0,
        operation_depth_m=130.0,
        start_time=base_time,
        end_time=base_time + timedelta(hours=48),
    )
    assert not bridge.is_cache_valid_for(q_mutated)

    # Invalidate cache
    bridge.invalidate_cache()
    assert bridge.cached_forecast is None
