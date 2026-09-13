"""Unit test fixtures and synthetic data generators."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

# Ensure src is in sys.path
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from seagent_marine_current.contracts import CurrentForecastData, CurrentQuery
from seagent_marine_current.provider import SyntheticCopernicusProvider


@pytest.fixture(autouse=True)
def enforce_test_profile(monkeypatch):
    """Set SEAGENT_CURRENT_ENV=test explicitly for unit test suite by default."""
    monkeypatch.setenv("SEAGENT_CURRENT_ENV", "test")


@pytest.fixture
def base_time():
    """Fixed reference datetime in UTC."""
    return datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def sample_query(base_time):
    """Standard valid query."""
    return CurrentQuery(
        latitude=19.6,
        longitude=113.0,
        operation_depth_m=130.0,
        start_time=base_time,
        end_time=base_time + timedelta(hours=48),
    )


@pytest.fixture
def sample_forecast(sample_query):
    """Standard synthetic forecast covering 48 hours."""
    provider = SyntheticCopernicusProvider(base_speed_mps=0.4)
    return provider.fetch(sample_query)
