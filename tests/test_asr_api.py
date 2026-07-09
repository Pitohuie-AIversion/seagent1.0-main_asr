"""
tests/test_asr_api.py - Unit test for the ASR API route and translation gateway
"""

import sys
import io
from unittest.mock import MagicMock

# Mock deep learning modules before importing web_backend which imports src modules
sys.modules['vllm'] = MagicMock()
sys.modules['vllm.SamplingParams'] = MagicMock()
sys.modules['transformers'] = MagicMock()

import unittest
from flask import json
import web_backend

class TestASRAPI(unittest.TestCase):
    def setUp(self):
        web_backend.app.testing = True
        self.client = web_backend.app.test_client()

        # Mock translation cache file to use a temporary one
        import tempfile
        from pathlib import Path
        self.test_cache_dir = tempfile.TemporaryDirectory()
        self.old_cache_file = web_backend._translation_cache_file
        web_backend._translation_cache_file = Path(self.test_cache_dir.name) / "test_cache.json"

        # Clear translation cache before each test
        with web_backend._translation_cache_lock:
            web_backend._translation_cache.clear()

        # Mock LLM Client for Translation
        self.mock_llm = MagicMock()
        self.mock_llm.chat.return_value = "在流花油田使用机械臂进行采油树控制面板插入。"
        self.old_llm = web_backend._shared_llm
        web_backend._shared_llm = self.mock_llm

        # Mock ASR Service
        self.mock_asr = MagicMock()
        self.old_asr = web_backend._shared_asr
        web_backend._shared_asr = self.mock_asr

    def tearDown(self):
        web_backend._shared_llm = self.old_llm
        web_backend._shared_asr = self.old_asr
        web_backend._translation_cache_file = self.old_cache_file
        self.test_cache_dir.cleanup()

    def test_asr_chinese_success(self):
        # Transcribe result mock for Chinese
        self.mock_asr.transcribe_file.return_value = {
            "text": "流花油田进行采油树控制面板插入。",
            "language_hint": "Chinese",
            "device": "mock",
            "elapsed_ms": 50,
            "segments": [{"text": "流花油田进行采油树控制面板插入。"}]
        }

        data = {
            "audio": (io.BytesIO(b"fake wav data"), "test.wav"),
            "language": "Chinese"
        }

        response = self.client.post(
            "/api/asr",
            data=data,
            content_type="multipart/form-data"
        )

        self.assertEqual(response.status_code, 200)
        res_data = json.loads(response.data)
        self.assertEqual(res_data["code"], 200)
        # Should NOT trigger translation
        self.mock_llm.chat.assert_not_called()
        self.assertEqual(res_data["text"], "流花油田进行采油树控制面板插入。")
        self.assertEqual(res_data["corrected_text"], "流花油田进行采油树控制面板插入。")

    def test_asr_english_success(self):
        # Transcribe result mock for English
        self.mock_asr.transcribe_file.return_value = {
            "text": "Using manipulator arm at Liuhua oilfield for xmas tree insertion.",
            "language_hint": "English",
            "device": "mock",
            "elapsed_ms": 50,
            "segments": [{"text": "Using manipulator arm at Liuhua oilfield for xmas tree insertion."}]
        }

        data = {
            "audio": (io.BytesIO(b"fake wav data"), "test.wav"),
            "language": "English"
        }

        response = self.client.post(
            "/api/asr",
            data=data,
            content_type="multipart/form-data"
        )

        self.assertEqual(response.status_code, 200)
        res_data = json.loads(response.data)
        self.assertEqual(res_data["code"], 200)
        # Should trigger translation
        self.mock_llm.chat.assert_called_once()
        self.assertEqual(res_data["text"], "Using manipulator arm at Liuhua oilfield for xmas tree insertion.")
        # The translated text from mock_llm is "在流花油田使用机械臂进行采油树控制面板插入。"
        self.assertEqual(res_data["corrected_text"], "在流花油田使用机械臂进行采油树控制面板插入。")
        self.assertTrue(res_data["normalization_changed"])

if __name__ == "__main__":
    unittest.main()
