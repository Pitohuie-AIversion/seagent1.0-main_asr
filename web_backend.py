"""
web_backend.py - Web 后端主控
支持多会话隔离：每个 session_id 拥有独立的 DialogueManager 实例，
共享只读模型（LLMClient, KnowledgeBase）。
"""

import threading
import uuid
import yaml
import logging
from pathlib import Path
from flask import Flask, request, jsonify, render_template
from datetime import datetime
from zoneinfo import ZoneInfo
import tempfile
from werkzeug.utils import secure_filename

from session import Session
from src.dialogue_manager import DialogueManager
from src.simulated_time import get_simulated_time
from src.history_manager import save_conversation, list_history, load_history
from src.asr_normalizer import normalize_terminology

# ========== 配置路径（与你的项目一致）==========
CONFIG_DIR = Path(__file__).parent / "config"

# ---------- 全局只读资源（所有会话共享）----------
_shared_llm = None       # LLMClient 实例
_shared_kb = None        # KnowledgeBase 实例
_shared_asr = None       # ASRService 实例

# ---------- 会话管理器 ----------
_sessions_manager: dict[str, DialogueManager] = {}
_sessions_lock = threading.Lock()

_sessions = {}           # 兼容原有的 Session 对象（用于前端展示）
_sess_lock = threading.Lock()


def init_manager(dialogue_manager):
    """在启动时由 run.py 调用，注入完整的 DialogueManager 实例，
    并从中提取只读的 llm 和 kb 供所有会话复用。
    """
    global _shared_llm, _shared_kb
    _shared_llm = dialogue_manager.llm
    _shared_kb = dialogue_manager.kb

def init_asr_service(asr_service):
    global _shared_asr
    _shared_asr = asr_service

def get_or_create_manager(sid: str) -> DialogueManager:
    """获取或创建会话专属的 DialogueManager 实例"""
    with _sessions_lock:
        if sid not in _sessions_manager:
            _sessions_manager[sid] = DialogueManager(_shared_llm, _shared_kb)
        return _sessions_manager[sid]


def print_status(manager: DialogueManager):
    """每轮对话后打印结构化任务状态面板"""
    status = manager.get_status()

    phase_labels = {
        "collecting":   "收集中",
        "blocked_hard": "⛔ Hard违规待修复",
        "blocked_soft": "⚠️  Soft警告待确认",
        "confirming":   "待用户确认",
        "done":         "✅ 已完成",
        "rejected":     "❌ 已拒绝",
    }
    phase_str = phase_labels.get(status["phase"], status["phase"])
    mode_str  = "🚨 紧急模式" if status["mode"] == "emergency" else "普通模式"

    print()
    print("┌─ 任务状态 " + "─" * 48)
    print(f"│ 阶段：{phase_str}　模式：{mode_str}")
    print("├─ 已提取字段（规范化结果）" + "─" * 33)

    if status["filled"]:
        for key, info in status["filled"].items():
            val = info["value"]
            if isinstance(val, dict):
                val_str = ", ".join(f"{k}={v}" for k, v in val.items())
            elif isinstance(val, list):
                val_str = " / ".join(str(x) for x in val)
            else:
                val_str = str(val)
            print(f"│  ✓ {info['label']:<18} {val_str}")
    else:
        print("│  （暂无）")

    print("├─ 待补充字段 " + "─" * 45)
    if status["missing"]:
        for m in status["missing"]:
            allowed = m.get("allowed_values", [])
            if allowed:
                print(f"│  ✗ {m['label']:<18} 可选：{allowed}")
            else:
                print(f"│  ✗ {m['label']}")
    else:
        print("│  （无缺失，所有必填字段已收集 ✓）")

    if status["whitelisted_soft"]:
        print("├─ 已忽略的 Soft 警告 " + "─" * 37)
        for cid in status["whitelisted_soft"]:
            print(f"│  ~ [{cid}]")

    print("└" + "─" * 58)
    print()


app = Flask(__name__, template_folder="./")

def _load_asr_api_config() -> dict:
    cfg_path = CONFIG_DIR / "asr.yaml"
    if not cfg_path.exists():
        return {}
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _config_bool(config: dict, key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    return default


_asr_api_config = _load_asr_api_config()
app.config["MAX_CONTENT_LENGTH"] = int(_asr_api_config.get("max_upload_mb", 25)) * 1024 * 1024
_asr_direct_to_llm = _config_bool(_asr_api_config, "direct_to_llm", True)
_allowed_audio_extensions = {
    str(ext).lower().lstrip(".")
    for ext in _asr_api_config.get("allowed_extensions", ["wav", "mp3", "flac", "m4a", "ogg", "webm"])
}


def _is_allowed_audio(filename: str) -> bool:
    suffix = Path(filename).suffix.lower().lstrip(".")
    return bool(suffix and suffix in _allowed_audio_extensions)


# ========== 日志过滤器：屏蔽 /api/time/current 的访问日志 ==========
class EndpointFilter(logging.Filter):
    """过滤包含 '/api/time/current' 的访问日志"""
    def filter(self, record):
        msg = record.getMessage()
        if '/api/time/current' in msg:
            return False
        return True

werkzeug_logger = logging.getLogger('werkzeug')
for f in werkzeug_logger.filters[:]:
    if isinstance(f, EndpointFilter):
        werkzeug_logger.removeFilter(f)
werkzeug_logger.addFilter(EndpointFilter())
# ==================================================================


@app.route("/api/robot/set-state-info", methods=["POST"])
def set_robot_state_info():
    try:
        data = request.get_json()
        robot_name = data.get("robot_name")
        params = data.get("params")

        if not robot_name or not params:
            return jsonify({"code": 400, "msg": "robot_name 和 params 不能为空"}), 400

        print("【更新前的state】")
        print(_shared_kb.state_info.get_robot_state(robot_name))

        _shared_kb.state_info.set_status(robot_name, params)

        updated_state = _shared_kb.state_info.get_robot_state(robot_name)
        print("【更新后的state】")
        print(updated_state)
        print(f"✅ 机器人 {robot_name} 状态已更新，update_timestamp = {updated_state.get('update_timestamp')}")

        return jsonify({
            "code": 200,
            "msg": "✅ 状态更新成功",
            "robot": robot_name,
            "updated_params": params,
            "final_timestamp": updated_state.get("update_timestamp")
        })
    except Exception as e:
        return jsonify({"code": 500, "msg": str(e)}), 500


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/asr", methods=["POST"])
def api_asr():
    if _shared_asr is None:
        return jsonify({"code": 503, "msg": "ASR service is not initialized"}), 503

    audio = request.files.get("audio")
    if audio is None or not audio.filename:
        return jsonify({"code": 400, "msg": "missing audio file"}), 400

    filename = secure_filename(audio.filename)
    if not _is_allowed_audio(filename):
        return jsonify({
            "code": 400,
            "msg": f"unsupported audio format: {Path(filename).suffix}",
            "allowed_extensions": sorted(_allowed_audio_extensions),
        }), 400

    language = (request.form.get("language") or _asr_api_config.get("language") or "Chinese").strip()

    try:
        with tempfile.TemporaryDirectory(prefix="seagent_asr_") as tmpdir:
            audio_path = Path(tmpdir) / filename
            audio.save(audio_path)
            result = _shared_asr.transcribe_file(audio_path, language=language)
            normalization = normalize_terminology(result["text"])

        return jsonify({
            "code": 200,
            "text": result["text"],
            "corrected_text": normalization["corrected_text"],
            "normalization_changed": normalization["normalization_changed"],
            "replacements": normalization["replacements"],
            "warnings": normalization["warnings"],
            "transcript": result["text"],
            "direct_to_llm": _asr_direct_to_llm,
            "language_hint": result["language_hint"],
            "device": result["device"],
            "elapsed_ms": result["elapsed_ms"],
            "segments": result["segments"],
        })
    except Exception as e:
        return jsonify({"code": 500, "msg": str(e)}), 500

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.json or {}
    sid = data.get("session_id") or str(uuid.uuid4())
    msg = data.get("message", "").strip()
    if not msg:
        return jsonify({"error": "empty message"}), 400

    mgr = get_or_create_manager(sid)

    with _sess_lock:
        if sid not in _sessions:
            _sessions[sid] = Session(sid)

    reply = mgr.process(msg)
    print_status(mgr)
    if mgr.phase == "done":
        try:
            save_conversation(
                session_id=sid,
                conversation_history=mgr.conversation_history,
                task_state=mgr.task_state,
                built_json=mgr._last_built_json,
                mode=mgr.mode,
                phase=mgr.phase,
                intent_id=mgr.task_state.get('intent_id'),  
            )
        except Exception as e:
            print(f"保存历史快照失败: {e}")

    return jsonify({
        "session_id": sid,
        "reply": reply,
        "done": mgr.phase == "done",
        "rejected": mgr.phase == "rejected",
        "collected": mgr._last_built_json,
        "missing": [miss["key"] for miss in mgr._last_missing],
        "task_type": mgr.task_state.get("task_type_key"),
        "emergency": mgr.mode == "emergency"
    })


@app.route("/api/reset", methods=["POST"])
def api_reset():
    sid = (request.json or {}).get("session_id")
    if sid:
        with _sessions_lock:
            mgr = _sessions_manager.pop(sid, None)
            if mgr:
                mgr.reset()
        with _sess_lock:
            _sessions.pop(sid, None)
    return jsonify({"ok": True})


@app.route("/api/translate", methods=["POST"])
def api_translate():
    data = request.json or {}
    text = data.get("text", "").strip()
    target_lang = data.get("target_lang", "English").strip()
    if not text:
        return jsonify({"code": 200, "translated_text": ""})

    system_instruction = (
        f"You are a professional translator. Translate the given text into {target_lang}. "
        "Keep all markdown formatting (e.g. tables, lists, bold text, code blocks), HTML tags, emojis, and technical names unchanged. "
        "Do not output any introductory explanations, thoughts, or notes. Output ONLY the translated text."
    )

    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": text}
    ]

    try:
        if _shared_llm is None:
            return jsonify({"code": 503, "msg": "LLM client is not initialized"}), 503
        translated = _shared_llm.chat(messages, temperature=0.1, max_tokens=1500)
        translated = _shared_llm.filter_reply(translated, temperature=0.1, max_tokens=1500)
        return jsonify({"code": 200, "translated_text": translated})
    except Exception as e:
        return jsonify({"code": 500, "msg": str(e)}), 500


# ==============================================================================
# 模拟时间接口（离线环境时间同步）
# ==============================================================================
@app.route("/api/time/current", methods=["GET"])
def get_current_time():
    sim = get_simulated_time()
    current = sim.get_current_time()
    return jsonify({
        "code": 200,
        "current_time": current.isoformat(),
        "timestamp": current.timestamp()
    })


@app.route("/api/time/set", methods=["POST"])
def set_current_time():
    data = request.get_json()
    time_str = data.get("time")
    if not time_str:
        return jsonify({"code": 400, "msg": "缺少 time 字段"}), 400
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        sim = get_simulated_time()
        sim.set_current_time(dt)
        return jsonify({
            "code": 200,
            "msg": "时间设置成功",
            "current_time": sim.get_current_time().isoformat()
        })
    except Exception as e:
        return jsonify({"code": 500, "msg": f"时间格式错误: {str(e)}"}), 500


@app.route("/api/history/list", methods=["GET"])
def api_history_list():
    """返回历史记录列表"""
    try:
        records = list_history()
        return jsonify({"code": 200, "data": records})
    except Exception as e:
        return jsonify({"code": 500, "msg": str(e)}), 500


@app.route("/api/history/load", methods=["POST"])
def api_history_load():
    """加载指定的历史快照，并恢复到当前会话"""
    data = request.get_json()
    history_id = data.get("history_id")
    sid = data.get("session_id")
    if not history_id or not sid:
        return jsonify({"code": 400, "msg": "缺少 history_id 或 session_id"}), 400

    snapshot = load_history(history_id)
    if not snapshot:
        return jsonify({"code": 404, "msg": "历史记录不存在"}), 404

    mgr = get_or_create_manager(sid)
    mgr.load_snapshot(snapshot)

    return jsonify({
        "code": 200,
        "session_id": sid,
        "conversation_history": mgr.conversation_history,
        "built_json": mgr._last_built_json,
        "missing": [miss["key"] for miss in mgr._last_missing],
        "task_type": mgr.task_state.get("task_type_key"),
        "mode": mgr.mode,
        "phase": mgr.phase,
    })