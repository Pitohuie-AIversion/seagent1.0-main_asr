"""Unit tests for depth/time linear interpolation and maximum speed calculation (10 test cases)."""

import math
from datetime import datetime, timedelta, timezone
import pytest

from seagent_marine_current.contracts import CurrentForecastData
from seagent_marine_current.processing import CurrentProcessor


@pytest.fixture
def manual_forecast(base_time):
    """Forecast with known deterministic u and v values for mathematical verification."""
    t0 = base_time
    t1 = base_time + timedelta(hours=6)
    t2 = base_time + timedelta(hours=12)

    # 2 depth layers: 100m and 200m
    # At 100m: u = [0.2, 0.4, 0.6], v = [0.0, 0.0, 0.0]
    # At 200m: u = [0.4, 0.8, 1.2], v = [0.0, 0.0, 0.0]
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
        native_time_steps=[t0, t1, t2],
        native_depth_layers_m=[100.0, 200.0],
        uo=[[0.2, 0.4], [0.4, 0.8], [0.6, 1.2]],
        vo=[[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]],
    )


def test_interpolate_depth_single_layer(base_time):
    fc = CurrentForecastData(
        request_fingerprint="test_fp",
        snapshot_id="snap_test",
        provider="copernicus_marine",
        product="prod",
        dataset="ds",
        retrieved_at=base_time,
        actual_grid_latitude=19.5,
        actual_grid_longitude=113.0,
        grid_distance_km=2.0,
        native_time_steps=[base_time, base_time + timedelta(hours=6)],
        native_depth_layers_m=[50.0],
        uo=[[0.3], [0.5]],
        vo=[[0.1], [0.2]],
    )
    nodes = CurrentProcessor.interpolate_depth_at_nodes(fc, 50.0)
    assert len(nodes) == 2
    assert nodes[0][1] == pytest.approx(0.3)
    assert nodes[1][1] == pytest.approx(0.5)


def test_interpolate_depth_two_layers_midpoint(manual_forecast):
    # Depth 150m is exact midpoint between 100m and 200m
    nodes = CurrentProcessor.interpolate_depth_at_nodes(manual_forecast, 150.0)
    assert len(nodes) == 3
    # At t0: 0.5*0.2 + 0.5*0.4 = 0.3
    assert nodes[0][1] == pytest.approx(0.3)
    # At t1: 0.5*0.4 + 0.5*0.8 = 0.6
    assert nodes[1][1] == pytest.approx(0.6)
    # At t2: 0.5*0.6 + 0.5*1.2 = 0.9
    assert nodes[2][1] == pytest.approx(0.9)


def test_interpolate_depth_two_layers_clamped(manual_forecast):
    # Depth 50m (< 100m) should clamp to layer 100m
    nodes_shallow = CurrentProcessor.interpolate_depth_at_nodes(manual_forecast, 50.0)
    assert nodes_shallow[0][1] == pytest.approx(0.2)
    # Depth 300m (> 200m) should clamp to layer 200m
    nodes_deep = CurrentProcessor.interpolate_depth_at_nodes(manual_forecast, 300.0)
    assert nodes_deep[0][1] == pytest.approx(0.4)


def test_evaluate_at_time_native_node(manual_forecast, base_time):
    point = CurrentProcessor.evaluate_at_time(manual_forecast, 150.0, base_time)
    assert point.is_native_node is True
    assert point.u_mps == pytest.approx(0.3)
    assert point.speed_mps == pytest.approx(0.3)


def test_evaluate_at_time_midpoint_vector_interpolation(manual_forecast, base_time):
    # Midpoint between t0 (u=0.3) and t1 (u=0.6) is t0 + 3h -> u=0.45
    t_mid = base_time + timedelta(hours=3)
    point = CurrentProcessor.evaluate_at_time(manual_forecast, 150.0, t_mid)
    assert point.is_native_node is False
    assert point.u_mps == pytest.approx(0.45)
    assert point.speed_mps == pytest.approx(0.45)


def test_speed_scalar_formula_hypot(base_time):
    # Test hypot(3, 4) = 5
    fc = CurrentForecastData(
        request_fingerprint="test_fp",
        snapshot_id="snap_test",
        provider="copernicus_marine",
        product="prod",
        dataset="ds",
        retrieved_at=base_time,
        actual_grid_latitude=19.5,
        actual_grid_longitude=113.0,
        grid_distance_km=2.0,
        native_time_steps=[base_time, base_time + timedelta(hours=6)],
        native_depth_layers_m=[10.0],
        uo=[[0.3], [0.3]],
        vo=[[0.4], [0.4]],
    )
    pt = CurrentProcessor.evaluate_at_time(fc, 10.0, base_time)
    assert pt.speed_mps == pytest.approx(0.5)


def test_evaluate_at_time_out_of_bounds_raises(manual_forecast, base_time):
    with pytest.raises(ValueError, match="outside available forecast range"):
        CurrentProcessor.evaluate_at_time(
            manual_forecast, 150.0, base_time - timedelta(hours=1)
        )


def test_interval_max_speed_exact_at_endpoints(manual_forecast, base_time):
    # Across [t0, t1], speed increases from 0.3 to 0.6 monotonically
    v_max, pt = CurrentProcessor.find_interval_max_speed(
        manual_forecast, 150.0, base_time, base_time + timedelta(hours=6)
    )
    assert v_max == pytest.approx(0.6)
    assert pt.timestamp == (base_time + timedelta(hours=6)).astimezone(timezone.utc)


def test_interval_max_speed_internal_peak(base_time):
    # Forecast where middle node has higher speed than endpoints
    t0 = base_time
    t1 = base_time + timedelta(hours=6)
    t2 = base_time + timedelta(hours=12)

    fc = CurrentForecastData(
        request_fingerprint="test_fp",
        snapshot_id="snap_test",
        provider="copernicus_marine",
        product="prod",
        dataset="ds",
        retrieved_at=base_time,
        actual_grid_latitude=19.5,
        actual_grid_longitude=113.0,
        grid_distance_km=2.0,
        native_time_steps=[t0, t1, t2],
        native_depth_layers_m=[10.0],
        uo=[[0.2], [0.8], [0.3]],
        vo=[[0.0], [0.0], [0.0]],
    )
    v_max, pt = CurrentProcessor.find_interval_max_speed(fc, 10.0, t0, t2)
    assert v_max == pytest.approx(0.8)
    assert pt.is_native_node is True


def test_convexity_property_verified(base_time):
    # On linear vector interpolation from (0.5, 0.0) to (-0.5, 0.0):
    # Endpoint speeds are 0.5. Midpoint is (0.0, 0.0), speed is 0.0.
    # Convexity guarantees max speed on interval [t0, t1] is at an endpoint!
    t0 = base_time
    t1 = base_time + timedelta(hours=6)
    fc = CurrentForecastData(
        request_fingerprint="test_fp",
        snapshot_id="snap_test",
        provider="copernicus_marine",
        product="prod",
        dataset="ds",
        retrieved_at=base_time,
        actual_grid_latitude=19.5,
        actual_grid_longitude=113.0,
        grid_distance_km=2.0,
        native_time_steps=[t0, t1],
        native_depth_layers_m=[10.0],
        uo=[[0.5], [-0.5]],
        vo=[[0.0], [0.0]],
    )
    v_max, pt = CurrentProcessor.find_interval_max_speed(fc, 10.0, t0, t1)
    assert v_max == pytest.approx(0.5)
    # Check midpoint has lower speed
    pt_mid = CurrentProcessor.evaluate_at_time(fc, 10.0, t0 + timedelta(hours=3))
    assert pt_mid.speed_mps == pytest.approx(0.0)
    assert pt_mid.speed_mps < v_max
