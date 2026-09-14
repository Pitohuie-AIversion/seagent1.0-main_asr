"""Security boundary tests enforcing v0.1 single source of truth (Copernicus Marine).

Strict requirements:
1. In production environment, setting SEAGENT_CURRENT_USE_SYNTHETIC=1 must be rejected with RuntimeError.
2. Synthetic Forecast fixture is strictly forbidden from yielding AVAILABLE in formal OperationWindowService;
   it must return NOT_EVALUABLE with reason_code="SYNTHETIC_DATA_NOT_ALLOWED".
"""

from datetime import datetime, timedelta, timezone
import os
import pytest

from seagent_marine_current.backend import WorkerBackend
from seagent_marine_current.contracts import CurrentQuery, SyntheticDataSecurityError
from seagent_marine_current.provider import SyntheticCopernicusProvider
from seagent_marine_current.windows import OperationWindowService


def test_production_environment_rejects_synthetic_configuration(monkeypatch):
    """P1 Test: In production, attempting to switch to synthetic provider must fail immediately."""
    monkeypatch.setenv("SEAGENT_CURRENT_ENV", "production")
    monkeypatch.setenv("SEAGENT_CURRENT_USE_SYNTHETIC", "1")

    with pytest.raises(SyntheticDataSecurityError) as exc_info:
        WorkerBackend()

    err_msg = str(exc_info.value)
    assert "Security Violation" in err_msg
    assert "Synthetic test fixture is strictly forbidden in production" in err_msg


def test_test_environment_allows_synthetic_fixture(monkeypatch):
    """In explicit test environment, synthetic fixture can be instantiated for unit tests."""
    monkeypatch.setenv("SEAGENT_CURRENT_ENV", "test")
    monkeypatch.setenv("SEAGENT_CURRENT_USE_SYNTHETIC", "1")

    backend = WorkerBackend()
    assert backend.worker is not None
    assert isinstance(backend.worker.provider, SyntheticCopernicusProvider)


def test_synthetic_forecast_rejected_by_formal_operation_window_check(sample_query):
    """P1 Test: Synthetic forecast MUST NOT yield AVAILABLE in formal CHECK service."""
    provider = SyntheticCopernicusProvider(base_speed_mps=0.3)
    synth_forecast = provider.fetch(sample_query)
    assert synth_forecast.is_synthetic is True

    # 1. Formal CHECK call (allow_synthetic_for_testing=False by default)
    check_result = OperationWindowService.check_window(
        forecast=synth_forecast,
        operation_depth_m=sample_query.operation_depth_m,
        start_time=sample_query.start_time,
        end_time=sample_query.end_time,
        current_limit_mps=1.0,
        allow_synthetic_for_testing=False,  # default in production
    )
    assert check_result.status == "NOT_EVALUABLE"
    assert check_result.reason_code == "SYNTHETIC_DATA_NOT_ALLOWED"
    assert "Synthetic test data cannot be used" in check_result.message


def test_synthetic_forecast_rejected_by_formal_operation_window_search(sample_query):
    """P1 Test: Synthetic forecast MUST NOT yield AVAILABLE in formal SEARCH service."""
    provider = SyntheticCopernicusProvider(base_speed_mps=0.3)
    synth_forecast = provider.fetch(sample_query)
    assert synth_forecast.is_synthetic is True

    # Formal SEARCH call (allow_synthetic_for_testing=False by default)
    search_result = OperationWindowService.search_windows(
        forecast=synth_forecast,
        operation_depth_m=sample_query.operation_depth_m,
        search_start=sample_query.start_time,
        search_end=sample_query.end_time,
        duration_hours=4.0,
        current_limit_mps=1.0,
        allow_synthetic_for_testing=False,  # default in production
    )
    assert search_result.status == "NOT_EVALUABLE"
    assert search_result.reason_code == "SYNTHETIC_DATA_NOT_ALLOWED"
    assert "Synthetic test data cannot be used" in search_result.message


def test_production_environment_rejects_synthetic_even_if_caller_flags_allow(monkeypatch, sample_query):
    """P1 Bypass Protection: In production, passing allow_synthetic_for_testing=True MUST STILL BE REJECTED."""
    monkeypatch.setenv("SEAGENT_CURRENT_ENV", "production")

    provider = SyntheticCopernicusProvider(base_speed_mps=0.3)
    synth_forecast = provider.fetch(sample_query)
    assert synth_forecast.is_synthetic is True

    # 1. Attempted CHECK bypass
    check_result = OperationWindowService.check_window(
        forecast=synth_forecast,
        operation_depth_m=sample_query.operation_depth_m,
        start_time=sample_query.start_time,
        end_time=sample_query.end_time,
        current_limit_mps=1.0,
        allow_synthetic_for_testing=True,  # Attacker or misconfigured caller tries to bypass!
    )
    assert check_result.status == "NOT_EVALUABLE"
    assert check_result.reason_code == "SYNTHETIC_DATA_NOT_ALLOWED"

    # 2. Attempted SEARCH bypass
    search_result = OperationWindowService.search_windows(
        forecast=synth_forecast,
        operation_depth_m=sample_query.operation_depth_m,
        search_start=sample_query.start_time,
        search_end=sample_query.end_time,
        duration_hours=4.0,
        current_limit_mps=1.0,
        allow_synthetic_for_testing=True,  # Attacker or misconfigured caller tries to bypass!
    )
    assert search_result.status == "NOT_EVALUABLE"
    assert search_result.reason_code == "SYNTHETIC_DATA_NOT_ALLOWED"


def test_production_environment_rejects_current_processor_direct_call(monkeypatch, sample_query):
    """P1 Bypass Protection: In production, calling CurrentProcessor directly on synthetic data raises Security Violation."""
    from seagent_marine_current.processing import CurrentProcessor

    monkeypatch.setenv("SEAGENT_CURRENT_ENV", "production")

    provider = SyntheticCopernicusProvider(base_speed_mps=0.3)
    synth_forecast = provider.fetch(sample_query)
    assert synth_forecast.is_synthetic is True

    with pytest.raises(SyntheticDataSecurityError) as exc_info:
        CurrentProcessor.interpolate_depth_at_nodes(synth_forecast, 130.0)

    err = str(exc_info.value)
    assert "Security Violation" in err
    assert "strictly restricted to SEAGENT_CURRENT_ENV=test" in err


def test_test_environment_requires_explicit_caller_authorization(monkeypatch, sample_query):
    """In test environment, synthetic forecast still requires explicit allow_synthetic_for_testing=True."""
    monkeypatch.setenv("SEAGENT_CURRENT_ENV", "test")

    provider = SyntheticCopernicusProvider(base_speed_mps=0.3)
    synth_forecast = provider.fetch(sample_query)

    # Without explicit allow -> still rejected
    res_no_allow = OperationWindowService.check_window(
        forecast=synth_forecast,
        operation_depth_m=sample_query.operation_depth_m,
        start_time=sample_query.start_time,
        end_time=sample_query.end_time,
        current_limit_mps=1.0,
        allow_synthetic_for_testing=False,
    )
    assert res_no_allow.status == "NOT_EVALUABLE"
    assert res_no_allow.reason_code == "SYNTHETIC_DATA_NOT_ALLOWED"

    # With explicit allow -> permitted for unit testing algorithms
    res_allowed = OperationWindowService.check_window(
        forecast=synth_forecast,
        operation_depth_m=sample_query.operation_depth_m,
        start_time=sample_query.start_time,
        end_time=sample_query.end_time,
        current_limit_mps=1.0,
        allow_synthetic_for_testing=True,
    )
    assert res_allowed.status in ("AVAILABLE", "UNAVAILABLE")
