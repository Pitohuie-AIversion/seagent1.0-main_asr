"""Browser regressions for the task workspace, with every request intercepted.

These tests render the real Jinja template and frontend assets. API responses are
fixtures; no dialogue server, model, task persistence, or robot is contacted.
Install Playwright/Chromium separately to enable this optional browser suite.
"""

from copy import deepcopy
import json
import mimetypes
import os
from pathlib import Path
import shutil
from urllib.parse import urlparse

import pytest

jinja2 = pytest.importorskip("jinja2", reason="Workspace browser tests require Jinja2")
pw = pytest.importorskip(
    "playwright.sync_api", reason="Workspace browser tests require Playwright"
)

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
ORIGIN = "http://seagent-workspace.test"
DOCUMENT_TITLE = "水下多智能体任务决策系统 - ROV HUD"


def response_fixture(*, task=False, phase="collecting", read_only=False, **actions):
    """A minimal public response schema, independent of the backend process."""
    permitted = {
        "can_send": True,
        "can_modify": task and not read_only,
        "can_confirm": task and phase == "confirming",
        "can_publish": task and phase == "confirming",
        "can_ignore_soft_warning": False,
        "can_cancel": task and not read_only,
    }
    permitted.update(actions)
    slots = [
        {
            "key": "task_type",
            "label": {"zh": "任务类型", "en": "Task type"},
            "value": "管缆巡检",
            "status": "valid",
            "source": "user",
            "type": "string",
        },
        {
            "key": "equipment_type",
            "label": {"zh": "设备型号", "en": "Equipment model"},
            "value": "观察级深海机器人 75HP",
            "status": "valid",
            "source": "auto",
            "type": "string",
        },
    ] if task else []
    return {
        "ok": True,
        "code": 200,
        "session_id": "isolated-browser-session",
        "reply": "Task parameters received." if task else "Capability query answered.",
        "ui_state": {
            "state_status": "ok",
            "dialogue_mode": "task_collection" if task else "knowledge_qa",
            "mode": "normal",
            "phase": phase,
            "workflow_phase": phase,
            "task_type_key": "pipeline_inspection" if task else None,
            "task_id": None,
            "task_id_preview": "PREVIEW-001" if task else None,
            "read_only": read_only,
            "slot_version": 1 if task else 0,
            "slots": slots,
            "constraint_state": {
                "hard_violations": [],
                "soft_warnings": [],
                "ignored_soft_warnings": [],
            },
            "actions": permitted,
        },
    }


class IsolatedWorkspace:
    def __init__(self, page):
        self.page = page
        self.messages = []
        self.resets = []
        self.pending = []
        self.pending_asr = []
        self.hold_chat = False
        self.response = response_fixture()
        self.errors = []
        self.unexpected_api = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.route("**/*", self._route)

    def _reply(self, route, data):
        data.setdefault("request_id", route.request.post_data_json.get("request_id"))
        if urlparse(route.request.url).path == "/api/chat/stream":
            body = (
                "event: delta\ndata: " + json.dumps({"delta": data["reply"]}) + "\n\n"
                "event: result\ndata: " + json.dumps(data) + "\n\n"
            )
            route.fulfill(status=200, content_type="text/event-stream", body=body)
        else:
            route.fulfill(status=200, json=data)

    def _route(self, route):
        request = route.request
        url = urlparse(request.url)
        if url.netloc != urlparse(ORIGIN).netloc:
            route.abort()  # Font/CDN requests must not leave this fixture.
            return
        path = url.path
        if path == "/":
            env = jinja2.Environment(
                loader=jinja2.FileSystemLoader(FRONTEND), autoescape=True
            )
            route.fulfill(
                status=200,
                content_type="text/html",
                body=env.get_template("index.html").render(),
            )
        elif path.startswith("/static/"):
            asset = (FRONTEND / path.removeprefix("/static/")).resolve()
            if asset.is_relative_to(FRONTEND) and asset.is_file():
                route.fulfill(
                    status=200,
                    content_type=mimetypes.guess_type(asset)[0] or "application/octet-stream",
                    body=asset.read_bytes(),
                )
            else:
                route.fulfill(status=404)
        elif path in ("/api/chat", "/api/chat/stream"):
            self.messages.append(request.post_data_json)
            if self.hold_chat:
                self.pending.append(route)
            else:
                self._reply(route, deepcopy(self.response))
        elif path == "/api/reset":
            self.resets.append(request.post_data_json)
            route.fulfill(json={"ok": True, "reset": True})
        elif path == "/api/asr":
            self.pending_asr.append(route)
        elif path == "/api/time/current":
            route.fulfill(json={"code": 200, "current_time": "2026-10-19T09:00:00"})
        elif path == "/api/dev/reload-events/stream":
            route.fulfill(status=404)
        elif path == "/api/dev/reload-events":
            route.fulfill(json={"ok": True, "events": []})
        elif path == "/api/session/state":
            route.fulfill(json={"ok": True, "exists": False})
        elif path == "/api/translate":
            route.fulfill(json={"code": 200, "translated_text": "Translated conversation."})
        else:
            if path.startswith("/api/"):
                self.unexpected_api.append(path)
            route.fulfill(status=404)

    def open(self, width=1280):
        self.page.set_viewport_size({"width": width, "height": 900})
        self.page.goto(ORIGIN, wait_until="domcontentloaded")
        pw.expect(self.page.locator(".welcome-layout")).to_be_visible()
        pw.expect(self.page.locator("#sendBtn")).to_be_enabled()

    def state(self, data, origin="snapshot"):
        self.page.evaluate(
            "({data, origin}) => window.updateSidebar(data, {origin})",
            {"data": data, "origin": origin},
        )

    def release(self):
        for route in self.pending:
            self._reply(route, deepcopy(self.response))
        self.pending.clear()
        self.hold_chat = False


@pytest.fixture(scope="module")
def chromium():
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
                pytest.skip("Workspace browser tests require a separately installed Chromium")
            raise
        yield browser
        browser.close()


@pytest.fixture
def workspace(chromium):
    context = chromium.new_context(reduced_motion="reduce")
    app = IsolatedWorkspace(context.new_page())
    yield app
    context.close()
    assert not app.errors, "Browser JavaScript errors: " + repr(app.errors)
    assert not app.unexpected_api, "Unmocked API calls: " + repr(app.unexpected_api)


def test_titles_empty_state_and_locale_preserve_existing_identity(workspace):
    workspace.open()
    page = workspace.page
    pw.expect(page).to_have_title(DOCUMENT_TITLE)
    pw.expect(page.locator("#ui-title")).to_have_text("水下多智能体任务决策大模型")
    pw.expect(page.locator("#taskEmptyState")).to_be_visible()
    for element in ("taskDetails", "dispatchCard", "constraintWarnings", "collectedFields", "confirmationCard"):
        pw.expect(page.locator("#" + element)).to_be_hidden()

    empty_zh = page.locator("#taskEmptyState").inner_text()
    page.locator("#langSelect").select_option("en")
    pw.expect(page.locator("#ui-title")).to_have_text("Underwater Multi-Robot Decision System")
    pw.expect(page.locator("#sendBtn")).to_have_text("Send")
    pw.expect(page.locator("#taskEmptyState")).not_to_have_text(empty_zh)
    pw.expect(page.locator(".welcome-layout [data-example]").first).to_contain_text(
        "What is the maximum operating depth"
    )
    pw.expect(page).to_have_title(DOCUMENT_TITLE)

    page.locator("#langSelect").select_option("zh")
    pw.expect(page.locator("#ui-title")).to_have_text("水下多智能体任务决策系统")
    pw.expect(page.locator("#taskEmptyState")).to_have_text(empty_zh)


def test_examples_only_fill_composer_and_shift_enter_preserves_newlines(workspace):
    workspace.open()
    page = workspace.page
    examples = page.locator(".welcome-layout [data-example]")
    pw.expect(examples).to_have_count(2)
    composer = page.locator("#messageInput")
    values = []
    for index in range(2):
        examples.nth(index).click()
        values.append(composer.input_value())
        assert values[-1].strip()
    assert values[0] != values[1]
    assert not workspace.messages
    composer.fill("第一行")
    composer.press("End")
    composer.press("Shift+Enter")
    composer.press("KeyA")
    pw.expect(composer).to_have_value("第一行\na")
    assert not workspace.messages


def test_read_only_question_hides_welcome_without_creating_task(workspace):
    workspace.open()
    page = workspace.page
    page.locator("#messageInput").fill("金牛座一号机的最大作业水深是多少？")
    page.locator("#sendBtn").click()
    pw.expect(page.locator("#sendBtn")).to_be_enabled()
    pw.expect(page.locator(".welcome-layout")).to_be_hidden()
    pw.expect(page.locator("#taskEmptyState")).to_be_visible()
    pw.expect(page.locator("#taskDetails")).to_be_hidden()
    pw.expect(page.locator("#dispatchCard")).to_be_hidden()
    pw.expect(page.locator("#messages")).to_contain_text("Capability query answered.")
    assert len(workspace.messages) == 1


def test_emergency_mode_stays_visible_without_inventing_a_task(workspace):
    workspace.open()
    data = response_fixture()
    data['ui_state']['dialogue_mode'] = 'emergency_intervention'
    data['ui_state']['mode'] = 'emergency'
    workspace.state(data)
    pw.expect(workspace.page.locator('#interactionStatus')).to_have_text('紧急干预模式')
    pw.expect(workspace.page.locator('#taskEmptyState')).to_be_visible()
    pw.expect(workspace.page.locator('#publishTaskBtn')).to_be_disabled()
    workspace.page.locator('#langSelect').select_option('en')
    pw.expect(workspace.page.locator('#interactionStatus')).to_have_text('Emergency intervention')


def test_task_context_and_reset_restore_empty_workspace(workspace):
    workspace.open()
    page = workspace.page
    workspace.response = response_fixture(task=True)
    page.locator("#messageInput").fill("创建管缆巡检任务")
    page.locator("#sendBtn").click()
    pw.expect(page.locator("#taskDetails")).to_be_visible()
    pw.expect(page.locator("#taskEmptyState")).to_be_hidden()
    pw.expect(page.locator("#taskProgress")).to_be_visible()
    pw.expect(page.locator("#collectedFields")).to_contain_text("观察级深海机器人 75HP")
    pw.expect(page.locator("#dispatchCard")).to_be_hidden()
    page.locator("#resetBtn").click()
    pw.expect(page.locator(".welcome-layout")).to_be_visible()
    pw.expect(page.locator("#taskEmptyState")).to_be_visible()
    pw.expect(page.locator("#taskDetails")).to_be_hidden()
    pw.expect(page.locator("#confirmationCard")).to_be_hidden()
    assert len(workspace.resets) == 1


@pytest.mark.parametrize("gate", ["can_publish", "can_confirm", "can_send", "read_only"])
def test_publication_respects_latest_permission_even_with_stale_button(workspace, gate):
    workspace.open()
    page = workspace.page
    workspace.state(response_fixture(task=True, phase="confirming"))
    pw.expect(page.locator("#publishTaskBtn")).to_be_enabled()
    restricted = response_fixture(task=True, phase="confirming")
    if gate == "read_only":
        restricted["ui_state"]["read_only"] = True
    else:
        restricted["ui_state"]["actions"][gate] = False
    workspace.state(restricted)
    assert page.locator("#publishTaskBtn").evaluate("el => el.disabled || !el.checkVisibility()")
    # Exercise the event handler after revocation as well as its visual state.
    page.locator("#publishTaskBtn").evaluate("el => { el.disabled = false; el.click(); }")
    page.wait_for_timeout(50)
    assert not workspace.messages


def test_publish_sends_explicit_command_once_while_request_is_pending(workspace):
    workspace.open()
    page = workspace.page
    workspace.state(response_fixture(task=True, phase="confirming"))
    pw.expect(page.locator("#confirmationCard")).to_be_visible()
    workspace.hold_chat = True
    page.locator("#publishTaskBtn").evaluate("el => { el.click(); el.click(); }")
    pw.expect(page.locator("#publishTaskBtn")).to_be_disabled()
    pw.expect(page.locator("#sendBtn")).to_be_disabled()
    page.wait_for_timeout(50)
    assert [message["message"] for message in workspace.messages] == ["确认发布"]
    workspace.response = response_fixture(task=True, phase="done", read_only=True)
    workspace.release()
    pw.expect(page.locator("#confirmationCard")).to_be_hidden()
    pw.expect(page.locator("#dispatchCard")).to_be_visible()
    assert len(workspace.messages) == 1


@pytest.mark.parametrize("phase,card", [("blocked_soft", ".warning-decision-card"), ("blocked_hard", ".hard-blocked-card")])
def test_validation_decisions_use_constraint_phase_not_workflow_label(workspace, phase, card):
    workspace.open()
    data = response_fixture(task=True, phase=phase, can_ignore_soft_warning=phase == "blocked_soft")
    data["ui_state"]["workflow_phase"] = "validating"
    key = "soft_warnings" if phase == "blocked_soft" else "hard_violations"
    data["ui_state"]["constraint_state"][key] = [
        {"constraint_id": "C001", "code": "C001", "message": "环境条件需要处理。"}
    ]
    workspace.response = data
    workspace.page.locator("#messageInput").fill("检查当前任务")
    workspace.page.locator("#sendBtn").click()
    pw.expect(workspace.page.locator(card)).to_be_visible()
    pw.expect(workspace.page.locator(card)).to_contain_text("环境条件需要处理。")
    pw.expect(workspace.page.locator("#confirmationCard")).to_be_hidden()


def install_audio_stub(page):
    """Provide deterministic microphone samples without accessing a real device."""
    page.add_init_script("""
        window.testAudioStopped = 0;
        Object.defineProperty(navigator, 'mediaDevices', {value: {
            getUserMedia: async () => ({getTracks: () => [{stop() { window.testAudioStopped++; }}]})
        }});
        window.AudioContext = class {
            constructor() { this.sampleRate = 16000; this.state = 'running'; this.destination = {}; }
            createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
            createScriptProcessor() {
                window.testAudioProcessor = {connect() {}, disconnect() {}, onaudioprocess: null};
                return window.testAudioProcessor;
            }
            async close() { this.state = 'closed'; }
        };
    """)


def test_voice_cancel_releases_recording_without_transcription_or_submission(workspace):
    page = workspace.page
    install_audio_stub(page)
    workspace.open()
    page.locator("#voiceBtn").click()
    pw.expect(page.locator("#voiceBtn")).to_have_attribute("aria-pressed", "true")
    pw.expect(page.locator("#voiceElapsed")).to_be_visible()
    page.evaluate("""() => window.testAudioProcessor.onaudioprocess({
        inputBuffer: {getChannelData: () => new Float32Array([0.1, -0.1, 0.2, -0.2])}
    })""")
    pw.expect(page.locator("#voiceElapsed")).not_to_have_text("00:00")
    page.locator("#voiceCancelBtn").click()
    pw.expect(page.locator("#voiceBtn")).to_have_attribute("aria-pressed", "false")
    pw.expect(page.locator("#audioWaveformWrapper")).to_be_hidden()
    pw.expect(page.locator("#messageInput")).to_have_value("")
    assert page.evaluate("window.testAudioStopped") == 1
    assert not workspace.messages
    assert not workspace.pending_asr
    assert not workspace.unexpected_api


def test_mobile_worksheet_can_be_opened_and_confirmed_with_keyboard(workspace):
    workspace.open(390)
    page = workspace.page
    workspace.state(response_fixture(task=True, phase="confirming"))
    summary = page.locator("#taskWorksheet > summary")
    pw.expect(page.locator("#confirmationCard")).to_be_hidden()
    summary.focus()
    summary.press("Enter")
    pw.expect(page.locator("#confirmationCard")).to_be_visible()
    guidance_links = page.locator("#taskNextStepCard button:visible:not(:disabled)")
    for index in range(guidance_links.count()):
        page.keyboard.press("Tab")
        pw.expect(guidance_links.nth(index)).to_be_focused()
    field_links = page.locator("#confirmationSummary button[data-slot-key]")
    assert field_links.count() > 0
    for index in range(field_links.count()):
        page.keyboard.press("Tab")
        pw.expect(field_links.nth(index)).to_be_focused()
    page.keyboard.press("Tab")
    pw.expect(page.locator("#publishTaskBtn")).to_be_focused()
    assert not workspace.messages
    with page.expect_request("**/api/chat/stream"):
        page.locator("#publishTaskBtn").press("Enter")
    pw.expect(page.locator("#sendBtn")).to_be_enabled()
    assert [message["message"] for message in workspace.messages] == ["确认发布"]


def test_ime_composition_enter_does_not_submit_text(workspace):
    workspace.open()
    page = workspace.page
    composer = page.locator("#messageInput")
    composer.fill("正在输入中文")
    composer.dispatch_event("compositionstart")
    composer.dispatch_event("keydown", {"key": "Enter", "code": "Enter", "isComposing": True})
    pw.expect(composer).to_have_value("正在输入中文")
    pw.expect(page.locator(".welcome-layout")).to_be_visible()
    assert not workspace.messages
    composer.dispatch_event("compositionend", {"data": "正在输入中文"})
    with page.expect_request("**/api/chat/stream"):
        composer.press("Enter")
    pw.expect(page.locator("#sendBtn")).to_be_enabled()
    assert [message["message"] for message in workspace.messages] == ["正在输入中文"]


def test_knowledge_query_preserves_existing_task_context(workspace):
    workspace.open()
    page = workspace.page
    task = response_fixture(task=True)
    workspace.state(task)
    before = page.locator("#collectedFields").inner_text()
    workspace.response = deepcopy(task)
    workspace.response["ui_state"]["dialogue_mode"] = "knowledge_qa"
    workspace.response["ui_state"]["actions"]["can_modify"] = False
    workspace.response["reply"] = "Capability query answered without changing the draft."
    page.locator("#messageInput").fill("只读查询设备能力，不修改任务")
    page.locator("#sendBtn").click()
    pw.expect(page.locator("#messages")).to_contain_text(workspace.response["reply"])
    pw.expect(page.locator("#taskDetails")).to_be_visible()
    pw.expect(page.locator("#taskEmptyState")).to_be_hidden()
    pw.expect(page.locator("#collectedFields")).to_have_text(before, use_inner_text=True)
    assert len(workspace.messages) == 1


def test_asr_transcription_cancel_stays_visible_and_ignores_late_result(workspace):
    page = workspace.page
    install_audio_stub(page)
    workspace.open()
    page.locator("#voiceBtn").click()
    pw.expect(page.locator("#voiceBtn")).to_have_attribute("aria-pressed", "true")
    page.evaluate("""() => window.testAudioProcessor.onaudioprocess({
        inputBuffer: {getChannelData: () => new Float32Array([0.1, -0.1, 0.2, -0.2])}
    })""")
    with page.expect_request("**/api/asr"):
        page.locator("#voiceBtn").click()
    pw.expect(page.locator("#audioWaveformWrapper")).to_be_hidden()
    pw.expect(page.locator("#voiceCancelBtn")).to_be_visible()
    assert len(workspace.pending_asr) == 1
    page.locator("#voiceCancelBtn").click()
    pw.expect(page.locator("#voiceBtn")).to_be_enabled()
    pw.expect(page.locator("#voiceCancelBtn")).to_be_hidden()
    for route in workspace.pending_asr:
        route.fulfill(json={"code": 200, "text": "迟到转写不得提交", "direct_to_llm": True})
    page.wait_for_timeout(50)
    pw.expect(page.locator("#messageInput")).to_have_value("")
    pw.expect(page.locator(".welcome-layout")).to_be_visible()
    assert not workspace.messages


@pytest.mark.parametrize("width", [390, 1280])
def test_workspace_fits_mobile_and_desktop_without_horizontal_overflow(workspace, width):
    workspace.open(width)
    # A fitting document must still reveal the welcome heading and composer;
    # stale message scrolling or a shrinking mobile flex child can hide both.
    pw.expect(workspace.page.locator(".welcome-intro")).to_be_in_viewport()
    pw.expect(workspace.page.locator("#messageInput")).to_be_in_viewport(ratio=1)
    pw.expect(workspace.page.locator("#jumpToLatestBtn")).to_be_hidden()
    for data in (None, response_fixture(task=True), response_fixture(task=True, phase="confirming")):
        if data:
            workspace.state(data)
        size = workspace.page.evaluate("""() => ({
            viewport: document.documentElement.clientWidth,
            html: document.documentElement.scrollWidth,
            body: document.body.scrollWidth
        })""")
        assert max(size["html"], size["body"]) <= size["viewport"] + 1, size


def insight_slot(key, value=None, *, status="valid", candidate=None, label=None, version=1):
    return {
        "key": key,
        "label": {"zh": label or key, "en": key.replace("_", " ").title()},
        "schema_type": "string",
        "value_type": "number" if isinstance(value, (int, float)) else "string",
        "value": value,
        "status": status,
        "candidate_value": candidate,
        "source": "user_input",
        "version": version,
    }


def insight_response(slots, *, version=1, request_id="insight-turn-1", phase="collecting"):
    data = response_fixture(task=True, phase=phase)
    data["request_id"] = request_id
    data["ui_state"]["slots"] = slots
    data["ui_state"]["slot_version"] = version
    return data


def test_insights_distinguish_changed_values_candidates_and_confirmed_status(workspace):
    workspace.open()
    before = insight_response([
        insight_slot("oilfield_name", "原油田", label="油田"),
        insight_slot("water_depth", 250, label="水深"),
        insight_slot("equipment_unit_id", status="candidate", candidate="ROV-PENDING", label="单机"),
    ])
    workspace.state(before)
    after = insight_response([
        insight_slot("oilfield_name", "新油田", label="油田", version=2),
        insight_slot("water_depth", 250, status="conflict", candidate=600, label="水深", version=2),
        insight_slot("equipment_unit_id", "ROV-PENDING", label="单机", version=2),
    ], version=2, request_id="insight-change-1")
    workspace.state(after, origin="reply")
    changes = workspace.page.locator("#taskChangesList")
    pw.expect(workspace.page.locator("#taskChangesCard")).to_be_visible()
    for value in ("原油田", "新油田", "水深", "250", "600", "单机", "ROV-PENDING"):
        pw.expect(changes).to_contain_text(value)
    pw.expect(workspace.page.locator("#worksheetCounts")).to_contain_text("待核对")
    # The retained valid value and conflicting proposal must remain distinguishable.
    pw.expect(workspace.page.locator('#collectedFields [data-slot-key="water_depth"]')).to_contain_text("当前有效值")
    pw.expect(workspace.page.locator('#collectedFields [data-slot-key="water_depth"]')).to_contain_text("冲突候选值")
    assert not workspace.messages


def test_insights_language_render_does_not_duplicate_or_forget_turn_changes(workspace):
    workspace.open()
    workspace.state(insight_response([insight_slot("water_depth", 100, label="水深")]))
    changed = insight_response(
        [insight_slot("water_depth", 200, label="水深", version=2)],
        version=2, request_id="insight-language-1",
    )
    workspace.state(changed, origin="reply")
    changes = workspace.page.locator("#taskChangesList")
    original = changes.inner_text()
    assert "100" in original and "200" in original
    count = changes.locator(":scope > *").count()
    assert count > 0
    workspace.page.locator("#langSelect").select_option("en")
    pw.expect(changes.locator(":scope > *")).to_have_count(count)
    pw.expect(changes).to_contain_text("100")
    workspace.page.locator("#langSelect").select_option("zh")
    pw.expect(changes).to_have_text(original, use_inner_text=True)
    workspace.state(changed, origin="render")
    pw.expect(changes).to_have_text(original, use_inner_text=True)
    pw.expect(changes.locator(":scope > *")).to_have_count(count)


def test_insights_restore_reset_and_refresh_do_not_invent_turn_edits(workspace):
    workspace.open()
    workspace.state(insight_response([insight_slot("water_depth", 100, label="水深")]))
    changed = insight_response([insight_slot("water_depth", 200, label="水深")], version=2)
    workspace.state(changed, origin="reply")
    pw.expect(workspace.page.locator("#taskChangesCard")).to_be_visible()
    restored = insight_response([insight_slot("water_depth", 900, label="水深")], version=8)
    workspace.state(restored, origin="restore")
    pw.expect(workspace.page.locator("#taskChangesCard")).to_be_hidden()
    pw.expect(workspace.page.locator("#turnReceiptSummary")).to_contain_text("恢复")
    pw.expect(workspace.page.locator("#turnReceiptSummary")).not_to_contain_text("本轮")
    refreshed = deepcopy(restored)
    refreshed["ui_state"]["constraint_state"]["validated_at"] = "2026-10-19T11:00:00"
    workspace.state(refreshed, origin="refresh")
    pw.expect(workspace.page.locator("#taskChangesCard")).to_be_hidden()
    unchanged = deepcopy(refreshed)
    unchanged["request_id"] = "insight-after-restoration"
    workspace.state(unchanged, origin="reply")
    pw.expect(workspace.page.locator("#taskChangesCard")).to_be_hidden()
    workspace.page.locator("#resetBtn").click()
    pw.expect(workspace.page.locator("#taskChangesCard")).to_be_hidden()
    pw.expect(workspace.page.locator("#turnReceipt")).to_be_hidden()


@pytest.mark.parametrize("identity", ["task_id", "task_type_key"])
def test_insights_task_switch_never_uses_another_tasks_previous_values(workspace, identity):
    workspace.open()
    previous = insight_response([insight_slot("oilfield_name", "OLD-TASK-FIELD", label="油田")])
    previous["ui_state"][identity] = "TASK-OLD" if identity == "task_id" else "pipeline_inspection"
    workspace.state(previous)
    current = insight_response(
        [insight_slot("oilfield_name", "NEW-TASK-FIELD", label="油田")],
        version=2, request_id="insight-different-task",
    )
    current["ui_state"][identity] = "TASK-NEW" if identity == "task_id" else "tree_valve_operation"
    workspace.state(current, origin="reply")
    pw.expect(workspace.page.locator("#taskChangesList")).not_to_contain_text("OLD-TASK-FIELD")
    pw.expect(workspace.page.locator("#collectedFields")).to_contain_text("NEW-TASK-FIELD")


def test_insights_confirmation_summary_only_uses_valid_values_and_keeps_zero(workspace):
    workspace.open()
    data = insight_response([
        insight_slot("water_depth", 0, label="水深"),
        insight_slot("oilfield_name", "CONFIRMED-OILFIELD", label="油田"),
        insight_slot("equipment_type", status="candidate", candidate="UNCONFIRMED-CANDIDATE", label="设备型号"),
        insight_slot("equipment_unit_id", "CONFLICT-OLD", status="conflict", candidate="CONFLICT-NEW", label="单机"),
        insight_slot("start_time", "INVALID-TIME", status="invalid", label="开始时间"),
        insight_slot("end_time", status="unresolved", candidate="UNRESOLVED-TIME", label="结束时间"),
    ], phase="confirming")
    data["ui_state"]["constraint_state"]["ignored_soft_warnings"] = [{
        "constraint_id": "C-RISK", "message": "已接受的低温作业风险", "acknowledged_at": "2026-10-19T09:00:00",
    }]
    data["ui_state"]["constraint_state"]["overall_status"] = "blocked_soft"
    workspace.state(data, origin="reply")
    summary = workspace.page.locator("#confirmationSummary")
    pw.expect(summary).to_be_visible()
    pw.expect(summary).to_contain_text("CONFIRMED-OILFIELD")
    pw.expect(summary).to_contain_text("0")
    for provisional in ("UNCONFIRMED-CANDIDATE", "CONFLICT-OLD", "CONFLICT-NEW", "INVALID-TIME", "UNRESOLVED-TIME"):
        pw.expect(summary).not_to_contain_text(provisional)
    review = workspace.page.locator("#confirmationReview")
    pw.expect(review).to_contain_text("已接受的低温作业风险")
    pw.expect(review).to_contain_text("已确认风险")
    pw.expect(review).not_to_contain_text("待处理")
    pw.expect(review).not_to_contain_text("通过")
    assert not workspace.messages


def test_insights_knowledge_reply_and_metadata_changes_do_not_claim_value_edits(workspace):
    workspace.open()
    original = insight_response([insight_slot("water_depth", 300, label="水深")])
    workspace.state(original)
    query = deepcopy(original)
    query["request_id"] = "insight-query"
    query["ui_state"]["dialogue_mode"] = "knowledge_qa"
    workspace.state(query, origin="reply")
    pw.expect(workspace.page.locator("#taskChangesCard")).to_be_hidden()
    pw.expect(workspace.page.locator("#collectedFields")).to_contain_text("300")
    metadata = deepcopy(original)
    metadata["request_id"] = "insight-metadata"
    metadata["ui_state"]["slot_version"] = 2
    metadata["ui_state"]["slots"][0].update(version=2, confidence=0.99)
    workspace.state(metadata, origin="reply")
    pw.expect(workspace.page.locator("#taskChangesCard")).to_be_hidden()
    pw.expect(workspace.page.locator("#collectedFields")).to_contain_text("300")


def test_insights_error_state_hides_old_confirmation_and_turn_changes(workspace):
    workspace.open()
    before = insight_response([insight_slot("water_depth", 100, label="水深")], phase="confirming")
    workspace.state(before)
    valid = insight_response(
        [insight_slot("water_depth", 200, label="水深")],
        phase="confirming", version=2, request_id="insight-before-error",
    )
    workspace.state(valid, origin="reply")
    pw.expect(workspace.page.locator("#confirmationSummary")).to_be_visible()
    failed = response_fixture()
    failed["ui_state"].update(state_status="error", read_only=True, slots=[])
    failed["ui_state"]["actions"] = {key: False for key in failed["ui_state"]["actions"]}
    workspace.state(failed, origin="refresh")
    pw.expect(workspace.page.locator("#confirmationSummary")).to_be_hidden()
    pw.expect(workspace.page.locator("#taskChangesCard")).to_be_hidden()
    pw.expect(workspace.page.locator("#publishTaskBtn")).to_be_disabled()


def test_insights_values_are_text_and_field_links_do_not_send_commands(workspace):
    workspace.open()
    workspace.state(insight_response([insight_slot("oilfield_name", "BEFORE", label="油田")]))
    payload = '<img src=x onerror="window.insightInjected=1">'
    changed = insight_response(
        [insight_slot("oilfield_name", payload, label="油田")],
        phase="confirming", version=2, request_id="insight-text-safety",
    )
    workspace.state(changed, origin="reply")
    for section in ("#taskChangesList", "#confirmationSummary"):
        pw.expect(workspace.page.locator(section)).to_contain_text(payload)
        pw.expect(workspace.page.locator(section + " img")).to_have_count(0)
    assert workspace.page.evaluate("window.insightInjected === undefined")
    field_button = workspace.page.locator('#confirmationSummary button[data-slot-key="oilfield_name"]')
    field_button.click()
    pw.expect(workspace.page.locator('#collectedFields [data-slot-key="oilfield_name"]')).to_be_in_viewport()
    assert not workspace.messages


def test_insights_constraint_evidence_preserves_zero_without_inventing_limits(workspace):
    workspace.open()
    data = insight_response([insight_slot("water_depth", 0, label="水深")], phase="blocked_hard")
    data["ui_state"]["workflow_phase"] = "validating"
    data["ui_state"]["constraint_state"].update({
        "source": "validation_result",
        "overall_status": "blocked_hard",
        "validated_at": "2026-10-19T12:34:56+00:00",
        "hard_violations": [
            {"constraint_id": "C-ZERO", "code": "C-ZERO", "message": "零值校验依据", "severity": "hard",
             "related_fields": ["water_depth"], "observed_value": 0, "threshold": 0},
            {"constraint_id": "C-NO-LIMIT", "code": "C-NO-LIMIT", "message": "后端未提供数值条件", "severity": "hard",
             "related_fields": ["water_depth"], "observed_value": None, "threshold": None},
        ],
    })
    workspace.state(data, origin="reply")
    zero = workspace.page.locator('#constraintWarnings [data-constraint-id="C-ZERO"]')
    pw.expect(zero).to_be_visible()
    evidence = zero.evaluate("el => Object.fromEntries([...el.querySelectorAll('dt')].map(term => [term.textContent, term.nextElementSibling.textContent]))")
    assert evidence["当前值"] == "0"
    assert evidence["限值 / 条件"] == "0"
    unknown = workspace.page.locator('#constraintWarnings [data-constraint-id="C-NO-LIMIT"]')
    labels = unknown.locator("dt").all_text_contents()
    assert "当前值" not in labels and "限值 / 条件" not in labels
    assert not workspace.messages


def test_insights_constraint_strings_are_rendered_as_text(workspace):
    workspace.open()
    payload = '<img src=x onerror="window.constraintInjected=1">'
    data = insight_response([insight_slot("water_depth", 250, label="水深")], phase="blocked_hard")
    data["ui_state"]["workflow_phase"] = "validating"
    data["ui_state"]["constraint_state"].update({
        "source": "validation_result",
        "overall_status": "blocked_hard",
        "hard_violations": [{
            "constraint_id": "C-TEXT", "code": "C-TEXT", "message": payload,
            "constraint_name": payload, "severity": "hard", "related_fields": ["water_depth"],
            "observed_value": payload, "threshold": {"note": payload},
        }],
    })
    workspace.state(data, origin="reply")
    evidence = workspace.page.locator('#constraintWarnings [data-constraint-id="C-TEXT"]')
    pw.expect(evidence).to_contain_text(payload)
    pw.expect(evidence.locator("img, script")).to_have_count(0)
    assert workspace.page.evaluate("window.constraintInjected === undefined")
    assert not workspace.messages


@pytest.mark.parametrize(
    "source,validation_status,expected",
    [
        ("validation_result", "pending_runtime_validation", "等待运行条件校验"),
        ("stale_validation_result", "valid", "历史校验结果已过期"),
    ],
)
def test_insights_receipt_does_not_present_pending_or_stale_validation_as_passed(
    workspace, source, validation_status, expected
):
    workspace.open()
    data = insight_response([insight_slot("water_depth", 0, label="水深")], phase="confirming")
    data["ui_state"]["constraint_state"].update({
        "source": source, "overall_status": validation_status,
        "validated_at": "2026-10-19T12:34:56+00:00",
        "state_snapshot": {
            "unit_id": "ROV-EVIDENCE-001", "state_version": 0,
            "updated_at": "2026-10-19T12:33:00+00:00",
        },
    })
    workspace.state(data, origin="reply")
    workspace.page.locator("#turnReceiptSummary").click()
    receipt = workspace.page.locator("#turnReceiptBody")
    pw.expect(receipt).to_be_visible()
    pw.expect(receipt).to_contain_text(expected)
    pw.expect(receipt).not_to_contain_text("校验通过")
    pw.expect(workspace.page.locator("#confirmationReview")).to_contain_text(expected)
    pw.expect(workspace.page.locator("#confirmationReview")).not_to_contain_text("校验通过")
    pw.expect(receipt).to_contain_text("ROV-EVIDENCE-001")
    pw.expect(receipt).to_contain_text("2026-10-19T12:34:56+00:00")
    evidence = receipt.evaluate("el => Object.fromEntries([...el.querySelectorAll('dt')].map(term => [term.textContent, term.nextElementSibling.textContent]))")
    assert evidence["所用状态版本"] == "0"


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("slot_status", ["conflict", "unresolved"])
def test_insights_long_pending_values_wrap_without_horizontal_overflow(workspace, width, slot_status):
    workspace.open(width)
    previous_value = "PREVIOUS-" + "A" * 132
    candidate_value = "PROPOSED-" + "B" * 132
    raw_value = "RAW-" + "C" * 132
    confirmed_value = "OILFIELD-" + "D" * 132
    before = insight_response([
        insight_slot("oilfield_name", "原油田", label="油田"),
        insight_slot("equipment_unit_id", previous_value, label="单机"),
    ])
    workspace.state(before)
    pending = insight_slot(
        "equipment_unit_id", previous_value if slot_status == "conflict" else None,
        status=slot_status, candidate=candidate_value, label="单机", version=2,
    )
    pending["raw_value"] = raw_value
    after = insight_response([
        insight_slot("oilfield_name", confirmed_value, label="油田", version=2), pending,
    ], phase="confirming", version=2, request_id=f"long-value-{slot_status}-{width}")
    workspace.state(after, origin="reply")
    page = workspace.page
    if width == 390:
        page.locator("#taskWorksheet > summary").click()
    summary = page.locator("#confirmationSummary")
    changes = page.locator("#taskChangesList")
    field = page.locator('#collectedFields [data-slot-key="equipment_unit_id"]')
    pw.expect(summary).to_contain_text(confirmed_value)
    pw.expect(changes).to_contain_text(candidate_value)
    pw.expect(changes).to_contain_text(previous_value)
    pw.expect(field).to_contain_text(candidate_value)
    pw.expect(field).to_contain_text(previous_value if slot_status == "conflict" else raw_value)
    for region in (summary, changes, field, page.locator("#sidebar")):
        pw.expect(region).to_be_visible()
        bounds = region.evaluate("el => ({width: el.clientWidth, content: el.scrollWidth})")
        assert bounds["content"] <= bounds["width"] + 1, bounds
    document_size = page.evaluate("""() => ({
        viewport: document.documentElement.clientWidth,
        html: document.documentElement.scrollWidth,
        body: document.body.scrollWidth
    })""")
    assert max(document_size["html"], document_size["body"]) <= document_size["viewport"] + 1, document_size


@pytest.mark.parametrize("slot_status", ["invalid", "unresolved"])
def test_insights_unaccepted_raw_input_changes_are_visible(workspace, slot_status):
    workspace.open()
    slot = insight_slot("start_time", status=slot_status, label="开始时间")
    slot["raw_value"] = "2026年13月45日"
    before = insight_response([slot])
    workspace.state(before)
    after = deepcopy(before)
    after["request_id"] = f"raw-change-{slot_status}"
    after["ui_state"]["slot_version"] = 2
    after["ui_state"]["slots"][0].update(raw_value="2026年2月30日", version=2)
    workspace.state(after, origin="reply")
    pw.expect(workspace.page.locator("#taskChangesCard")).to_be_visible()
    changes = workspace.page.locator("#taskChangesList")
    pw.expect(changes).to_contain_text("开始时间")
    pw.expect(changes).to_contain_text("2026年13月45日")
    pw.expect(changes).to_contain_text("2026年2月30日")
    assert not workspace.messages


def test_insights_valid_normalized_value_is_unchanged_when_only_wording_changes(workspace):
    workspace.open()
    slot = insight_slot("water_depth", 200, label="水深")
    slot["raw_value"] = "两百米"
    before = insight_response([slot])
    workspace.state(before)
    field = workspace.page.locator('#collectedFields [data-slot-key="water_depth"] .field-value')
    normalized_display = field.inner_text()
    after = deepcopy(before)
    after["request_id"] = "raw-wording-only"
    after["ui_state"]["slot_version"] = 2
    after["ui_state"]["slots"][0].update(raw_value="200 米", version=2)
    workspace.state(after, origin="reply")
    pw.expect(workspace.page.locator("#taskChangesCard")).to_be_hidden()
    pw.expect(field).to_have_text(normalized_display, use_inner_text=True)
    assert not workspace.messages


def test_insights_failed_context_transition_keeps_baseline_after_generation_change(workspace):
    workspace.open()
    workspace.state(insight_response([insight_slot("water_depth", 100, label="水深")]))
    current = insight_response(
        [insight_slot("water_depth", 200, label="水深", version=2)],
        version=2, request_id="before-failed-transition",
    )
    workspace.state(current, origin="reply")
    changes = workspace.page.locator("#taskChangesList")
    before_failure = changes.inner_text()
    assert "100" in before_failure and "200" in before_failure
    # Failed reset/history requests preserve the task but advance the local
    # generation to invalidate old asynchronous responses.
    workspace.page.evaluate("window.sessionGeneration += 1")
    workspace.state(current, origin="render")
    pw.expect(changes).to_have_text(before_failure, use_inner_text=True)
    unchanged_reply = deepcopy(current)
    unchanged_reply["request_id"] = "after-failed-transition"
    workspace.state(unchanged_reply, origin="reply")
    pw.expect(workspace.page.locator("#taskChangesCard")).to_be_hidden()
    pw.expect(workspace.page.locator("#collectedFields")).to_contain_text("200")
    assert not workspace.messages


def expanded_review_response():
    """Enough confirmed parameters to require a compact review, plus a proposal."""
    return insight_response([
        insight_slot("task_type", "管缆巡检", label="任务类型"),
        insight_slot("equipment_type", "观察级深海机器人 75HP", label="设备型号"),
        insight_slot("equipment_unit_id", "REVIEW-ROV-001", label="机器人编号"),
        insight_slot("water_depth", 320, label="水深"),
        insight_slot("start_time", "2026-10-27 10:00:00", label="开始时间"),
        insight_slot("end_time", "2026-10-27 12:00:00", label="结束时间"),
        insight_slot("oilfield_name", "REVIEW-OILFIELD", label="油田"),
        insight_slot("extension_note", "EXTENSION-KEPT", label="扩展参数"),
        insight_slot("cable_type", status="candidate", candidate="PROPOSAL-ONLY", label="管缆类型"),
    ], phase="confirming")


def test_task_guidance_locates_missing_and_review_fields_without_submitting(workspace):
    workspace.open()
    page = workspace.page
    page.locator("#messageInput").fill("保留尚未发送的草稿")
    for status in ("missing", "candidate"):
        data = insight_response([
            insight_slot("water_depth", 320, label="水深"),
            insight_slot("end_time", status=status, candidate="PROPOSED-END" if status == "candidate" else None,
                         label="结束时间"),
        ])
        workspace.state(data)
        pw.expect(page.locator("#taskNextStepCard")).to_be_visible()
        pw.expect(page.locator("#taskNextStepBody")).to_contain_text("结束时间")
        link = page.locator('#taskNextStepBody button[data-slot-key="end_time"]')
        link.focus()
        link.press("Enter")
        row = page.locator('.field-row[data-slot-key="end_time"]')
        pw.expect(row).to_be_focused()
        pw.expect(row).to_be_in_viewport()
        pw.expect(page.locator("#messageInput")).to_have_value("保留尚未发送的草稿")
    assert not workspace.messages


def test_task_guidance_prioritizes_blockers_and_avoids_edit_prompts_for_archived_tasks(workspace):
    workspace.open()
    page = workspace.page
    data = insight_response([
        insight_slot("water_depth", 650, label="水深"),
        insight_slot("end_time", status="missing", label="结束时间"),
    ], phase="blocked_hard")
    data["ui_state"]["workflow_phase"] = "validating"
    data["ui_state"]["constraint_state"].update({
        "source": "validation_result", "overall_status": "blocked_hard",
        "hard_violations": [{"constraint_id": "GUIDANCE-DEPTH", "message": "工作水深超过设备允许范围。",
                             "related_fields": ["water_depth"], "observed_value": 650, "threshold": 600}],
    })
    workspace.state(data, origin="reply")
    card = page.locator("#taskNextStepCard")
    pw.expect(card).to_have_attribute("data-tone", "danger")
    warning_link = card.locator('button[data-section-id="constraintsCard"]')
    pw.expect(warning_link).to_be_visible()
    pw.expect(card.locator('button[data-slot-key="end_time"]')).to_have_count(0)
    warning_link.click()
    pw.expect(page.locator("#constraintsCard")).to_be_in_viewport()
    pw.expect(page.locator("#publishTaskBtn")).to_be_disabled()

    for phase in ("collecting", "done"):
        archived = deepcopy(data)
        archived["ui_state"].update(phase=phase, workflow_phase=phase, read_only=True)
        archived["ui_state"]["actions"].update(can_modify=False, can_publish=False, can_confirm=False)
        workspace.state(archived, origin="restore")
        pw.expect(card.locator("button[data-slot-key]:visible:not(:disabled)")).to_have_count(0)
        pw.expect(page.locator("#confirmationCard")).to_be_hidden()
    assert not workspace.messages


def test_compact_confirmation_keeps_key_parameters_and_risks_visible(workspace):
    workspace.open()
    page = workspace.page
    data = expanded_review_response()
    data["ui_state"]["constraint_state"].update({
        "source": "validation_result", "overall_status": "pending_runtime_validation",
        "ignored_soft_warnings": [{"constraint_id": "REVIEW-RISK", "message": "已接受的低温作业风险"}],
    })
    workspace.state(data, origin="reply")
    summary = page.locator("#confirmationSummary")
    more = summary.locator("details.confirmation-more")
    assert more.evaluate("el => !el.open")
    for key in ("task_type", "equipment_type", "equipment_unit_id", "water_depth", "start_time", "end_time"):
        pw.expect(summary.locator(f'button[data-slot-key="{key}"]')).to_be_visible()
    for key in ("oilfield_name", "extension_note"):
        pw.expect(more.locator(f'button[data-slot-key="{key}"]')).to_be_hidden()
    pw.expect(summary).not_to_contain_text("PROPOSAL-ONLY")
    review = page.locator("#confirmationReview")
    pw.expect(review).to_be_visible()
    pw.expect(review).to_contain_text("等待运行条件校验")
    pw.expect(review).to_contain_text("已接受的低温作业风险")
    assert review.evaluate("el => !el.closest('details.confirmation-more')")
    toggle = more.locator(":scope > summary")
    toggle.focus()
    toggle.press("Enter")
    pw.expect(more.locator('button[data-slot-key="extension_note"]')).to_be_visible()
    assert not workspace.messages


def test_compact_confirmation_does_not_hide_a_short_parameter_list(workspace):
    workspace.open()
    page = workspace.page
    data = expanded_review_response()
    data["ui_state"]["slots"] = data["ui_state"]["slots"][2:8]
    workspace.state(data)
    summary = page.locator("#confirmationSummary")
    pw.expect(summary.locator("details.confirmation-more")).to_have_count(0)
    for slot in data["ui_state"]["slots"]:
        pw.expect(summary.locator(f'button[data-slot-key="{slot["key"]}"]')).to_be_visible()
    assert not workspace.messages


def test_compact_confirmation_expansion_survives_locale_but_not_task_switch_or_reset(workspace):
    workspace.open()
    page = workspace.page
    data = expanded_review_response()
    workspace.state(data)
    more = page.locator("#confirmationSummary details.confirmation-more")
    more.locator(":scope > summary").click()
    for lang in ("en", "zh"):
        page.locator("#langSelect").select_option(lang)
        assert more.evaluate("el => el.open")
        pw.expect(more.locator('button[data-slot-key="extension_note"]')).to_be_visible()
    changed = deepcopy(data)
    changed["request_id"] = "review-same-task"
    changed["ui_state"]["slot_version"] = 2
    changed["ui_state"]["slots"][3]["value"] = 330
    workspace.state(changed, origin="reply")
    assert more.evaluate("el => el.open")

    switched = deepcopy(data)
    switched["ui_state"]["task_type_key"] = "tree_valve_operation"
    workspace.state(switched, origin="reply")
    assert more.evaluate("el => !el.open")
    more.locator(":scope > summary").click()
    page.locator("#resetBtn").click()
    pw.expect(page.locator("#taskEmptyState")).to_be_visible()
    workspace.state(switched)
    assert more.evaluate("el => !el.open")
    assert not workspace.messages


def test_grouped_fields_preserve_unknown_parameters_and_reference_focus(workspace):
    workspace.open()
    page = workspace.page
    data = expanded_review_response()
    data["ui_state"]["slots"].extend([
        insight_slot("equipment_family", status="missing", label="设备类别"),
        insight_slot("new_required_parameter", status="missing", label="扩展必填参数"),
    ])
    workspace.state(data)
    rows = page.locator("#collectedFields .field-row, #missingFields .field-row")
    actual_keys = rows.evaluate_all("rows => rows.map(row => row.dataset.slotKey)")
    expected_keys = [slot["key"] for slot in data["ui_state"]["slots"]]
    assert sorted(actual_keys) == sorted(expected_keys)
    assert page.locator("#collectedFields .field-group").count() >= 2
    for key in ("extension_note", "new_required_parameter"):
        row = page.locator(f'.field-row[data-slot-key="{key}"]')
        assert row.evaluate("el => Boolean(el.closest('.field-group')?.querySelector('h4.field-group-title')?.textContent.trim())")
    more = page.locator("#confirmationSummary details.confirmation-more")
    more.locator(":scope > summary").click()
    link = more.locator('button[data-slot-key="extension_note"]')
    link.focus()
    link.press("Enter")
    row = page.locator('#collectedFields .field-row[data-slot-key="extension_note"]')
    pw.expect(row).to_be_focused()
    pw.expect(row).to_be_in_viewport()
    pw.expect(row).to_contain_text("EXTENSION-KEPT")
    pw.expect(page.locator('#collectedFields .field-row[data-slot-key="cable_type"]')).to_contain_text("PROPOSAL-ONLY")
    assert not workspace.messages


def test_mobile_task_shortcut_opens_current_work_without_submitting_or_losing_draft(workspace):
    workspace.open(390)
    page = workspace.page
    shortcut = page.locator("#mobileTaskShortcut")
    pw.expect(shortcut).to_be_hidden()
    data = insight_response([
        insight_slot("water_depth", 320, label="水深"),
        insight_slot("start_time", status="missing", label="开始时间"),
        insight_slot("end_time", status="missing", label="结束时间"),
        insight_slot("equipment_unit_id", status="candidate", candidate="REVIEW-ROV", label="机器人编号"),
    ])
    workspace.state(data)
    draft = "补充时间前先核对机器人编号"
    page.locator("#messageInput").fill(draft)
    pw.expect(shortcut).to_be_visible()
    pw.expect(shortcut).to_contain_text("待补充 2 项")
    pw.expect(shortcut).to_contain_text("待核对 1 项")
    pw.expect(shortcut).to_have_attribute("aria-controls", "taskWorksheet")
    pw.expect(shortcut).to_have_attribute("aria-expanded", "false")
    shortcut.focus()
    shortcut.press("Enter")
    heading = page.locator("#taskWorksheet > summary")
    pw.expect(heading).to_be_focused()
    pw.expect(heading).to_be_in_viewport()
    pw.expect(shortcut).to_have_attribute("aria-expanded", "true")
    pw.expect(page.locator("#messageInput")).to_have_value(draft)
    heading.press("Enter")
    pw.expect(shortcut).to_have_attribute("aria-expanded", "false")
    assert not workspace.messages
    page.locator("#resetBtn").click()
    pw.expect(shortcut).to_be_hidden()
    pw.expect(page.locator("#taskEmptyState")).to_be_visible()
    assert not workspace.messages


def test_mobile_task_shortcut_reports_blocked_read_only_and_unavailable_states(workspace):
    workspace.open(320)
    page = workspace.page
    shortcut = page.locator("#mobileTaskShortcut")
    scenarios = [
        ({"phase": "blocked_hard", "workflow_phase": "validating"}, "条件未满足", "Conditions not met"),
        ({"phase": "blocked_soft", "workflow_phase": "validating"}, "待处理警告", "Warning to review"),
        ({"read_only": True}, "只读", "Read-only"),
        ({"state_status": "error"}, "状态暂不可用", "State unavailable"),
    ]
    for state, chinese, english in scenarios:
        data = response_fixture(task=True)
        data["ui_state"].update(state)
        workspace.state(data)
        for lang, expected in (("zh", chinese), ("en", english)):
            page.locator("#langSelect").select_option(lang)
            pw.expect(shortcut).to_be_visible()
            pw.expect(shortcut).to_contain_text(expected)
            shortcut.click()
            pw.expect(page.locator("#taskWorksheet > summary")).to_be_focused()
            pw.expect(page.locator("#publishTaskBtn")).to_be_hidden()
    empty_error = response_fixture()
    empty_error["ui_state"]["state_status"] = "error"
    workspace.state(empty_error)
    pw.expect(shortcut).to_be_hidden()
    workspace.state(response_fixture(task=True))
    page.set_viewport_size({"width": 1280, "height": 900})
    pw.expect(shortcut).to_be_hidden()
    assert not workspace.messages
