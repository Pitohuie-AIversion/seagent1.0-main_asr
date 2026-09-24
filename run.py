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
from src.dispatch.result_paths import get_result_dir
from src.temporal.simulated_time import get_simulated_time
from src.asr.asr_service import ASRConfig, ASRService

# 强制离线
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

LOCAL_MODEL_PATH = (
    os.environ.get("LOCAL_MODEL_PATH")
    or os.environ.get("SEAGENT_MODEL_DIR")
    or "/root/autodl-tmp/model/Qwen3.5-9B"
)
PORT = int(os.environ.get("PORT", "8890"))

# ====================== 配置路径（与你的代码一致）======================
CONFIG_DIR = Path(__file__).parent / "config"
# ======================================================================

import socket


def check_port_available(port: int, host: str = "0.0.0.0") -> None:
    """检查目标 TCP 端口是否可用。

    安全原则（P1-3）：
    端口被占用时默认报错退出（Fail-closed），严禁自动全局扫描 /proc 发送 SIGKILL 或
    调用 pkill 杀掉外部进程，防止多实例或共享 GPU 主机上误杀其他服务。
    """
    target_host = "127.0.0.1" if host in ("0.0.0.0", "") else host

    # 1. 尝试建立连接（检测是否有活跃服务正监听该端口）
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn_sock:
        conn_sock.settimeout(0.5)
        if conn_sock.connect_ex((target_host, port)) == 0:
            raise RuntimeError(
                f"TCP 端口 {port} 已被占用，启动中止！\n"
                f"为保障共享 GPU 环境与其他工作区实例安全，禁止自动终止外部进程。\n"
                f"请手动关闭占用该端口的服务，或通过指定 PORT 环境变量（如 PORT=8891）更换端口。"
            )

    # 2. 尝试实际独占绑定（不设 SO_REUSEADDR 以确保强独占性）
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as bind_sock:
        try:
            bind_sock.bind((host, port))
        except OSError as exc:
            raise RuntimeError(
                f"TCP 端口 {port} 无法绑定（已被占用或处于占用状态），启动中止！\n"
                f"为保障共享 GPU 环境与其他工作区实例安全，禁止自动终止外部进程。\n"
                f"请手动关闭占用该端口的服务，或通过指定 PORT 环境变量（如 PORT=8891）更换端口。"
            ) from exc






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
    vllm_gpu_util = float(os.getenv("VLLM_GPU_MEMORY_UTILIZATION", "0.80"))
    vllm_max_seqs = int(os.getenv("VLLM_MAX_NUM_SEQS", "64"))
    vllm_max_model_len = int(os.getenv("VLLM_MAX_MODEL_LEN", "16384"))
    llm_engine = LLM(
        model=LOCAL_MODEL_PATH,
        trust_remote_code=True,
        dtype="bfloat16" if torch.cuda.is_bf16_supported() else "float16",
        gpu_memory_utilization=vllm_gpu_util,
        max_num_seqs=vllm_max_seqs,
        max_model_len=vllm_max_model_len,
        enable_prefix_caching=True,
    )

    print("Loading knowledge base...")
    kb = KnowledgeBase()

    llm_client = LLMClient(llm_engine, tok)
    manager = DialogueManager(llm_client, kb)
    web_backend.init_manager(manager)

    # 预热后端核心 JSON Schema 与基本推理
    _warmup_llm_client(llm_client)

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


def _warmup_llm_client(llm_client: LLMClient) -> None:
    """在对外提供 HTTP 服务之前，预热 JSON Schema FSM 编译与核心 Token 缓存"""
    if llm_client.is_mock:
        return
    print("🔥 预热 LLM 引擎...")
    start_t = time.time()
    try:
        warmup_msgs = [{"role": "user", "content": "你好"}]
        llm_client.generate_text(warmup_msgs, max_tokens=32)
        elapsed = time.time() - start_t
        print(f"⚡ LLM 预热完成，耗时 {elapsed:.2f} 秒")
    except Exception as exc:
        print(f"⚠️ LLM 预热跳过: {exc}")


_mock_rosbridge_srv = None


def _init_mcp_service_if_requested(kb, is_mock: bool = False):
    """初始化 SEAgent MCP 桥接服务"""
    global _mock_rosbridge_srv
    enable_mcp = os.environ.get("ENABLE_MCP") != "0" and ("--mcp" in sys.argv or is_mock)
    if not enable_mcp:
        return

    try:
        mcp_dir = str(Path(__file__).parent / "mcp")
        mcp_core_dir = str(Path(__file__).parent / "mcp" / "core")
        mcp_mock_dir = str(Path(__file__).parent / "mcp" / "mock")
        for p in [mcp_dir, mcp_core_dir, mcp_mock_dir]:
            if p not in sys.path:
                sys.path.insert(0, p)

        runtime_file = Path(__file__).parent / "config" / "ros2_runtime.yaml"
        protocol_file = Path(__file__).parent / "config" / "ros2_protocol_spec.yaml"
        from mcp.shim.runtime_config import load_ros2_runtime_config

        runtime_config = load_ros2_runtime_config(runtime_file, protocol_file)
        gw_host = runtime_config.gateway.host
        gw_port = runtime_config.gateway.port

        mcp_host = os.environ.get("MCP_HOST", gw_host)
        mcp_port = int(os.environ.get("MCP_PORT", str(gw_port)))
        os.environ.setdefault(
            "SEAGENT_ROS2_ID_DIR", str(get_result_dir(create=True))
        )

        start_embedded_mock = (
            is_mock
            and os.environ.get("MCP_EMBEDDED_MOCK", "1") != "0"
        )
        if start_embedded_mock and _mock_rosbridge_srv is None:
            try:
                from mcp.shim.mock_rosbridge_server import MockRosbridgeServer
            except ImportError:
                from mcp.mock.mock_rosbridge_server import MockRosbridgeServer
            _mock_rosbridge_srv = MockRosbridgeServer(port=mcp_port)
            _mock_rosbridge_srv.start()
            time.sleep(0.3)
            print(f"🛠️ 本地 Mock rosbridge 仿真服务器已启动 (ws://127.0.0.1:{mcp_port})")

        try:
            from mcp.shim.bridge_service import SEAgentMCPBridgeService
        except ImportError:
            from mcp.core.bridge_service import SEAgentMCPBridgeService
        mcp_bridge = SEAgentMCPBridgeService(
            host=mcp_host,
            port=mcp_port,
            state_info=getattr(kb, "state_info", None),
            connect_timeout=3.0,
            runtime_config_path=runtime_file,
            protocol_config_path=protocol_file,
        )
        # Keep a validated offline bridge available to the dashboard so operators
        # can reconnect or change gateways even when the first connection fails.
        web_backend.init_mcp_bridge_service(mcp_bridge)
        try:
            mcp_bridge.start()
        except Exception as exc:
            print(f"⚠️ MCP 网关暂不可达，桥接服务已保留，可在监控页重新连接: {exc}")
            return
        print(f"📡 MCP 桥接服务启动成功 (ws://{mcp_host}:{mcp_port})")
    except Exception as exc:
        print(f"⚠️ MCP 桥接服务初始化跳过: {exc}")


if __name__ == "__main__":
    check_port_available(PORT)
    startup()
    print(f"🌐 Server running at http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
