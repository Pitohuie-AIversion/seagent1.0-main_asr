"""Live integration test against official Copernicus Marine API.

Strict rules from Task Spec Section 17:
- Must NOT use mock to pretend real data test.
- If credentials or internet unavailable, marks test as skipped with explicit reason.
- When credentials present, executes real remote dataset retrieval.
"""

import os
from datetime import datetime, timedelta, timezone
import pytest

from seagent_marine_current.contracts import CurrentQuery
from seagent_marine_current.provider import CopernicusProvider, ProviderError


@pytest.mark.integration
def test_live_copernicus_remote_forecast():
    """Real live test querying Copernicus Marine Toolbox if credentials provided."""
    username = os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME")
    password = os.environ.get("COPERNICUSMARINE_SERVICE_PASSWORD")

    if not username or not password:
        pytest.skip(
            "Live Copernicus integration test skipped: COPERNICUSMARINE_SERVICE_USERNAME "
            "or COPERNICUSMARINE_SERVICE_PASSWORD not configured in environment."
        )

    # Future 24-hour interval
    now_utc = datetime.now(timezone.utc)
    st = now_utc + timedelta(days=1)
    et = st + timedelta(hours=24)

    query = CurrentQuery(
        latitude=19.6,
        longitude=113.0,
        operation_depth_m=130.0,
        start_time=st,
        end_time=et,
    )

    provider = CopernicusProvider(username=username, password=password)
    try:
        data = provider.fetch(query)
        assert data is not None
        assert data.provider == "copernicus_marine"
        assert len(data.native_time_steps) >= 2
        assert len(data.native_depth_layers_m) in (1, 2)
        assert len(data.uo) == len(data.native_time_steps)
    except ProviderError as err:
        pytest.fail(f"Live Copernicus provider failed: [{err.code}] {err.message}")
    except Exception as exc:
        pytest.fail(f"Live Copernicus unexpected connection exception: {exc}")
