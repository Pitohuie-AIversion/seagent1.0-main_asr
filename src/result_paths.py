"""
src/result_paths.py — 统一结果与历史输出路径配置模块
"""

import os
from pathlib import Path


DEFAULT_RESULT_DIR = Path("/root/autodl-tmp/result")


def get_result_dir(create: bool = False) -> Path:
    """获取统一结果根目录。"""
    env_dir = os.environ.get("SEAGENT_RESULT_DIR")
    path = Path(env_dir) if env_dir else DEFAULT_RESULT_DIR
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_task_dir(create: bool = False) -> Path:
    """获取 TaskIntent 文件保存目录。"""
    env_task = os.environ.get("SEAGENT_TASK_DIR")
    if env_task:
        path = Path(env_task)
    else:
        base = get_result_dir(create=False)
        path = base / "task" if base.name != "task" else base
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_history_dir(create: bool = False) -> Path:
    """获取对话历史文件保存目录。"""
    env_history = os.environ.get("SEAGENT_HISTORY_DIR")
    if env_history:
        path = Path(env_history)
    else:
        base = get_result_dir(create=False)
        path = base / "history" if base.name != "history" else base
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path
