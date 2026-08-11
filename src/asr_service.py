"""
ASR service wrapper for Qwen3-ASR-0.6B.

The web layer should only call ``transcribe_file``. Model loading, device
selection, and inference serialization stay here so DialogueManager remains a
text-only component.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ASRConfig:
    model_path: Path
    device: str = "auto"
    language: str = "Chinese"
    max_new_tokens: int = 256
    max_inference_batch_size: int = 1


class ASRService:
    def __init__(self, config: ASRConfig):
        self.config = config
        self.model = None
        self.device = "cpu"
        self.dtype = None
        self._lock = threading.Lock()
        self.is_degraded = False

    def load(self) -> None:
        if self.model is not None:
            return

        if os.environ.get("OFFLINE_MOCK") == "1":
            self.device = "mock"
            self.model = "mock_model"
            self.is_degraded = True
            return

        model_path = self.config.model_path.resolve()
        if str(self.config.model_path).lower() == "mock" or self.config.model_path.name.lower() == "mock":
            self.device = "mock"
            self.model = "mock_model"
            self.is_degraded = True
            return

        if not model_path.exists():
            raise FileNotFoundError(f"ASR model path does not exist: {model_path}")

        import torch
        try:
            from qwen_asr import Qwen3ASRModel
        except ImportError as exc:
            raise RuntimeError(
                "qwen-asr is required as the local Qwen3-ASR runtime. "
                "Install it in the server virtualenv, for example: "
                "/home/ubuntu/SEAgent1.0/.seagentone/bin/pip install qwen-asr. "
                "The model weights are still loaded from the local model_path."
            ) from exc

        if self.config.device == "auto":
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self.device = self.config.device

        if self.device == "cuda":
            self.dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
            device_map = "cuda:0"
        else:
            self.dtype = torch.float32
            device_map = "cpu"

        model_kwargs = dict(
            dtype=self.dtype,
            device_map=device_map,
            max_inference_batch_size=self.config.max_inference_batch_size,
            max_new_tokens=self.config.max_new_tokens,
        )

        try:
            self.model = Qwen3ASRModel.from_pretrained(
                str(model_path),
                local_files_only=True,
                **model_kwargs,
            )
        except TypeError:
            # Some qwen-asr versions do not expose local_files_only at this wrapper level.
            self.model = Qwen3ASRModel.from_pretrained(str(model_path), **model_kwargs)
        except Exception as exc:
            # 降级到 mock 模式，避免因为 ASR 模型显存不足导致整个服务启动失败。
            self.device = "mock"
            self.model = "mock_model"
            self.is_degraded = True
            print(f"⚠️ ASR model load failed ({exc}), falling back to mock mode")

    def transcribe_file(self, audio_path: str | Path, language: str | None = None) -> dict[str, Any]:
        if self.model is None:
            raise RuntimeError("ASR model is not loaded")

        if os.environ.get("OFFLINE_MOCK") == "1":
            transcript = "流花油田，水深300米，使用sealien_work_class进行采油树控制面板插入。"
            return {
                "text": transcript,
                "language_hint": language if language else "Chinese",
                "device": "mock",
                "elapsed_ms": 120,
                "segments": [{"text": transcript}],
            }

        audio_path = Path(audio_path).resolve()
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file does not exist: {audio_path}")

        language_hint = language if language else self.config.language
        if language_hint and language_hint.lower() == "auto":
            language_hint = None

        started = time.perf_counter()
        with self._lock:
            results = self.model.transcribe(audio=str(audio_path), language=language_hint)
        elapsed_ms = int((time.perf_counter() - started) * 1000)


        segments = [self._result_to_dict(item) for item in results]
        transcript = "".join(item.get("text", "") for item in segments).strip()

        return {
            "text": transcript,
            "language_hint": language_hint,
            "device": self.device,
            "elapsed_ms": elapsed_ms,
            "segments": segments,
        }

    @staticmethod
    def _result_to_dict(result: Any) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for name in ("text", "language"):
            if hasattr(result, name):
                value = getattr(result, name)
                if value is not None:
                    data[name] = value

        if hasattr(result, "timestamps"):
            timestamps = getattr(result, "timestamps")
            if timestamps is not None:
                data["timestamps"] = timestamps

        if not data:
            data["raw"] = str(result)
        return data
