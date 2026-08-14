"""为自动化测试统一配置与用户运行态分离的产物目录。"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import tempfile


_USER_DEFAULT_RESULT_DIR = Path("/root/autodl-tmp/result").resolve()
_created_root: Path | None = None
_owner_pid: int | None = None


def _cleanup_created_root() -> None:
    if _created_root is None or _owner_pid != os.getpid():
        return
    shutil.rmtree(_created_root, ignore_errors=True)


def configure_test_artifact_paths() -> Path:
    """在业务模块导入前设置单一测试产物树，并让子进程继承。"""
    global _created_root, _owner_pid

    configured = os.environ.get("SEAGENT_TEST_RESULT_DIR")
    if configured:
        test_root = Path(configured).expanduser().resolve()
    else:
        test_root = Path(tempfile.mkdtemp(prefix="seagent-tests-")).resolve()
        _created_root = test_root
        _owner_pid = os.getpid()
        os.environ["SEAGENT_TEST_RESULT_DIR"] = str(test_root)
        atexit.register(_cleanup_created_root)

    if test_root == _USER_DEFAULT_RESULT_DIR:
        raise RuntimeError(
            "SEAGENT_TEST_RESULT_DIR must not point to the user runtime result directory"
        )

    (test_root / "task").mkdir(parents=True, exist_ok=True)
    (test_root / "history").mkdir(parents=True, exist_ok=True)

    # 测试根目录具有最高优先级，覆盖调用测试命令时继承到的用户运行配置。
    # task/history 不单独固定；它们始终从当前 result 根派生，因此单个测试临时
    # 覆盖 SEAGENT_RESULT_DIR 时仍能获得完整、自洽的局部沙箱。
    os.environ["SEAGENT_RESULT_DIR"] = str(test_root)
    os.environ.pop("SEAGENT_TASK_DIR", None)
    os.environ.pop("SEAGENT_HISTORY_DIR", None)
    return test_root
