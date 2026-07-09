"""
tests/test_translate_api.py - Unit tests for the /api/translate route and translation helpers
Covers: dirty detection, quality validation, chunked translation, input limits,
        target_lang validation, cache bypass, and retries.
"""

import sys
from unittest.mock import MagicMock

# Mock deep learning modules before importing web_backend
for mod in ['vllm', 'vllm.SamplingParams']:
    sys.modules[mod] = MagicMock()
sys.modules['transformers'] = MagicMock()

import unittest
from flask import json
import web_backend


class TestIsDirtyTranslation(unittest.TestCase):
    """Unit tests for _is_dirty_translation helper."""

    def test_empty_string(self):
        self.assertTrue(web_backend._is_dirty_translation("English", ""))

    def test_whitespace_only(self):
        self.assertTrue(web_backend._is_dirty_translation("English", "   "))

    def test_json_object(self):
        self.assertTrue(web_backend._is_dirty_translation("English", '{"task_type": "pipeline_inspection"}'))

    def test_json_array(self):
        self.assertTrue(web_backend._is_dirty_translation("English", '["item1", "item2"]'))

    def test_english_target_with_chinese(self):
        self.assertTrue(web_backend._is_dirty_translation("English", "收到，任务已启动。"))

    def test_clean_english(self):
        self.assertFalse(web_backend._is_dirty_translation("English", "Pipeline inspection initiated."))

    def test_clean_chinese_target(self):
        self.assertFalse(web_backend._is_dirty_translation("Chinese", "管道检测任务已完成。"))

    def test_chinese_target_english_content_not_dirty(self):
        # No rule flags English content for Chinese target
        self.assertFalse(web_backend._is_dirty_translation("Chinese", "Pipeline inspection completed."))


class TestValidateTranslationQuality(unittest.TestCase):
    """Unit tests for _validate_translation_quality helper."""

    def test_dirty_content_fails(self):
        valid, reason = web_backend._validate_translation_quality(
            "Hello world", '{"key": "value"}', "English"
        )
        self.assertFalse(valid)
        self.assertEqual(reason, "dirty_content")

    def test_length_ratio_too_short(self):
        original = "a" * 200
        translated = "x" * 10  # ratio = 0.05 < 0.08 → invalid
        valid, reason = web_backend._validate_translation_quality(original, translated, "English")
        self.assertFalse(valid)
        self.assertIn("length_ratio_abnormal", reason)

    def test_length_ratio_too_long(self):
        original = "a" * 200
        translated = "b" * 1500  # ratio = 7.5 > 6.0 → invalid
        valid, reason = web_backend._validate_translation_quality(original, translated, "English")
        self.assertFalse(valid)
        self.assertIn("length_ratio_abnormal", reason)

    def test_short_text_skips_length_check(self):
        # Text ≤100 chars skips length ratio check
        valid, reason = web_backend._validate_translation_quality("Hi", "你好", "Chinese")
        self.assertTrue(valid)
        valid2, _ = web_backend._validate_translation_quality("a" * 100, "b", "English")
        self.assertTrue(valid2)  # exactly 100 chars → no length check

    def test_valid_translation(self):
        # Long enough text (>100 chars) with reasonable ratio
        original = "The pipeline inspection for subsea oilfield equipment has been completed successfully. All parameters are within normal range."
        translated = "海底油田设备管道巡检已成功完成，所有参数均在正常范围内。"
        valid, reason = web_backend._validate_translation_quality(original, translated, "Chinese")
        self.assertTrue(valid)
        self.assertEqual(reason, "ok")


class TestSplitIntoChunks(unittest.TestCase):
    """Unit tests for _split_into_chunks."""

    def test_short_text_single_chunk(self):
        text = "Short text paragraph."
        chunks = web_backend._split_into_chunks(text, 2000)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], text)

    def test_multiple_paragraphs_split(self):
        para = "x" * 500
        text = "\n\n".join([para] * 6)  # 6 x 500 = 3000 chars
        chunks = web_backend._split_into_chunks(text, 2000)
        self.assertGreater(len(chunks), 1)

    def test_single_large_paragraph_as_one_chunk(self):
        text = "x" * 3000  # No paragraph breaks
        chunks = web_backend._split_into_chunks(text, 2000)
        self.assertEqual(len(chunks), 1)


class TestTranslateAPIRoute(unittest.TestCase):
    """Integration tests for /api/translate via Flask test client."""

    def setUp(self):
        web_backend.app.testing = True
        self.client = web_backend.app.test_client()

        import tempfile
        from pathlib import Path
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_cache_file = web_backend._translation_cache_file
        web_backend._translation_cache_file = Path(self._tmpdir.name) / "cache.json"

        with web_backend._translation_cache_lock:
            web_backend._translation_cache.clear()

        self._orig_use_cache = web_backend.TRANSLATION_USE_CACHE
        web_backend.TRANSLATION_USE_CACHE = False

        self.mock_llm = MagicMock()
        self.mock_llm.chat.return_value = "Hello, this is a test translation."
        self._orig_llm = web_backend._shared_llm
        web_backend._shared_llm = self.mock_llm

    def tearDown(self):
        web_backend._shared_llm = self._orig_llm
        web_backend._translation_cache_file = self._orig_cache_file
        web_backend.TRANSLATION_USE_CACHE = self._orig_use_cache
        self._tmpdir.cleanup()

    def _post(self, payload):
        return self.client.post(
            "/api/translate",
            data=json.dumps(payload),
            content_type="application/json",
        )

    # ── Basic correctness ──────────────────────────────────────────────
    def test_success(self):
        resp = self._post({"text": "你好，这是测试。", "target_lang": "English"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["code"], 200)
        self.assertEqual(data["translated_text"], "Hello, this is a test translation.")

    def test_empty_text_skips_llm(self):
        resp = self._post({"text": "", "target_lang": "English"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["translated_text"], "")
        self.mock_llm.chat.assert_not_called()

    def test_llm_not_initialized(self):
        web_backend._shared_llm = None
        resp = self._post({"text": "你好", "target_lang": "English"})
        self.assertEqual(resp.status_code, 503)

    # ── target_lang validation ─────────────────────────────────────────
    def test_unsupported_target_lang(self):
        resp = self._post({"text": "Hello", "target_lang": "Japanese"})
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertIn("Unsupported", data["msg"])

    def test_chinese_target_lang_accepted(self):
        self.mock_llm.chat.return_value = "你好，测试。"
        resp = self._post({"text": "Hello test.", "target_lang": "Chinese"})
        self.assertEqual(resp.status_code, 200)

    # ── Cache bypass (TRANSLATION_USE_CACHE=False) ─────────────────────
    def test_no_cache_mode_calls_llm_every_time(self):
        web_backend.TRANSLATION_USE_CACHE = False
        payload = {"text": "你好。", "target_lang": "English"}
        self._post(payload)
        self.mock_llm.chat.assert_called_once()
        self.mock_llm.chat.reset_mock()
        self._post(payload)
        self.mock_llm.chat.assert_called_once()

    def test_cache_mode_hits_on_second_call(self):
        web_backend.TRANSLATION_USE_CACHE = True
        payload = {"text": "你好。", "target_lang": "English"}
        self._post(payload)
        self.mock_llm.chat.assert_called_once()
        self.mock_llm.chat.reset_mock()
        self._post(payload)
        self.mock_llm.chat.assert_not_called()

    # ── Quality validation: dirty LLM response → original returned ─────
    def test_dirty_chinese_response_falls_back(self):
        self.mock_llm.chat.return_value = "这是中文回复，不应该被接受。"
        resp = self._post({"text": "Confirm the task.", "target_lang": "English"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        # Quality check fails → original text returned
        self.assertEqual(data["translated_text"], "Confirm the task.")

    def test_dirty_json_response_falls_back(self):
        self.mock_llm.chat.return_value = '{"task_type": "pipeline_inspection"}'
        resp = self._post({"text": "Start the task.", "target_lang": "English"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["translated_text"], "Start the task.")

    # ── Input length limit ─────────────────────────────────────────────
    def test_oversized_input_truncated(self):
        huge_text = "你好 " * 8000  # >> 20000 chars
        self.mock_llm.chat.return_value = "Hello " * 10
        resp = self._post({"text": huge_text, "target_lang": "English"})
        self.assertEqual(resp.status_code, 200)
        # Verify LLM received truncated input
        call_args = self.mock_llm.chat.call_args
        user_content = call_args[0][0][-1]["content"]
        self.assertLessEqual(len(user_content), web_backend.TRANSLATION_MAX_INPUT_CHARS)

    # ── Chunked translation ────────────────────────────────────────────
    def test_long_text_calls_llm_multiple_times(self):
        para = "这是用于测试分段翻译的中文段落文本内容。" * 25  # ~500 chars each
        long_text = "\n\n".join([para] * 6)  # ~3000 chars > CHUNK_SIZE=2000
        self.mock_llm.chat.return_value = "This is a test paragraph for chunked translation."
        resp = self._post({"text": long_text, "target_lang": "English"})
        self.assertEqual(resp.status_code, 200)
        # Should have called LLM more than once
        self.assertGreater(self.mock_llm.chat.call_count, 1)


if __name__ == "__main__":
    unittest.main()
