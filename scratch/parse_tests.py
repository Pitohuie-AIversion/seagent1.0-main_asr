"""
scratch/parse_tests.py — Backward compatibility facade.
Delegates to scripts/parse_tests.py.
"""

from scripts.parse_tests import (
    AuditTestResult,
    get_git_commit,
    configure_test_artifact_paths,
)

__all__ = [
    "AuditTestResult",
    "get_git_commit",
    "configure_test_artifact_paths",
]

if __name__ == "__main__":
    from scripts.parse_tests import main
    main()
