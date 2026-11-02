"""
src/dispatch/storage — 任务结果与持久化基础设施子包
包含 POSIX 原子写入、排他文件锁、检疫隔离、目录解析与递增序列号生成。
"""

from .atomic_task_storage import (
    TaskCommitUncertainError,
    TaskPublishLock,
    _write_all,
    _atomic_commit_noreplace,
)
from .result_paths import (
    DEFAULT_RESULT_DIR,
    get_history_dir,
    get_result_dir,
    get_task_dir,
)
from .id_sequence import (
    next_daily_id,
    next_daily_task_id,
    peek_daily_task_id,
    validate_intent_id,
    validate_task_id,
    validate_task_id_for_task_type,
    validate_task_prefix,
)

__all__ = [
    "TaskCommitUncertainError",
    "TaskPublishLock",
    "_write_all",
    "_atomic_commit_noreplace",
    "DEFAULT_RESULT_DIR",
    "get_history_dir",
    "get_result_dir",
    "get_task_dir",
    "next_daily_id",
    "next_daily_task_id",
    "peek_daily_task_id",
    "validate_intent_id",
    "validate_task_id",
    "validate_task_id_for_task_type",
    "validate_task_prefix",
]
