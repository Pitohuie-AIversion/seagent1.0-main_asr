"""The 8088 application is a read-only bridge monitor, not a chat client."""

import json
import os
import re
import shutil
from unittest.mock import Mock
from urllib.parse import urljoin

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
    # Both task tables preserve all five columns and expose keyboard scrolling.
    assert rendered.count('class="table-wrap" tabindex="0" role="region"') == 2
    assert rendered.count("执行反馈</th>") == 2
    assert rendered.count("发送记录</th>") == 2
    assert 'class="progress"' not in rendered
    assert "返回对话" not in rendered


def test_dashboard_mobile_execution_feedback_remains_visible_and_keyboard_scrollable():
    pw = pytest.importorskip("playwright.sync_api", reason="Mobile layout check requires Playwright")
    with dashboard_server.app.test_client() as client:
        rendered = client.get("/").get_data(as_text=True)

    with pw.sync_playwright() as playwright:
        executable = (
            os.environ.get("SEAGENT_BROWSER_EXECUTABLE")
            or shutil.which("chromium")
            or shutil.which("google-chrome")
        )
        try:
            browser = playwright.chromium.launch(
                headless=True, executable_path=executable, args=["--no-sandbox"]
            )
        except pw.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Mobile layout check requires a separately installed Chromium")
            raise
        try:
            # Inline styles are real; disabling scripts isolates layout from telemetry.
            page = browser.new_page(
                viewport={"width": 390, "height": 844}, java_script_enabled=False,
                reduced_motion="reduce",
            )
            page.set_content(rendered)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            for table in page.locator(".table-wrap").all():
                feedback = table.locator("th").nth(4)
                pw.expect(feedback).to_have_text("执行反馈")
                pw.expect(feedback).to_be_visible()
                assert table.evaluate("el => el.scrollWidth > el.clientWidth")
                table.scroll_into_view_if_needed()
                table.focus()
                pw.expect(table).to_be_focused()
                table.press("ArrowRight", delay=50)
                page.wait_for_function("el => el.scrollLeft > 0", arg=table.element_handle(), timeout=5000)
        finally:
            browser.close()


def test_dashboard_frontend_renders_yaml_driven_subscriptions_safely():
    html = dashboard_server.FRONTEND_FILE.read_text(encoding="utf-8")

    assert 'id="dynamicSubscriptions"' in html
    assert "dynamic_subscriptions" in html
    assert "renderDynamicSubscriptions" in html
    assert "document.createElement" in html
    assert ".textContent" in html
    assert "refresh_interval_ms" in html


@pytest.mark.parametrize("host,prefix", [("main", ""), ("main", "/seagent"), ("standalone", "")])
def test_dashboard_uses_registered_host_routes(monkeypatch, host, prefix):
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
        response = client.get(page, environ_overrides={"SCRIPT_NAME": prefix})
        assert response.status_code == 200
        rendered = response.get_data(as_text=True)
        match = re.search(r"const dashboardApi = (\{[^;]+\});", rendered)
        assert match, "dashboard must receive its host's API routes"
        endpoints = json.loads(match.group(1))
        assert endpoints == {key: prefix + value for key, value in expected.items()}
        if host == "main":
            assert '<a class="btn nav-link" href="./">返回对话</a>' in rendered
            assert urljoin(f"https://example.test{prefix}{page}", "./") == f"https://example.test{prefix}/"
        else:
            assert "返回对话" not in rendered
        for endpoint in (expected["status"], expected["gateway"]):
            result = client.get(endpoint)
            assert result.status_code == 200
            assert result.is_json
        gateway_rule = next(rule for rule in application.url_map.iter_rules() if rule.rule == expected["gateway"])
        assert {"GET", "POST"}.issubset(gateway_rule.methods)
        gateway = {"host": "127.0.0.2", "port": 9091, "mode": "mock"}
        result = client.post(expected["gateway"], json=gateway)
        assert result.status_code == 200
        assert result.is_json
    if host == "standalone":
        assert [call.args[0] for call in proxy.call_args_list] == [
            "/api/mcp/status", "/api/mcp/gateway", "/api/mcp/gateway",
        ]
        assert proxy.call_args.kwargs == {"method": "POST", "payload": gateway}
    else:
        bridge.reconnect.assert_called_once_with("127.0.0.2", 9091, mode="mock")
