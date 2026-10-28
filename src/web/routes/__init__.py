"""
src/web/routes — Web 后端控制器与 API 端点路由子包
包含前端页面、ASR 语音、对话推流、虚拟时钟、机器人物理状态、MCP 控制与文本翻译路由。
"""

from .pages import pages_bp
from .dev import dev_bp
from .robot import robot_bp
from .asr import asr_bp
from .time_history import time_history_bp
from .translate import translate_bp
from .mcp import mcp_bp
from .chat import chat_bp

__all__ = [
    "pages_bp",
    "dev_bp",
    "robot_bp",
    "asr_bp",
    "time_history_bp",
    "translate_bp",
    "mcp_bp",
    "chat_bp",
]
