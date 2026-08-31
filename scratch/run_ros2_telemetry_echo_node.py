"""ROS-only task execution simulator for the SEAgent rosbridge path."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path


SEAGENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SEAGENT_ROOT))
sys.path.insert(0, str(SEAGENT_ROOT / "mcp" / "core"))

from mcp.shim.rosbridge_client import RosbridgeClient  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
active_tasks = {}
lock = threading.Lock()


_POSITION_TARGET_TASK_TYPES = {1, 2, 3, 4, 5, 10}
_ROUTE_TASK_TYPES = {2, 10}


def _target_depth(command: dict) -> float | None:
    """Return the final positional target depth for motion-capable tasks only."""
    try:
        task_type = int(command.get("task_type"))
    except (TypeError, ValueError):
        return None
    if task_type not in _POSITION_TARGET_TASK_TYPES:
        return None

    targets = command.get("pos_target") or []
    if not targets:
        return None
    target = targets[-1] if task_type in _ROUTE_TASK_TYPES else targets[0]
    try:
        return abs(float(target["position"]["z"]))
    except (KeyError, TypeError, ValueError):
        return None


def _manage_task(command: dict) -> None:
    params = command.get("params") or []
    if not params:
        return
    action = int(params[0])
    target_id = int(params[1]) if len(params) > 1 else None
    if action == 0 and target_id in active_tasks:
        active_tasks[target_id]["status"] = 6
    elif action == 1 and target_id in active_tasks:
        active_tasks[target_id]["status"] = 3
        active_tasks[target_id]["start_time"] = time.monotonic() - 2.0
    elif action == 2:
        for task in active_tasks.values():
            task["status"] = 6
    elif action == 3:
        for task in active_tasks.values():
            if task["status"] == 6:
                task["status"] = 3
    elif action == 4 and target_id in active_tasks:
        active_tasks.pop(target_id, None)
    elif action == 5:
        active_tasks.clear()


def on_task_cmd_received(command: dict) -> None:
    """Create simulator state only from a real /task_cmd subscription callback."""
    try:
        task_id = int(command.get("task_id") or 0)
        task_type = int(command.get("task_type") or 0)
        if not task_id:
            return
        with lock:
            if task_type == 0:
                _manage_task(command)
                return
            if task_id in active_tasks:
                logging.warning("忽略重复 ROS task_id: 0x%X", task_id)
                return
            active_tasks[task_id] = {
                "command": command,
                "task_type": task_type,
                "target_depth": _target_depth(command),
                "start_time": time.monotonic(),
                "status": 1,
                "progress": 10.0,
            }
        logging.info("ROS 模拟器收到任务: task_id=0x%X type=%d", task_id, task_type)
    except Exception as exc:
        logging.error("解析 /task_cmd 失败: %s", exc, exc_info=True)


def _advance_tasks(now: float) -> None:
    for task in active_tasks.values():
        if task["status"] in (5, 6, 7):
            continue
        elapsed = now - task["start_time"]
        if elapsed >= 15.0:
            task["status"] = 5
            task["progress"] = 100.0
        elif elapsed >= 1.0:
            task["status"] = 3
            task["progress"] = min(99.0, elapsed / 15.0 * 100.0)
        else:
            task["status"] = 1
            task["progress"] = 10.0


def _task_status_message(task: dict) -> dict:
    return {"task": task["command"], "status": task["status"]}


def _build_system_status(tick: int) -> dict:
    now = time.monotonic()
    with lock:
        _advance_tasks(now)
        tasks = list(active_tasks.values())
        motion_tasks = [task for task in tasks if task["target_depth"] is not None]
        current_motion = motion_tasks[-1] if motion_tasks else None
        task_list = [_task_status_message(task) for task in tasks]

    if current_motion:
        ratio = max(0.0, min(1.0, current_motion["progress"] / 100.0))
        depth = 85.0 + (current_motion["target_depth"] - 85.0) * ratio
        pose_x = 115.3 + (165.8 - 115.3) * ratio
        pose_y = 20.9 + (85.4 - 20.9) * ratio
    else:
        depth = 85.0
        pose_x = 115.3
        pose_y = 20.9
    is_moving = bool(
        current_motion and current_motion["status"] in (1, 2, 3, 4)
    )
    altitude = 2.5 + (tick % 3) * 0.02

    return {
        "pose": {
            "header": {"frame_id": "odom"},
            "pose": {
                "position": {
                    "x": round(pose_x, 2),
                    "y": round(pose_y, 2),
                    "z": round(-depth, 2),
                },
                "orientation": {"x": 0.0, "y": 0.0, "z": 0.7071, "w": 0.7071},
            },
        },
        "twist": {
            "linear": {
                "x": 0.3 if is_moving else 0.0,
                "y": 0.1 if is_moving else 0.0,
                "z": 0.0,
            },
            "angular": {"x": 0.0, "y": 0.0, "z": 0.01},
        },
        "alt": round(altitude, 2),
        "ctr_mode": 4,
        "health": 0,
        "task_list": task_list,
    }


def run_telemetry_loop() -> None:
    host = os.environ.get("ROSBRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("ROSBRIDGE_PORT", "9090"))
    message_type = "sealien_ctrlpilot_llmbridge/msg/SysStatus"
    tick = 0

    while True:
        client = RosbridgeClient(host=host, port=port, connect_timeout=3.0)
        try:
            client.connect()
            client.advertise("/task/system_status", message_type)
            client.subscribe(
                "/task_cmd",
                "sealien_ctrlpilot_llmbridge/msg/SysTaskCmd",
                on_task_cmd_received,
            )
            logging.info("ROS 模拟器已连接 ws://%s:%d", host, port)
            while client.is_connected():
                tick += 1
                client.publish(
                    "/task/system_status", message_type, _build_system_status(tick)
                )
                time.sleep(1.0)
        except Exception as exc:
            logging.error("ROS 模拟器链路异常: %s", exc)
        finally:
            client.disconnect()
        time.sleep(2.0)


if __name__ == "__main__":
    run_telemetry_loop()
