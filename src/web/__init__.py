"""
src/web - Web 后端模块化分层系统
提供蓝图注册、状态管理与各业务 API 路由组件。
"""

from flask import Flask

from src.web.routes_asr import asr_bp
from src.web.routes_chat import chat_bp
from src.web.routes_dev import dev_bp
from src.web.routes_mcp import mcp_bp
from src.web.routes_pages import pages_bp
from src.web.routes_robot import robot_bp
from src.web.routes_time_history import time_history_bp
from src.web.routes_translate import translate_bp
from src.web.state import (
    CONFIG_DIR,
    FRONTEND_DIR,
    EndpointFilter,
    get_mcp_bridge,
    get_or_create_manager,
    init_asr_service,
    init_manager,
    init_mcp_bridge_service,
    print_status,
)


def register_blueprints(app: Flask) -> None:
    """批量向 Flask 应用注册业务蓝图"""
    app.register_blueprint(pages_bp)
    app.register_blueprint(dev_bp)
    app.register_blueprint(robot_bp)
    app.register_blueprint(asr_bp)
    app.register_blueprint(time_history_bp)
    app.register_blueprint(translate_bp)
    app.register_blueprint(mcp_bp)
    app.register_blueprint(chat_bp)


__all__ = [
    "register_blueprints",
    "pages_bp",
    "dev_bp",
    "robot_bp",
    "asr_bp",
    "time_history_bp",
    "translate_bp",
    "mcp_bp",
    "chat_bp",
    "init_manager",
    "init_asr_service",
    "init_mcp_bridge_service",
    "get_mcp_bridge",
    "get_or_create_manager",
    "print_status",
    "EndpointFilter",
    "CONFIG_DIR",
    "FRONTEND_DIR",
]
