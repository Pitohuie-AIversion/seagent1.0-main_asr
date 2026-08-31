"""Protocol semantics for the ROS-only execution simulator."""

import time

import pytest

from scratch import run_ros2_telemetry_echo_node as simulator


@pytest.fixture(autouse=True)
def isolated_active_tasks():
    with simulator.lock:
        original = dict(simulator.active_tasks)
        simulator.active_tasks.clear()
    try:
        yield
    finally:
        with simulator.lock:
            simulator.active_tasks.clear()
            simulator.active_tasks.update(original)


def _pose(z: float) -> dict:
    return {
        "position": {"x": 115.0, "y": 20.0, "z": z},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
    }


def test_control_and_management_params_are_never_interpreted_as_depth():
    assert simulator._target_depth({"task_type": 6, "params": [1.0, 50.0]}) is None
    assert simulator._target_depth({"task_type": 0, "params": [6.0]}) is None


def test_motion_depth_comes_from_protocol_position_targets():
    assert simulator._target_depth(
        {"task_type": 5, "pos_target": [_pose(-300.0)], "params": []}
    ) == pytest.approx(300.0)
    assert simulator._target_depth(
        {
            "task_type": 2,
            "pos_target": [_pose(-80.0), _pose(-120.0)],
            "params": [],
        }
    ) == pytest.approx(120.0)


def test_control_task_preserves_last_motion_pose_and_has_zero_linear_velocity():
    now = time.monotonic()
    simulator.active_tasks[0x80001] = {
        "command": {"task_type": 5, "task_id": 0x80001},
        "task_type": 5,
        "target_depth": 300.0,
        "start_time": now - 20.0,
        "status": 5,
        "progress": 100.0,
    }
    simulator.active_tasks[0x80002] = {
        "command": {"task_type": 6, "task_id": 0x80002},
        "task_type": 6,
        "target_depth": None,
        "start_time": now - 2.0,
        "status": 3,
        "progress": 60.0,
    }

    status = simulator._build_system_status(tick=1)

    assert status["pose"]["pose"]["position"]["z"] == pytest.approx(-300.0)
    assert status["twist"]["linear"] == {"x": 0.0, "y": 0.0, "z": 0.0}
