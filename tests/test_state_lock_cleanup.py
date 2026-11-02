"""Robot state lock setup must release descriptors even before acquiring a lock."""

import errno
import os

import pytest

from src.exceptions import StatePersistenceError
from src.state_info import RobotStateInfo


@pytest.mark.parametrize("operation", ["fchmod", "flock"])
@pytest.mark.parametrize("failure", [OSError(errno.EIO, "setup failed"), KeyboardInterrupt()])
def test_state_lock_setup_failure_closes_descriptor(tmp_path, monkeypatch, operation, failure):
    store = RobotStateInfo()
    store.state_file = tmp_path / "state.yaml"
    opened = []
    real_open = os.open

    def record_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def fail_setup(*args):
        raise failure

    monkeypatch.setattr("src.state_info.os.open", record_open)
    target = "os.fchmod" if operation == "fchmod" else "fcntl.flock"
    monkeypatch.setattr(f"src.state_info.{target}", fail_setup)
    try:
        expected = StatePersistenceError if isinstance(failure, OSError) else KeyboardInterrupt
        with pytest.raises(expected) as caught:
            with store._snapshot_lock(exclusive=True):
                pytest.fail("failed setup must not enter the body")
        if isinstance(failure, OSError):
            assert caught.value.__cause__ is failure
        else:
            assert caught.value is failure
        with pytest.raises(OSError) as closed:
            os.fstat(opened[0])
        assert closed.value.errno == errno.EBADF
    finally:
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass
