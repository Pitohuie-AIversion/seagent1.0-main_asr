"""
atomic_task_storage.py — 任务落盘跨进程排他锁与 POSIX 原子写入基础设施
"""
import copy
import errno
import fcntl
import logging
import os
import re
import stat
import threading
import uuid
from pathlib import Path
from typing import Optional

from src.exceptions import TaskPersistenceError
from .result_paths import get_quarantine_dir

logger = logging.getLogger(__name__)


class TaskCommitUncertainError(TaskPersistenceError):
    """The official file is visible, but its durability was not confirmed."""

    def __init__(self, message: str, intent: dict):
        super().__init__(message)
        self.intent = copy.deepcopy(intent)


class TaskPublishLock:
    """进程间与线程间任务发布排他锁"""

    def __init__(self, task_dir: Path):
        self.lock_path = task_dir / ".task_intent_publish.lock"
        self._fd = None

    def __enter__(self):
        try:
            self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except BaseException as e:
            # Interrupted acquisition never reaches __exit__, so release here too.
            self._close_fd()
            if isinstance(e, Exception):
                raise TaskPersistenceError(f"Failed to acquire publish lock: {e}") from e
            raise
        return self

    def _close_fd(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                logger.debug("Failed to close publish lock %s", self.lock_path, exc_info=True)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except Exception:
                logger.debug("Failed to unlock publish lock %s", self.lock_path, exc_info=True)
            finally:
                self._close_fd()


def _write_all(fd: int, content: bytes) -> None:
    """Write the complete buffer, including when the OS accepts only a prefix."""
    remaining = memoryview(content)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise TaskPersistenceError("Task file write made no progress")
        remaining = remaining[written:]


def _atomic_commit_noreplace(temp_file: Path, final_file: Path) -> None:
    """原子提交临时文件为正式文件，已存在时拒绝覆盖"""
    if final_file.exists():
        raise FileExistsError(f"Final file already exists: {final_file}")

    try:
        os.link(temp_file, final_file)
        try:
            temp_file.unlink()
        except Exception as unlink_err:
            logger.debug("Failed to unlink temp file %s after hardlink: %s", temp_file, unlink_err)
    except FileExistsError:
        raise
    except Exception as e:
        raise TaskPersistenceError(f"Atomic commit failed: {e}") from e


def _sync_directory(directory: Path) -> None:
    """Flush directory entry updates to disk via fsync on an O_DIRECTORY fd."""
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_all_fd(fd: int) -> bytes:
    """Read complete byte content from a regular-file descriptor."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _verify_fd_ownership(
    fd: int,
    path: Path,
    expected_stat: Optional[os.stat_result] = None,
    expected_bytes: Optional[bytes] = None,
) -> bool:
    """Verify fd is a regular file matching expected stat/content and path's current lstat."""
    try:
        fd_stat = os.fstat(fd)
    except OSError:
        return False

    if not stat.S_ISREG(fd_stat.st_mode):
        return False

    if expected_stat is not None:
        if (fd_stat.st_dev, fd_stat.st_ino, fd_stat.st_size) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
            expected_stat.st_size,
        ):
            return False

    if expected_bytes is not None:
        if fd_stat.st_size != len(expected_bytes):
            return False
        try:
            actual_bytes = _read_all_fd(fd)
        except OSError:
            return False
        if actual_bytes != expected_bytes:
            return False

    try:
        path_stat = os.lstat(path)
    except OSError:
        return False

    if not stat.S_ISREG(path_stat.st_mode):
        return False

    return (path_stat.st_dev, path_stat.st_ino, path_stat.st_size) == (
        fd_stat.st_dev,
        fd_stat.st_ino,
        fd_stat.st_size,
    )


def _same_regular_stat(path: Path, bound_stat: os.stat_result) -> bool:
    try:
        current = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(current.st_mode) and (
        current.st_dev,
        current.st_ino,
        current.st_size,
    ) == (bound_stat.st_dev, bound_stat.st_ino, bound_stat.st_size)


def quarantine_artifact(
    source_path: Path | str,
    *,
    quarantine_dir: Path | str | None = None,
    reason: str = "uncertain_artifact",
    expected_stat: Optional[os.stat_result] = None,
    expected_bytes: Optional[bytes] = None,
) -> Optional[Path]:
    """Safely isolate a verified regular artifact into quarantine without overwriting existing files."""
    src = Path(source_path)
    if src.is_symlink():
        return None

    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(src, open_flags)
    except OSError:
        return None

    try:
        if not _verify_fd_ownership(
            fd,
            src,
            expected_stat=expected_stat,
            expected_bytes=expected_bytes,
        ):
            return None

        bound_stat = os.fstat(fd)
        q_dir = Path(quarantine_dir) if quarantine_dir is not None else get_quarantine_dir(create=True)
        q_dir.mkdir(parents=True, exist_ok=True)

        safe_reason = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(reason or "")).strip("_") or "quarantined"
        base_name = f"{src.name}.{safe_reason}"

        dst: Optional[Path] = None
        used_exdev = False
        for idx in range(100000):
            candidate_name = base_name if idx == 0 else f"{base_name}.{idx}"
            candidate = q_dir / candidate_name

            if not _same_regular_stat(src, bound_stat):
                return None

            try:
                os.link(src, candidate)
                dst = candidate
                break
            except FileExistsError:
                continue
            except OSError as link_err:
                if link_err.errno != errno.EXDEV:
                    raise
                used_exdev = True
                payload = _read_all_fd(fd)
                tmp_q = q_dir / f".quarantine_tmp_{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}"
                tmp_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
                tmp_fd = os.open(tmp_q, tmp_flags, 0o600)
                try:
                    try:
                        _write_all(tmp_fd, payload)
                        os.fsync(tmp_fd)
                    finally:
                        os.close(tmp_fd)
                    for exdev_idx in range(idx, 100000):
                        exdev_name = base_name if exdev_idx == 0 else f"{base_name}.{exdev_idx}"
                        exdev_dst = q_dir / exdev_name
                        try:
                            os.link(tmp_q, exdev_dst)
                            dst = exdev_dst
                            break
                        except FileExistsError:
                            continue
                finally:
                    try:
                        tmp_q.unlink(missing_ok=True)
                    except OSError:
                        logger.debug("Failed to unlink temporary quarantine file %s", tmp_q, exc_info=True)
                break

        if dst is None:
            raise TaskPersistenceError(f"Exhausted deterministic quarantine filenames for {src.name}")

        if not used_exdev:
            os.fsync(fd)
        _sync_directory(q_dir)

        if _same_regular_stat(src, bound_stat):
            os.unlink(src)
            _sync_directory(src.parent)
        else:
            logger.warning("Source %s changed after quarantine link; preserving replacement", src)

        return dst
    finally:
        os.close(fd)


quarantine_file = quarantine_artifact
