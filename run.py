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
import time
import yaml
from pathlib import Path
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
PORT = int(os.environ.get("PORT", "8890"))

# ====================== 配置路径（与你的代码一致）======================
CONFIG_DIR = Path(__file__).parent / "config"
# ======================================================================

def cleanup_port(port: int) -> None:
    """清理指定 TCP 端口的占用进程（纯 Python /proc fallback 兼容无 fuser 环境）"""
    if os.system(f"fuser -k {port}/tcp 2>/dev/null") == 0:
        return

    hex_port = f"{port:04X}"
    inodes = set()
    for net_file in ["/proc/net/tcp", "/proc/net/tcp6"]:
        if not os.path.exists(net_file):
            continue
        try:
            with open(net_file, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) > 9:
                        local_addr = parts[1]
                        state = parts[3]
                        inode = parts[9]
                        if local_addr.endswith(":" + hex_port) and state == "0A":
                            inodes.add(inode)
        except Exception:
            pass

    if not inodes:
        return

    import glob
    import signal
    current_pid = str(os.getpid())
    for p in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            link = os.readlink(p)
            for inode in inodes:
                if f"socket:[{inode}]" in link:
                    pid_str = p.split("/")[2]
                    if pid_str != current_pid:
                        pid = int(pid_str)
                        print(f"🧹 清理端口 {port} 占用进程 (PID: {pid})...")
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass
        except Exception:
            pass


cleanup_port(PORT)

if not (os.environ.get("OFFLINE_MOCK") == "1" or os.environ.get("SEAGENT_OFFLINE_MOCK") == "1"):
    os.system("pkill -f VLLM::EngineCore 2>/dev/null")



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

    if os.environ.get("OFFLINE_MOCK") == "1" or os.environ.get("SEAGENT_OFFLINE_MOCK") == "1":
        print("🛠️ OFFLINE_MOCK 模式开启，跳过 vLLM 和 ASR 模型物理加载！")
        kb = KnowledgeBase()
        llm_client = LLMClient(None, None)
        manager = DialogueManager(llm_client, kb)
        web_backend.init_manager(manager)
        
        asr_service = ASRService(ASRConfig(model_path=Path("mock")))
        asr_service.load()
        web_backend.init_asr_service(asr_service)

        # 启动 MCP 桥接服务 (OFFLINE_MOCK 模式自动连通 Mock 9091)
        _init_mcp_service_if_requested(kb, is_mock=True)
        print("✅ Mock models loaded successfully (Dry Run Mode)")
        return

    # 延迟导入：仅在全量启动时引入 vllm/torch，避免 mock 模式崩溃
    import torch
    from vllm import LLM
    from transformers import AutoTokenizer

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
    try:
        asr_service = load_asr_service()
        web_backend.init_asr_service(asr_service)
        if asr_service.is_degraded:
            print("⚠️ ASR model unavailable; ASR requests will fail closed")
        else:
            print(f"ASR model loaded successfully on {asr_service.device}")
    except Exception as exc:
        print(f"⚠️ ASR initialization failed ({exc}); ASR requests will return unavailable")
        web_backend.init_asr_service(None)

    _init_mcp_service_if_requested(kb, is_mock=False)
    print("✅ Model loaded successfully")


_mock_rosbridge_srv = None


def _init_mcp_service_if_requested(kb, is_mock: bool = False):
    """初始化 SEAgent MCP 桥接服务"""
    global _mock_rosbridge_srv
    enable_mcp = os.environ.get("ENABLE_MCP") == "1" or "--mcp" in sys.argv or is_mock
    if not enable_mcp:
        return

    try:
        mcp_dir = str(Path(__file__).parent / "mcp")
        if mcp_dir not in sys.path:
            sys.path.insert(0, mcp_dir)

        mcp_host = os.environ.get("MCP_HOST", "127.0.0.1")
        mcp_port = int(os.environ.get("MCP_PORT", "9091" if is_mock else "9090"))

        if is_mock and _mock_rosbridge_srv is None:
            from mock_rosbridge_server import MockRosbridgeServer
            _mock_rosbridge_srv = MockRosbridgeServer(port=mcp_port)
            _mock_rosbridge_srv.start()
            time.sleep(0.3)
            print(f"🛠️ 本地 Mock rosbridge 仿真服务器已启动 (ws://127.0.0.1:{mcp_port})")

        from bridge_service import SEAgentMCPBridgeService
        mcp_bridge = SEAgentMCPBridgeService(
            host=mcp_host,
            port=mcp_port,
            state_info=getattr(kb, "state_info", None),
            connect_timeout=3.0,
        )
        mcp_bridge.start()
        web_backend.init_mcp_bridge_service(mcp_bridge)
        print(f"📡 MCP 桥接服务启动成功 (ws://{mcp_host}:{mcp_port})")
    except Exception as exc:
        print(f"⚠️ MCP 桥接服务初始化跳过: {exc}")


if __name__ == "__main__":
    startup()
    print(f"🌐 Server running at http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
