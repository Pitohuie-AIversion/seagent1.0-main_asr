"""MCP protocol test fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path
import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


@pytest.fixture(autouse=True)
def clean_copernicus_env():
    """Ensure environment credentials do not leak into unconfigured tests."""
    old_user = os.environ.pop("COPERNICUSMARINE_SERVICE_USERNAME", None)
    old_pwd = os.environ.pop("COPERNICUSMARINE_SERVICE_PASSWORD", None)
    old_synth = os.environ.pop("SEAGENT_CURRENT_USE_SYNTHETIC", None)
    yield
    if old_user:
        os.environ["COPERNICUSMARINE_SERVICE_USERNAME"] = old_user
    if old_pwd:
        os.environ["COPERNICUSMARINE_SERVICE_PASSWORD"] = old_pwd
    if old_synth:
        os.environ["SEAGENT_CURRENT_USE_SYNTHETIC"] = old_synth
