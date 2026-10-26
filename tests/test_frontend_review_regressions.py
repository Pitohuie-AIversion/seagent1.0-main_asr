"""Product regressions using real assets and fully intercepted browser requests."""

from copy import deepcopy
import json
from urllib.parse import urlparse

import pytest

from tests.test_frontend_workspace_browser import (
    IsolatedWorkspace,
    chromium,
    install_audio_stub,
    pw,
    response_fixture,
)


def telemetry_response(status="pending_runtime_validation", state=None):
    data = response_fixture(task=True, phase="confirming")
    data["ui_state"]["confirmation_token"] = "summary-v1"
    data["ui_state"]["constraint_state"].update({
        "source": "validation_result",
        "overall_status": status,
        "telemetry_freshness": {"status": "fresh", "usable": True},
        "state_snapshot": {
            "unit_id": "OBSROV-75-001",
            "state_version": 1,
            "updated_at": "2026-10-20T18:30:45+08:00",
            "state": {"overall_status": "available"} if state is None else state,
        },
    })
    return data


@pytest.fixture
def workspace(chromium):
    context = chromium.new_context(reduced_motion="reduce", timezone_id="UTC")
    app = IsolatedWorkspace(context.new_page())
    yield app
    context.close()
    assert not app.errors


def send(app, text="检查当前任务"):
    app.page.locator("#messageInput").fill(text)
    app.page.locator("#sendBtn").click()
    pw.expect(app.page.locator("#sendBtn")).to_be_enabled()


def test_pending_runtime_card_does_not_claim_missing_telemetry_is_healthy(workspace):
    workspace.response = telemetry_response()
    workspace.open()
    send(workspace)
    card = workspace.page.locator(".telemetry-decision-card")
    pw.expect(card).to_contain_text("等待运行条件校验")
    pw.expect(card).not_to_contain_text("即时核验通过")
    assert card.locator(".subsys-ok").count() == 0
    assert card.locator(".subsys-status-text").all_text_contents() == ["未知"] * 8
    assert card.locator(".metric-value").all_text_contents() == ["未提供"] * 4


def test_telemetry_metrics_do_not_invent_threshold_violations(workspace):
    workspace.response = telemetry_response("valid", {
        "overall_status": "available", "water_current_velocity": 0.8, "water_turbidity": 4.0,
    })
    workspace.open()
    send(workspace)
    card = workspace.page.locator(".telemetry-decision-card")
    pw.expect(card).to_contain_text("即时核验通过")
    assert card.locator(".metric-val-warn").count() == 0
    assert "0.8 m/s" in card.inner_text()


@pytest.mark.parametrize("success", [True, False])
def test_telemetry_refresh_reads_current_session_and_reports_failure(workspace, success):
    workspace.response = telemetry_response()
    workspace.open()
    send(workspace)
    calls = []
    fresh = telemetry_response("valid", {"overall_status": "available", "thruster_status": "fault"})
    fresh.update(exists=True)

    def refresh(route):
        calls.append(route.request.url)
        route.fulfill(status=200 if success else 503, json=fresh if success else {"ok": False})

    workspace.page.route("**/api/session/state?**", refresh)
    workspace.page.locator(".btn-telemetry-refresh").click()
    workspace.page.wait_for_timeout(100)
    assert len(calls) == 1
    assert "refresh_constraints=1" in calls[0]
    if success:
        card = workspace.page.locator(".telemetry-decision-card:not(.warning-card-resolved)")
        pw.expect(card).to_contain_text("故障")
    else:
        pw.expect(workspace.page.locator(".telemetry-refresh-status")).to_contain_text("刷新失败")
        pw.expect(workspace.page.locator(".btn-telemetry-refresh")).to_be_enabled()


def test_only_final_stream_reply_is_translated(workspace):
    page = workspace.page
    translations = []
    pending = []
    final_reply = "已记录开始时间为明天上午8点，请提供起点坐标。"
    result = response_fixture(task=True)
    result["reply"] = final_reply

    def stream(route):
        workspace.messages.append(route.request.post_data_json)
        result["request_id"] = route.request.post_data_json["request_id"]
        events = [
            {"step": "guard_check", "message": "正在审查请求合法性与全局门禁..."},
            {"delta": final_reply},
            result,
        ]
        route.fulfill(content_type="text/event-stream", body="".join(
            "data: " + json.dumps(event) + "\n\n" for event in events
        ))

    def translate(route):
        translations.append(route.request.post_data_json["text"])
        pending.append(route)

    workspace.open()
    page.route("**/api/chat/stream", stream)
    page.route("**/api/translate", translate)
    page.locator("#langSelect").select_option("en")
    send(workspace, "Set the task start time to tomorrow at 8am.")
    bubble = page.locator(".message.bot").last.locator(".bubble")
    pw.expect(bubble).to_have_text(final_reply)
    assert translations == [final_reply]
    pending[0].fulfill(json={"code": 200, "translated_text": "Start time saved. Please provide coordinates."})
    pw.expect(bubble).to_have_text("Start time saved. Please provide coordinates.")


@pytest.mark.parametrize("manual", [True, False])
def test_late_translation_preserves_original_after_language_switch(workspace, manual):
    page = workspace.page
    pending = []
    workspace.response = response_fixture(task=True)
    workspace.response["reply"] = "任务参数已记录，请核对。"
    workspace.open()
    page.route("**/api/translate", lambda route: pending.append(route))
    if not manual:
        page.locator("#langSelect").select_option("en")
    send(workspace, "Review this task.")
    message = page.locator(".message.bot").last
    if manual:
        message.locator(".msg-translate-bar").click()
        page.locator("#langSelect").select_option("en")
    page.locator("#langSelect").select_option("zh")
    assert pending
    for route in pending:
        route.fulfill(json={"code": 200, "translated_text": "Task parameters saved. Please review."})
    page.wait_for_timeout(100)
    pw.expect(message.locator(".bubble")).to_have_text("任务参数已记录，请核对。")


@pytest.mark.parametrize("phase", ["confirming", "blocked_soft", "blocked_hard"])
def test_modify_buttons_preserve_unsent_draft(workspace, phase):
    workspace.response = telemetry_response()
    workspace.response["ui_state"].update(phase=phase, workflow_phase=phase)
    workspace.response["ui_state"]["actions"]["can_ignore_soft_warning"] = phase == "blocked_soft"
    workspace.open()
    send(workspace)
    draft = "结束时间改为明天下午四点，起点经纬度保留原值"
    workspace.page.locator("#messageInput").fill(draft)
    workspace.page.locator(".btn-modify-warning-task").click()
    pw.expect(workspace.page.locator("#messageInput")).to_have_value(draft)


def test_programmatic_confirmation_preserves_unsent_draft_and_sends_seen_token(workspace):
    workspace.response = telemetry_response()
    workspace.open()
    send(workspace)
    draft = "下一步需要核对支持船编号"
    workspace.page.locator("#messageInput").fill(draft)
    workspace.page.locator("#publishTaskBtn").click()
    pw.expect(workspace.page.locator("#sendBtn")).to_be_enabled()
    assert workspace.messages[-1]["message"] == "确认发布"
    assert workspace.messages[-1].get("expected_confirmation_token") == "summary-v1"
    pw.expect(workspace.page.locator("#messageInput")).to_have_value(draft)


@pytest.mark.parametrize("current", [
    "2026-10-20T18:30:45+08:00", "2026-10-20T10:30:45Z", "2026-10-20T18:30:45",
])
def test_time_editor_round_trip_preserves_shanghai_time_in_utc_browser(workspace, current):
    calls = []
    workspace.page.route("**/api/time/current", lambda route: route.fulfill(
        json={"code": 200, "current_time": current}
    ))
    workspace.page.route("**/api/time/set", lambda route: (
        calls.append(route.request.post_data_json),
        route.fulfill(json={"code": 200}),
    ))
    workspace.page.on("dialog", lambda dialog: dialog.accept())
    workspace.open()
    workspace.page.locator(".clock-card > summary").click()
    pw.expect(workspace.page.locator("#timePicker")).to_have_value("2026-10-20T18:30:45")
    pw.expect(workspace.page.locator("#hint-simtime")).to_contain_text("UTC+08:00")
    workspace.page.locator("#setTimeBtn").click()
    workspace.page.wait_for_timeout(100)
    assert calls == [{"time": "2026-10-20T18:30:45+08:00"}]


@pytest.mark.parametrize("transport", ["http", "sse", "sync"])
def test_stale_confirmation_updates_review_without_retry(workspace, transport):
    workspace.response = telemetry_response()
    workspace.open()
    send(workspace)
    changed = deepcopy(workspace.response)
    changed["ui_state"]["confirmation_token"] = "summary-v2"
    changed["ui_state"]["slots"].append({
        "key": "water_depth", "label": "水深", "value": 320, "status": "valid", "type": "number",
    })
    failure = {**changed, "code": 409, "ok": False, "error": "ConfirmationStale",
               "msg": "任务摘要已变化，请重新核对后确认。"}
    calls = []

    def reject(route):
        calls.append(route.request.post_data_json)
        if transport == "sse":
            route.fulfill(content_type="text/event-stream", body="data: " + json.dumps(failure) + "\n\n")
        else:
            route.fulfill(status=409, json=failure)

    if transport == "sync":
        workspace.page.evaluate("window.ReadableStream = undefined")
        workspace.page.route("**/api/chat", reject)
    else:
        workspace.page.route("**/api/chat/stream", reject)
    workspace.page.locator("#publishTaskBtn").click()
    pw.expect(workspace.page.locator("#sendBtn")).to_be_enabled()
    assert len(calls) == 1
    assert calls[0].get("expected_confirmation_token") == "summary-v1"
    pw.expect(workspace.page.locator("#confirmationSummary")).to_contain_text("320")
    pw.expect(workspace.page.locator(".message.bot").last).to_contain_text("重新核对")
    workspace.page.locator("#publishTaskBtn").click()
    pw.expect(workspace.page.locator("#sendBtn")).to_be_enabled()
    assert len(calls) == 2
    assert calls[1].get("expected_confirmation_token") == "summary-v2"


@pytest.mark.parametrize("status,allowed", [("pending", True), ("blocked_hard", False), ("acknowledged", False)])
def test_archived_runtime_review_has_independent_acknowledgement(workspace, status, allowed):
    data = response_fixture(task=True, phase="done", read_only=True)
    data["ui_state"]["confirmation_token"] = "runtime-review-v1"
    data["ui_state"]["actions"]["can_acknowledge_runtime_warning"] = allowed
    data["ui_state"]["runtime_execution_review"] = {
        "status": status,
        "violations": [{"constraint_id": "C001", "message": "执行前海流条件已变化。", "severity": "soft"}],
    }
    data["ros2_dispatch"] = {"state": "BLOCKED", "retry_allowed": True}
    workspace.open()
    workspace.state(data)
    pw.expect(workspace.page.locator("#runtimeExecutionReview")).to_contain_text("执行前海流条件已变化")
    if allowed:
        button = workspace.page.locator("#acknowledgeRuntimeWarningBtn")
        pw.expect(button).to_be_enabled()
        button.click()
        pw.expect(workspace.page.locator("#sendBtn")).to_be_enabled()
        assert workspace.messages[-1]["message"] == "确认执行风险"
        assert workspace.messages[-1].get("expected_confirmation_token") == "runtime-review-v1"
    else:
        pw.expect(workspace.page.locator("#acknowledgeRuntimeWarningBtn")).to_be_hidden()
    pw.expect(workspace.page.locator("#publishTaskBtn")).to_be_hidden()


def test_control_cancellation_hides_dispatch_and_reports_transport_truth(workspace):
    data = response_fixture(task=True, phase="done", read_only=True)
    data["ros2_dispatch"] = {"state": "NOT_SENT", "retry_allowed": True}
    data["ui_state"]["execution_control"] = {"control_state": "stop_requested", "last_control_request": {
        "outcome": "not_sent_cancelled", "message": "尚未发送的任务已取消。",
    }}
    workspace.open()
    workspace.state(data)
    pw.expect(workspace.page.locator("#executionControlStatus")).to_contain_text("尚未发送的任务已取消")
    pw.expect(workspace.page.locator("#dispatchRetryBtn")).to_be_hidden()
    calls = []
    workspace.page.route("**/api/mcp/dispatch", lambda route: (
        calls.append(route.request.post_data_json), route.fulfill(json={"code": 200}),
    ))
    # An old click handler must also refuse the action, not just hide its button.
    workspace.page.locator("#dispatchRetryBtn").evaluate("(button) => button.click()")
    workspace.page.wait_for_timeout(50)
    assert not calls


def test_dispatch_response_exposes_new_runtime_review_and_sends_seen_token(workspace):
    data = response_fixture(task=True, phase="done", read_only=True)
    data["ui_state"]["confirmation_token"] = "archived-v1"
    data["ros2_dispatch"] = {"state": "SCHEDULED", "retry_allowed": True}
    workspace.response = data
    workspace.open()
    send(workspace)
    changed = deepcopy(data)
    changed["ui_state"]["confirmation_token"] = "runtime-review-v2"
    changed["ui_state"]["actions"]["can_acknowledge_runtime_warning"] = True
    changed["ui_state"]["runtime_execution_review"] = {
        "status": "pending", "violations": [{"message": "海流条件已变化，需要重新确认。"}],
    }
    changed["ros2_dispatch"] = {"state": "BLOCKED", "retry_allowed": True}
    changed["persistence_warning"] = "执行结果未能保存，请保留当前会话。"
    calls = []

    def dispatch(route):
        calls.append(route.request.post_data_json)
        route.fulfill(json=changed)

    workspace.page.route("**/api/mcp/dispatch", dispatch)
    workspace.page.locator("#dispatchRetryBtn").click()
    pw.expect(workspace.page.locator("#acknowledgeRuntimeWarningBtn")).to_be_enabled()
    assert calls[0].get("expected_confirmation_token") == "archived-v1"
    pw.expect(workspace.page.locator("#runtimeExecutionReview")).to_contain_text("海流条件已变化")
    pw.expect(workspace.page.locator("#dispatchDetail")).to_contain_text("执行结果未能保存")
    pw.expect(workspace.page.locator("#dispatchRetryBtn")).to_be_hidden()
    workspace.page.locator("#acknowledgeRuntimeWarningBtn").click()
    pw.expect(workspace.page.locator("#sendBtn")).to_be_enabled()
    assert workspace.messages[-1].get("expected_confirmation_token") == "runtime-review-v2"


def test_voice_review_appends_to_existing_draft_without_auto_sending(workspace):
    page = workspace.page
    install_audio_stub(page)
    workspace.open()
    draft = "任务开始时间为明天八点"
    page.locator("#messageInput").fill(draft)
    page.locator("#voiceBtn").click()
    page.evaluate("""() => window.testAudioProcessor.onaudioprocess({
        inputBuffer: {getChannelData: () => new Float32Array([0.1, -0.1, 0.2, -0.2])}
    })""")
    page.locator("#voiceBtn").click()
    pw.expect(page.locator("#asrPanel")).to_contain_text("转写")
    for route in workspace.pending_asr:
        route.fulfill(json={"code": 200, "text": "水深320米", "direct_to_llm": True})
    pw.expect(page.locator("#messageInput")).to_have_value(draft + "\n水深320米")
    assert not workspace.messages


def test_history_forks_session_and_preserves_unsent_draft(workspace):
    workspace.open()
    send(workspace, "查询当前能力")
    draft = "接下来创建另一个巡检任务"
    workspace.page.locator("#messageInput").fill(draft)
    calls = []
    workspace.page.route("**/api/history/list", lambda route: route.fulfill(json={
        "code": 200, "data": [{"id": "history-a", "task_id": "PI-A", "task_type": "pipeline_inspection",
                              "saved_at": "2026-10-20T18:30:45+08:00"}],
    }))

    def restore(route):
        calls.append(route.request.post_data_json)
        route.fulfill(json={**response_fixture(task=True), "session_id": "restored-fork",
                            "conversation_history": []})

    workspace.page.route("**/api/history/load", restore)
    workspace.page.locator("#historyBtn").click()
    workspace.page.locator(".history-item").click()
    pw.expect(workspace.page.locator("#sendBtn")).to_be_enabled()
    assert calls[0].get("restore_mode") == "fork"
    pw.expect(workspace.page.locator("#messageInput")).to_have_value(draft)


def test_history_preservation_notice_can_open_original_draft(workspace):
    workspace.open()
    send(workspace, "创建巡检任务")
    workspace.page.locator("#messageInput").fill("尚未发送的补充说明")
    initial_chat_count = len(workspace.messages)
    calls = []

    def restore(route):
        request = route.request.post_data_json
        calls.append(request)
        result = {**response_fixture(task=True), "conversation_history": [],
                  "session_id": "history-fork" if len(calls) == 1 else "draft-fork"}
        if len(calls) == 1:
            result.update(previous_session_id="isolated-browser-session",
                          preserved_draft_history_id="original-draft-backup")
        route.fulfill(json=result)

    workspace.page.route("**/api/history/load", restore)
    workspace.page.evaluate("window.restoreHistory('selected-history')")
    notice = workspace.page.locator(".preserved-draft-notice")
    pw.expect(notice).to_contain_text("原草稿已保存到历史记录")
    pw.expect(notice).to_contain_text("新会话")
    notice.get_by_role("button", name="打开原草稿").click()
    pw.expect(workspace.page.locator("#sendBtn")).to_be_enabled()
    assert [call["history_id"] for call in calls] == ["selected-history", "original-draft-backup"]
    assert calls[1]["session_id"] == "history-fork"
    assert all(call["restore_mode"] == "fork" for call in calls)
    assert len(workspace.messages) == initial_chat_count
    pw.expect(workspace.page.locator("#messageInput")).to_have_value("尚未发送的补充说明")
