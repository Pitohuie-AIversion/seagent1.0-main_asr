---
name: chrome_automation
description: Procedures and code patterns for running headless Google Chrome, Selenium, Playwright, and web scraping scripts under root user environment on the server.
---

# Chrome Automation & Headless Operations

本 Skill 记录在服务器 `root` 权限环境下高效、稳定运行 Google Chrome、Selenium、Playwright 以及网页抓取脚本的标准规范与代码范例。

---

## 1. 系统配置与二进制路径
- **安装路径**: `/usr/bin/google-chrome` (链接至 `/opt/google/chrome/google-chrome`)
- **Root 沙箱说明**: 在 Linux 的 `root` 用户下运行 Chrome 时必须包含 `--no-sandbox`。系统启动包装脚本 `/opt/google/chrome/google-chrome` 已完成配置，在 `root` 用户下执行时会自动追加 `--no-sandbox`。

---

## 2. 命令行（CLI）直接调用
可直接在 Shell 命令或子进程中调用无头模式：
```bash
# 获取网页 DOM 源码
google-chrome --headless --disable-gpu --dump-dom https://example.com

# 网页截图导出
google-chrome --headless --disable-gpu --screenshot=/path/to/screenshot.png https://example.com
```

---

## 3. Python 自动化代码范例

### 3.1 Selenium (Python)
使用 `selenium` 驱动系统 Chrome：
```python
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

def get_driver():
    options = Options()
    options.binary_location = "/usr/bin/google-chrome"
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=options)
    return driver

if __name__ == "__main__":
    driver = get_driver()
    driver.get("https://example.com")
    print("Page Title:", driver.title)
    driver.quit()
```

### 3.2 Playwright (Python)
使用 Playwright 指定系统 Chrome 可执行文件：
```python
import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()
        await page.goto("https://example.com")
        print("Page Title:", await page.title())
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
```

### 3.3 Subprocess 标准子进程模式
```python
import subprocess

def fetch_page_html(url: str) -> str:
    cmd = [
        "google-chrome",
        "--headless",
        "--disable-gpu",
        "--dump-dom",
        url
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout
```
