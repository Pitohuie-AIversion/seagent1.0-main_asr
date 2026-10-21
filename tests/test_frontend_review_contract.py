"""Keep product regression coverage when optional browser tooling is unavailable."""

from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is required")
def test_business_time_and_translation_contract():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "tests/frontend_review_contract.cjs"],
        cwd=root, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
