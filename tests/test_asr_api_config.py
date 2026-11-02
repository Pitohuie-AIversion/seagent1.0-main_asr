"""ASR API configuration compatibility and voice submission policy."""

import io
from unittest.mock import Mock

import pytest
import yaml
from flask import Flask

from src.web import routes_asr


def load_config(tmp_path, monkeypatch, data):
    (tmp_path / "asr.yaml").write_text(
        yaml.safe_dump(data), encoding="utf-8"
    )
    monkeypatch.setattr(routes_asr, "CONFIG_DIR", tmp_path)
    return routes_asr._load_asr_api_config()


def test_existing_flat_asr_config_preserves_api_settings(tmp_path, monkeypatch):
    config = load_config(tmp_path, monkeypatch, {
        "model_path": "/local/asr-model",
        "device": "cuda:1",
        "language": "English",
        "direct_to_llm": False,
        "allowed_extensions": ["wav"],
        "max_upload_mb": 8,
    })

    assert config == {
        "language": "English",
        "direct_to_llm": False,
        "allowed_extensions": ["wav"],
        "max_upload_mb": 8,
    }


def test_nested_api_overrides_flat_settings_without_losing_false(tmp_path, monkeypatch):
    config = load_config(tmp_path, monkeypatch, {
        "language": "Chinese",
        "direct_to_llm": True,
        "allowed_extensions": ["wav", "mp3"],
        "max_upload_mb": 25,
        "api": {
            "language": "English",
            "direct_to_llm": False,
            "allowed_extensions": ["flac"],
            "model_path": "/not-an-api-setting",
        },
    })

    assert config == {
        "language": "English",
        "direct_to_llm": False,
        "allowed_extensions": ["flac"],
        "max_upload_mb": 25,
    }


@pytest.mark.parametrize("api", [None, False, [], "invalid"])
def test_invalid_api_section_preserves_flat_settings(tmp_path, monkeypatch, api):
    assert load_config(tmp_path, monkeypatch, {
        "direct_to_llm": False,
        "api": api,
    }) == {"direct_to_llm": False}


def test_nested_api_config_remains_supported(tmp_path, monkeypatch):
    assert load_config(tmp_path, monkeypatch, {
        "api": {"direct_to_llm": "false", "max_upload_mb": 10},
    }) == {"direct_to_llm": "false", "max_upload_mb": 10}


@pytest.mark.parametrize("data", [None, [], "invalid"])
def test_non_mapping_config_keeps_defaults(tmp_path, monkeypatch, data):
    assert load_config(tmp_path, monkeypatch, data) == {}


def test_flat_false_is_returned_to_browser_as_manual_submission(tmp_path, monkeypatch):
    config = load_config(tmp_path, monkeypatch, {"direct_to_llm": False})
    monkeypatch.setattr(routes_asr, "_asr_api_config", config)
    monkeypatch.setattr(
        routes_asr, "_asr_direct_to_llm",
        routes_asr._config_bool(config, "direct_to_llm", True),
    )
    asr_service = Mock()
    asr_service.transcribe_file.return_value = {
        "text": "你好",
        "language_hint": "Chinese",
        "device": "test",
        "elapsed_ms": 1,
        "segments": [],
    }
    monkeypatch.setattr(routes_asr.state, "_shared_asr", asr_service)
    app = Flask(__name__)
    app.register_blueprint(routes_asr.asr_bp)

    response = app.test_client().post("/api/asr", data={
        "audio": (io.BytesIO(b"test audio"), "sample.wav"),
        "language": "Chinese",
    })

    assert response.status_code == 200
    assert response.json["text"] == "你好"
    assert response.json["direct_to_llm"] is False
