"""The 8088 application is a read-only bridge monitor, not a chat client."""

import json
import re
from unittest.mock import Mock

import dashboard_server
import pytest
import web_backend


def test_dashboard_exposes_only_monitor_and_gateway_routes():
    rules = {rule.rule: sorted(rule.methods - {"HEAD", "OPTIONS"}) for rule in dashboard_server.app.url_map.iter_rules()}

    assert "/api/bridge/status" in rules
    assert rules["/api/bridge/status"] == ["GET"]
    assert "/api/chat" not in rules
    assert "/api/telemetry/reset" not in rules


def test_dashboard_frontend_has_no_chat_controls():
    html = dashboard_server.FRONTEND_FILE.read_text(encoding="utf-8")

    assert 'id="interactiveModeContainer"' not in html
    assert 'id="modeSwitchBtn"' not in html
    assert "'/api/chat'" not in html
    assert "'/api/telemetry/reset'" not in html
    with dashboard_server.app.test_client() as client:
        rendered = client.get("/").get_data(as_text=True)
    assert '"/api/bridge/status"' in rendered
    assert "fetch(dashboardApi.status" in html
    assert "Intent ID" in html
    assert "SEAgent task ID" not in html
    assert "th:nth-child(5), td:nth-child(5) { display: none; }" in html


def test_dashboard_frontend_renders_yaml_driven_subscriptions_safely():
    html = dashboard_server.FRONTEND_FILE.read_text(encoding="utf-8")

    assert 'id="dynamicSubscriptions"' in html
    assert "dynamic_subscriptions" in html
    assert "renderDynamicSubscriptions" in html
    assert "document.createElement" in html
    assert ".textContent" in html
    assert "refresh_interval_ms" in html


@pytest.mark.parametrize("host", ["main", "standalone"])
def test_dashboard_uses_registered_host_routes(monkeypatch, host):
    bridge = Mock(host="127.0.0.1", port=9090, gateway_mode="real")
    bridge.status_payload.return_value = {"mcp_connected": True, "snapshot": {}}
    bridge.is_healthy.return_value = True
    # The main host owns bridge state; the standalone host only proxies it.
    monkeypatch.setattr("src.web.routes_mcp.get_mcp_bridge", lambda: bridge)
    monkeypatch.setattr("src.web.routes_mcp._persist_active_gateway", Mock())
    proxy = Mock(side_effect=lambda path, **kwargs: ({"ok": True, "upstream": path}, 200))
    monkeypatch.setattr(dashboard_server, "_proxy_backend", proxy)
    if host == "main":
        application, page = web_backend.app, "/dashboard"
        expected = {"status": "/api/mcp/status", "gateway": "/api/mcp/gateway"}
    else:
        application, page = dashboard_server.app, "/"
        expected = {"status": "/api/bridge/status", "gateway": "/api/config/gateway"}

    with application.test_client() as client:
        response = client.get(page)
        assert response.status_code == 200
        match = re.search(r"const dashboardApi = (\{[^;]+\});", response.get_data(as_text=True))
        assert match, "dashboard must receive its host's API routes"
        endpoints = json.loads(match.group(1))
        assert endpoints == expected
        for endpoint in (endpoints["status"], endpoints["gateway"]):
            result = client.get(endpoint)
            assert result.status_code == 200
            assert result.is_json
        gateway_rule = next(rule for rule in application.url_map.iter_rules() if rule.rule == endpoints["gateway"])
        assert {"GET", "POST"}.issubset(gateway_rule.methods)
        gateway = {"host": "127.0.0.2", "port": 9091, "mode": "mock"}
        result = client.post(endpoints["gateway"], json=gateway)
        assert result.status_code == 200
        assert result.is_json
    if host == "standalone":
        assert [call.args[0] for call in proxy.call_args_list] == [
            "/api/mcp/status", "/api/mcp/gateway", "/api/mcp/gateway",
        ]
        assert proxy.call_args.kwargs == {"method": "POST", "payload": gateway}
    else:
        bridge.reconnect.assert_called_once_with("127.0.0.2", 9091, mode="mock")
