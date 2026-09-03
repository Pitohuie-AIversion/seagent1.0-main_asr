"""Global pytest configuration fixture for SEAgent test suite."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.runtime_isolation import configure_test_artifact_paths

# Configure isolated test artifact paths before running tests
configure_test_artifact_paths()
