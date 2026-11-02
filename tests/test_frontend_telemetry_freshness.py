"""Render production assets with explicit freshness metadata from the backend."""

import pytest

from tests.test_frontend_review_regressions import telemetry_response, send, workspace
from tests.test_frontend_workspace_browser import chromium, pw


@pytest.mark.parametrize("freshness", [
    {"status": "stale", "usable": False},
    {"status": "invalid", "usable": False},
    {"status": "missing", "usable": False},
    None,
])
def test_unusable_and_legacy_snapshot_cannot_show_live_health(workspace, freshness):
    data = telemetry_response("valid", {
        "overall_status": "available", "thruster_status": "normal", "sonar_status": "normal",
        "water_current_velocity": 1.0, "water_turbidity": 8.0,
        "obstacle_density": "high", "mothership_support": "strong",
    })
    if freshness is None:
        data["ui_state"]["constraint_state"].pop("telemetry_freshness")
    else:
        data["ui_state"]["constraint_state"]["telemetry_freshness"] = freshness
    workspace.response = data
    workspace.open()
    send(workspace)

    card = workspace.page.locator(".telemetry-decision-card")
    pw.expect(card).to_contain_text("无法确认当前状态")
    for false_assurance in ("LIVE TELEMETRY", "就绪可用", "即时核验通过", "1 m/s", "8 NTU"):
        pw.expect(card).not_to_contain_text(false_assurance)
    assert card.locator(".subsys-ok").count() == 0
    assert card.locator(".metric-value").all_text_contents() == ["未提供"] * 4
    receipt = workspace.page.locator("#turnReceiptBody")
    pw.expect(receipt).to_contain_text("无法确认当前状态")
    pw.expect(receipt).not_to_contain_text("1 m/s")
    pw.expect(card).to_contain_text("2026-10-20T18:30:45+08:00")
    pw.expect(card.locator(".btn-telemetry-refresh")).to_be_enabled()
    # Freshness affects presentation, not the backend's independent permission.
    pw.expect(card.locator(".btn-telemetry-confirm-publish")).to_be_enabled()


@pytest.mark.parametrize("status", ["pending_runtime_validation", "blocked_soft", "warning"])
def test_deferred_validation_does_not_claim_live_execution_health(workspace, status):
    workspace.response = telemetry_response(status, {
        "overall_status": "available", "thruster_status": "normal", "water_current_velocity": 1,
    })
    workspace.response["ui_state"]["constraint_state"]["runtime_validation_deferred"] = True
    workspace.open()
    send(workspace)
    card = workspace.page.locator(".telemetry-decision-card")
    pw.expect(card).to_contain_text("等待运行条件校验")
    pw.expect(card).not_to_contain_text("就绪可用")
    pw.expect(card).not_to_contain_text("LIVE TELEMETRY")
    assert card.locator(".subsys-ok").count() == 0
    pw.expect(workspace.page.locator("#turnReceiptBody")).to_contain_text("等待运行条件校验")
    pw.expect(workspace.page.locator("#turnReceiptBody")).not_to_contain_text("刷新遥测")


def test_stale_warning_cards_identify_historical_source_without_weakening_controls(workspace):
    data = telemetry_response("blocked_soft", {"overall_status": "available", "water_current_velocity": 1})
    ui = data["ui_state"]
    ui.update(phase="blocked_soft", workflow_phase="blocked_soft")
    ui["actions"].update(can_ignore_soft_warning=True, can_publish=False, can_confirm=False)
    ui["constraint_state"].update(
        telemetry_freshness={"status": "stale", "usable": False},
        soft_warnings=[{
            "constraint_id": "C016", "code": "C016", "severity": "soft",
            "check_type": "current_velocity", "message": "当前流速1.00米/秒",
            "observed_value": 1, "threshold": 0.8,
        }],
    )
    workspace.response = data
    workspace.open()
    send(workspace)
    for selector in (".warning-decision-card", "#constraintWarnings"):
        pw.expect(workspace.page.locator(selector)).to_contain_text("基于历史快照")
        pw.expect(workspace.page.locator(selector)).to_contain_text("不能代表当前海况或设备健康")
        pw.expect(workspace.page.locator(selector)).to_contain_text("C016")
    pw.expect(workspace.page.locator("#constraintWarnings")).to_contain_text("校验所用值")
    pw.expect(workspace.page.locator(".btn-ignore-warning-confirm")).to_be_enabled()
    assert workspace.page.locator(".btn-telemetry-confirm-publish").count() == 0
