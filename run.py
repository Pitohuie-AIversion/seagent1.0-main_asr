"""
run.py - 应用启动入口（完全离线版本）
"""

import os

from backend_logging import setup_backend_logging

BACKEND_LOG_FILE = setup_backend_logging()


def _ensure_positive_int_env(name: str, default: str) -> None:
    value = os.environ.get(name, "").strip()
    if not value.isdigit() or int(value) <= 0:
        os.environ[name] = default


_ensure_positive_int_env("OMP_NUM_THREADS", "1")
_ensure_positive_int_env("MKL_NUM_THREADS", "1")

import sys
import yaml
from pathlib import Path
import torch
from vllm import LLM
from transformers import AutoTokenizer
from flask import request, jsonify

import web_backend
from web_backend import app

sys.path.insert(0, str(Path(__file__).parent))

from src.llm_client import LLMClient
from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.simulated_time import get_simulated_time
from src.asr_service import ASRConfig, ASRService

# 强制离线
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

LOCAL_MODEL_PATH = "/root/autodl-tmp/model/Qwen3.5-9B"
PORT = 8890

# ====================== 配置路径（与你的代码一致）======================
CONFIG_DIR = Path(__file__).parent / "config"
# ======================================================================

os.system("pkill -f VLLM::EngineCore 2>/dev/null")
os.system(f"fuser -k {PORT}/tcp 2>/dev/null")


def load_asr_service() -> ASRService:
    cfg_path = CONFIG_DIR / "asr.yaml"
    cfg = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    raw_model_path = Path(cfg.get("model_path", "model/Qwen3-ASR-0.6B"))
    if not raw_model_path.is_absolute():
        raw_model_path = Path(__file__).parent / raw_model_path

    asr = ASRService(
        ASRConfig(
            model_path=raw_model_path,
            device=cfg.get("device", "auto"),
            language=cfg.get("language", "Chinese"),
            max_new_tokens=int(cfg.get("max_new_tokens", 256)),
            max_inference_batch_size=int(cfg.get("max_inference_batch_size", 1)),
        )
    )
    asr.load()
    return asr


def startup():
    # 启动模拟计时器（默认使用系统时间）
    sim_time = get_simulated_time()
    sim_time.start()
    print("⏱️ 模拟时间模块已启动，当前时间:", sim_time.get_current_time().strftime("%Y-%m-%d %H:%M:%S"))

    if os.environ.get("OFFLINE_MOCK") == "1":
        print("🛠️ OFFLINE_MOCK 模式开启，跳过 vLLM 和 ASR 模型物理加载！")
        kb = KnowledgeBase()
        llm_client = LLMClient(None, None)
        manager = DialogueManager(llm_client, kb)
        web_backend.init_manager(manager)
        
        asr_service = ASRService(ASRConfig(model_path=Path("mock")))
        asr_service.load()
        web_backend.init_asr_service(asr_service)
        print("✅ Mock models loaded successfully (Dry Run Mode)")
        return

    print("Loading tokenizer...")
    tok = AutoTokenizer.from_pretrained(
        LOCAL_MODEL_PATH,
        trust_remote_code=True,
        local_files_only=True,
    )

    print("Loading vLLM model...")
    llm_engine = LLM(
        model=LOCAL_MODEL_PATH,
        trust_remote_code=True,
        dtype="bfloat16" if torch.cuda.is_bf16_supported() else "float16",
        max_num_seqs=1,
    )

    print("Loading knowledge base...")
    kb = KnowledgeBase()

    llm_client = LLMClient(llm_engine, tok)
    manager = DialogueManager(llm_client, kb)
    web_backend.init_manager(manager)

    #增加asr模块260611
    print("Loading ASR model...")
    asr_service = load_asr_service()
    web_backend.init_asr_service(asr_service)
    print(f"ASR model loaded successfully on {asr_service.device}")

    print("✅ Model loaded successfully")


if __name__ == "__main__":
    startup()
    print(f"🌐 Server running at http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)