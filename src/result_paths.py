"""
src/result_paths.py — 统一结果与历史输出路径配置模块
"""

import os
from pathlib import Path

DEFAULT_RESULT_DIR = Path("/root/autodl-tmp/result")


def get_result_dir(create: bool = False) -> Path:
    """获取统一结果根目录"""
    env_dir = os.environ.get("SEAGENT_RESULT_DIR")
    if env_dir:
        p = Path(env_dir)
    else:
        p = DEFAULT_RESULT_DIR
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def get_task_dir(create: bool = False) -> Path:
    """获取 TaskIntent 文件保存目录"""
    env_task = os.environ.get("SEAGENT_TASK_DIR")
    if env_task:
        p = Path(env_task)
    else:
        base = get_result_dir(create=False)
        p = base / "task" if not base.name == "task" else base
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def get_history_dir(create: bool = False) -> Path:
    """获取对话历史文件保存目录"""
    env_hist = os.environ.get("SEAGENT_HISTORY_DIR")
    if env_hist:
        p = Path(env_hist)
    else:
        base = get_result_dir(create=False)
        p = base / "history" if not base.name == "history" else base
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p
