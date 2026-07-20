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
import json

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

_translation_cache = {}
_translation_cache_lock = threading.Lock()
_translation_cache_file = CONFIG_DIR / "translation_cache.json"

def _load_translation_cache():
    global _translation_cache
    if _translation_cache_file.exists():
        try:
            with open(_translation_cache_file, "r", encoding="utf-8") as f:
                _translation_cache = json.load(f) or {}
        except Exception as e:
            logging.error(f"Failed to load translation cache: {e}")
            _translation_cache = {}

def _save_translation_cache():
    try:
        _translation_cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(_translation_cache_file, "w", encoding="utf-8") as f:
            json.dump(_translation_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Failed to save translation cache: {e}")

_load_translation_cache()

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
    req_id = f"req_{uuid.uuid4().hex[:8]}"
    if _shared_asr is None:
        return jsonify({
            "ok": False,
            "code": 503,
            "error": "service_unavailable",
            "msg": "ASR service is not initialized",
            "request_id": req_id,
            "retryable": True
        }), 503

    audio = request.files.get("audio")
    if audio is None or not audio.filename:
        return jsonify({
            "ok": False,
            "code": 400,
            "error": "missing_file",
            "msg": "missing audio file (expected form field: 'audio')",
            "request_id": req_id,
            "retryable": False
        }), 400

    filename = secure_filename(audio.filename)
    if not _is_allowed_audio(filename):
        return jsonify({
            "ok": False,
            "code": 400,
            "error": "unsupported_format",
            "msg": f"unsupported audio format: {Path(filename).suffix}",
            "allowed_extensions": sorted(_allowed_audio_extensions),
            "request_id": req_id,
            "retryable": False
        }), 400

    language = (request.form.get("language") or _asr_api_config.get("language") or "Chinese").strip()

    try:
        with tempfile.TemporaryDirectory(prefix="seagent_asr_") as tmpdir:
            audio_path = Path(tmpdir) / filename
            audio.save(audio_path)
            result = _shared_asr.transcribe_file(audio_path, language=language)
            
            raw_text = result["text"]
            if language.lower() == "english" and raw_text.strip():
                try:
                    translated_text = _translate_text_internal(raw_text, "Chinese")
                except Exception as translate_err:
                    logging.error(f"Failed to translate English ASR to Chinese: {translate_err}")
                    translated_text = raw_text
            else:
                translated_text = raw_text

            normalization = normalize_terminology(translated_text)

        return jsonify({
            "code": 200,
            "text": result["text"],
            "corrected_text": normalization["corrected_text"],
            "normalization_changed": normalization["normalization_changed"] or (translated_text != raw_text),
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
        logging.error(f"ASR processing exception: {e}", exc_info=True)
        return jsonify({
            "ok": False,
            "code": 500,
            "error": "ASRProcessingError",
            "msg": "语音识别服务异常，请稍后重试。",
            "request_id": req_id,
            "retryable": True
        }), 500

from src.slot_store import SlotVersionConflict
from src.exceptions import TaskPersistenceError, TaskRollbackError, IntentIdConflict, IdReservationError

@app.route("/api/chat", methods=["POST"])
def api_chat():
    try:
        data = request.json or {}
        sid = data.get("session_id") or str(uuid.uuid4())
        request_id = data.get("request_id") or f"req_{uuid.uuid4().hex[:8]}"
        msg = data.get("message", "").strip()
        if not msg:
            return jsonify({
                "ok": False,
                "code": 400,
                "error": "EmptyMessage",
                "msg": "消息内容不能为空。",
                "request_id": request_id,
                "retryable": False
            }), 400

        mgr = get_or_create_manager(sid)

        with _sess_lock:
            if sid not in _sessions:
                _sessions[sid] = Session(sid)

        reply = mgr.process(msg, request_id=request_id)
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
                    slot_store=mgr.slot_store.export_snapshot(),
                )
            except Exception as e:
                logging.error(f"保存历史快照失败: {e}", exc_info=True)

        resp_data = {
            "code": 200,
            "session_id": sid,
            "request_id": request_id,
            "reply": reply,
            "done": mgr.phase == "done",
            "rejected": mgr.phase == "rejected",
            "collected": mgr._last_built_json,
            "missing": [miss["key"] if isinstance(miss, dict) else str(miss) for miss in mgr._last_missing],
            "task_type": mgr.task_state.get("task_type_key"),
            "emergency": mgr.mode == "emergency",
            "final_json": mgr._last_built_json if mgr.phase == "done" else None
        }
        for k, v in resp_data.items():
            try:
                json.dumps(v)
            except Exception as e:
                raise TypeError(f"Field '{k}' is not JSON serializable: {type(v)} -> {v}") from e

        return jsonify(resp_data)
    except SlotVersionConflict as svc:
        logging.error(f"Slot version conflict in /api/chat: {svc}", exc_info=True)
        return jsonify({
            "ok": False,
            "code": 409,
            "error": "SlotVersionConflict",
            "msg": f"并发版本冲突: {str(svc)}",
            "request_id": request_id if 'request_id' in locals() else "req_unknown",
            "retryable": True
        }), 409
    except IntentIdConflict as iic:
        logging.error(f"Intent ID conflict in /api/chat: {iic}", exc_info=True)
        return jsonify({
            "ok": False,
            "code": 409,
            "error": "IntentIdConflict",
            "msg": "Intent ID 存在冲突，未覆盖已有任务文件。",
            "request_id": request_id if 'request_id' in locals() else "req_unknown",
            "retryable": True
        }), 409
    except (TaskPersistenceError, IdReservationError, TaskRollbackError) as tpe:
        logging.error(f"Task persistence error in /api/chat: {tpe}", exc_info=True)
        return jsonify({
            "ok": False,
            "code": 500,
            "error": type(tpe).__name__,
            "msg": "任务文件保存失败，任务未能成功下发。",
            "request_id": request_id if 'request_id' in locals() else "req_unknown",
            "retryable": True
        }), 500
    except ValueError as ve:
        logging.error(f"Validation error in /api/chat: {ve}", exc_info=True)
        return jsonify({
            "ok": False,
            "code": 400,
            "error": "ValidationError",
            "msg": f"槽位校验失败: {str(ve)}",
            "request_id": request_id if 'request_id' in locals() else "req_unknown",
            "retryable": False
        }), 400
    except Exception as exc:
        logging.error(f"Unhandled exception in /api/chat: {exc}", exc_info=True)
        return jsonify({
            "ok": False,
            "code": 500,
            "error": "InternalServerError",
            "msg": "服务器内部错误，请稍后重试。",
            "request_id": request_id if 'request_id' in locals() else "req_unknown",
            "retryable": True
        }), 500


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


import re as _re_module

_CJK_RE = _re_module.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

# 翻译缓存开关：True = 启用缓存（节省推理时间），False = 全部走 LLM（保证翻译正确性）
TRANSLATION_USE_CACHE = False

# 单次翻译最大输入字符数（超出则分段）
TRANSLATION_CHUNK_SIZE = 2000

# 输入长度硬上限（超出直接拒绝，防止 OOM）
TRANSLATION_MAX_INPUT_CHARS = 20000

# 翻译推理 max_tokens（足够覆盖长段落）
TRANSLATION_MAX_TOKENS = 4096

# 翻译系统提示词
_TRANSLATE_SYSTEM_PROMPT = (
    "You are a professional translator specializing in subsea engineering and oilfield operations. "
    "Translate the given text into {target_lang}. "
    "Rules: "
    "1. Keep ALL markdown formatting (tables, lists, bold, code blocks, headers) exactly as-is. "
    "2. Keep HTML tags, emojis, and technical identifiers (e.g. sealien_work_class, PL-003, A03) unchanged. "
    "3. Do NOT add any explanations, notes, or preamble. "
    "4. Output ONLY the translated text, nothing else. "
    "5. If the input is already in {target_lang}, output it unchanged."
)


def _is_dirty_translation(target_lang: str, translated: str) -> bool:
    """
    检测翻译结果是否为脏数据（与目标语言不符）。
    与前端 isDirtyTranslation() 逻辑保持一致。
    """
    if not translated or not translated.strip():
        return True
    t = translated.strip()
    # JSON / 列表格式不应作为翻译结果
    if t.startswith("{") or t.startswith("["):
        return True
    # English 目标但结果含中文字符
    if target_lang == "English" and _CJK_RE.search(translated):
        return True
    return False


def _validate_translation_quality(
    original: str, translated: str, target_lang: str
) -> tuple[bool, str]:
    """
    校验翻译结果质量。
    返回 (is_valid, reason)。

    注意：中文字符信息密度约为英文的 2-4 倍，因此中英互译后长度差异较大是正常现象。
    例如：英文 50 字符 → 中文约 15-20 字符（ratio ~0.3~0.4）。
    下限设置为 0.08，上限设置为 6.0，仅过滤极端异常情况。
    """
    if _is_dirty_translation(target_lang, translated):
        return False, "dirty_content"
    # 翻译结果长度比例校验（容忍中英文字符密度差异）
    orig_len = len(original)
    tran_len = len(translated)
    if orig_len > 100:  # 仅对较长文本做比例检查
        ratio = tran_len / orig_len if orig_len > 0 else 0
        if ratio < 0.08 or ratio > 6.0:
            return False, f"length_ratio_abnormal({ratio:.2f})"
    return True, "ok"


def _split_into_chunks(text: str, chunk_size: int) -> list[str]:
    """
    按段落分割文本，尽量保持段落完整性。
    优先按双换行（段落边界）分割，不超过 chunk_size 字符。
    """
    # 先按双换行分段
    paragraphs = _re_module.split(r"\n\n+", text)
    chunks = []
    current = []
    current_len = 0
    for para in paragraphs:
        para_len = len(para)
        if current_len + para_len > chunk_size and current:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len + 2  # +2 for \n\n
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _translate_single_chunk(text: str, target_lang: str) -> str:
    """翻译单个文本块，不做缓存，直接走 LLM。"""
    system_instruction = _TRANSLATE_SYSTEM_PROMPT.format(target_lang=target_lang)
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": text},
    ]
    return _shared_llm.chat(messages, temperature=0.1, max_tokens=TRANSLATION_MAX_TOKENS)


def _translate_text_internal(text: str, target_lang: str) -> str:
    """
    核心翻译函数。
    - TRANSLATION_USE_CACHE=False 时全量走 LLM（当前模式，保证正确性）。
    - 超过 TRANSLATION_CHUNK_SIZE 字符时分段翻译后合并。
    - 翻译结果经质量校验；校验失败时返回原文并记录 warning。
    """
    text = text.strip()
    if not text:
        return ""

    # 输入长度硬限制
    if len(text) > TRANSLATION_MAX_INPUT_CHARS:
        logging.warning(
            f"[translate] Input too long ({len(text)} chars > {TRANSLATION_MAX_INPUT_CHARS}), "
            "truncating to limit."
        )
        text = text[:TRANSLATION_MAX_INPUT_CHARS]

    # 缓存读取（仅在 TRANSLATION_USE_CACHE=True 时启用）
    if TRANSLATION_USE_CACHE:
        with _translation_cache_lock:
            cached = (_translation_cache.get(target_lang) or {}).get(text)
            if cached is not None:
                if not _is_dirty_translation(target_lang, cached):
                    return cached
                logging.warning(
                    f"[translate] Dirty cache entry removed for lang={target_lang}, "
                    f"key='{text[:40]}...'"
                )
                del _translation_cache[target_lang][text]
                _save_translation_cache()

    if _shared_llm is None:
        raise RuntimeError("LLM client is not initialized")

    # 分段翻译（长文本）
    if len(text) > TRANSLATION_CHUNK_SIZE:
        chunks = _split_into_chunks(text, TRANSLATION_CHUNK_SIZE)
        logging.info(
            f"[translate] Long text ({len(text)} chars) split into {len(chunks)} chunks."
        )
        translated_chunks = []
        for i, chunk in enumerate(chunks):
            chunk_result = _translate_single_chunk(chunk, target_lang)
            valid, reason = _validate_translation_quality(chunk, chunk_result, target_lang)
            if not valid:
                logging.warning(
                    f"[translate] Chunk {i+1}/{len(chunks)} quality check failed: {reason}. "
                    "Falling back to original chunk."
                )
                translated_chunks.append(chunk)  # 原文回退
            else:
                translated_chunks.append(chunk_result)
        translated = "\n\n".join(translated_chunks)
    else:
        translated = _translate_single_chunk(text, target_lang)

    # 整体翻译质量校验
    valid, reason = _validate_translation_quality(text, translated, target_lang)
    if not valid:
        logging.error(
            f"[translate] Translation quality check failed: {reason}. "
            f"lang={target_lang}, input='{text[:60]}...'"
        )
        # 返回原文（安全回退），不缓存
        return text

    # 写入缓存（仅在缓存模式下）
    if TRANSLATION_USE_CACHE:
        with _translation_cache_lock:
            if target_lang not in _translation_cache:
                _translation_cache[target_lang] = {}
            _translation_cache[target_lang][text] = translated
            _save_translation_cache()

    return translated


@app.route("/api/translate", methods=["POST"])
def api_translate():
    req_id = f"req_{uuid.uuid4().hex[:8]}"
    data = request.json or {}
    text = data.get("text", "").strip()

    if "target_lang" not in data or data.get("target_lang") is None:
        return jsonify({
            "ok": False,
            "code": 400,
            "error": "missing_parameter",
            "msg": "Missing required parameter: target_lang",
            "request_id": req_id,
            "retryable": False
        }), 400

    target_lang = str(data.get("target_lang", "")).strip()

    if not text:
        return jsonify({"code": 200, "translated_text": ""})

    # 校验 target_lang
    allowed_langs = {"English", "Chinese"}
    if target_lang not in allowed_langs:
        return jsonify({
            "ok": False,
            "code": 400,
            "error": "unsupported_language",
            "msg": f"Unsupported target_lang: {target_lang}. Allowed: {sorted(allowed_langs)}",
            "request_id": req_id,
            "retryable": False
        }), 400

    try:
        original_text = text
        translated = _translate_text_internal(text, target_lang)

        # 检测是否发生了原文回退（质量校验失败时 translated == original）
        quality_warning = None
        if translated == original_text and _is_dirty_translation(target_lang, original_text) is False:
            # 正常情况（原文本身就是目标语言）不报 warning
            pass
        elif translated == original_text and target_lang == "English" and _CJK_RE.search(original_text):
            quality_warning = "fallback_to_original"

        resp = {"code": 200, "translated_text": translated}
        if quality_warning:
            resp["quality_warning"] = quality_warning
        return jsonify(resp)

    except RuntimeError as re_err:
        return jsonify({
            "ok": False,
            "code": 503,
            "error": "model_error",
            "msg": str(re_err),
            "request_id": req_id,
            "retryable": True
        }), 503
    except Exception as e:
        logging.exception("[translate] Unexpected error in api_translate")
        return jsonify({
            "ok": False,
            "code": 500,
            "error": "internal_error",
            "msg": "Internal server error during translation",
            "request_id": req_id,
            "retryable": True
        }), 500


# ==============================================================================
# 翻译缓存管理接口
# ==============================================================================
@app.route("/api/admin/translate-cache/reload", methods=["POST"])
def admin_reload_translate_cache():
    """从磁盘重载翻译缓存到内存，无需重启服务。"""
    global _translation_cache
    try:
        with _translation_cache_lock:
            if _translation_cache_file.exists():
                with open(_translation_cache_file, "r", encoding="utf-8") as f:
                    _translation_cache = json.load(f) or {}
            else:
                _translation_cache = {}
        total = sum(len(v) for v in _translation_cache.values())
        logging.info(f"[admin] Translation cache reloaded from disk: {total} entries")
        return jsonify({"code": 200, "msg": f"Cache reloaded: {total} entries"})
    except Exception as e:
        logging.error(f"[admin] Failed to reload translation cache: {e}")
        return jsonify({"code": 500, "msg": str(e)}), 500


from src.slot_store import SlotVersionConflict, SnapshotValidationError

@app.route("/api/history/load", methods=["POST"])
def api_history_load():
    """加载指定的历史快照，并恢复到当前会话"""
    data = request.get_json() or {}
    history_id = data.get("history_id")
    sid = data.get("session_id")
    request_id = data.get("request_id") or f"req_{uuid.uuid4().hex[:8]}"
    if not history_id or not sid:
        return jsonify({"code": 400, "error": "MissingField", "msg": "缺少 history_id 或 session_id", "request_id": request_id}), 400

    snapshot = load_history(history_id)
    if not snapshot:
        return jsonify({"code": 404, "error": "NotFound", "msg": "历史记录不存在", "request_id": request_id}), 404

    mgr = get_or_create_manager(sid)
    try:
        mgr.load_snapshot(snapshot)
    except SnapshotValidationError as sve:
        logging.error(f"Snapshot validation error in /api/history/load: {sve}", exc_info=True)
        return jsonify({
            "code": 400,
            "error": "SnapshotValidationError",
            "msg": f"快照结构非法: {str(sve)}",
            "request_id": request_id
        }), 400
    except Exception as exc:
        logging.error(f"Failed loading snapshot in /api/history/load: {exc}", exc_info=True)
        return jsonify({
            "code": 500,
            "error": "InternalServerError",
            "msg": "服务器内部错误，请稍后重试。",
            "request_id": request_id
        }), 500

    return jsonify({
        "code": 200,
        "session_id": sid,
        "request_id": request_id,
        "conversation_history": mgr.conversation_history,
        "built_json": mgr._last_built_json,
        "missing": [miss["key"] for miss in mgr._last_missing],
        "task_type": mgr.task_state.get("task_type_key"),
        "mode": mgr.mode,
        "phase": mgr.phase
    })


@app.route("/api/admin/translate-cache/stats", methods=["GET"])
def admin_translate_cache_stats():
    """返回当前内存中翻译缓存的统计信息。"""
    with _translation_cache_lock:
        stats = {lang: len(entries) for lang, entries in _translation_cache.items()}
        total = sum(stats.values())
    return jsonify({"code": 200, "total": total, "by_lang": stats})


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

