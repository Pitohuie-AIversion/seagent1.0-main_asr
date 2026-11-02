"""Decode real uploads locally; replace only model inference, never the audio path."""

import io
import shutil
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from flask import Flask

from src.asr.asr_service import ASRConfig, ASRService
from src.web import routes_asr


def _wav_bytes(*, seconds=2, amplitude=0):
    samples = np.zeros(int(16000 * seconds), dtype=np.int16)
    if amplitude:
        samples[::2] = amplitude
        samples[1::2] = -amplitude
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.tobytes())
    return output.getvalue()


@pytest.fixture
def service():
    service = ASRService(ASRConfig(model_path=Path("unused-real-model")))
    service.model = MagicMock()
    service.model.transcribe.return_value = [SimpleNamespace(text="嗯。", language="Chinese")]
    return service


@pytest.fixture
def client(service, monkeypatch):
    app = Flask(__name__)
    app.testing = True
    app.register_blueprint(routes_asr.asr_bp)
    monkeypatch.setattr(routes_asr.state, "_shared_asr", service)
    return app.test_client()


@pytest.mark.parametrize("seconds", [2, 6])
def test_silent_pcm_bypasses_model_and_returns_empty_text(service, tmp_path, seconds):
    audio = tmp_path / "silence.wav"
    audio.write_bytes(_wav_bytes(seconds=seconds))

    result = service.transcribe_file(audio)

    assert result["text"] == ""
    assert result["segments"] == []
    service.model.transcribe.assert_not_called()


def test_browser_webm_silence_uses_the_same_gate(service, tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("WebM fixture encoding requires ffmpeg")
    wav = tmp_path / "silence.wav"
    wav.write_bytes(_wav_bytes())
    webm = tmp_path / "recording.webm"
    subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-i", str(wav), "-c:a", "libopus", str(webm)],
        check=True, capture_output=True, timeout=15,
    )

    result = service.transcribe_file(webm)

    assert result["text"] == ""
    service.model.transcribe.assert_not_called()


def test_short_quiet_signal_is_preserved_and_decoded_once(service, tmp_path):
    audio = tmp_path / "quiet.wav"
    audio.write_bytes(_wav_bytes(seconds=0.05, amplitude=1))
    service.model.transcribe.return_value = [SimpleNamespace(text="开始", language="Chinese")]

    result = service.transcribe_file(audio, language="auto")

    assert result["text"] == "开始"
    service.model.transcribe.assert_called_once()
    waveform, sample_rate = service.model.transcribe.call_args.kwargs["audio"]
    assert sample_rate == 16000
    assert waveform.size == 800
    assert np.max(np.abs(waveform)) == pytest.approx(1 / 32768)
    assert service.model.transcribe.call_args.kwargs["language"] is None


@pytest.mark.parametrize("payload", [b"", b"not an audio stream", _wav_bytes(seconds=0)])
def test_invalid_audio_is_a_nonretryable_client_error(client, service, payload):
    response = client.post(
        "/api/asr", data={"audio": (io.BytesIO(payload), "upload.wav")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 422
    body = response.get_json()
    assert body["error"] == "invalid_audio"
    assert body["retryable"] is False
    assert "text" not in body
    service.model.transcribe.assert_not_called()


def test_silence_has_no_translation_or_normalized_transcript(client, service, monkeypatch):
    translate = MagicMock(return_value="错误识别")
    monkeypatch.setattr(routes_asr, "_translate_text_internal", translate)
    response = client.post(
        "/api/asr", data={"audio": (io.BytesIO(_wav_bytes()), "silence.wav"), "language": "English"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["text"] == body["corrected_text"] == body["transcript"] == ""
    assert body["segments"] == []
    translate.assert_not_called()
    service.model.transcribe.assert_not_called()


def test_valid_audio_inference_failure_stays_retryable_server_error(client, service):
    service.model.transcribe.side_effect = ValueError("model inference failed")
    response = client.post(
        "/api/asr", data={"audio": (io.BytesIO(_wav_bytes(amplitude=100)), "speech.wav")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 500
    assert response.get_json()["retryable"] is True
    assert response.get_json()["error"] == "ASRProcessingError"


def test_unavailable_model_still_returns_503(client, service):
    service.model = None
    response = client.post(
        "/api/asr", data={"audio": (io.BytesIO(_wav_bytes()), "silence.wav")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "service_unavailable"
