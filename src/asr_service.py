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


class ASRUnavailableError(RuntimeError):
    """Raised when real ASR initialization failed and inference is unavailable."""


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
        self._mock_mode = False
        self._load_error: Exception | None = None

    def _enable_explicit_mock(self) -> None:
        self.device = "mock"
        self.model = "mock_model"
        self.is_degraded = True
        self._mock_mode = True
        self._load_error = None

    def _mark_unavailable(self, exc: Exception) -> None:
        self.device = "unavailable"
        self.model = None
        self.is_degraded = True
        self._mock_mode = False
        self._load_error = exc

    def load(self) -> None:
        if self.model is not None:
            return

        if (
            os.environ.get("OFFLINE_MOCK") == "1"
            or os.environ.get("SEAGENT_OFFLINE_MOCK") == "1"
        ):
            self._enable_explicit_mock()
            return

        model_path = self.config.model_path.resolve()
        if str(self.config.model_path).lower() == "mock" or self.config.model_path.name.lower() == "mock":
            self._enable_explicit_mock()
            return

        if not model_path.exists():
            self._mark_unavailable(
                FileNotFoundError(f"ASR model path does not exist: {model_path}")
            )
            return

        try:
            import torch
            from qwen_asr import Qwen3ASRModel
        except (ImportError, RuntimeError) as exc:
            self._mark_unavailable(exc)
            return

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
            # Keep the dialogue service alive, but never fabricate a transcript
            # for real user audio after an unexpected model failure.
            self._mark_unavailable(exc)
            print(f"⚠️ ASR model load failed ({exc}); ASR requests will fail closed")

    def transcribe_file(self, audio_path: str | Path, language: str | None = None) -> dict[str, Any]:
        if self._mock_mode:
            transcript = "流花油田，水深300米，使用sealien_work_class进行采油树控制面板插入。"
            return {
                "text": transcript,
                "language_hint": language if language else "Chinese",
                "device": "mock",
                "elapsed_ms": 120,
                "segments": [{"text": transcript}],
            }

        if self.model is None:
            detail = f": {self._load_error}" if self._load_error else ""
            raise ASRUnavailableError(f"ASR service is unavailable{detail}")

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
