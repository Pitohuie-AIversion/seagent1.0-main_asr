"""Read-only ROS 2 bridge dashboard served on port 8088."""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, request, send_file


SEAGENT_ROOT = Path(__file__).resolve().parent
FRONTEND_FILE = SEAGENT_ROOT / "frontend" / "ros2_dashboard.html"
SEAGENT_BACKEND_URL = os.environ.get(
    "SEAGENT_BACKEND_URL", "http://127.0.0.1:6006"
).rstrip("/")

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)


def _proxy_backend(path: str, method: str = "GET", payload: dict | None = None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    backend_request = urllib.request.Request(
        f"{SEAGENT_BACKEND_URL}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(backend_request, timeout=8.0) as response:
            data = json.loads(response.read().decode("utf-8"))
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            data = json.loads(exc.read().decode("utf-8"))
        except Exception:
            data = {"code": status, "msg": str(exc)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        status = 502
        data = {
            "code": status,
            "mcp_connected": False,
            "telemetry_fresh": False,
            "msg": f"无法连接 SEAgent 后端: {exc}",
            "snapshot": {},
        }
    response = jsonify(data)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response, status


@app.route("/", methods=["GET"])
def index():
    if FRONTEND_FILE.exists():
        return send_file(FRONTEND_FILE)
    return "<h1>Dashboard UI Not Found</h1>", 404


@app.route("/api/bridge/status", methods=["GET"])
def bridge_status():
    """Expose the 6006 bridge status without adding another runtime state store."""
    return _proxy_backend("/api/mcp/status")


@app.route("/api/config/gateway", methods=["GET", "POST"])
def gateway_config():
    """Delegate gateway reads and live reconnects to the owning 6006 process."""
    if request.method == "GET":
        return _proxy_backend("/api/mcp/gateway")
    return _proxy_backend(
        "/api/mcp/gateway", method="POST", payload=request.get_json(silent=True) or {}
    )


def run_dashboard_server(port: int = 8088):
    print(f"ROS 2 bridge dashboard: http://127.0.0.1:{port}")
    print(f"SEAgent backend: {SEAGENT_BACKEND_URL}")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    selected_port = int(sys.argv[1]) if len(sys.argv) > 1 else 8088
    run_dashboard_server(port=selected_port)
