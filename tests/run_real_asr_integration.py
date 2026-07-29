"""Real ASR HTTP integration test.

This runner is intentionally outside unittest discovery because it requires a
loaded ASR model and a labelled domain-audio fixture.
"""

import os
import sys
from pathlib import Path

import requests


BASE_URL = os.environ.get("SEAGENT_BASE_URL", "http://localhost:8890")
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("SEAGENT_ASR_TIMEOUT_SECONDS", "300"))


def required_environment():
    audio_value = os.environ.get("SEAGENT_ASR_AUDIO", "").strip()
    terms_value = os.environ.get("SEAGENT_ASR_EXPECTED_TERMS", "").strip()
    if not audio_value:
        raise ValueError("SEAGENT_ASR_AUDIO must point to a labelled domain-audio fixture")
    if not terms_value:
        raise ValueError("SEAGENT_ASR_EXPECTED_TERMS must contain comma-separated raw ASR terms")

    audio_path = Path(audio_value)
    if not audio_path.is_file():
        raise FileNotFoundError(f"ASR fixture does not exist: {audio_path}")

    expected_terms = [item.strip() for item in terms_value.split(",") if item.strip()]
    if not expected_terms:
        raise ValueError("SEAGENT_ASR_EXPECTED_TERMS did not contain any non-empty terms")
    return audio_path, expected_terms


def run():
    audio_path, expected_terms = required_environment()
    language = os.environ.get("SEAGENT_ASR_LANGUAGE", "Chinese")

    with audio_path.open("rb") as audio_stream:
        response = requests.post(
            f"{BASE_URL}/api/asr",
            files={"audio": (audio_path.name, audio_stream)},
            data={"language": language},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    response.raise_for_status()
    payload = response.json()

    if payload.get("code") != 200:
        raise AssertionError(f"Unexpected ASR response: {payload}")
    if payload.get("device") == "mock":
        raise AssertionError("ASR service is degraded to mock mode; real-model evidence is required")

    raw_text = payload.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise AssertionError(f"ASR returned empty raw text: {payload}")

    missing_terms = [term for term in expected_terms if term not in raw_text]
    if missing_terms:
        raise AssertionError(
            f"Raw ASR text missed labelled terms {missing_terms}; raw_text={raw_text!r}"
        )

    print(
        "REAL ASR PASSED: "
        f"device={payload.get('device')}, elapsed_ms={payload.get('elapsed_ms')}, "
        f"raw_text={raw_text!r}"
    )


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"REAL ASR FAILED: {exc}", file=sys.stderr)
        raise
