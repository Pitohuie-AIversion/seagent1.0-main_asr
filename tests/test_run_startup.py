"""
test_run_startup.py
====================
针对 run.py 启动脚本与 MCP 自动注入流程的测试套件

场景：
  - 测试在 OFFLINE_MOCK 模式下调用 startup()，自动拉起 Mock rosbridge (9091) 并完成 Web MCP 初始化
"""

import os
import socket
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))


class TestRunStartupMCP(unittest.TestCase):

    def setUp(self):
        # startup also sets dispatch/ID directories; none may leak into later
        # tests, where unrelated intents must not reuse this test's records.
        environment = patch.dict(os.environ, {"OFFLINE_MOCK": "1", "MCP_PORT": "9100"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_mock_startup_with_mcp(self):
        """验证 OFFLINE_MOCK=1 下 startup() 正确引导 MCP 桥接服务"""
        import run
        import web_backend

        run.startup()
        time.sleep(0.3)

        bridge = web_backend.get_mcp_bridge()
        self.assertIsNotNone(bridge)
        self.assertTrue(bridge.is_healthy())
        self.assertEqual(bridge.port, 9100)

        # 清理
        bridge.stop()
        if run._mock_rosbridge_srv:
            run._mock_rosbridge_srv.stop()
            run._mock_rosbridge_srv = None
        web_backend.init_mcp_bridge_service(None)

    def test_offline_models_can_connect_to_external_rosbridge(self):
        """离线模型模式可连接外部 ROS2 rosbridge，而不重复占用其端口。"""
        from mcp.shim.mock_rosbridge_server import MockRosbridgeServer
        import run
        import web_backend

        external_rosbridge = MockRosbridgeServer(port=9101)
        external_rosbridge.start()
        os.environ["MCP_PORT"] = "9101"
        os.environ["MCP_EMBEDDED_MOCK"] = "0"
        try:
            run.startup()
            time.sleep(0.3)

            bridge = web_backend.get_mcp_bridge()
            self.assertIsNotNone(bridge)
            self.assertTrue(bridge.is_healthy())
            self.assertEqual(bridge.port, 9101)
            self.assertIsNone(run._mock_rosbridge_srv)
        finally:
            bridge = web_backend.get_mcp_bridge()
            if bridge is not None:
                bridge.stop()
            external_rosbridge.stop()
            web_backend.init_mcp_bridge_service(None)


@pytest.fixture
def isolated_mcp_startup(tmp_path, monkeypatch):
    """Keep startup configuration, IDs and gateway changes outside the repository."""
    import run
    import web_backend
    from src.web import state

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("ros2_runtime.yaml", "ros2_protocol_spec.yaml"):
        (config_dir / name).write_bytes((PROJECT_ROOT / "config" / name).read_bytes())
    monkeypatch.setattr(run, "__file__", str(tmp_path / "run.py"))
    monkeypatch.setattr(web_backend, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(state, "_shared_mcp_bridge", None)
    monkeypatch.setattr(sys, "argv", ["run.py", "--mcp"])
    monkeypatch.setenv("ENABLE_MCP", "1")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("SEAGENT_ROS2_ID_DIR", str(tmp_path / "ids"))
    monkeypatch.setenv("SEAGENT_MCP_DISPATCH_DIR", str(tmp_path / "ids"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    try:
        yield run, web_backend, tmp_path
    finally:
        bridge = web_backend.get_mcp_bridge()
        if bridge is not None:
            bridge.stop()


def test_unavailable_gateway_remains_manageable_and_can_reconnect(isolated_mcp_startup, monkeypatch):
    """A refused initial connection must not disable dashboard gateway recovery."""
    from mcp.shim.mock_rosbridge_server import MockRosbridgeServer

    run, web_backend, tmp_path = isolated_mcp_startup
    with socket.socket() as unavailable_gateway:
        # Reserve a localhost port without listening, guaranteeing connection refusal.
        unavailable_gateway.bind(("127.0.0.1", 0))
        unavailable_port = unavailable_gateway.getsockname()[1]
        monkeypatch.setenv("MCP_PORT", str(unavailable_port))
        run._init_mcp_service_if_requested(SimpleNamespace(state_info=None))

        bridge = web_backend.get_mcp_bridge()
        assert bridge is not None
        assert not bridge.is_healthy()
        client = web_backend.app.test_client()
        response = client.get("/api/mcp/gateway")
        assert response.status_code == 200
        assert response.get_json()["gateway"]["port"] == unavailable_port
        assert response.get_json()["gateway"]["connected"] is False
        response = client.get("/api/mcp/status")
        assert response.status_code == 200
        assert response.get_json()["mcp_connected"] is False
        assert response.get_json()["port"] == unavailable_port
        assert client.post("/api/mcp/dispatch", json={}).status_code == 503
        with pytest.raises(RuntimeError, match="未连接"):
            bridge.dispatch_intent({"intent_id": "offline-startup"})

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        mock_port = reservation.getsockname()[1]
    server = MockRosbridgeServer(port=mock_port)
    server.start()
    try:
        time.sleep(0.2)
        response = client.post("/api/mcp/gateway", json={
            "host": "127.0.0.1", "port": mock_port, "mode": "mock",
        })
        assert response.status_code == 200, response.get_json()
        assert bridge.is_healthy()
        assert client.get("/api/mcp/status").get_json()["mcp_connected"] is True
        assert client.get("/api/mcp/gateway").get_json()["gateway"]["port"] == mock_port
        assert server.get_received_publishes() == []

        runtime_file = tmp_path / "config" / "ros2_runtime.yaml"
        runtime_data = yaml.safe_load(runtime_file.read_text())
        runtime_data["dashboard"]["refresh_interval_ms"] = 1750
        runtime_file.write_text(yaml.safe_dump(runtime_data, allow_unicode=True))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if bridge.runtime_config_payload()["dashboard"]["refresh_interval_ms"] == 1750:
                break
            time.sleep(0.05)
        assert bridge.runtime_config_payload()["dashboard"]["refresh_interval_ms"] == 1750
        assert bridge.is_healthy()
    finally:
        bridge.stop()
        server.stop()
        server._thread.join(timeout=3)


@pytest.mark.parametrize("invalid_artifact", ["runtime_config", "dispatch_records"])
def test_invalid_bridge_configuration_does_not_register_service(isolated_mcp_startup, invalid_artifact):
    """Retaining a disconnected bridge must not accept invalid startup evidence."""
    run, web_backend, tmp_path = isolated_mcp_startup
    if invalid_artifact == "runtime_config":
        (tmp_path / "config" / "ros2_runtime.yaml").write_text("gateway: invalid\n")
    else:
        records_dir = tmp_path / "ids"
        records_dir.mkdir()
        (records_dir / ".mcp_dispatch_records.json").write_text("{broken")

    run._init_mcp_service_if_requested(SimpleNamespace(state_info=None))

    assert web_backend.get_mcp_bridge() is None
    assert web_backend.app.test_client().get("/api/mcp/gateway").status_code == 503


if __name__ == "__main__":
    unittest.main()
