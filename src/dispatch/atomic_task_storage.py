"""
atomic_task_storage.py — 任务落盘跨进程排他锁与 POSIX 原子写入基础设施
"""
import copy
import fcntl
import logging
import os
from pathlib import Path
from src.exceptions import TaskPersistenceError

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
