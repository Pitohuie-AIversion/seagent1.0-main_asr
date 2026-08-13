import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import torch
    HAS_TORCH = True
except ImportError:
    torch = None
    HAS_TORCH = False

from src.asr_service import ASRConfig, ASRService


class TestASRServiceFallback(unittest.TestCase):
    def test_load_falls_back_when_model_load_ooms(self):
        oom_exc = torch.OutOfMemoryError if (HAS_TORCH and hasattr(torch, "OutOfMemoryError")) else RuntimeError

        class FakeQwen3ASRModel:
            @classmethod
            def from_pretrained(cls, *args, **kwargs):
                raise oom_exc("CUDA out of memory")

        fake_qwen_asr = types.SimpleNamespace(Qwen3ASRModel=FakeQwen3ASRModel)

        with patch.dict(sys.modules, {"qwen_asr": fake_qwen_asr}):
            service = ASRService(ASRConfig(model_path=Path("mock"), device="cuda"))
            service.load()

        self.assertTrue(service.is_degraded)
        self.assertEqual(service.device, "mock")
        self.assertEqual(service.model, "mock_model")

    def test_explicit_mock_mode_can_transcribe_without_offline_env(self):
        with patch.dict("os.environ", {}, clear=True):
            service = ASRService(ASRConfig(model_path=Path("mock")))
            service.load()
            with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
                result = service.transcribe_file(audio.name)

        self.assertEqual("mock", result["device"])
        self.assertTrue(result["text"])

    def test_real_model_load_failure_does_not_fabricate_transcript(self):
        class FakeQwen3ASRModel:
            @classmethod
            def from_pretrained(cls, *args, **kwargs):
                raise RuntimeError("simulated model load failure")

        fake_qwen_asr = types.SimpleNamespace(Qwen3ASRModel=FakeQwen3ASRModel)
        with tempfile.TemporaryDirectory() as model_dir, \
             patch.dict("os.environ", {}, clear=True), \
             patch.dict(sys.modules, {"qwen_asr": fake_qwen_asr}):
            service = ASRService(ASRConfig(model_path=Path(model_dir)))
            service.load()
            with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
                with self.assertRaisesRegex(RuntimeError, "ASR.*unavailable"):
                    service.transcribe_file(audio.name)

        self.assertTrue(service.is_degraded)
        self.assertNotEqual("mock", service.device)


if __name__ == "__main__":
    unittest.main()
