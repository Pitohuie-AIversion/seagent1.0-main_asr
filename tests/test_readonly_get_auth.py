"""Auth contract tests for the 4 read-only GET endpoints protected by @_require_api_token."""

import pytest

import web_backend


READONLY_GET_ENDPOINTS = [
    "/api/session/state?session_id=nonexistent-auth-check",
    "/api/telemetry",
    "/api/time/current",
    "/api/history/list",
]


def _assert_expected_200_payload(endpoint: str, payload: dict) -> None:
    assert isinstance(payload, dict)
    assert payload.get("code") == 200
    if endpoint.startswith("/api/session/state"):
        assert payload.get("ok") is True
        assert payload.get("exists") is False
    elif endpoint == "/api/telemetry":
        assert "snapshot" in payload
        assert isinstance(payload["snapshot"], dict)
    elif endpoint == "/api/time/current":
        assert "current_time" in payload
        assert "timestamp" in payload
    elif endpoint == "/api/history/list":
        assert isinstance(payload.get("data"), list)
    else:
        raise AssertionError(f"Unexpected endpoint in matrix: {endpoint}")


@pytest.fixture
def isolated_client(monkeypatch, tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SEAGENT_HISTORY_DIR", str(history_dir))
    return web_backend.app.test_client()


@pytest.mark.parametrize("endpoint", READONLY_GET_ENDPOINTS)
def test_readonly_get_rejects_unauthenticated_when_token_configured(
    isolated_client, monkeypatch, endpoint
):
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", ["token-alpha", "token-beta"])

    resp = isolated_client.get(endpoint)
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"
    data = resp.get_json()
    assert data["code"] == 401
    assert data["error"] == "Unauthorized"
    assert data.get("msg")


@pytest.mark.parametrize("endpoint", READONLY_GET_ENDPOINTS)
@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer wrong-token"},
        {"X-API-Token": "wrong-token"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer"},
        {"X-API-Token": ""},
    ],
)
def test_readonly_get_rejects_invalid_or_malformed_tokens(
    isolated_client, monkeypatch, endpoint, headers
):
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", ["token-alpha", "token-beta"])

    resp = isolated_client.get(endpoint, headers=headers)
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"
    data = resp.get_json()
    assert data["code"] == 401
    assert data["error"] == "Unauthorized"


@pytest.mark.parametrize("endpoint", READONLY_GET_ENDPOINTS)
@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer token-alpha"},
        {"Authorization": "Bearer token-beta"},
        {"X-API-Token": "token-alpha"},
        {"X-API-Token": "token-beta"},
    ],
)
def test_readonly_get_allows_valid_bearer_and_x_api_token(
    isolated_client, monkeypatch, endpoint, headers
):
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", ["token-alpha", "token-beta"])

    resp = isolated_client.get(endpoint, headers=headers)
    assert resp.status_code == 200
    _assert_expected_200_payload(endpoint, resp.get_json())


@pytest.mark.parametrize("endpoint", READONLY_GET_ENDPOINTS)
def test_readonly_get_open_when_tokens_empty(isolated_client, monkeypatch, endpoint):
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", [])

    resp = isolated_client.get(endpoint)
    assert resp.status_code == 200
    _assert_expected_200_payload(endpoint, resp.get_json())


def test_session_state_missing_session_id_requires_auth_before_validation(
    isolated_client, monkeypatch
):
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", ["token-alpha"])

    unauth_resp = isolated_client.get("/api/session/state")
    assert unauth_resp.status_code == 401
    assert unauth_resp.headers.get("WWW-Authenticate") == "Bearer"
    assert unauth_resp.get_json()["error"] == "Unauthorized"

    auth_resp = isolated_client.get(
        "/api/session/state",
        headers={"Authorization": "Bearer token-alpha"},
    )
    assert auth_resp.status_code == 400
    data = auth_resp.get_json()
    assert data["code"] == 400
    assert data["error"] == "MissingSessionId"
