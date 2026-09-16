"""
src/web/routes_pages.py - 静态页面与基础前端路由
"""

from flask import Blueprint, render_template, request, url_for

pages_bp = Blueprint("pages", __name__)


@pages_bp.after_app_request
def disable_static_cache_after_request(response):
    """Disable browser static caching during development to ensure instant frontend JS/CSS updates."""
    if request.path.startswith("/static/") or request.path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@pages_bp.route("/")
def index():
    return render_template("index.html")


@pages_bp.route("/dashboard")
def dashboard():
    return render_template("ros2_dashboard.html", dashboard_api={
        "status": url_for("mcp.get_mcp_status"),
        "gateway": url_for("mcp.mcp_gateway"),
    })


@pages_bp.route("/favicon.ico")
def favicon():
    return "", 204
