"""The 8088 application is a read-only bridge monitor, not a chat client."""

import dashboard_server


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
    assert "'/api/bridge/status'" in html
    assert "Intent ID" in html
    assert "SEAgent task ID" not in html
    assert "th:nth-child(5), td:nth-child(5) { display: none; }" in html
