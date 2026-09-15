#!/usr/bin/env python3
"""
download_qwen38_models.py
从 ModelScope 极速下载 Qwen3.8-27B-FP8 和 Qwen3.8-27B 原版权重到数据盘 /root/autodl-tmp/model/
"""

import os
import sys
import time
from pathlib import Path

# 确保清除任何阻断国内直连的代理变量
for var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
    if var in os.environ:
        del os.environ[var]

from modelscope import snapshot_download

# 目标模型存放根路径：优先支持环境变量 SEAGENT_MODEL_DIR，次级回退到 AutoDL 数据盘或工程本地 models/
custom_target = os.environ.get("SEAGENT_MODEL_DIR")
if custom_target:
    TARGET_BASE = Path(custom_target)
elif Path("/root/autodl-tmp").exists():
    TARGET_BASE = Path("/root/autodl-tmp/model")
else:
    TARGET_BASE = Path(__file__).resolve().parent.parent / "models"

try:
    TARGET_BASE.mkdir(parents=True, exist_ok=True)
except OSError as err:
    print(f"⚠️ 无法创建目标目录 {TARGET_BASE}: {err}，回退到工程本地 models/")
    TARGET_BASE = Path(__file__).resolve().parent.parent / "models"
    TARGET_BASE.mkdir(parents=True, exist_ok=True)

DOWNLOAD_TASKS = [
    {
        "name": "Qwen3.8-27B-FP8 (约 27GB，单卡/双卡极速版)",
        "model_id": "Qwen/Qwen3.8-27B-FP8",
        "local_dir": str(TARGET_BASE / "Qwen3.8-27B-FP8"),
    },
    {
        "name": "Qwen3.8-27B (约 54GB，BF16 满血版)",
        "model_id": "Qwen/Qwen3.8-27B",
        "local_dir": str(TARGET_BASE / "Qwen3.8-27B"),
    },
]

def download_model(task: dict) -> None:
    name = task["name"]
    mid = task["model_id"]
    dest = task["local_dir"]
    print(f"\n{'='*60}")
    print(f"🚀 开始下载: {name}")
    print(f"📦 模型 ID: {mid}")
    print(f"📂 目标路径: {dest}")
    print(f"{'='*60}\n", flush=True)
    
    t0 = time.time()
    try:
        path = snapshot_download(
            model_id=mid,
            local_dir=dest,
            max_workers=8,
        )
        elapsed = time.time() - t0
        print(f"\n✅ 下载完成: {name}")
        print(f"⏱️ 耗时: {elapsed/60:.2f} 分钟 ({elapsed:.1f} 秒)")
        print(f"📁 路径: {path}\n", flush=True)
    except Exception as exc:
        print(f"\n❌ 下载失败: {name} - 错误: {exc}\n", file=sys.stderr, flush=True)
        raise

def main():
    print("🎯 SEAgent 模型批量下载任务启动", flush=True)
    print(f"💾 数据盘空间挂载: {TARGET_BASE}", flush=True)
    for i, task in enumerate(DOWNLOAD_TASKS, 1):
        print(f"\n[{i}/{len(DOWNLOAD_TASKS)}] 处理任务: {task['name']}", flush=True)
        download_model(task)
    print("\n🎉 全部模型下载任务执行完毕！", flush=True)

if __name__ == "__main__":
    main()
