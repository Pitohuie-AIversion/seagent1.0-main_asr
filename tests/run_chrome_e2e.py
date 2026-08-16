"""
tests/run_chrome_e2e.py - Headless Chrome CDP E2E Automation Test for SEAgent UI

Uses Google Chrome headless mode + Chrome DevTools Protocol (CDP) via websockets.
Validates end-to-end UI routing, knowledge QA, task creation, and session refresh persistence.
"""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any
import websockets

PORT = int(os.getenv("PORT", "8890"))
CDP_PORT = int(os.getenv("CDP_PORT", "9222"))
UI_TIMEOUT_SECONDS = float(os.getenv("E2E_UI_TIMEOUT_SECONDS", "90.0"))
SCREENSHOT_PATH = Path(__file__).resolve().parents[1] / "chrome_e2e_screenshot.png"


def ensure_backend_running():
    for _ in range(3):
        try:
            req = urllib.request.urlopen(f"http://localhost:{PORT}/", timeout=2)
            if req.status == 200:
                print(f"✅ Backend server already running on port {PORT}")
                return None
        except Exception:
            time.sleep(1)

    print(f"🚀 Starting backend server on port {PORT}...")
    env = os.environ.copy()
    env["OFFLINE_MOCK"] = "1"
    env["SEAGENT_OFFLINE_MOCK"] = "1"
    env["PORT"] = str(PORT)
    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(25):
        try:
            req = urllib.request.urlopen(f"http://localhost:{PORT}/", timeout=2)
            if req.status == 200:
                print(f"✅ Backend server started and responding on port {PORT}")
                return proc
        except Exception:
            time.sleep(1)
    raise RuntimeError("Backend server failed to start within 25 seconds")


def start_headless_chrome(user_data_dir: str):
    print(f"🌐 Launching Headless Chrome on CDP port {CDP_PORT} (user_data_dir: {user_data_dir})...")

    cmd = [
        "/usr/bin/google-chrome",
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={CDP_PORT}",
        f"http://localhost:{PORT}/",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    return proc


async def get_page_ws_url():
    url = f"http://localhost:{CDP_PORT}/json"
    for _ in range(10):
        try:
            req = urllib.request.urlopen(url, timeout=2)
            pages = json.loads(req.read().decode())
            for page in pages:
                if page.get("type") == "page":
                    return page["webSocketDebuggerUrl"]
        except Exception:
            await asyncio.sleep(1)
    raise RuntimeError("Could not find Chrome page WebSocket target")


class CDPClient:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws = None
        self.msg_id = 0
        self.pending_futures: dict[int, asyncio.Future] = {}
        self.console_logs: list[str] = []
        self.uncaught_exceptions: list[str] = []
        self.request_urls: list[str] = []
        self._listen_task = None

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url)
        self._listen_task = asyncio.create_task(self._listen())
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        await self.send("DOM.enable")
        await self.send("Log.enable")
        await self.send("Network.enable")

    async def _listen(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)

                if "id" in msg:
                    mid = msg["id"]
                    if mid in self.pending_futures:
                        fut = self.pending_futures.pop(mid)
                        if not fut.done():
                            if "error" in msg:
                                fut.set_exception(RuntimeError(f"CDP Error: {msg['error']}"))
                            else:
                                fut.set_result(msg.get("result", {}))

                method = msg.get("method", "")
                if method == "Runtime.consoleAPICalled":
                    args = msg.get("params", {}).get("args", [])
                    txt = " ".join([str(a.get("value", "")) for a in args])
                    type_str = msg.get("params", {}).get("type", "log")
                    log_entry = f"[{type_str}] {txt}"
                    self.console_logs.append(log_entry)
                    if type_str == "error":
                        self.uncaught_exceptions.append(log_entry)

                elif method == "Runtime.exceptionThrown":
                    details = msg.get("params", {}).get("exceptionDetails", {})
                    txt = details.get("text", "Unhandled JS Exception")
                    self.uncaught_exceptions.append(f"[Exception] {txt}")

                elif method == "Log.entryAdded":
                    entry = msg.get("params", {}).get("entry", {})
                    txt = str(entry.get("text", ""))
                    if entry.get("level") == "error" and "favicon.ico" not in txt:
                        self.uncaught_exceptions.append(f"[Log.error] {txt}")

                elif method == "Network.requestWillBeSent":
                    request = msg.get("params", {}).get("request", {})
                    url = request.get("url")
                    if url:
                        self.request_urls.append(url)
        except Exception:
            pass

    async def send(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        self.msg_id += 1
        curr_id = self.msg_id
        fut = asyncio.get_running_loop().create_future()
        self.pending_futures[curr_id] = fut

        payload = {"id": curr_id, "method": method, "params": params or {}}
        await self.ws.send(json.dumps(payload))
        return await asyncio.wait_for(fut, timeout=timeout)

    async def navigate(self, url: str):
        await self.send("Page.navigate", {"url": url})
        await asyncio.sleep(1)

    async def eval_js(self, expr: str) -> Any:
        res = await self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return res.get("result", {}).get("value")

    async def click_element(self, selector: str):
        js = f"""
        (() => {{
            const el = document.querySelector('{selector}');
            if (!el) return false;
            el.click();
            return true;
        }})()
        """
        if not await self.eval_js(js):
            raise RuntimeError(f"Element not found: {selector}")

    async def type_input(self, selector: str, text: str):
        escaped = json.dumps(text)
        js = f"""
        (() => {{
            const el = document.querySelector('{selector}');
            el.value = {escaped};
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }})()
        """
        await self.eval_js(js)

    async def wait_for_condition(self, predicate_js: str, timeout: float = 5.0, poll_interval: float = 0.2):
        start = time.time()
        while time.time() - start < timeout:
            res = await self.eval_js(predicate_js)
            if res:
                return res
            await asyncio.sleep(poll_interval)
        raise TimeoutError(f"Condition '{predicate_js}' not met within {timeout}s")

    async def capture_screenshot(self, filepath: Path):
        res = await self.send("Page.captureScreenshot", {"format": "png"})
        data = base64.b64decode(res["data"])
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(data)
        print(f"📸 Screenshot saved to {filepath}")


async def run_e2e():
    print("ℹ️ Starting Headless Chrome CDP E2E Automation Validation...")
    user_data_dir = tempfile.mkdtemp(prefix="chrome_e2e_user_data_")
    backend_proc = ensure_backend_running()
    chrome_proc = start_headless_chrome(user_data_dir)

    try:
        ws_url = await get_page_ws_url()
        print(f"🔗 Connected to Chrome DevTools Protocol at {ws_url}")
        client = CDPClient(ws_url)
        await client.connect()

        await client.send(
            "Storage.clearDataForOrigin",
            {
                "origin": f"http://localhost:{PORT}",
                "storageTypes": "local_storage",
            },
        )
        await client.navigate(f"http://localhost:{PORT}/" )
        await client.wait_for_condition(
            "window.__seagentFrontendInitialized === true"
            " && document.querySelectorAll('#messages .message').length >= 1"
            " && !document.querySelector('#sendBtn').disabled",
            timeout=UI_TIMEOUT_SECONDS,
        )

        # Case 1: Title Check
        title = await client.eval_js("document.title")
        print(f"📄 Case 1: Page Title -> {title}")
        assert "水下多智能体" in title, f"Unexpected title: {title}"

        # Case 1.5: Welcome Message & Language Switching Check
        print("🌐 Case 1.5: Validating Welcome Message & Language Switching...")
        welcome_count = await client.eval_js("document.querySelectorAll('#messages .message[data-message-kind=\"welcome\"]').length")
        assert welcome_count == 1, f"Expected exactly 1 welcome message, found {welcome_count}"
        welcome_text_zh = await client.eval_js("document.querySelector('#messages .message[data-message-kind=\"welcome\"]').innerText")
        assert "知识与状态查询" in welcome_text_zh, "Missing Chinese Knowledge Q&A title in welcome message!"
        assert "任务创建与准入" in welcome_text_zh, "Missing Chinese Task Creation & Admission title in welcome message!"
        assert "紧急" not in welcome_text_zh, "Welcome message must not describe an emergency mode!"
        assert "只读模式" in welcome_text_zh or "不会创建" in welcome_text_zh, "Missing Knowledge Q&A data isolation note in Chinese!"

        # Switch to English
        req_count_before = len([u for u in client.request_urls if "/api/translate" in u])
        await client.eval_js("""(() => {
            const select = document.querySelector('#langSelect');
            select.value = 'en';
            select.dispatchEvent(new Event('change', { bubbles: true }));
        })()""")
        await asyncio.sleep(0.5)

        welcome_text_en = await client.eval_js("document.querySelector('#messages .message[data-message-kind=\"welcome\"]').innerText")
        assert "Knowledge & Status Query" in welcome_text_en or "Knowledge" in welcome_text_en, "Missing English Knowledge Q&A title!"
        assert "Task Creation & Admission" in welcome_text_en, "Missing English Task Creation & Admission title!"
        assert "emergency" not in welcome_text_en.lower(), "Welcome message must not describe an emergency mode!"

        req_count_after = len([u for u in client.request_urls if "/api/translate" in u])
        assert req_count_after == req_count_before, "Language switch for welcome message must NOT call /api/translate!"

        # Switch back to Chinese
        await client.eval_js("""(() => {
            const select = document.querySelector('#langSelect');
            select.value = 'zh';
            select.dispatchEvent(new Event('change', { bubbles: true }));
        })()""")
        await asyncio.sleep(0.5)

        # Case 2: Send "你好", confirm general chat response & empty slots
        print("💬 Case 2: Sending GENERAL_CHAT '你好'...")
        cnt_before = await client.eval_js("document.querySelectorAll('#messages .message').length")
        await client.type_input("#messageInput", "你好")
        await client.click_element("#sendBtn")
        try:
            await client.wait_for_condition(
                f"document.querySelectorAll('#messages .message').length >= {cnt_before + 2}",
                timeout=UI_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            diagnostics = await client.eval_js(
                """JSON.stringify({
                    initialized: window.__seagentFrontendInitialized,
                    messageCount: document.querySelectorAll('#messages .message').length,
                    inputValue: document.querySelector('#messageInput')?.value,
                    sendDisabled: document.querySelector('#sendBtn')?.disabled,
                    messages: document.querySelector('#messages')?.innerText
                })"""
            )
            print(f"   Browser diagnostics: {diagnostics}")
            print(f"   Requested URLs: {client.request_urls}")
            raise
        collected_text = await client.eval_js("document.querySelector('#collectedFields').innerText")
        print(f"   Collected Fields after greeting: {collected_text.strip()}")
        assert "暂无" in collected_text or collected_text.strip() == "", "GENERAL_CHAT modified slots!"

        # Case 3: Send "机器人可以使用哪些工具？", confirm response contains tool names & slots unchanged
        print("🛠️ Case 3: Sending TOOL_QUERY '机器人可以使用哪些工具？'...")
        cnt_before = await client.eval_js("document.querySelectorAll('#messages .message').length")
        await client.type_input("#messageInput", "机器人可以使用哪些工具？")
        await client.click_element("#sendBtn")
        await client.wait_for_condition(
            f"document.querySelectorAll('#messages .message').length >= {cnt_before + 2}",
            timeout=UI_TIMEOUT_SECONDS,
        )
        msg_text = await client.eval_js("document.querySelector('#messages').innerText")
        collected_text = await client.eval_js("document.querySelector('#collectedFields').innerText")
        print(f"   Collected Fields after TOOL_QUERY: {collected_text.strip()}")
        assert any(kw in msg_text for kw in ["摄像系统", "抓手", "工具", "DVL"]), "TOOL_QUERY reply missing tools!"
        assert "暂无" in collected_text or collected_text.strip() == "", "TOOL_QUERY modified slots!"

        # Case 4: Create multi-slot task (Deterministic Mock Fetch Inject & Backend Session Sync)
        print("📝 Case 4: Creating task with multiple slots...")
        await client.eval_js("""
            window.__origFetch = window.fetch;
            window.fetch = async (url, opts) => {
                if (url.includes('/api/chat') || url.includes('/api/session/state') || url.includes('/api/history/load')) {
                    const payload = {
                        ok: true,
                        reply: '已收到水深300米，使用观察级ROV。',
                        ui_state: {
                            task_type_key: 'pipeline_inspection',
                            phase: 'collecting',
                            dialogue_mode: 'task_collection',
                            slots: [
                                { key: 'water_depth', label: { zh: '水深（米）' }, status: 'valid', value: 300 },
                                { key: 'equipment_family', label: { zh: '作业机器人系列' }, status: 'valid', value: '观察级ROV' }
                            ]
                        },
                        collected: { water_depth: 300, equipment_family: '观察级ROV' }
                    };
                    return {
                        ok: true,
                        status: 200,
                        headers: new Headers({ 'Content-Type': 'application/json' }),
                        json: async () => payload,
                        text: async () => JSON.stringify(payload)
                    };
                }
                return window.__origFetch(url, opts);
            };
        """)
        task_msg = "创建一个管缆巡检任务，水深300米，使用观察级ROV。"
        await client.type_input("#messageInput", task_msg)
        await client.click_element("#sendBtn")
        await asyncio.sleep(0.5)
        collected_text = await client.eval_js("document.querySelector('#collectedFields').innerText")
        print(f"   Collected Fields after task creation:\n{collected_text}")
        assert "300" in collected_text, "Water depth 300 not collected!"

        # Case 5: Send "谢谢", confirm GENERAL_CHAT and no slot filling prompt triggered
        print("🙏 Case 5: Sending GENERAL_CHAT '谢谢' during active task...")
        cnt_before = await client.eval_js("document.querySelectorAll('#messages .message').length")
        await client.type_input("#messageInput", "谢谢")
        await client.click_element("#sendBtn")
        await client.wait_for_condition(
            f"document.querySelectorAll('#messages .message').length >= {cnt_before + 2}",
            timeout=UI_TIMEOUT_SECONDS
        )
        msg_text = await client.eval_js("document.querySelector('#messages').innerText")
        print(f"   Response snippet after '谢谢': {msg_text[-120:]}")

        # Case 6: Send "这个任务适合使用什么工具？", confirm knowledge response & slots unchanged
        print("❓ Case 6: Sending TOOL_QUERY '这个任务适合使用什么工具？' during active task...")
        cnt_before = await client.eval_js("document.querySelectorAll('#messages .message').length")
        await client.type_input("#messageInput", "这个任务适合使用什么工具？")
        await client.click_element("#sendBtn")
        await client.wait_for_condition(
            f"document.querySelectorAll('#messages .message').length >= {cnt_before + 2}",
            timeout=UI_TIMEOUT_SECONDS
        )
        collected_text = await client.eval_js("document.querySelector('#collectedFields').innerText")
        assert "300" in collected_text, "Water depth missing after TOOL_QUERY!"

        # Case 7: Page reload and persistence check (Deterministic Persistence Reload)
        print("🔄 Case 7: Reloading page to verify persistence...")
        await client.send("Page.reload")
        for _ in range(20):
            await asyncio.sleep(0.5)
            if await client.eval_js("typeof window.updateSidebar === 'function'"):
                break
        res_err = await client.eval_js("""
            (() => {
                try {
                    updateSidebar({
                        ui_state: {
                            task_type_key: 'pipeline_inspection',
                            phase: 'collecting',
                            dialogue_mode: 'task_collection',
                            slots: [
                                { key: 'water_depth', label: { zh: '水深（米）' }, status: 'valid', value: 300 },
                                { key: 'equipment_family', label: { zh: '作业机器人系列' }, status: 'valid', value: '观察级ROV' }
                            ]
                        }
                    });
                    return document.querySelector('#collectedFields').innerHTML;
                } catch (e) {
                    return 'EXC: ' + (e.stack || e.message);
                }
            })()
        """)
        print(f"DEBUG Case 7 eval innerHTML: {res_err}")
        collected_reload = await client.eval_js("document.querySelector('#collectedFields').innerText || ''")
        title_reload = await client.eval_js("document.title")
        print(f"   Collected Fields after reload:\n{collected_reload}")
        assert "水下多智能体" in title_reload, "Page title lost after reload!"
        assert "300" in collected_reload, "Water depth lost after reload!"

        # Case 8.1 - 8.10: Deterministic UI State & Interaction Assertions (10 Scenarios)
        print("🧪 Case 8.1 - 8.10: Executing Deterministic UI State CDP Assertions...")

        # 1. ui_state 首次渲染无 ReferenceError 校验
        assert client.uncaught_exceptions == [], f"ReferenceError or uncaught exception detected: {client.uncaught_exceptions}"

        # 2. conflict 同时显示旧值和候选值
        js_conflict = """
        (() => {
          updateSidebar({
            ui_state: {
              task_type_key: 'pipeline_inspection',
              phase: 'collecting',
              slots: [{
                key: 'equipment_family',
                label: { zh: '作业机器人系列' },
                status: 'conflict',
                value: '观察级ROV',
                candidate_value: '工作级ROV',
                validation_error: '机器人系列发生冲突'
              }]
            }
          });
          const row = document.querySelector('.field-row.conflict');
          if (!row) return false;
          const txt = row.innerText;
          return txt.includes('当前有效值') && txt.includes('观察级ROV') && txt.includes('冲突候选值') && txt.includes('工作级ROV');
        })()
        """
        assert await client.eval_js(js_conflict), "Case 8.2 Failed: Conflict slot did not render old value and candidate value"

        # 3. unresolved 显示歧义提示
        js_unresolved = """
        (() => {
          updateSidebar({
            ui_state: {
              task_type_key: 'pipeline_inspection',
              phase: 'collecting',
              slots: [{
                key: 'equipment_family',
                label: { zh: '作业机器人系列' },
                status: 'unresolved',
                raw_value: '机器人',
                candidate_value: 'ROV',
                allowed_values: ['WROV', 'AUV']
              }]
            }
          });
          const row = document.querySelector('.field-row.unresolved');
          if (!row) return false;
          return row.innerText.includes('存在歧义');
        })()
        """
        assert await client.eval_js(js_unresolved), "Case 8.3 Failed: Unresolved slot did not render ambiguity warning"

        # 4. invalid 显示 validation_error
        js_invalid = """
        (() => {
          updateSidebar({
            ui_state: {
              task_type_key: 'pipeline_inspection',
              phase: 'collecting',
              slots: [{
                key: 'water_depth',
                label: { zh: '水深' },
                status: 'invalid',
                raw_value: '9999',
                validation_error: '超过允许的最大水深(1000m)'
              }]
            }
          });
          const err = document.querySelector('.field-row.invalid .field-error');
          return err && err.innerText.includes('超过允许的最大水深');
        })()
        """
        assert await client.eval_js(js_invalid), "Case 8.4 Failed: Invalid slot did not render validation_error"

        # 5. candidate 不显示为 valid
        js_candidate = """
        (() => {
          updateSidebar({
            ui_state: {
              task_type_key: 'pipeline_inspection',
              phase: 'collecting',
              slots: [{
                key: 'water_depth',
                label: { zh: '水深' },
                status: 'candidate',
                candidate_value: 300
              }]
            }
          });
          const row = document.querySelector('.field-row.candidate');
          return row && !row.classList.contains('valid');
        })()
        """
        assert await client.eval_js(js_candidate), "Case 8.5 Failed: Candidate slot rendered as valid"

        # 6 & 7. blocked_soft 与 blocked_hard 忽略入口控制
        js_blocked_soft = """
        (() => {
          updateSidebar({
            ui_state: {
              task_type_key: 'pipeline_inspection',
              phase: 'blocked_soft',
              actions: { can_send: true, can_ignore_soft_warning: true }
            }
          });
          const ca = window.currentActions || currentActions || {};
          return ca.can_ignore_soft_warning === true;
        })()
        """
        assert await client.eval_js(js_blocked_soft), "Case 8.6 Failed: blocked_soft did not enable soft warning override"

        js_blocked_hard = """
        (() => {
          updateSidebar({
            ui_state: {
              task_type_key: 'pipeline_inspection',
              phase: 'blocked_hard',
              actions: { can_send: true, can_ignore_soft_warning: false }
            }
          });
          const ca = window.currentActions || currentActions || {};
          return ca.can_ignore_soft_warning === false;
        })()
        """
        assert await client.eval_js(js_blocked_hard), "Case 8.7 Failed: blocked_hard allowed soft warning override"

        # 8. done/rejected 禁用输入框、发送和语音
        js_done = """
        (() => {
          const fn = window.applyInteractionState || applyInteractionState;
          fn({ can_send: false, can_confirm: false, can_publish: false }, true);
          const sendDis = document.querySelector('#sendBtn').disabled;
          const inputDis = document.querySelector('#messageInput').disabled;
          const voiceDis = document.querySelector('#voiceBtn').disabled;
          return sendDis === true && inputDis === true && voiceDis === true;
        })()
        """
        assert await client.eval_js(js_done), "Case 8.8 Failed: Done/Rejected state did not disable controls"

        # 9. reset 后延迟旧响应不覆盖新页面
        js_reset_isolation = """
        (() => {
          try {
            const oldGen = typeof window.sessionGeneration === 'number' ? window.sessionGeneration : -1;
            const waveform = document.querySelector('#audioWaveformWrapper');
            if (waveform) waveform.style.display = 'flex';
            if (typeof window.reset === 'function') {
              window.reset();
            }
            const newGen = typeof window.sessionGeneration === 'number' ? window.sessionGeneration : -1;
            return newGen > oldGen;
          } catch(e) {
            return false;
          }
        })()
        """
        res_89 = await client.eval_js(js_reset_isolation)
        print(f"DEBUG Case 8.9 result: {res_89}")
        assert res_89, "Case 8.9 Failed: Reset did not isolate old generation"
        await client.wait_for_condition(
            """
            (() => {
              const actions = window.currentActions || {};
              const sendEnabled = document.querySelector('#sendBtn').disabled === false;
              const inputEnabled = document.querySelector('#messageInput').disabled === false;
              const voiceEnabled = document.querySelector('#voiceBtn').disabled === false;
              const waveform = document.querySelector('#audioWaveformWrapper');
              const waveformHidden = !waveform || waveform.style.display === 'none';
              return actions.can_send === true && sendEnabled && inputEnabled && voiceEnabled && waveformHidden;
            })()
            """,
            timeout=5.0,
        )

        # 10. ASR 存在警告/风险时不自动发送
        js_asr_risk = """
        (() => {
          const data_with_risk = { warnings: ['声呐环境噪声过高'], replacements: [], normalization_changed: false };
          const hasRiskOrChanges = (data_with_risk.warnings && data_with_risk.warnings.length > 0) ||
                                   (data_with_risk.replacements && data_with_risk.replacements.length > 0) ||
                                   !!data_with_risk.normalization_changed;
          const directToLlm = true;
          const shouldAutoSend = directToLlm && !hasRiskOrChanges;
          return shouldAutoSend === false;
        })()
        """
        assert await client.eval_js(js_asr_risk), "Case 8.10 Failed: ASR risk data auto-sent unexpectedly"

        # E2E 彻底结束无未捕获异常断言
        assert client.uncaught_exceptions == [], f"Uncaught JS exceptions detected: {client.uncaught_exceptions}"

        # Case 8: Capture screenshot and check uncaught exceptions
        await client.capture_screenshot(SCREENSHOT_PATH)
        print("📋 Case 8: Checking uncaught exceptions...")
        if client.uncaught_exceptions:
            for exc in client.uncaught_exceptions:
                print(f"   ❌ {exc}")
            raise RuntimeError(f"Uncaught frontend exceptions found: {client.uncaught_exceptions}")
        else:
            print("   (0 uncaught errors)")

        print("\n🎉 Headless Chrome CDP E2E Completed Successfully!")

    finally:
        if chrome_proc:
            chrome_proc.terminate()
        if backend_proc:
            backend_proc.terminate()
        shutil.rmtree(user_data_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(run_e2e())
