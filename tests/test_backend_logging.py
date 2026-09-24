"""Tests for backend_logging module."""

import io
from pathlib import Path
import sys

from backend_logging import TeeStream, setup_backend_logging


def test_tee_stream():
    out1 = io.StringIO()
    out2 = io.StringIO()
    tee = TeeStream(out1, out2)

    tee.write("hello world\n")
    tee.flush()

    assert out1.getvalue() == "hello world\n"
    assert out2.getvalue() == "hello world\n"


def test_setup_backend_logging_explicit_dir(tmp_path):
    target_dir = tmp_path / "custom_logs"
    log_path = setup_backend_logging(log_dir=target_dir, app_name="test_app")

    assert log_path.exists()
    assert log_path.parent == target_dir
    assert log_path.name.startswith("test_app_")
    assert log_path.suffix == ".log"


def test_setup_backend_logging_env_dir(tmp_path, monkeypatch):
    env_dir = tmp_path / "env_logs"
    monkeypatch.setenv("SEAGENT_LOG_DIR", str(env_dir))

    log_path = setup_backend_logging(app_name="env_app")

    assert log_path.exists()
    assert log_path.parent == env_dir
    assert log_path.name.startswith("env_app_")


def test_setup_backend_logging_unwritable_fallback(monkeypatch, tmp_path):
    # Simulate unwritable DEFAULT_LOG_DIR
    unwritable = tmp_path / "unwritable"
    monkeypatch.setattr("backend_logging.DEFAULT_LOG_DIR", unwritable)
    monkeypatch.delenv("SEAGENT_LOG_DIR", raising=False)

    def mock_mkdir(*args, **kwargs):
        raise PermissionError("Access denied")

    # Only patch mkdir on unwritable
    original_mkdir = Path.mkdir

    def selective_mkdir(self, *args, **kwargs):
        if str(self).startswith(str(unwritable)):
            raise PermissionError("Access denied")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", selective_mkdir)

    log_path = setup_backend_logging(app_name="fallback_app")
    assert log_path.exists()
    assert "fallback_app_" in log_path.name
