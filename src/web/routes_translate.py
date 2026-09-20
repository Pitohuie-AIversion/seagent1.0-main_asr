"""
src/web/routes_translate.py - 文本翻译服务路由与长文本分块缓存机制
"""

import logging
import re
import sys
import threading
import uuid
from flask import Blueprint, jsonify, request

import src.web.state as state
from src.dispatch.result_paths import get_result_dir
from src.web.state import _require_api_token

logger = logging.getLogger(__name__)

translate_bp = Blueprint("translate", __name__)

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

TRANSLATION_CHUNK_SIZE = 2000
TRANSLATION_MAX_INPUT_CHARS = 20000
TRANSLATION_MAX_TOKENS = 4096
TRANSLATION_CACHE_FILE = get_result_dir(create=True) / "translation_cache.json"
_translation_cache_file = TRANSLATION_CACHE_FILE
_translation_cache_lock = state._translation_cache_lock
_translation_cache = state._translation_cache
TRANSLATION_USE_CACHE = True


def _use_cache() -> bool:
    backend = sys.modules.get("web_backend")
    if backend is not None and hasattr(backend, "TRANSLATION_USE_CACHE"):
        return getattr(backend, "TRANSLATION_USE_CACHE")
    return TRANSLATION_USE_CACHE


def _get_cache_key(text: str, target_lang: str) -> str:
    return f"{target_lang}:{text}"


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
    paragraphs = re.split(r"\n\n+", text)
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
    from src.extraction.model_profile import ModelRole
    system_instruction = _TRANSLATE_SYSTEM_PROMPT.format(target_lang=target_lang)
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": text},
    ]
    return state._shared_llm.chat(
        messages,
        temperature=0.1,
        max_tokens=TRANSLATION_MAX_TOKENS,
        role=ModelRole.TRANSLATION,
    )


def _translate_text_internal(text: str, target_lang: str) -> str:
    """
    核心翻译函数。
    - 每次请求均调用 LLM 实时翻译。
    - 超过 TRANSLATION_CHUNK_SIZE 字符时分段翻译后合并。
    - 翻译结果经质量校验；校验失败时返回原文并记录 warning。
    """
    text = text.strip()
    if not text:
        return ""

    cache_key = _get_cache_key(text, target_lang)
    if _use_cache():
        with _translation_cache_lock:
            if cache_key in _translation_cache:
                return _translation_cache[cache_key]

    # 输入长度硬限制
    if len(text) > TRANSLATION_MAX_INPUT_CHARS:
        logging.warning(
            f"[translate] Input too long ({len(text)} chars > {TRANSLATION_MAX_INPUT_CHARS}), "
            "truncating to limit."
        )
        text = text[:TRANSLATION_MAX_INPUT_CHARS]

    if state._shared_llm is None:
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
        # 返回原文（安全回退）
        return text

    if _use_cache():
        with _translation_cache_lock:
            _translation_cache[cache_key] = translated

    return translated


@translate_bp.route("/api/translate", methods=["POST"])
@_require_api_token
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
