"""Failure paths must release real publish locks and preserve original errors."""

import errno
import fcntl
import os

import pytest

from src.exceptions import TaskPersistenceError
from src.task_intent_builder import TaskPublishLock


@pytest.mark.parametrize("body_fails", [False, True])
def test_unlock_failure_closes_descriptor_and_releases_lock(tmp_path, monkeypatch, body_fails):
    lock = TaskPublishLock(tmp_path)
    real_flock = fcntl.flock
    body_error = RuntimeError("publish failed")

    def fail_unlock(fd, operation):
        if operation == fcntl.LOCK_UN:
            raise OSError(errno.EIO, "unlock failed")
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", fail_unlock)
    held_fd = None
    try:
        try:
            with lock:
                held_fd = lock._fd
                if body_fails:
                    raise body_error
        except RuntimeError as exc:
            assert body_fails and exc is body_error
        else:
            assert not body_fails

        with pytest.raises(OSError) as closed:
            os.fstat(held_fd)
        assert closed.value.errno == errno.EBADF
        # A separately opened descriptor must acquire the same lock immediately.
        with lock.lock_path.open("r+") as contender:
            real_flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        # Also clean up when run against the leaking implementation.
        if held_fd is not None:
            try:
                os.close(held_fd)
            except OSError:
                pass


@pytest.mark.parametrize("failure", [OSError(errno.EIO, "lock failed"), KeyboardInterrupt()])
def test_failed_acquisition_closes_descriptor_and_preserves_error(tmp_path, monkeypatch, failure):
    lock = TaskPublishLock(tmp_path)
    opened = []
    real_open = os.open

    def record_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def fail_acquisition(fd, operation):
        raise failure

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(fcntl, "flock", fail_acquisition)
    try:
        expected = TaskPersistenceError if isinstance(failure, OSError) else KeyboardInterrupt
        with pytest.raises(expected) as caught:
            with lock:
                pytest.fail("failed acquisition must not enter the body")
        if isinstance(failure, OSError):
            assert caught.value.__cause__ is failure
        else:
            assert caught.value is failure
        with pytest.raises(OSError) as closed:
            os.fstat(opened[0])
        assert closed.value.errno == errno.EBADF
        assert lock._fd is None
    finally:
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass


def test_close_error_does_not_mask_body_error_or_retry_descriptor(tmp_path, monkeypatch):
    lock = TaskPublishLock(tmp_path)
    real_close = os.close
    close_calls = []
    body_error = RuntimeError("publish failed")

    def close_then_fail(fd):
        close_calls.append(fd)
        real_close(fd)
        raise OSError(errno.EIO, "close reported an error")

    monkeypatch.setattr(os, "close", close_then_fail)
    with pytest.raises(RuntimeError) as caught:
        with lock:
            raise body_error
    assert caught.value is body_error
    lock.__exit__(None, None, None)
    assert len(close_calls) == 1
    assert lock._fd is None
