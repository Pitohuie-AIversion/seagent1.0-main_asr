"""Unit tests for Copernicus and Synthetic providers (10 test cases)."""

from datetime import datetime, timedelta, timezone
import pytest

from seagent_marine_current.contracts import CurrentQuery
from seagent_marine_current.provider import (
    COPERNICUS_STANDARD_DEPTHS,
    CopernicusProvider,
    ProviderError,
    SyntheticCopernicusProvider,
    calculate_grid_distance_km,
    find_bounding_depth_layers,
    generate_bounding_6h_time_nodes,
)


def test_depth_layers_exact_match():
    layers = find_bounding_depth_layers(111.41, COPERNICUS_STANDARD_DEPTHS)
    assert layers == [111.41]


def test_depth_layers_between_bounds():
    # 130m is between 127.08m and 144.82m
    layers = find_bounding_depth_layers(130.0, COPERNICUS_STANDARD_DEPTHS)
    assert layers == [127.08, 144.82]


def test_depth_layers_shallower_than_surface():
    layers = find_bounding_depth_layers(0.1, COPERNICUS_STANDARD_DEPTHS)
    assert layers == [COPERNICUS_STANDARD_DEPTHS[0]]


def test_depth_layers_deeper_than_seabed():
    layers = find_bounding_depth_layers(6500.0, COPERNICUS_STANDARD_DEPTHS)
    assert layers == [COPERNICUS_STANDARD_DEPTHS[-1]]


def test_generate_6h_time_nodes_boundary_alignment(base_time):
    # base_time is 12:00:00 UTC
    end_time = base_time + timedelta(hours=14)  # 02:00:00 next day
    nodes = generate_bounding_6h_time_nodes(base_time, end_time)
    assert nodes[0] == datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
    assert nodes[1] == datetime(2026, 9, 12, 18, 0, 0, tzinfo=timezone.utc)
    assert nodes[2] == datetime(2026, 9, 13, 0, 0, 0, tzinfo=timezone.utc)
    assert nodes[3] == datetime(2026, 9, 13, 6, 0, 0, tzinfo=timezone.utc)
    assert nodes[0] <= base_time
    assert nodes[-1] >= end_time


def test_calculate_grid_distance_km():
    # Same point
    d0 = calculate_grid_distance_km(19.0, 113.0, 19.0, 113.0)
    assert d0 == pytest.approx(0.0, abs=1e-5)
    # Approx 1 deg latitude is ~111 km
    d1 = calculate_grid_distance_km(19.0, 113.0, 20.0, 113.0)
    assert 110.0 < d1 < 112.5


def test_copernicus_check_credentials_raises_when_missing():
    provider = CopernicusProvider(username="", password="")
    with pytest.raises(ProviderError) as exc_info:
        provider.check_credentials()
    assert exc_info.value.code == "NOT_CONFIGURED"
    assert exc_info.value.retryable is False


def test_synthetic_provider_basic_generation(sample_query):
    provider = SyntheticCopernicusProvider()
    data = provider.fetch(sample_query)
    assert data.request_fingerprint == sample_query.fingerprint
    assert len(data.native_time_steps) >= 2
    assert len(data.native_depth_layers_m) in (1, 2)
    assert len(data.uo) == len(data.native_time_steps)
    assert len(data.vo) == len(data.native_time_steps)


def test_synthetic_provider_depth_attenuation(base_time):
    q_surface = CurrentQuery(
        latitude=19.6, longitude=113.0, operation_depth_m=1.0,
        start_time=base_time, end_time=base_time + timedelta(hours=12)
    )
    q_deep = CurrentQuery(
        latitude=19.6, longitude=113.0, operation_depth_m=1000.0,
        start_time=base_time, end_time=base_time + timedelta(hours=12)
    )
    provider = SyntheticCopernicusProvider(base_speed_mps=0.8)
    data_surface = provider.fetch(q_surface)
    data_deep = provider.fetch(q_deep)

    speed_surface = abs(data_surface.uo[0][0])
    speed_deep = abs(data_deep.uo[0][0])
    assert speed_deep < speed_surface


def test_synthetic_provider_error_injection(sample_query):
    provider_land = SyntheticCopernicusProvider(force_error="LAND_OR_INVALID")
    with pytest.raises(ProviderError) as exc:
        provider_land.fetch(sample_query)
    assert exc.value.code == "LAND_OR_INVALID"


def test_provider_timeout_error(sample_query):
    """Scenario 6: provider timeout must raise structured ProviderError."""
    provider_timeout = SyntheticCopernicusProvider(force_error="TIMEOUT")
    with pytest.raises(ProviderError) as exc:
        provider_timeout.fetch(sample_query)
    assert exc.value.code == "TIMEOUT"
    assert exc.value.retryable is True


def test_provider_missing_data_error(sample_query):
    """Scenario 8: provider returning empty or missing data must raise ProviderError."""
    provider_empty = SyntheticCopernicusProvider(force_error="MISSING_DATA")
    with pytest.raises(ProviderError) as exc:
        provider_empty.fetch(sample_query)
    assert exc.value.code == "MISSING_DATA"


def test_depth_layers_non_adjacent_gap_rejected():
    """Scenario 12: depth layers with missing intermediate levels must not jump-interpolate."""
    # Custom depths array with gap: 0, 50, 200 (missing 100m level)
    gap_depths = [0.0, 50.0, 200.0]
    # Target depth 75m is between 50 and 200; bounding layers should be exactly [50, 200]
    layers = find_bounding_depth_layers(75.0, gap_depths)
    assert layers == [50.0, 200.0]


def test_native_time_nodes_gap_detection(base_time):
    """Scenario 15: native 6h nodes must be strictly continuous without gaps."""
    # Interval of 24 hours should contain exactly 5 nodes: 0h, 6h, 12h, 18h, 24h
    end_time = base_time + timedelta(hours=24)
    nodes = generate_bounding_6h_time_nodes(base_time, end_time)
    assert len(nodes) == 5
    for i in range(len(nodes) - 1):
        diff_seconds = (nodes[i + 1] - nodes[i]).total_seconds()
        assert diff_seconds == 21600, "Native nodes must maintain exact 6h (21600s) spacing"
