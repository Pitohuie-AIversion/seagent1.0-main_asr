import sys
import threading
from pathlib import Path

from mcp.shim.bridge_service import SEAgentMCPBridgeService
from mcp.core.ros_mcp_client import RosMCPClient, _exception_details


FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "fake_ros_mcp_server.py"


def _client(**overrides):
    options = {
        "host": "192.0.2.10",
        "port": 9090,
        "connect_timeout": 1.0,
        "server_command": sys.executable,
        "server_args": [str(FIXTURE_SERVER)],
        "startup_timeout": 5.0,
        "tool_timeout": 5.0,
        "telemetry_poll_interval": 0.05,
        "telemetry_timeout": 0.1,
    }
    options.update(overrides)
    return RosMCPClient(**options)


def test_stdio_session_initializes_and_discovers_required_tools():
    client = _client()
    try:
        client.connect()
        assert client.is_connected()
        assert RosMCPClient.REQUIRED_TOOLS <= client.tool_names
    finally:
        client.disconnect()
    assert not client.is_connected()


def test_nested_transport_errors_preserve_the_root_cause():
    error = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [ConnectionRefusedError("192.168.5.250:9090 refused")],
    )

    assert _exception_details(error) == (
        "ConnectionRefusedError: 192.168.5.250:9090 refused"
    )


def test_publish_uses_mcp_publish_once_tool():
    client = _client()
    try:
        client.connect()
        result = client.call_tool(
            "publish_once",
            {
                "topic": "/task_cmd",
                "msg_type": "example/msg/Command",
                "msg": {"task_id": 42},
            },
        )
        assert result["success"] is True
        assert result["msg"]["task_id"] == 42
    finally:
        client.disconnect()


def test_system_status_is_polled_through_mcp_subscription_tool():
    received = []
    updated = threading.Event()
    client = _client()

    def receive(message):
        received.append(message)
        updated.set()

    client.subscribe_system_status(receive)
    try:
        client.connect()
        assert updated.wait(2.0)
        assert received[-1]["pose"]["pose"]["position"]["z"] == -3.0
    finally:
        client.disconnect()


def test_seagent_bridge_dispatches_over_mcp_client_factory():
    def client_factory(*, host, port, connect_timeout):
        return _client(host=host, port=port, connect_timeout=connect_timeout)

    bridge = SEAgentMCPBridgeService(
        host="192.0.2.10",
        port=9090,
        connect_timeout=1.0,
        client_factory=client_factory,
    )
    try:
        bridge.start()
        task_id = bridge.dispatch_intent(
            {
                "intent_id": "mcp-integration-1",
                "task_type": "tree_valve_operation",
                "priority": 15,
                "location": {"water_depth_m": 3.0},
                "task": {
                    "type": "tree_valve_operation",
                    "details": {
                        "target": {
                            "x": 1.0,
                            "y": 2.0,
                            "z": -3.0,
                            "frame_id": "odom",
                        }
                    },
                },
            }
        )
        assert bridge.is_healthy()
        assert task_id >= 0x80001
        assert bridge.runtime_snapshot()["active_tasks"][0]["status"] == "SENT"
    finally:
        bridge.stop()
