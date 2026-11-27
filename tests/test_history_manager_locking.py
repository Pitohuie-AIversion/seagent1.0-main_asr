"""Comprehensive unit, failure-injection, and concurrency tests for BUG-4c (history_manager locking & fsync)."""

import errno
import fcntl
import json
import multiprocessing as mp
import os
import stat
import threading
from pathlib import Path
from queue import Empty
from unittest.mock import patch

import pytest

from src.session import history_manager
from src.session.history_manager import (
    _HistoryLock,
    list_history,
    load_history,
    save_conversation,
)


def _save_sample(intent_id: str | None = "TI2026080101", session_id: str = "sess_1", marker: str = "v1") -> str:
    return save_conversation(
        session_id=session_id,
        conversation_history=[{"role": "user", "content": f"msg-{marker}"}],
        task_state={"task_type_key": "pipeline_inspection", "task_id": "PI-20260801-001", "marker": marker},
        built_json={"task_id": "PI-20260801-001", "water_depth": 300.0},
        mode="normal",
        phase="collecting",
        intent_id=intent_id,
    )


def _mp_hold_history_lock(lock_path_str: str, ready_event, release_event) -> None:
    fd = os.open(lock_path_str, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ready_event.set()
        release_event.wait(timeout=20)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _mp_writer_worker(history_dir_str: str, intent_id: str, marker: str, res_queue) -> None:
    os.environ["SEAGENT_HISTORY_DIR"] = history_dir_str
    try:
        name = _save_sample(intent_id=intent_id, session_id=f"proc_{os.getpid()}", marker=marker)
        res_queue.put(("ok", name))
    except BaseException as exc:
        res_queue.put(("err", f"{type(exc).__name__}: {exc}"))


def _mp_reader_worker(history_dir_str: str, history_id: str, res_queue) -> None:
    os.environ["SEAGENT_HISTORY_DIR"] = history_dir_str
    try:
        data = load_history(history_id)
        res_queue.put(("ok", data.get("task_state", {}).get("marker") if data else None))
    except BaseException as exc:
        res_queue.put(("err", f"{type(exc).__name__}: {exc}"))


def _mp_concurrent_hammer_worker(history_dir_str: str, worker_idx: int, iterations: int, res_queue) -> None:
    os.environ["SEAGENT_HISTORY_DIR"] = history_dir_str
    try:
        for i in range(iterations):
            name = _save_sample(intent_id="TI_MP_SHARED", session_id=f"w{worker_idx}", marker=f"{worker_idx}_{i}")
            loaded = load_history(name)
            assert isinstance(loaded, dict) and loaded["intent_id"] == "TI_MP_SHARED"
            listed = list_history()
            assert any(item["id"] == name for item in listed)
        res_queue.put(("ok", worker_idx))
    except BaseException as exc:
        res_queue.put(("err", f"{type(exc).__name__}: {exc}"))


def test_save_creates_dotfile_lock_with_0600_and_list_filters_dotfiles(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_HISTORY_DIR", str(tmp_path))

    filename = _save_sample(intent_id="TI2026080101")
    lock_file = tmp_path / ".history_manager.lock"
    assert lock_file.is_file()
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600

    temp_files = [p for p in tmp_path.iterdir() if ".tmp_" in p.name]
    assert temp_files == []

    records = list_history()
    assert [r["id"] for r in records] == [filename]


def test_save_uses_dotfile_temp_with_excl_nofollow_and_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_HISTORY_DIR", str(tmp_path))

    open_calls = []
    real_open = os.open

    def spy_open(path, flags, mode=0o777, *args, **kwargs):
        open_calls.append((Path(path), flags, mode))
        return real_open(path, flags, mode, *args, **kwargs)

    with patch("os.open", side_effect=spy_open):
        filename = _save_sample(intent_id="TI2026080102")

    temp_opens = [c for c in open_calls if ".tmp_" in c[0].name]
    assert len(temp_opens) == 1
    temp_path, temp_flags, temp_mode = temp_opens[0]
    assert temp_path.name.startswith(f".{filename}.tmp_")
    assert temp_flags & os.O_CREAT
    assert temp_flags & os.O_EXCL
    assert (temp_flags & os.O_ACCMODE) == os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        assert temp_flags & os.O_NOFOLLOW
    assert temp_mode == 0o600


def test_save_fsyncs_temp_file_and_parent_directory_in_order(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_HISTORY_DIR", str(tmp_path))

    events = []
    real_fsync = os.fsync
    real_replace = os.replace

    def spy_fsync(fd):
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            events.append("dir_fsync")
        else:
            events.append("file_fsync")
        return real_fsync(fd)

    def spy_replace(src, dst):
        events.append("replace")
        return real_replace(src, dst)

    with patch("os.fsync", side_effect=spy_fsync), patch("os.replace", side_effect=spy_replace):
        _save_sample(intent_id="TI2026080103")

    assert events == ["file_fsync", "replace", "dir_fsync"]


def test_save_file_fsync_failure_cleans_temp_and_preserves_existing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_HISTORY_DIR", str(tmp_path))

    filename = _save_sample(intent_id="TI2026080104", marker="original")
    target = tmp_path / filename
    original_bytes = target.read_bytes()

    real_fsync = os.fsync

    def fail_file_fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "file fsync error")
        return real_fsync(fd)

    with patch("os.fsync", side_effect=fail_file_fsync):
        with pytest.raises(OSError):
            _save_sample(intent_id="TI2026080104", marker="updated")

    assert target.read_bytes() == original_bytes
    assert [p for p in tmp_path.iterdir() if ".tmp_" in p.name] == []


def test_save_directory_fsync_failure_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_HISTORY_DIR", str(tmp_path))

    real_fsync = os.fsync

    def fail_dir_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "dir fsync error")
        return real_fsync(fd)

    with patch("os.fsync", side_effect=fail_dir_fsync):
        with pytest.raises(OSError, match="dir fsync error"):
            _save_sample(intent_id="TI2026080105")

    assert [p for p in tmp_path.iterdir() if ".tmp_" in p.name] == []


@pytest.mark.parametrize("body_fails", [False, True])
def test_unlock_failure_closes_fd_and_releases_lock(tmp_path, monkeypatch, body_fails):
    lock = _HistoryLock(tmp_path, exclusive=True)
    real_flock = fcntl.flock
    body_error = RuntimeError("history save failed")

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
        with lock.lock_path.open("r+") as contender:
            real_flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            real_flock(contender.fileno(), fcntl.LOCK_UN)
    finally:
        if held_fd is not None:
            try:
                os.close(held_fd)
            except OSError:
                pass


@pytest.mark.parametrize("failure", [OSError(errno.EIO, "lock failed"), KeyboardInterrupt()])
def test_failed_flock_acquisition_closes_fd_and_preserves_error(tmp_path, monkeypatch, failure):
    lock = _HistoryLock(tmp_path, exclusive=True)
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
        with pytest.raises(type(failure)) as caught:
            with lock:
                pytest.fail("failed acquisition must not enter body")
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


def test_close_error_does_not_mask_body_error_or_double_close(tmp_path, monkeypatch):
    lock = _HistoryLock(tmp_path, exclusive=True)
    real_close = os.close
    close_calls = []
    body_error = RuntimeError("body error")

    def close_then_fail(fd):
        close_calls.append(fd)
        real_close(fd)
        raise OSError(errno.EIO, "close failed")

    monkeypatch.setattr(os, "close", close_then_fail)
    with pytest.raises(RuntimeError) as caught:
        with lock:
            raise body_error
    assert caught.value is body_error
    lock.__exit__(None, None, None)
    assert len(close_calls) == 1
    assert lock._fd is None


def test_symlink_lockfile_rejected(tmp_path):
    target = tmp_path / "external_lock_target"
    target.write_text("untouched", encoding="utf-8")
    os.symlink(target, tmp_path / ".history_manager.lock")

    with pytest.raises(OSError):
        with _HistoryLock(tmp_path, exclusive=True):
            pytest.fail("symlink lockfile must be rejected")

    assert target.read_text(encoding="utf-8") == "untouched"


def test_reentrant_ex_then_sh_does_not_deadlock_and_keeps_ex_until_outer_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_HISTORY_DIR", str(tmp_path))
    filename = _save_sample(intent_id="TI2026080106")

    with _HistoryLock(tmp_path, exclusive=True):
        records = list_history()
        loaded = load_history(filename)
        assert len(records) == 1
        assert loaded is not None

        # Another independent fd must still see LOCK_EX held until outer exits.
        with (tmp_path / ".history_manager.lock").open("r+") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    with (tmp_path / ".history_manager.lock").open("r+") as contender:
        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender.fileno(), fcntl.LOCK_UN)


def test_lock_upgrade_sh_to_ex_fails_closed_without_deadlock(tmp_path):
    with _HistoryLock(tmp_path, exclusive=False):
        with pytest.raises(RuntimeError, match="Cannot upgrade shared history lock"):
            with _HistoryLock(tmp_path, exclusive=True):
                pytest.fail("upgrade must not succeed")

    with (tmp_path / ".history_manager.lock").open("r+") as contender:
        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender.fileno(), fcntl.LOCK_UN)


def test_save_and_load_reject_symlinks_and_list_skips_symlinks(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_HISTORY_DIR", str(tmp_path))
    real_name = _save_sample(intent_id="TI2026080107")
    real_file = tmp_path / real_name

    # Intra-dir symlink
    intra_sym = tmp_path / "intra_link.json"
    os.symlink(real_file, intra_sym)
    with pytest.raises(ValueError, match="symbolic-link"):
        load_history("intra_link.json")

    # External symlink
    external_target = tmp_path.parent / "outside_history.json"
    external_target.write_text('{"conversation_history": [], "saved_at": "2026-08-01T00:00:00"}', encoding="utf-8")
    ext_sym = tmp_path / "ext_link.json"
    os.symlink(external_target, ext_sym)
    with pytest.raises(ValueError, match="symbolic-link"):
        load_history("ext_link.json")

    # list_history skips symlinks cleanly
    assert [item["id"] for item in list_history()] == [real_name]

    # save_conversation refuses symlink target
    target_sym = tmp_path / "history_TI_SYM_TARGET.json"
    os.symlink(external_target, target_sym)
    with pytest.raises(ValueError, match="symbolic-link"):
        _save_sample(intent_id="TI_SYM_TARGET")


def test_read_operations_on_missing_dir_do_not_create_dir_or_lock(tmp_path, monkeypatch):
    missing_dir = tmp_path / "nonexistent_history"
    monkeypatch.setenv("SEAGENT_HISTORY_DIR", str(missing_dir))

    assert list_history() == []
    assert load_history("missing.json") is None
    assert not missing_dir.exists()


def test_concurrent_threads_save_list_load_same_intent(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_HISTORY_DIR", str(tmp_path))
    errors = []

    def worker(idx: int):
        try:
            for j in range(10):
                name = _save_sample(intent_id="TI_THREAD_SHARED", session_id=f"t{idx}", marker=f"{idx}_{j}")
                data = load_history(name)
                assert isinstance(data, dict) and data["intent_id"] == "TI_THREAD_SHARED"
                listed = list_history()
                assert any(item["id"] == name for item in listed)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(idx,)) for idx in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert [p for p in tmp_path.iterdir() if ".tmp_" in p.name] == []
    final_data = load_history("history_TI_THREAD_SHARED.json")
    assert isinstance(final_data, dict)


def test_multiprocess_flock_blocks_concurrent_writer_and_reader(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_HISTORY_DIR", str(tmp_path))
    filename = _save_sample(intent_id="TI_MP_LOCK", marker="initial")
    lock_path = tmp_path / ".history_manager.lock"
    ctx = mp.get_context("spawn")

    # 1. Holding LOCK_EX blocks save_conversation
    ready1 = ctx.Event()
    release1 = ctx.Event()
    res_q1 = ctx.Queue()
    p_holder1 = ctx.Process(target=_mp_hold_history_lock, args=(str(lock_path), ready1, release1))
    p_writer = ctx.Process(target=_mp_writer_worker, args=(str(tmp_path), "TI_MP_LOCK", "after_lock", res_q1))
    try:
        p_holder1.start()
        assert ready1.wait(timeout=15)
        p_writer.start()
        with pytest.raises(Empty):
            res_q1.get(timeout=0.3)
        assert p_writer.is_alive()
        release1.set()
        assert res_q1.get(timeout=15) == ("ok", filename)
        p_holder1.join(timeout=15)
        p_writer.join(timeout=15)
        assert p_holder1.exitcode == 0 and p_writer.exitcode == 0
    finally:
        release1.set()
        for proc in (p_holder1, p_writer):
            if proc.is_alive():
                proc.terminate()
            proc.join(timeout=5)
            proc.close()

    # 2. Holding LOCK_EX blocks load_history (LOCK_SH)
    ready2 = ctx.Event()
    release2 = ctx.Event()
    res_q2 = ctx.Queue()
    p_holder2 = ctx.Process(target=_mp_hold_history_lock, args=(str(lock_path), ready2, release2))
    p_reader = ctx.Process(target=_mp_reader_worker, args=(str(tmp_path), filename, res_q2))
    try:
        p_holder2.start()
        assert ready2.wait(timeout=15)
        p_reader.start()
        with pytest.raises(Empty):
            res_q2.get(timeout=0.3)
        assert p_reader.is_alive()
        release2.set()
        assert res_q2.get(timeout=15) == ("ok", "after_lock")
        p_holder2.join(timeout=15)
        p_reader.join(timeout=15)
        assert p_holder2.exitcode == 0 and p_reader.exitcode == 0
    finally:
        release2.set()
        for proc in (p_holder2, p_reader):
            if proc.is_alive():
                proc.terminate()
            proc.join(timeout=5)
            proc.close()


def test_multiprocess_concurrent_saves_and_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_HISTORY_DIR", str(tmp_path))
    ctx = mp.get_context("spawn")
    res_queue = ctx.Queue()
    procs = [
        ctx.Process(target=_mp_concurrent_hammer_worker, args=(str(tmp_path), idx, 8, res_queue))
        for idx in range(4)
    ]
    try:
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
            assert p.exitcode == 0
        results = [res_queue.get(timeout=5) for _ in procs]
        assert sorted(results) == [("ok", 0), ("ok", 1), ("ok", 2), ("ok", 3)]
        assert [p for p in tmp_path.iterdir() if ".tmp_" in p.name] == []
        loaded = load_history("history_TI_MP_SHARED.json")
        assert isinstance(loaded, dict) and loaded["intent_id"] == "TI_MP_SHARED"
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
            p.join(timeout=5)
            p.close()
