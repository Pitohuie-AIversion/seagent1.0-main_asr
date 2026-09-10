"""
src/web/routes_asr.py - ASR 语音上传解析、格式白名单过滤与纠错流端点
"""

import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from flask import Blueprint, jsonify, request
from werkzeug.utils import secure_filename
import yaml

import src.web.state as state
from src.asr_normalizer import normalize_terminology
from src.asr_service import ASRUnavailableError
from src.web.routes_translate import _translate_text_internal
from src.web.state import CONFIG_DIR, _require_api_token

logger = logging.getLogger(__name__)

asr_bp = Blueprint("asr", __name__)


def _load_asr_api_config() -> dict:
    cfg_path = CONFIG_DIR / "asr.yaml"
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                return data.get("api", {}) or {}
    except Exception:
        pass
    return {}


def _config_bool(config: dict, key: str, default: bool) -> bool:
    val = config.get(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val)


_asr_api_config = _load_asr_api_config()
_asr_direct_to_llm = _config_bool(_asr_api_config, "direct_to_llm", True)
_allowed_audio_extensions = {
    str(ext).lower().lstrip(".")
    for ext in _asr_api_config.get("allowed_extensions", ["wav", "mp3", "flac", "m4a", "ogg", "webm"])
}


def _is_allowed_audio(filename: str) -> bool:
    suffix = Path(filename).suffix.lower().lstrip(".")
    return bool(suffix and suffix in _allowed_audio_extensions)


@asr_bp.route("/api/asr", methods=["POST"])
@_require_api_token
def api_asr():
    req_id = f"req_{uuid.uuid4().hex[:8]}"
    if state._shared_asr is None:
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

    original_filename = audio.filename
    filename = secure_filename(original_filename)
    content_length = request.content_length
    audit_ip = request.remote_addr or "UNKNOWN"
    audit_ua = (request.headers.get('User-Agent') or '')[:200]
    audit_ts = datetime.now(timezone.utc).isoformat()
    audit_size = content_length if content_length is not None else -1
    logger.info(
        "[SECURITY_ASR_AUDIT] sanitization orig_filename=%r safe_filename=%r size_bytes=%s remote_ip=%s user_agent=%r utc_time=%s request_id=%s",
        original_filename, filename, audit_size, audit_ip, audit_ua, audit_ts, req_id,
    )
    if filename != original_filename:
        logger.warning(
            "[SECURITY_ASR_SANITIZED] Filename was changed by secure_filename(). orig=%r safe=%r ip=%s ua=%r time=%s request_id=%s",
            original_filename, filename, audit_ip, audit_ua[:100], audit_ts, req_id,
        )
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
            result = state._shared_asr.transcribe_file(audio_path, language=language)

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
    except ASRUnavailableError as e:
        logging.error("ASR service unavailable: %s", e)
        return jsonify({
            "ok": False,
            "code": 503,
            "error": "service_unavailable",
            "msg": "语音识别服务当前不可用，请稍后重试。",
            "request_id": req_id,
            "retryable": True
        }), 503
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
