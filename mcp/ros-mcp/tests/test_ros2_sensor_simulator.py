"""Contracts for the real-rosbridge sensor telemetry simulator."""

from scripts.run_ros2_telemetry_echo_node import (
    _SENSOR_TOPICS,
    _build_sensor_messages,
)


def test_sensor_simulator_uses_confirmed_topics_and_message_packages():
    assert _SENSOR_TOPICS == {
        "/sensor/depth": "sealien_ctrlpilot_msgmanagement/msg/DepthStatus",
        "/sensor/imu_dvl": "sealien_ctrlpilot_msgmanagement/msg/ImuDvlStatus",
        "/sensor/thruster_status": (
            "sealien_ctrlpilot_msgmanagement/msg/ThrusterStatus"
        ),
        "/system/heartbeat": (
            "sealien_ctrlpilot_msgmanagement/msg/HeartbeatStatus"
        ),
    }


def test_sensor_simulator_builds_complete_fixed_size_payloads():
    messages = _build_sensor_messages(7)

    depth = messages["/sensor/depth"]
    assert len(depth["depth_m"]) == 4
    assert len(depth["temperature_c"]) == 4

    imu = messages["/sensor/imu_dvl"]
    assert imu["imu_error_code"] == 0
    assert imu["dvl_status"] == 7
    assert set(imu["velocity_mps"]) == {"x", "y", "z"}

    thruster = messages["/sensor/thruster_status"]
    for field in (
        "speed_rpm",
        "power_w",
        "temperature_c",
        "thruster_status_code",
        "thruster_error_code",
    ):
        assert len(thruster[field]) == 12
    assert thruster["power_lock"] == 0

    heartbeat = messages["/system/heartbeat"]
    assert heartbeat["system_status"] == 0
    assert heartbeat["mavlink_version"] == 3
