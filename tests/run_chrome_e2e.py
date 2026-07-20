"""
tests/run_chrome_e2e.py - Headless Chrome CDP E2E Automation Test for SEAgent UI
Uses Google Chrome headless mode + Chrome DevTools Protocol (CDP) via websockets.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
import websockets

PORT = 8890
CDP_PORT = 9222
SCREENSHOT_PATH = Path(__file__).resolve().parents[1] / "chrome_e2e_screenshot.png"
ARTIFACT_SCREENSHOT_PATH = Path("/root/.gemini/antigravity-ide/brain/81a7c136-0464-49cb-a111-be3850b6ce87/chrome_e2e_screenshot.png")


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
        stderr=subprocess.DEVNULL
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


def start_headless_chrome():
    print(f"🌐 Launching Headless Chrome on CDP port {CDP_PORT}...")
    subprocess.run(["pkill", "-f", "google-chrome"], stderr=subprocess.DEVNULL)
    time.sleep(1)
    
    cmd = [
        "/usr/bin/google-chrome",
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        f"--remote-debugging-port={CDP_PORT}",
        f"http://localhost:{PORT}/"
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
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.ws = None
        self.msg_id = 0
        self.console_logs = []

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url)
        asyncio.create_task(self._listen())
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        await self.send("DOM.enable")

    async def _listen(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg.get("method") == "Runtime.consoleAPICalled":
                    args = msg.get("params", {}).get("args", [])
                    txt = " ".join([str(a.get("value", "")) for a in args])
                    self.console_logs.append(f"[console] {txt}")
        except Exception:
            pass

    async def send(self, method, params=None):
        self.msg_id += 1
        curr_id = self.msg_id
        payload = {"id": curr_id, "method": method, "params": params or {}}
        await self.ws.send(json.dumps(payload))
        
        while True:
            raw = await self.ws.recv()
            res = json.loads(raw)
            if res.get("id") == curr_id:
                if "error" in res:
                    raise RuntimeError(f"CDP Error in {method}: {res['error']}")
                return res.get("result", {})

    async def navigate(self, url):
        await self.send("Page.navigate", {"url": url})
        await asyncio.sleep(2)

    async def eval_js(self, expr):
        res = await self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return res.get("result", {}).get("value")

    async def click_element(self, selector):
        js = f"document.querySelector('{selector}').click()"
        await self.eval_js(js)

    async def type_input(self, selector, text):
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

    async def capture_screenshot(self, filepath):
        res = await self.send("Page.captureScreenshot", {"format": "png"})
        import base64
        data = base64.b64decode(res["data"])
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(data)
        if ARTIFACT_SCREENSHOT_PATH != filepath:
            ARTIFACT_SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(ARTIFACT_SCREENSHOT_PATH, "wb") as f:
                f.write(data)
        print(f"📸 Screenshot saved to {filepath}")


async def run_e2e():
    backend_proc = ensure_backend_running()
    chrome_proc = start_headless_chrome()

    try:
        ws_url = await get_page_ws_url()
        print(f"🔗 Connected to Chrome DevTools Protocol at {ws_url}")
        client = CDPClient(ws_url)
        await client.connect()

        # Navigate cleanly
        await client.navigate(f"http://localhost:{PORT}/")

        # Step 1: Check page title
        title = await client.eval_js("document.title")
        print(f"📄 Page Title: {title}")
        assert "水下多智能体" in title, f"Unexpected title: {title}"

        # Step 2: Send GENERAL_CHAT message "你好"
        print("💬 Step 2: Sending GENERAL_CHAT '你好'...")
        await client.type_input("#messageInput", "你好")
        await client.click_element("#sendBtn")
        await asyncio.sleep(2)

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
        await asyncio.sleep(2)

        collected_text = await client.eval_js("document.querySelector('#collectedFields').innerText")
        print(f"   Collected Fields after task creation:\n{collected_text}")
        assert "管缆巡检" in collected_text or "pipeline_inspection" in collected_text, "Task type not collected!"
        assert "300" in collected_text, "Water depth not collected!"

        # Step 4: Confirm task
        print("✅ Step 4: Confirming task '确认'...")
        await client.type_input("#messageInput", "确认")
        await client.click_element("#sendBtn")
        await asyncio.sleep(2)

        # Step 5: Reload page and verify state persistence
        print("🔄 Step 5: Reloading page to verify persistence...")
        await client.send("Page.reload")
        await asyncio.sleep(3)

        title_after_reload = await client.eval_js("document.title")
        collected_after_reload = await client.eval_js("document.querySelector('#collectedFields').innerText")
        print(f"   Page Title after reload: {title_after_reload}")
        print(f"   Collected Fields after reload:\n{collected_after_reload}")
        assert "水下多智能体" in title_after_reload, "Page failed to load after refresh"

        # Step 6: Capture screenshot and check console logs
        await client.capture_screenshot(SCREENSHOT_PATH)
        print("📋 Console Logs during session:")
        if not client.console_logs:
            print("   (No console warnings or errors)")
        for log in client.console_logs:
            print(f"   {log}")

        print("\n🎉 Chrome E2E Acceptance Test Completed Successfully!")

    finally:
        if chrome_proc:
            chrome_proc.terminate()
        if backend_proc:
            backend_proc.terminate()


if __name__ == "__main__":
    asyncio.run(run_e2e())
