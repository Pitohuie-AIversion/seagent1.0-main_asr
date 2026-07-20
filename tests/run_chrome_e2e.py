"""
tests/run_chrome_e2e.py - Headless Chrome CDP E2E Automation Test for SEAgent UI (Mock UI E2E)
Uses Google Chrome headless mode + Chrome DevTools Protocol (CDP) via websockets.
Note: This script performs Mock UI E2E testing using OFFLINE_MOCK mode for UI workflow validation.
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
import websockets

PORT = 8890
CDP_PORT = 9222
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
    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(15):
        try:
            req = urllib.request.urlopen(f"http://localhost:{PORT}/", timeout=2)
            if req.status == 200:
                print(f"✅ Backend server started and responding on port {PORT}")
                return proc
        except Exception:
            time.sleep(1)
    raise RuntimeError("Backend server failed to start within 15 seconds")


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
        self._listen_task = None

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url)
        self._listen_task = asyncio.create_task(self._listen())
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        await self.send("DOM.enable")
        await self.send("Log.enable")

    async def _listen(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)

                # 消息 ID 响应处理（非阻塞 Future 分发）
                if "id" in msg:
                    mid = msg["id"]
                    if mid in self.pending_futures:
                        fut = self.pending_futures.pop(mid)
                        if not fut.done():
                            if "error" in msg:
                                fut.set_exception(RuntimeError(f"CDP Error: {msg['error']}"))
                            else:
                                fut.set_result(msg.get("result", {}))

                # 事件监听
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
                    if entry.get("level") == "error":
                        self.uncaught_exceptions.append(f"[Log.error] {entry.get('text')}")
        except Exception:
            pass

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.msg_id += 1
        curr_id = self.msg_id
        fut = asyncio.get_running_loop().create_future()
        self.pending_futures[curr_id] = fut

        payload = {"id": curr_id, "method": method, "params": params or {}}
        await self.ws.send(json.dumps(payload))
        return await fut

    async def navigate(self, url: str):
        await self.send("Page.navigate", {"url": url})
        await asyncio.sleep(1)

    async def eval_js(self, expr: str) -> Any:
        res = await self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return res.get("result", {}).get("value")

    async def click_element(self, selector: str):
        js = f"document.querySelector('{selector}').click()"
        await self.eval_js(js)

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
    print("ℹ️ Starting Mock UI E2E Automation Validation...")
    user_data_dir = tempfile.mkdtemp(prefix="chrome_e2e_user_data_")
    backend_proc = ensure_backend_running()
    chrome_proc = start_headless_chrome(user_data_dir)

    try:
        ws_url = await get_page_ws_url()
        print(f"🔗 Connected to Chrome DevTools Protocol at {ws_url}")
        client = CDPClient(ws_url)
        await client.connect()

        await client.navigate(f"http://localhost:{PORT}/")
        await client.eval_js("localStorage.clear(); sessionStorage.clear();")

        # Step 1: Check page title
        title = await client.eval_js("document.title")
        print(f"📄 Page Title: {title}")
        assert "水下多智能体" in title, f"Unexpected title: {title}"

        # Step 2: Send GENERAL_CHAT message "你好"
        print("💬 Step 2: Sending GENERAL_CHAT '你好'...")
        await client.type_input("#messageInput", "你好")
        await client.click_element("#sendBtn")

        await client.wait_for_condition(
            "document.querySelectorAll('#messages .message').length >= 2",
            timeout=5.0
        )

        messages_text = await client.eval_js("document.querySelector('#messages').innerText")
        collected_text = await client.eval_js("document.querySelector('#collectedFields').innerText")
        print(f"   Response received. Messages snippet: {messages_text[:80]}...")
        print(f"   Collected Fields: {collected_text}")
        assert "暂无" in collected_text or collected_text.strip() == "", "GENERAL_CHAT modified slot store!"

        # Step 3: Create task with multiple slots
        print("📝 Step 3: Creating task with multiple slots...")
        task_msg = "创建一个水下巡检任务，水深300米，使用观察级ROV在北纬19.5度、东经115.2度执行。"
        await client.type_input("#messageInput", task_msg)
        await client.click_element("#sendBtn")

        await client.wait_for_condition(
            "document.querySelector('#collectedFields').innerText.includes('300')",
            timeout=5.0
        )

        collected_text = await client.eval_js("document.querySelector('#collectedFields').innerText")
        print(f"   Collected Fields after task creation:\n{collected_text}")
        assert "管缆巡检" in collected_text or "pipeline_inspection" in collected_text, "Task type not collected!"
        assert "300" in collected_text, "Water depth not collected!"

        # Step 4: Reload page and verify state persistence
        print("🔄 Step 4: Reloading page to verify persistence...")
        await client.send("Page.reload")
        await asyncio.sleep(2)

        title_after_reload = await client.eval_js("document.title")
        collected_after_reload = await client.eval_js("document.querySelector('#collectedFields').innerText")
        messages_after_reload = await client.eval_js("document.querySelector('#messages').innerText")
        print(f"   Page Title after reload: {title_after_reload}")
        print(f"   Collected Fields after reload:\n{collected_after_reload}")
        
        assert "水下多智能体" in title_after_reload, "Page failed to load after refresh"
        assert "300" in collected_after_reload, "Water depth missing after refresh!"
        assert len(messages_after_reload.strip()) > 0, "Chat history lost after refresh!"

        # Step 5: Capture screenshot and check console logs
        await client.capture_screenshot(SCREENSHOT_PATH)
        
        print("📋 Uncaught Frontend Exceptions:")
        if client.uncaught_exceptions:
            for exc in client.uncaught_exceptions:
                print(f"   ❌ {exc}")
            raise RuntimeError(f"Uncaught frontend exceptions found: {client.uncaught_exceptions}")
        else:
            print("   (0 uncaught errors)")

        print("\n🎉 Headless Chrome CDP E2E (Mock UI) Completed Successfully!")

    finally:
        if chrome_proc:
            chrome_proc.terminate()
        if backend_proc:
            backend_proc.terminate()
        shutil.rmtree(user_data_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(run_e2e())
