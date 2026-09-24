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
_isolated_runtime_dir: Path | None = None


def _cleanup_created_root() -> None:
    if _created_root is None or _owner_pid != os.getpid():
        return
    shutil.rmtree(_created_root, ignore_errors=True)


def configure_test_artifact_paths() -> Path:
    """在业务模块导入前设置单一测试产物树，并让子进程继承。"""
    global _created_root, _owner_pid, _isolated_runtime_dir

    configured = os.environ.get("SEAGENT_TEST_RESULT_DIR")
    if configured:
        test_root = Path(configured).expanduser().resolve()
    else:
        test_root = Path(tempfile.mkdtemp(prefix="seagent-tests-")).resolve()
        _created_root = test_root
        _owner_pid = os.getpid()
        os.environ["SEAGENT_TEST_RESULT_DIR"] = str(test_root)
        atexit.register(_cleanup_created_root)

    # 校验测试目录与运行态目录的关系（拒绝与默认目录及外部自定义 SEAGENT_RESULT_DIR 相同或重叠）
    prohibited_dirs = [_USER_DEFAULT_RESULT_DIR]
    user_result_env = os.environ.get("SEAGENT_RESULT_DIR")
    if user_result_env:
        resolved_user_dir = Path(user_result_env).expanduser().resolve()
        # 若非本模块先前主动导出的测试沙箱本身，则属于外部运行态目录，必须严格禁止重叠
        if resolved_user_dir != _isolated_runtime_dir:
            prohibited_dirs.append(resolved_user_dir)

    for prohibited in prohibited_dirs:
        if test_root == prohibited:
            raise RuntimeError(
                f"SEAGENT_TEST_RESULT_DIR must not point to the runtime result directory: {prohibited}"
            )
        try:
            test_root.relative_to(prohibited)
            raise RuntimeError(
                f"SEAGENT_TEST_RESULT_DIR must not be inside runtime result directory: {prohibited}"
            )
        except ValueError:
            pass
        try:
            prohibited.relative_to(test_root)
            raise RuntimeError(
                f"SEAGENT_TEST_RESULT_DIR must not contain runtime result directory: {prohibited}"
            )
        except ValueError:
            pass

    (test_root / "task").mkdir(parents=True, exist_ok=True)
    (test_root / "history").mkdir(parents=True, exist_ok=True)

    # 测试根目录具有最高优先级，覆盖调用测试命令时继承到的用户运行配置。
    # task/history 不单独固定；它们始终从当前 result 根派生，因此单个测试临时
    # 覆盖 SEAGENT_RESULT_DIR 时仍能获得完整、自洽的局部沙箱。
    os.environ["SEAGENT_RESULT_DIR"] = str(test_root)
    _isolated_runtime_dir = test_root
    os.environ.pop("SEAGENT_TASK_DIR", None)
    os.environ.pop("SEAGENT_HISTORY_DIR", None)

    # 隔离 state.yaml：继承来的运行态状态路径只能作为复制源，绝不能被测试直接写入。
    # 统一将状态文件复制到测试根目录，并强制重写 SEAGENT_STATE_FILE 指向沙箱副本。
    project_root = Path(__file__).resolve().parents[1]
    inherited_state = os.environ.get("SEAGENT_STATE_FILE")
    if inherited_state:
        orig_state = Path(inherited_state).expanduser().resolve()
    else:
        orig_state = (project_root / "config" / "state.yaml").resolve()

    isolated_state = (test_root / "state.yaml").resolve()
    if orig_state.exists() and orig_state != isolated_state:
        shutil.copy2(orig_state, isolated_state)
    elif not isolated_state.exists():
        isolated_state.touch(mode=0o600)

    os.environ["SEAGENT_STATE_FILE"] = str(isolated_state)

    return test_root
