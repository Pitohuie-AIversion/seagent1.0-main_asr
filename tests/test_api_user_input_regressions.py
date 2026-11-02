"""User-facing input failures must stay recoverable and preserve valid data."""

import io
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from flask import Flask

from src.web import routes_asr, routes_time_history, routes_translate


@pytest.fixture
def client():
    app = Flask(__name__)
    app.testing = True
    for blueprint in (routes_asr.asr_bp, routes_time_history.time_history_bp,
                      routes_translate.translate_bp):
        app.register_blueprint(blueprint)
    return app.test_client()


@pytest.mark.parametrize("endpoint", ["/api/time/set", "/api/history/load", "/api/translate"])
@pytest.mark.parametrize("body", ["null", "[1]", "true", '"text"', "{broken"])
def test_non_object_and_invalid_json_returns_json_client_error(client, endpoint, body):
    response = client.post(endpoint, data=body, content_type="application/json")
    assert response.status_code == 400
    assert response.get_json()["code"] == 400


@pytest.mark.parametrize("value", [False, 123, [], {}, " ", "invalid", "2026-02-30T12:00:00"])
def test_invalid_time_does_not_change_clock(client, monkeypatch, value):
    clock = Mock()
    monkeypatch.setattr(routes_time_history, "get_simulated_time", lambda: clock)
    response = client.post("/api/time/set", json={"time": value})
    assert response.status_code == 400
    clock.set_current_time.assert_not_called()


def test_valid_time_keeps_explicit_offset(client, monkeypatch):
    clock = Mock()
    value = datetime.fromisoformat("2026-09-26T10:30:00-05:00")
    clock.get_current_time.return_value = value
    monkeypatch.setattr(routes_time_history, "get_simulated_time", lambda: clock)
    response = client.post("/api/time/set", json={"time": value.isoformat()})
    assert response.status_code == 200
    clock.set_current_time.assert_called_once_with(value)


@pytest.mark.parametrize("field", ["session_id", "history_id"])
@pytest.mark.parametrize("value", [["invalid"], {"id": "invalid"}, 12, True, " "])
def test_history_identifiers_are_validated_before_loading(client, monkeypatch, field, value):
    load = Mock()
    monkeypatch.setattr(routes_time_history, "load_history", load)
    payload = {"session_id": "active-session", "history_id": "saved.json", field: value}
    response = client.post("/api/history/load", json=payload)
    assert response.status_code == 400
    assert response.get_json()["code"] == 400
    load.assert_not_called()


@pytest.mark.parametrize("value", [None, 42, False, [], {}])
def test_translation_rejects_non_string_text(client, monkeypatch, value):
    llm = Mock()
    monkeypatch.setattr(routes_translate.state, "_shared_llm", llm)
    response = client.post("/api/translate", json={"text": value, "target_lang": "English"})
    assert response.status_code == 400
    assert response.get_json()["code"] == 400
    llm.chat.assert_not_called()


def test_translation_validates_language_for_empty_text(client):
    response = client.post("/api/translate", json={"text": "", "target_lang": "Japanese"})
    assert response.status_code == 400
    assert response.get_json()["error"] == "unsupported_language"


@pytest.mark.parametrize("filename", ["录音.wav", "🎤.WAV", "../../录音.wav", r"..\录音.wav"])
def test_localized_audio_filename_is_safe_and_supported(client, monkeypatch, filename):
    uploaded_paths = []

    def transcribe(path, *, language):
        uploaded_paths.append(Path(path))
        assert path.suffix.lower() == ".wav"
        assert path.parent.name.startswith("seagent_asr_")
        assert path.read_bytes() == b"fixture audio"
        return {"text": "你好", "language_hint": language, "device": "test",
                "elapsed_ms": 1, "segments": []}

    monkeypatch.setattr(routes_asr.state, "_shared_asr", Mock(transcribe_file=transcribe))
    response = client.post("/api/asr", data={"audio": (io.BytesIO(b"fixture audio"), filename)})
    assert response.status_code == 200
    assert response.get_json()["text"] == "你好"
    assert len(uploaded_paths) == 1
    assert not uploaded_paths[0].exists()


@pytest.mark.parametrize("filename", ["录音.exe", "录音.wav.exe", "录音", "wav"])
def test_unsupported_audio_extension_is_still_rejected(client, monkeypatch, filename):
    asr = Mock()
    monkeypatch.setattr(routes_asr.state, "_shared_asr", asr)
    response = client.post("/api/asr", data={"audio": (io.BytesIO(b"fixture audio"), filename)})
    assert response.status_code == 400
    asr.transcribe_file.assert_not_called()


@pytest.mark.parametrize("invalid_time", [42, True, [], {}, "not-a-time"])
def test_invalid_history_metadata_does_not_hide_valid_history(client, monkeypatch, tmp_path, invalid_time):
    monkeypatch.setenv("SEAGENT_HISTORY_DIR", str(tmp_path))
    valid = {"saved_at": "2026-09-26T12:00:00", "conversation_history": []}
    (tmp_path / "good.json").write_text(json.dumps(valid), encoding="utf-8")
    (tmp_path / "invalid.json").write_text(
        json.dumps({**valid, "saved_at": invalid_time}), encoding="utf-8")
    (tmp_path / "undecodable.json").write_bytes(b"\xff\xfe")
    response = client.get("/api/history/list")
    assert response.status_code == 200
    assert [record["id"] for record in response.get_json()["data"]] == ["good.json"]
