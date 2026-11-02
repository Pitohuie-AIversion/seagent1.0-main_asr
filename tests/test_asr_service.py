import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    import torch
    HAS_TORCH = True
except ImportError:
    torch = None
    HAS_TORCH = False

from src.asr.asr_service import ASRConfig, ASRService


class TestASRDeviceSelection(unittest.TestCase):
    def test_dtype_matches_selected_device_and_restores_current_device(self):
        cases = [
            ("auto", [], "cpu", "float32"),
            ("auto", [True], "cuda:0", "bfloat16"),
            ("auto", [True, False], "cuda:1", "float16"),
            ("auto", [False, True], "cuda:1", "bfloat16"),
            ("cuda:0", [True, False], "cuda:0", "bfloat16"),
            ("cuda:1", [True, False], "cuda:1", "float16"),
        ]
        for configured, capabilities, expected_device, expected_dtype in cases:
            with self.subTest(configured=configured, capabilities=capabilities):
                state = {"current": 0}

                @contextmanager
                def select_device(device):
                    previous = state["current"]
                    state["current"] = int(device.split(":")[1]) if ":" in device else previous
                    try:
                        yield
                    finally:
                        state["current"] = previous

                fake_torch = types.SimpleNamespace(
                    bfloat16="bfloat16", float16="float16", float32="float32",
                    cuda=types.SimpleNamespace(
                        is_available=lambda: bool(capabilities),
                        device_count=lambda: len(capabilities),
                        device=select_device,
                        is_bf16_supported=lambda: capabilities[state["current"]],
                    ),
                )
                model_type = MagicMock()
                fake_asr = types.SimpleNamespace(Qwen3ASRModel=model_type)
                with tempfile.TemporaryDirectory() as model_dir, \
                     patch.dict("os.environ", {"OFFLINE_MOCK": "0", "SEAGENT_OFFLINE_MOCK": "0"}), \
                     patch.dict(sys.modules, {"torch": fake_torch, "qwen_asr": fake_asr}):
                    service = ASRService(ASRConfig(model_path=Path(model_dir), device=configured))
                    service.load()

                self.assertIs(service.model, model_type.from_pretrained.return_value)
                self.assertEqual(service.device, expected_device)
                self.assertEqual(service.dtype, expected_dtype)
                self.assertEqual(model_type.from_pretrained.call_args.kwargs["dtype"], expected_dtype)
                self.assertEqual(model_type.from_pretrained.call_args.kwargs["device_map"], expected_device)
                self.assertEqual(state["current"], 0)


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
