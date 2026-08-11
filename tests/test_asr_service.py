import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import torch

from src.asr_service import ASRConfig, ASRService


class TestASRServiceFallback(unittest.TestCase):
    def test_load_falls_back_when_model_load_ooms(self):
        class FakeQwen3ASRModel:
            @classmethod
            def from_pretrained(cls, *args, **kwargs):
                raise torch.OutOfMemoryError("CUDA out of memory")

        fake_qwen_asr = types.SimpleNamespace(Qwen3ASRModel=FakeQwen3ASRModel)

        with patch.dict(sys.modules, {"qwen_asr": fake_qwen_asr}):
            service = ASRService(ASRConfig(model_path=Path("mock"), device="cuda"))
            service.load()

        self.assertTrue(service.is_degraded)
        self.assertEqual(service.device, "mock")
        self.assertEqual(service.model, "mock_model")


if __name__ == "__main__":
    unittest.main()
