import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from mcp.shim.bridge_service import SEAgentMCPBridgeService
from mcp.shim.mock_rosbridge_server import MockRosbridgeServer
from mcp.shim.runtime_config import (
    Ros2RuntimeConfigError,
    extract_display_fields,
    load_ros2_runtime_config,
)
from mcp.shim.rosbridge_client import RosbridgeClient


SEAGENT_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_SPEC = SEAGENT_ROOT / "config" / "ros2_protocol_spec.yaml"
RUNTIME_CONFIG = SEAGENT_ROOT / "config" / "ros2_runtime.yaml"
PORT = 9096


def test_repository_runtime_config_enables_confirmed_sensor_topics():
    config = load_ros2_runtime_config(RUNTIME_CONFIG, PROTOCOL_SPEC)
    subscriptions = {item.id: item for item in config.enabled_subscriptions}

    expected = {
        "depth_status": (
            "/sensor/depth",
            "sealien_ctrlpilot_msgmanagement/msg/DepthStatus",
        ),
        "imu_dvl_status": (
            "/sensor/imu_dvl",
            "sealien_ctrlpilot_msgmanagement/msg/ImuDvlStatus",
        ),
        "thruster_status": (
            "/sensor/thruster_status",
            "sealien_ctrlpilot_msgmanagement/msg/ThrusterStatus",
        ),
        "heartbeat_status": (
            "/system/heartbeat",
            "sealien_ctrlpilot_msgmanagement/msg/HeartbeatStatus",
        ),
    }
    for subscription_id, (topic, message_type) in expected.items():
        subscription = subscriptions[subscription_id]
        assert (subscription.topic, subscription.message_type) == (
            topic,
            message_type,
        )
        assert subscription.parser == "raw"
        assert subscription.show_raw_message is False
        assert subscription.fields


def _runtime_yaml(*, port: int = PORT, include_aux: bool = True) -> str:
    aux = """
  - id: test_text
    enabled: true
    topic: /test/runtime_text
    message_type: std_msgs/msg/String
    parser: raw
    stale_after_seconds: 5
    display:
      title: 动态文本
      show_raw_message: true
      fields:
        - path: data
          label: 内容
""" if include_aux else ""
    return f"""\
version: '1.0'
gateway:
  host: 127.0.0.1
  port: {port}
  mode: mock
reload:
  mode: automatic
  check_interval_seconds: 0.2
dashboard:
  refresh_interval_ms: 750
  max_raw_message_bytes: 4096
subscriptions:
  - id: system_status
    enabled: true
    topic: /task/system_status
    message_type: sealien_ctrlpilot_llmbridge/msg/SysStatus
    parser: system_status
    stale_after_seconds: 5
    display:
      title: 系统状态
      show_raw_message: false
      fields:
        - path: health
          label: 健康码
{aux}"""


def test_runtime_config_loads_gateway_subscription_and_dashboard(tmp_path):
    runtime_file = tmp_path / "ros2_runtime.yaml"
    runtime_file.write_text(_runtime_yaml(), encoding="utf-8")

    config = load_ros2_runtime_config(runtime_file, PROTOCOL_SPEC)

    assert config.gateway.host == "127.0.0.1"
    assert config.gateway.port == PORT
    assert config.dashboard.refresh_interval_ms == 750
    assert config.system_status.topic == "/task/system_status"
    assert [item.id for item in config.enabled_subscriptions] == [
        "system_status",
        "test_text",
    ]


def test_runtime_config_rejects_core_protocol_drift(tmp_path):
    runtime_file = tmp_path / "ros2_runtime.yaml"
    runtime_file.write_text(
        _runtime_yaml().replace(
            "sealien_ctrlpilot_llmbridge/msg/SysStatus",
            "sealien_ctrlpilot_msgmanagement/msg/SysStatus",
        ),
        encoding="utf-8",
    )

    with pytest.raises(Ros2RuntimeConfigError, match="system_status.*静态协议"):
        load_ros2_runtime_config(runtime_file, PROTOCOL_SPEC)


def test_repository_sensor_subscription_rejects_static_protocol_drift(tmp_path):
    runtime_file = tmp_path / "ros2_runtime.yaml"
    runtime_file.write_text(
        RUNTIME_CONFIG.read_text(encoding="utf-8").replace(
            "topic: /sensor/depth", "topic: /sensor/guessed_depth"
        ),
        encoding="utf-8",
    )

    with pytest.raises(Ros2RuntimeConfigError, match="depth_status.*静态协议"):
        load_ros2_runtime_config(runtime_file, PROTOCOL_SPEC)


def test_runtime_config_rejects_duplicate_ids(tmp_path):
    runtime_file = tmp_path / "ros2_runtime.yaml"
    runtime_file.write_text(
        _runtime_yaml().replace("id: test_text", "id: system_status"),
        encoding="utf-8",
    )

    with pytest.raises(Ros2RuntimeConfigError, match="重复订阅 ID"):
        load_ros2_runtime_config(runtime_file, PROTOCOL_SPEC)


def test_extract_display_fields_supports_nested_paths_and_missing_values():
    fields = extract_display_fields(
        {"pose": {"position": {"z": -85.0}}},
        [
            {"path": "pose.position.z", "label": "深度", "unit": "m"},
            {"path": "health", "label": "健康码"},
        ],
    )

    assert fields == [
        {"path": "pose.position.z", "label": "深度", "unit": "m", "value": -85.0},
        {"path": "health", "label": "健康码", "unit": "", "value": None},
    ]


def test_extract_display_fields_indexes_rosbridge_base64_uint8_arrays():
    fields = extract_display_fields(
        {"thruster_error_code": "AAECAw=="},
        [
            {
                "path": "thruster_error_code.2",
                "label": "3号故障码",
            }
        ],
    )

    assert fields[0]["value"] == 2


@pytest.fixture
def mock_gateway():
    server = MockRosbridgeServer(port=PORT)
    server.start()
    time.sleep(0.2)
    yield server
    server.stop()


def test_bridge_subscribes_and_exposes_yaml_driven_raw_message(
    tmp_path, mock_gateway
):
    runtime_file = tmp_path / "ros2_runtime.yaml"
    runtime_file.write_text(_runtime_yaml(), encoding="utf-8")
    bridge = SEAgentMCPBridgeService(
        host="127.0.0.1",
        port=PORT,
        connect_timeout=3.0,
        runtime_config_path=runtime_file,
        protocol_config_path=PROTOCOL_SPEC,
    )
    bridge.start()
    try:
        bridge.client.publish(
            "/test/runtime_text", "std_msgs/msg/String", {"data": "hello yaml"}
        )
        deadline = time.monotonic() + 3.0
        item = None
        while time.monotonic() < deadline:
            items = bridge.runtime_snapshot().get("dynamic_subscriptions", [])
            item = next((entry for entry in items if entry["id"] == "test_text"), None)
            if item and item["message_count"]:
                break
            time.sleep(0.05)

        assert item is not None
        assert item["title"] == "动态文本"
        assert item["fields"][0]["value"] == "hello yaml"
        assert item["raw_message"] == {"data": "hello yaml"}
        assert item["status"] == "LIVE"
    finally:
        bridge.stop()


def test_bridge_hot_reload_removes_subscription_and_keeps_last_good_on_error(
    tmp_path, mock_gateway
):
    runtime_file = tmp_path / "ros2_runtime.yaml"
    runtime_file.write_text(_runtime_yaml(), encoding="utf-8")
    bridge = SEAgentMCPBridgeService(
        host="127.0.0.1",
        port=PORT,
        connect_timeout=3.0,
        runtime_config_path=runtime_file,
        protocol_config_path=PROTOCOL_SPEC,
    )
    bridge.start()
    try:
        assert {item["id"] for item in bridge.runtime_snapshot()["dynamic_subscriptions"]} == {
            "system_status",
            "test_text",
        }

        runtime_file.write_text(_runtime_yaml(include_aux=False), encoding="utf-8")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            ids = [
                item["id"]
                for item in bridge.runtime_snapshot()["dynamic_subscriptions"]
            ]
            if ids == ["system_status"]:
                break
            time.sleep(0.05)
        assert [item["id"] for item in bridge.runtime_snapshot()["dynamic_subscriptions"]] == [
            "system_status"
        ]
        assert bridge.runtime_config_payload()["generation"] >= 2

        runtime_file.write_text("subscriptions: [", encoding="utf-8")
        with pytest.raises(Ros2RuntimeConfigError):
            bridge.reload_runtime_config()
        payload = bridge.status_payload()
        assert [item["id"] for item in payload["snapshot"]["dynamic_subscriptions"]] == [
            "system_status"
        ]
        assert payload["runtime_config"]["last_error"]
    finally:
        bridge.stop()


def test_runtime_snapshot_limits_raw_message_size(tmp_path):
    runtime_file = tmp_path / "ros2_runtime.yaml"
    runtime_file.write_text(
        _runtime_yaml().replace("max_raw_message_bytes: 4096", "max_raw_message_bytes: 32"),
        encoding="utf-8",
    )
    config = load_ros2_runtime_config(runtime_file, PROTOCOL_SPEC)
    spec = next(item for item in config.enabled_subscriptions if item.id == "test_text")

    view = spec.build_view({"data": "x" * 200}, received_at="2026-08-31T00:00:00+00:00", message_count=1)

    assert view["raw_message"] is None
    assert view["raw_truncated"] is True
    assert json.dumps(view, ensure_ascii=False)


def test_runtime_snapshot_omits_non_json_raw_value_without_breaking_view(tmp_path):
    runtime_file = tmp_path / "ros2_runtime.yaml"
    runtime_file.write_text(_runtime_yaml(), encoding="utf-8")
    config = load_ros2_runtime_config(runtime_file, PROTOCOL_SPEC)
    spec = next(item for item in config.enabled_subscriptions if item.id == "test_text")

    view = spec.build_view(
        {"data": float("nan")}, received_at=None, message_count=1
    )

    assert view["raw_message"] is None
    assert view["raw_truncated"] is True
    assert view["fields"][0]["value"] is None
    assert json.dumps(view, ensure_ascii=False, allow_nan=False)


def test_rosbridge_unsubscribe_keeps_shared_topic_until_last_callback():
    client = RosbridgeClient()
    client._ws = Mock(connected=True)
    first = Mock()
    second = Mock()
    client._subscriptions["/shared"] = [first, second]

    client.unsubscribe("/shared", first)
    assert client._subscriptions["/shared"] == [second]
    client._ws.send.assert_not_called()

    client.unsubscribe("/shared", second)
    assert "/shared" not in client._subscriptions
    sent = json.loads(client._ws.send.call_args.args[0])
    assert sent == {"op": "unsubscribe", "topic": "/shared"}
