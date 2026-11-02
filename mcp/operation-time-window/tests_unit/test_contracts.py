"""Unit tests for contracts, validation, and serialization (10 test cases)."""

import json
from datetime import datetime, timedelta, timezone
import pytest
from pydantic import ValidationError

from seagent_marine_current.contracts import (
    CurrentForecastData,
    CurrentQuery,
    ForecastError,
    ForecastReply,
)


def test_valid_query_construction(base_time):
    q = CurrentQuery(
        latitude=18.5,
        longitude=112.5,
        operation_depth_m=100.0,
        start_time=base_time,
        end_time=base_time + timedelta(hours=12),
    )
    assert q.latitude == 18.5
    assert q.longitude == 112.5
    assert q.operation_depth_m == 100.0


def test_invalid_latitude_bounds(base_time):
    with pytest.raises(ValidationError):
        CurrentQuery(
            latitude=95.0,
            longitude=112.5,
            operation_depth_m=50.0,
            start_time=base_time,
            end_time=base_time + timedelta(hours=6),
        )


def test_invalid_longitude_bounds(base_time):
    with pytest.raises(ValidationError):
        CurrentQuery(
            latitude=20.0,
            longitude=-185.0,
            operation_depth_m=50.0,
            start_time=base_time,
            end_time=base_time + timedelta(hours=6),
        )


def test_invalid_depth_negative(base_time):
    with pytest.raises(ValidationError):
        CurrentQuery(
            latitude=20.0,
            longitude=110.0,
            operation_depth_m=-5.0,
            start_time=base_time,
            end_time=base_time + timedelta(hours=6),
        )


def test_invalid_time_order(base_time):
    with pytest.raises(ValueError, match="end_time .* must be strictly after start_time"):
        CurrentQuery(
            latitude=20.0,
            longitude=110.0,
            operation_depth_m=50.0,
            start_time=base_time,
            end_time=base_time - timedelta(hours=1),
        )


def test_deterministic_query_fingerprint(base_time):
    q1 = CurrentQuery(
        latitude=19.6,
        longitude=113.0,
        operation_depth_m=130.0,
        start_time=base_time,
        end_time=base_time + timedelta(hours=24),
    )
    q2 = CurrentQuery(
        latitude=19.6,
        longitude=113.0,
        operation_depth_m=130.0,
        start_time=base_time,
        end_time=base_time + timedelta(hours=24),
    )
    q3 = CurrentQuery(
        latitude=19.6,
        longitude=113.0,
        operation_depth_m=131.0,
        start_time=base_time,
        end_time=base_time + timedelta(hours=24),
    )
    assert q1.fingerprint == q2.fingerprint
    assert q1.fingerprint != q3.fingerprint


def test_forecast_reply_ok(sample_forecast):
    reply = ForecastReply(status="OK", data=sample_forecast)
    assert reply.status == "OK"
    assert reply.data is not None
    assert reply.error is None


def test_forecast_reply_not_evaluable():
    err = ForecastError(code="TIMEOUT", message="Timed out", retryable=True)
    reply = ForecastReply(status="NOT_EVALUABLE", error=err)
    assert reply.status == "NOT_EVALUABLE"
    assert reply.data is None
    assert reply.error.code == "TIMEOUT"
    assert reply.error.retryable is True


def test_forecast_reply_inconsistent_raises(sample_forecast):
    err = ForecastError(code="BUSY", message="Busy", retryable=True)
    with pytest.raises(ValueError, match="error must be null when status is OK"):
        ForecastReply(status="OK", data=sample_forecast, error=err)


def test_matrix_dimensions_mismatch_raises(base_time):
    t_nodes = [base_time, base_time + timedelta(hours=6)]
    with pytest.raises(ValueError, match="uo time dimension length"):
        CurrentForecastData(
            request_fingerprint="test_fp",
            snapshot_id="snap_123",
            provider="copernicus_marine",
            product="prod",
            dataset="ds",
            retrieved_at=base_time,
            actual_grid_latitude=19.5,
            actual_grid_longitude=113.0,
            grid_distance_km=5.0,
            native_time_steps=t_nodes,
            native_depth_layers_m=[100.0, 150.0],
            uo=[[0.1, 0.2]],  # only 1 row for 2 time steps
            vo=[[0.1, 0.2], [0.1, 0.2]],
        )
