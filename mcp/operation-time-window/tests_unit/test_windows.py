"""Unit tests for OperationWindowService CHECK and SEARCH algorithms (11 test cases)."""

from datetime import datetime, timedelta, timezone
import pytest

from seagent_marine_current.contracts import CurrentForecastData
from seagent_marine_current.windows import OperationWindowService


@pytest.fixture
def window_forecast(base_time):
    """Predictable current forecast for 48 hours."""
    t_nodes = [base_time + timedelta(hours=i * 6) for i in range(9)]  # 0h to 48h
    # Alternating low (0.3 m/s) and high (0.8 m/s) currents
    uo_data = []
    for i in range(9):
        val = 0.3 if i % 2 == 0 else 0.8
        uo_data.append([val])
    vo_data = [[0.0] for _ in range(9)]

    return CurrentForecastData(
        request_fingerprint="test_fp",
        snapshot_id="snap_test",
        provider="copernicus_marine",
        product="prod",
        dataset="ds",
        retrieved_at=base_time,
        actual_grid_latitude=19.5,
        actual_grid_longitude=113.0,
        grid_distance_km=2.0,
        native_time_steps=t_nodes,
        native_depth_layers_m=[100.0],
        uo=uo_data,
        vo=vo_data,
    )


def test_check_window_available(window_forecast, base_time):
    # Over [0h, 0.5h], speed stays around 0.3-0.34 m/s <= 0.5 m/s
    st = base_time
    et = base_time + timedelta(minutes=30)
    res = OperationWindowService.check_window(
        window_forecast, 100.0, st, et, current_limit_mps=0.5
    )
    assert res.status == "AVAILABLE"
    assert res.v_max_mps <= 0.5
    assert res.execution_authorized is False
    assert res.basis == "interpolated_model_current_only"


def test_check_window_unavailable(window_forecast, base_time):
    # Over [0h, 6h], speed peaks at 0.8 m/s > 0.5 m/s limit
    st = base_time
    et = base_time + timedelta(hours=6)
    res = OperationWindowService.check_window(
        window_forecast, 100.0, st, et, current_limit_mps=0.5
    )
    assert res.status == "UNAVAILABLE"
    assert res.reason_code == "CURRENT_EXCEEDED"
    assert res.v_max_mps == pytest.approx(0.8)


def test_check_window_not_evaluable_uncovered_time(window_forecast, base_time):
    # 100h is far beyond 48h coverage
    st = base_time + timedelta(hours=60)
    et = base_time + timedelta(hours=70)
    res = OperationWindowService.check_window(
        window_forecast, 100.0, st, et, current_limit_mps=0.5
    )
    assert res.status == "NOT_EVALUABLE"
    assert res.reason_code == "TIME_RANGE_NOT_COVERED"


def test_check_window_not_evaluable_invalid_time_order(window_forecast, base_time):
    st = base_time + timedelta(hours=5)
    et = base_time + timedelta(hours=2)
    res = OperationWindowService.check_window(
        window_forecast, 100.0, st, et, current_limit_mps=0.5
    )
    assert res.status == "NOT_EVALUABLE"
    assert res.reason_code == "INVALID_INTERVAL"


def test_check_window_fixed_basis_and_no_authorization(window_forecast, base_time):
    res = OperationWindowService.check_window(
        window_forecast, 100.0, base_time, base_time + timedelta(hours=1), current_limit_mps=1.5
    )
    assert res.execution_authorized is False
    assert res.basis == "interpolated_model_current_only"


def test_search_windows_duration_too_short_raises_not_evaluable(window_forecast, base_time):
    # Search range is 2 hours, but requested task duration is 5 hours
    res = OperationWindowService.search_windows(
        window_forecast, 100.0, base_time, base_time + timedelta(hours=2),
        duration_hours=5.0, current_limit_mps=0.5
    )
    assert res.status == "NOT_EVALUABLE"
    assert res.reason_code == "SEARCH_RANGE_TOO_SHORT"


def test_search_windows_negative_duration_raises_not_evaluable(window_forecast, base_time):
    res = OperationWindowService.search_windows(
        window_forecast, 100.0, base_time, base_time + timedelta(hours=10),
        duration_hours=-2.0, current_limit_mps=0.5
    )
    assert res.status == "NOT_EVALUABLE"
    assert res.reason_code == "INVALID_DURATION"


def test_search_windows_uncovered_raises_not_evaluable(window_forecast, base_time):
    res = OperationWindowService.search_windows(
        window_forecast, 100.0, base_time + timedelta(hours=100), base_time + timedelta(hours=120),
        duration_hours=4.0, current_limit_mps=0.5
    )
    assert res.status == "NOT_EVALUABLE"
    assert res.reason_code == "TIME_RANGE_NOT_COVERED"


def test_search_windows_available_sorted_order(window_forecast, base_time):
    # Search over first 24 hours with task duration of 1 hour and limit of 0.4 m/s
    res = OperationWindowService.search_windows(
        window_forecast, 100.0, base_time, base_time + timedelta(hours=24),
        duration_hours=1.0, current_limit_mps=0.4, candidate_step_seconds=3600.0
    )
    assert res.status == "AVAILABLE"
    assert len(res.available_windows) > 0

    # Verify sorting: start_time ascending, then v_max ascending
    for i in range(len(res.available_windows) - 1):
        w0 = res.available_windows[i]
        w1 = res.available_windows[i + 1]
        assert (w0.start_time, w0.v_max_mps) <= (w1.start_time, w1.v_max_mps)


def test_search_windows_unavailable_when_all_exceed_limit(window_forecast, base_time):
    # Limit is 0.1 m/s, but minimum speed is 0.3 m/s
    res = OperationWindowService.search_windows(
        window_forecast, 100.0, base_time, base_time + timedelta(hours=12),
        duration_hours=2.0, current_limit_mps=0.1
    )
    assert res.status == "UNAVAILABLE"
    assert res.reason_code == "NO_SUITABLE_WINDOW"
    assert len(res.available_windows) == 0
    assert res.total_checked_candidates > 0


def test_search_windows_boundary_candidate_inclusion(window_forecast, base_time):
    # Range 7.5 hours with duration 3.0 hours and step 2 hours:
    # Starts: 0.0, 2.0, 4.0. Last candidate before boundary is 4.0.
    # Boundary candidate (7.5 - 3.0 = 4.5h) must be included!
    st = base_time
    et = base_time + timedelta(hours=7.5)
    res = OperationWindowService.search_windows(
        window_forecast, 100.0, st, et,
        duration_hours=3.0, current_limit_mps=1.0, candidate_step_seconds=7200.0
    )
    # Check that candidate ending exactly at et is present
    end_times = [w.end_time for w in res.available_windows]
    assert et.astimezone(timezone.utc) in end_times
