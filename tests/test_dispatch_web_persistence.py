"""Real confirmation, durable display state, and explicit safe retry via HTTP."""
from datetime import timedelta
import json
from unittest.mock import Mock

import pytest


@pytest.fixture
def web_task(tmp_path, monkeypatch):
    import web_backend
    import src.web.state as state
    from mcp.core.bridge_service import SEAgentMCPBridgeService
    from tests.test_failure_recovery_benchmark import FailureRecoveryBenchmarkTest
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    case = FailureRecoveryBenchmarkTest()
    case.setUp()
    case._setup_full_confirming_task()
    manager = case.dm
    manager.session_id = "dispatch-web-regression"
    monkeypatch.setitem(state._sessions_manager, manager.session_id, manager)
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", [])
    bridge = SEAgentMCPBridgeService(dispatch_records_dir=tmp_path / "dispatch")
    bridge.is_healthy = Mock(return_value=True)
    bridge.client.publish_task_cmd = Mock()
    monkeypatch.setattr(state, "_shared_mcp_bridge", bridge)
    return web_backend.app.test_client(), manager, bridge, tmp_path


def _reply(response, stream):
    if not stream:
        return response.get_json()
    return next(json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
                if line.startswith("data: ") and '"ros2_dispatch"' in line)


@pytest.mark.parametrize("stream", [False, True])
def test_dispatch_failure_survives_state_history_and_explicit_same_intent_retry(web_task, stream):
    client, manager, bridge, root = web_task
    bridge.client.publish_task_cmd.side_effect = RuntimeError("gateway rejected before send")
    response = client.post("/api/chat/stream" if stream else "/api/chat",
                           json={"session_id": manager.session_id, "message": "确认发布"})
    result = _reply(response, stream)
    assert result["ros2_dispatch"]["state"] == "FAILED", result
    assert "已生成并下发" not in result["reply"]
    state = client.get("/api/session/state", query_string={"session_id": manager.session_id}).get_json()
    assert state["ros2_dispatch"]["state"] == "FAILED"
    history = json.loads(next((root / "history").glob("history_*.json")).read_text())
    assert history["ros2_dispatch"]["state"] == "FAILED"
    intent = manager.final_result.copy()
    assigned = bridge._dispatch_records[intent["intent_id"]]["task_id"]
    bridge.client.publish_task_cmd.side_effect = None
    result = client.post("/api/mcp/dispatch", json={"session_id": manager.session_id}).get_json()
    assert result["ros2_dispatch"]["state"] == "SENT", result
    assert result["task_id"] == assigned and manager.final_result == intent
    assert len(list((root / "task").glob("task_intent_*.json"))) == 1
    client.post("/api/mcp/dispatch", json={"session_id": manager.session_id})
    assert bridge.client.publish_task_cmd.call_count == 2


def test_future_confirmation_and_early_manual_retry_never_send(web_task):
    from src.temporal.simulated_time import get_current_datetime
    client, manager, bridge, _ = web_task
    manager.slot_store.slots["start_time"].value = (get_current_datetime() + timedelta(hours=2)).isoformat()
    manager._rebuild_cache()
    result = client.post("/api/chat", json={"session_id": manager.session_id, "message": "确认发布"}).get_json()
    if manager.phase == "blocked_soft":
        # Future planning emits an existing runtime-check notice which the
        # established dialogue requires the user to acknowledge explicitly.
        client.post("/api/chat", json={"session_id": manager.session_id, "message": "忽略警告"})
        result = client.post("/api/chat", json={"session_id": manager.session_id, "message": "确认发布"}).get_json()
    assert result["ros2_dispatch"]["state"] == "SCHEDULED", result
    retry = client.post("/api/mcp/dispatch", json={"session_id": manager.session_id}).get_json()
    assert retry["ros2_dispatch"]["state"] == "SCHEDULED"
    bridge.client.publish_task_cmd.assert_not_called()


def test_custom_intent_flag_cannot_bypass_confirmed_session(web_task):
    import web_backend
    client, manager, bridge, _ = web_task
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setitem(web_backend.app.config, "ALLOW_MCP_CUSTOM_INTENT", True)
        response = client.post("/api/mcp/dispatch", json={"task_intent": {"task_type": "pipeline_inspection"}})
    assert response.status_code == 400
    bridge.client.publish_task_cmd.assert_not_called()
