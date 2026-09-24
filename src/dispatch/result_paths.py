"""
src/dispatch/result_paths.py — 统一结果与历史输出路径配置模块
"""

import os
from pathlib import Path


DEFAULT_RESULT_DIR = Path("/root/autodl-tmp/result")
_REPO_FALLBACK_RESULT_DIR = Path(__file__).resolve().parents[2] / "result"


def _is_directory_usable(path: Path) -> bool:
    """检查目录是否已存在且可写，或者其最近的存在父目录是否可写。"""
    try:
        p = path.expanduser().resolve()
        if p.exists():
            return os.access(p, os.W_OK)
        parent = p.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        return parent.exists() and os.access(parent, os.W_OK)
    except Exception:
        return False


def get_result_dir(create: bool = False) -> Path:
    """获取统一结果根目录。

    规则：
    1. 若环境变量 SEAGENT_RESULT_DIR 显式指定：
       - 无论 create 为 True 还是 False，均返回该指定路径。
       - create=True 时创建目录；若无权限必须抛出 PermissionError/OSError，绝不能静默切换到其他备用目录。
    2. 若未显式指定（使用默认 DEFAULT_RESULT_DIR）：
       - 优先使用 DEFAULT_RESULT_DIR。
       - 若 DEFAULT_RESULT_DIR 无权写入/创建，安全 fallback 到仓库根目录下的 result/。
       - 无论 create 为 True 还是 False，均返回一致的解析路径。
    """
    env_dir = os.environ.get("SEAGENT_RESULT_DIR")
    if env_dir:
        path = Path(env_dir).expanduser().resolve()
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    default_path = DEFAULT_RESULT_DIR.expanduser().resolve()
    if create:
        try:
            default_path.mkdir(parents=True, exist_ok=True)
            return default_path
        except PermissionError:
            fallback = _REPO_FALLBACK_RESULT_DIR.expanduser().resolve()
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback
    else:
        if _is_directory_usable(default_path):
            return default_path
        return _REPO_FALLBACK_RESULT_DIR.expanduser().resolve()


def get_task_dir(create: bool = False) -> Path:
    """获取 TaskIntent 文件保存目录。"""
    env_task = os.environ.get("SEAGENT_TASK_DIR")
    if env_task:
        path = Path(env_task).expanduser().resolve()
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    base = get_result_dir(create=create)
    path = base / "task" if base.name != "task" else base
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_history_dir(create: bool = False) -> Path:
    """获取对话历史文件保存目录。"""
    env_history = os.environ.get("SEAGENT_HISTORY_DIR")
    if env_history:
        path = Path(env_history).expanduser().resolve()
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    base = get_result_dir(create=create)
    path = base / "history" if base.name != "history" else base
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path
