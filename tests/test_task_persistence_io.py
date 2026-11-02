"""Exercise short writes and descriptor ownership through real task persistence."""

import errno
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.exceptions import TaskPersistenceError
from src.knowledge_retriever import KnowledgeBase
from src.dispatch.task_intent_builder import TaskIntentBuilder


@pytest.fixture
def persistence(tmp_path, monkeypatch):
    monkeypatch.setattr("src.dispatch.task_intent_builder.get_task_dir", lambda **kwargs: tmp_path)
    intent = {
        "schema_version": 2,
        "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "task_id": "PI-20260721-001",
        "intent_id": "TI2026072101",
        "task_type": "pipeline_inspection",
        "priority": 7,
        "time": {"start": None, "end": None},
        "location": {"oilfield": None, "water_depth_m": 300.0},
        "task": {"type": "pipeline_inspection", "details": {}},
        "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": {"name": None}},
        "conditions": {},
    }
    return TaskIntentBuilder(KnowledgeBase()), intent, tmp_path


@pytest.mark.parametrize("stage", ["staging", "publish"])
def test_short_writes_produce_complete_json(persistence, monkeypatch, stage):
    builder, intent, task_dir = persistence
    staging = builder.create_staging(intent) if stage == "publish" else None
    real_write = os.write

    def short_write(fd, content):
        return real_write(fd, content[:7])

    monkeypatch.setattr(os, "write", short_write)
    if stage == "staging":
        result = builder.create_staging(intent)
    else:
        result = task_dir / builder.publish_staging(staging, intent)
    assert json.loads(result.read_text(encoding="utf-8")) == intent


@pytest.mark.parametrize("stage", ["staging", "publish"])
def test_zero_progress_write_fails_without_publishing(persistence, monkeypatch, stage):
    builder, intent, task_dir = persistence
    staging = builder.create_staging(intent) if stage == "publish" else None
    calls = []

    def stalled_write(fd, content):
        calls.append(fd)
        # Bound this regression test even if a retry loop forgets to stop on zero.
        if len(calls) > 1:
            raise AssertionError("must stop when write makes no progress")
        return 0

    monkeypatch.setattr(os, "write", stalled_write)
    with pytest.raises(TaskPersistenceError):
        if stage == "staging":
            builder.create_staging(intent)
        else:
            builder.publish_staging(staging, intent)
    assert len(calls) == 1
    assert not (task_dir / f"task_intent_{intent['intent_id']}.json").exists()


@pytest.mark.parametrize(
    ("stage", "failure"),
    [(stage, failure) for stage in ("staging", "publish") for failure in ("fstat", "fdopen")]
    + [("staging", "not_regular")],
)
def test_read_setup_failure_closes_descriptor(persistence, monkeypatch, stage, failure):
    builder, intent, task_dir = persistence
    staging = builder.create_staging(intent)
    real_open, real_fstat, real_fdopen = os.open, os.fstat, os.fdopen
    target_fds = []

    def track_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        matches = Path(path) == staging if stage == "staging" else Path(path).name.startswith(".tmp_publish_")
        if matches and flags & os.O_ACCMODE == os.O_RDONLY:
            target_fds.append(fd)
        return fd

    def fail_stat(fd):
        if fd in target_fds:
            if failure == "fstat":
                raise OSError(errno.EIO, "stat failed")
            if failure == "not_regular":
                return SimpleNamespace(st_mode=stat.S_IFDIR)
        return real_fstat(fd)

    def fail_fdopen(fd, *args, **kwargs):
        if fd in target_fds and failure == "fdopen":
            raise OSError(errno.EIO, "fdopen failed")
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "fstat", fail_stat)
    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    try:
        with pytest.raises(TaskPersistenceError):
            builder.publish_staging(staging, intent)
        assert target_fds
        for fd in target_fds:
            with pytest.raises(OSError) as closed:
                real_fstat(fd)
            assert closed.value.errno == errno.EBADF
        assert not (task_dir / f"task_intent_{intent['intent_id']}.json").exists()
    finally:
        for fd in set(target_fds):
            try:
                os.close(fd)
            except OSError:
                pass
