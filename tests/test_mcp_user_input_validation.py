"""Invalid dashboard commands must not change gateways or reach a robot."""

from unittest.mock import Mock

import pytest

import web_backend
from src.web import routes_mcp


@pytest.fixture
def control_client(monkeypatch):
    bridge = Mock(host="127.0.0.1", port=9091, gateway_mode="mock")
    bridge.is_healthy.return_value = True
    bridge.control_device.return_value = 0x80001
    bridge.suspend_task.return_value = 0x80002
    monkeypatch.setattr(routes_mcp, "get_mcp_bridge", lambda: bridge)
    persist = Mock()
    monkeypatch.setattr(routes_mcp, "_persist_active_gateway", persist)
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", [])
    return web_backend.app.test_client(), bridge, persist


@pytest.mark.parametrize("endpoint", ["gateway", "dispatch", "task-manage", "ctrl-task"])
@pytest.mark.parametrize("body", [[1], "invalid", 42, True])
def test_control_routes_reject_non_object_json(control_client, endpoint, body):
    client, bridge, persist = control_client
    response = client.post(f"/api/mcp/{endpoint}", json=body)
    assert response.status_code == 400
    assert response.is_json
    assert not bridge.reconnect.called
    assert not bridge.control_device.called
    assert not bridge.suspend_task.called
    persist.assert_not_called()


@pytest.mark.parametrize("task_id", [1.5, True, "bad", -1, 2**32, "NaN"])
def test_invalid_task_reference_is_not_dispatched(control_client, task_id):
    client, bridge, _ = control_client
    response = client.post("/api/mcp/task-manage", json={"action": "suspend", "task_id": task_id})
    assert response.status_code == 400
    bridge.suspend_task.assert_not_called()


@pytest.mark.parametrize("fields", [
    {"device_id": 1.5}, {"device_id": True}, {"device_id": "bad"},
    {"value": "NaN"}, {"value": "Infinity"}, {"value": True}, {"value": None},
    {"value": 1e39}, {"value": -1e39}, {"value": 1e300},
])
def test_invalid_device_command_is_not_dispatched(control_client, fields):
    client, bridge, _ = control_client
    response = client.post("/api/mcp/ctrl-task", json={"device_id": 1, "value": 50, **fields})
    assert response.status_code == 400
    bridge.control_device.assert_not_called()


@pytest.mark.parametrize("fields", [
    {"port": 9091.5}, {"port": True}, {"port": "NaN"},
    {"host": None}, {"host": ["localhost"]}, {"mode": "unknown"},
    {"mode": False}, {"mode": 0}, {"mode": []}, {"mode": {}}, {"mode": None},
])
def test_invalid_gateway_is_not_connected_or_saved(control_client, fields):
    client, bridge, persist = control_client
    response = client.post("/api/mcp/gateway", json={
        "host": "127.0.0.1", "port": 9091, "mode": "mock", **fields,
    })
    assert response.status_code == 400
    bridge.reconnect.assert_not_called()
    persist.assert_not_called()


def test_numeric_form_strings_remain_usable(control_client):
    client, bridge, _ = control_client
    response = client.post("/api/mcp/ctrl-task", json={"device_id": "1", "value": "50.5"})
    assert response.status_code == 200
    bridge.control_device.assert_called_once_with(device_id=1, value=50.5)
    response = client.post("/api/mcp/task-manage", json={"action": "suspend", "task_id": "524289"})
    assert response.status_code == 200
    bridge.suspend_task.assert_called_once_with(524289)
