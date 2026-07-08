"""
tests/test_translate_api.py - Unit test for the translate API route
"""

import sys
from unittest.mock import MagicMock

# Mock deep learning modules before importing web_backend which imports src modules
mock_vllm = MagicMock()
sys.modules['vllm'] = mock_vllm
sys.modules['vllm.SamplingParams'] = MagicMock()
sys.modules['transformers'] = MagicMock()

import unittest
from flask import json
import web_backend

class TestTranslateAPI(unittest.TestCase):
    def setUp(self):
        web_backend.app.testing = True
        self.client = web_backend.app.test_client()
        
        # Mock LLM Client
        self.mock_llm = MagicMock()
        self.mock_llm.chat.return_value = "Hello, this is a test translation."
        self.mock_llm.filter_reply.return_value = "Hello, this is a test translation."
        
        # Inject the mock shared LLM
        self.old_llm = web_backend._shared_llm
        web_backend._shared_llm = self.mock_llm

    def tearDown(self):
        web_backend._shared_llm = self.old_llm

    def test_translate_success(self):
        payload = {
            "text": "你好，这是一个测试。",
            "target_lang": "English"
        }
        response = self.client.post(
            "/api/translate",
            data=json.dumps(payload),
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data["code"], 200)
        self.assertEqual(data["translated_text"], "Hello, this is a test translation.")
        
        self.mock_llm.chat.assert_called_once()
        self.mock_llm.filter_reply.assert_called_once()

    def test_translate_empty(self):
        payload = {
            "text": "",
            "target_lang": "English"
        }
        response = self.client.post(
            "/api/translate",
            data=json.dumps(payload),
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data["code"], 200)
        self.assertEqual(data["translated_text"], "")

    def test_translate_no_llm(self):
        web_backend._shared_llm = None
        payload = {
            "text": "你好",
            "target_lang": "English"
        }
        response = self.client.post(
            "/api/translate",
            data=json.dumps(payload),
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 503)

if __name__ == "__main__":
    unittest.main()
