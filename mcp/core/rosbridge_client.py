"""
rosbridge_client.py
====================
SEAgent 生产级 rosbridge WebSocket 客户端

基于 ROS 组 `sealien_ctrlpilot_llmbridge` 消息协议，通过 rosbridge v2.0
WebSocket 协议直连支持船 Topside 网关，实现：

关键边界：本文件承接的主链路消息仅为 `sealien_ctrlpilot_llmbridge` 套件（UI 接口协议），
不再与 `outside/sealien_ctrlpilot_msgmanagement-dev_rov-msg` 的同名主任务消息并行对齐。
该目录下的 msg 文件仅用于可选扩展链路（视觉/辅助控制/底层状态），不会覆盖任务主闭环判据。

1. 任务下发   publish_task_cmd(intent)         -> /task_cmd
2. 系统配置   publish_sys_config(mode)          -> /task/sys_config
3. 任务管理   task_manage(action, task_id)      -> /task_cmd (TASK_MANAGE)
4. 灯/继电器  ctrl_task(device_id, value)       -> /task_cmd (CTRL_TASK)
5. AUV 任务   auv_task(waypoints, params)        -> /task_cmd (AUV_TASK)
6. 遥测订阅   subscribe_system_status(callback)  <- /task/system_status
7. 视觉信息   subscribe_keypoints(callback)      <- /vision/keypoints

内部协议参考：
outside/sealien_ctrlpilot_llmbridge-ros-mcp-server/
sealien_ctrlpilot_llmbridge/UI接口协议.md
"""

import fcntl
import json
import logging
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import websocket
import yaml

from .sealien_protocol import (
    LocalOrigin,
    ProtocolValidationError,
    geodetic_to_odom_position,
    validate_priority,
    validate_task_id,
    validate_uint32,
)

logger = logging.getLogger(__name__)

# The simulator still exposes the legacy msgmanagement ROS contract.  Keep the
# production llmbridge contract as the default and opt into the simulator
# adapter explicitly through the service environment.
LEGACY_MSGMANAGEMENT = os.environ.get("SEAGENT_ROS2_COMPAT", "").lower() in {
    "msgmanagement", "legacy", "simulator"
}


# ============================================================================
# ROS 组协议常量
# ============================================================================
# 任务闭环主链路（唯一来源）
TASK_TOPIC = "/task/sys_task_cmd" if LEGACY_MSGMANAGEMENT else "/task_cmd"
CONFIG_TOPIC = "/task/sys_config"
STATUS_TOPIC = "/task/system_status"

TASK_MESSAGE_TYPE = (
    "sealien_ctrlpilot_msgmanagement/msg/SysTaskCmd"
    if LEGACY_MSGMANAGEMENT
    else "sealien_ctrlpilot_llmbridge/msg/SysTaskCmd"
)
CONFIG_MESSAGE_TYPE = (
    "sealien_ctrlpilot_msgmanagement/msg/SysConfig"
    if LEGACY_MSGMANAGEMENT
    else "sealien_ctrlpilot_llmbridge/msg/SysConfig"
)
STATUS_MESSAGE_TYPE = (
    "sealien_ctrlpilot_msgmanagement/msg/SysStatus"
    if LEGACY_MSGMANAGEMENT
    else "sealien_ctrlpilot_llmbridge/msg/SysStatus"
)

# 非核心辅助通道（不作为主协议判据）
COMPRESSED_IMAGE_TOPIC = "/vision/compressd_image"
IMAGE_TOPIC = "/vision/image"
KEYPOINTS_TOPIC = "/vision/keypoints"
PLUG_HOLE_TOPIC = "/vision/plug_hole"

COMPRESSED_IMAGE_TYPE = "sensor_msgs/msg/CompressedImage"
IMAGE_TYPE = "sensor_msgs/msg/Image"
KEYPOINTS_TYPE = "sealien_ctrlpilot_msgmanagement/msg/Keypoints"
PLUG_HOLE_TYPE = "sealien_ctrlpilot_msgmanagement/msg/ConnectChristmasTreePlug"

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROTOCOL_SPEC = _PROJECT_ROOT / "config" / "ros2_protocol_spec.yaml"


def load_protocol_spec() -> Dict[str, Any]:
    """Load the optional YAML protocol catalog without making startup fragile."""
    try:
        with _PROTOCOL_SPEC.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("无法读取 ROS 订阅配置 %s: %s", _PROTOCOL_SPEC, exc)
        return {}

class TaskType(IntEnum):
    """SysTaskCmd.msg 任务类型枚举（与 UI接口协议.md 严格对齐）"""
    TASK_MANAGE = 0    # 任务管理（挂起/恢复/暂停/继续/删除）
    CLAMP_CABLE = 1    # 夹缆
    SEARCH_CABLE = 2   # 巡缆
    CLAMP_PIN = 3      # 夹销
    INSERT_PLUG = 4    # 插插销/采油树阀门操作
    MOVE_TASK = 5      # 移动任务
    CTRL_TASK = 6      # 开关灯、继电器等控制
    AUV_TASK = 10      # AUV 任务


class TaskManageAction(IntEnum):
    """TASK_MANAGE params[0] 动作编号"""
    SUSPEND = 0      # 挂起指定 task_id
    RESUME = 1       # 恢复指定 task_id
    SUSPEND_ALL = 2  # 挂起所有任务
    RESUME_ALL = 3   # 恢复所有任务
    DELETE = 4       # 删除指定 task_id
    DELETE_ALL = 5   # 删除所有任务
    QUERY = 6        # 查询当前任务状态
    CLEAR_BLOCK = 7  # 清除当前阻塞状态


class TaskStatus(IntEnum):
    """TaskStatus.msg status 字段"""
    READY = 0    # 就绪
    PLAN = 1     # 规划中
    ENTER = 2    # 进入（条件判断）
    ONGOING = 3  # 执行中
    EXIT = 4     # 执行完，正退出
    FINISH = 5   # 执行完成，已退出
    PAUSE = 6    # 挂起
    FAIL = 7     # 失败


class PilotMode(IntEnum):
    """SysConfig / SysStatus ctr_mode 字段"""
    NONE = 0
    MANUAL = 1           # 手动
    STABILIZE1 = 2       # 稳定：定深+定向
    STABILIZE2 = 3       # 稳定：定高+定向
    AUTODEPTH = 4        # 定深
    AUTODHIGHT = 5       # 定高（保留 ROS 消息中的既有拼写）
    AUTODIRCETION = 6    # 定向（保留 ROS 消息中的既有拼写）
    AUTOHOLD1 = 7        # x/y/z 位置保持（定深）
    AUTOHOLD2 = 8        # x/y/z 位置保持（定高）
    MISSION1 = 9         # 路径跟踪（定深）
    MISSION2 = 10        # 路径跟踪（定高）

    # 可读别名不改变 ROS 协议常量和值。
    AUTOHEIGHT = AUTODHIGHT
    AUTODIRECTION = AUTODIRCETION


# SEAgent TaskIntent task_type -> SysTaskCmd TaskType 映射
SEAGENT_TO_ROS2_TASK_TYPE: Dict[str, TaskType] = {
    "pipeline_inspection":  TaskType.SEARCH_CABLE,   # 巡缆/巡线
    "管缆巡检":             TaskType.SEARCH_CABLE,
    "管道/电缆巡检":        TaskType.SEARCH_CABLE,
    "pipeline_burial":      TaskType.CLAMP_CABLE,    # 管道/电缆埋设
    "管缆埋设":             TaskType.CLAMP_CABLE,
    "管道/电缆埋设":        TaskType.CLAMP_CABLE,
    "cable_burial":         TaskType.CLAMP_CABLE,    # 夹缆/埋设
    "cable_pin":            TaskType.CLAMP_PIN,      # 夹销
    "valve_operation":      TaskType.INSERT_PLUG,    # 阀门/插销操作
    "常规阀门操作":         TaskType.INSERT_PLUG,
    "tree_valve_operation": TaskType.INSERT_PLUG,    # 采油树阀门
    "采油树阀门操作":       TaskType.INSERT_PLUG,
    "underwater_move":      TaskType.MOVE_TASK,      # 移动任务
    "水下移动任务":         TaskType.MOVE_TASK,
    "light_control":        TaskType.CTRL_TASK,      # 灯光/继电器控制
    "relay_control":        TaskType.CTRL_TASK,      # 继电器控制
    "auv_mission":          TaskType.AUV_TASK,       # AUV 航行任务
    "task_manage":          TaskType.TASK_MANAGE,    # 任务管理
}

# AI 生成任务 ID 前缀（UI接口协议.md：0x8XXXX 是 AI，0x9XXXX 是 UI）
_AI_TASK_ID_BASE = 0x80000
_task_id_counter = 0
_task_id_lock = threading.Lock()


def generate_task_id() -> int:
    """生成 AI 任务 ID；生产模式通过原子序号文件防止重启后复用。"""
    global _task_id_counter
    with _task_id_lock:
        sequence_dir_text = os.environ.get("SEAGENT_ROS2_ID_DIR")
        if not sequence_dir_text:
            _task_id_counter += 1
            if _task_id_counter > 0xFFFF:
                raise RuntimeError("ROS 2 AI task_id 范围已耗尽")
            return _AI_TASK_ID_BASE + _task_id_counter

        sequence_dir = Path(sequence_dir_text)
        sequence_dir.mkdir(parents=True, exist_ok=True)
        lock_path = sequence_dir / ".ros2_task_id.lock"
        counter_path = sequence_dir / ".ros2_task_id_sequence"
        with open(lock_path, "a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                disk_value = 0
                if counter_path.exists():
                    raw_value = counter_path.read_text(encoding="ascii").strip()
                    if not raw_value.isdigit():
                        raise RuntimeError(
                            f"ROS 2 task_id 序号文件损坏: {counter_path}"
                        )
                    disk_value = int(raw_value)
                next_value = max(disk_value, _task_id_counter) + 1
                if next_value > 0xFFFF:
                    raise RuntimeError("ROS 2 AI task_id 范围已耗尽")

                temporary_path = sequence_dir / (
                    f".ros2_task_id_sequence.tmp_{os.getpid()}_{threading.get_ident()}"
                )
                try:
                    with open(temporary_path, "w", encoding="ascii") as temp_handle:
                        temp_handle.write(str(next_value))
                        temp_handle.flush()
                        os.fsync(temp_handle.fileno())
                    os.replace(temporary_path, counter_path)
                    directory_fd = os.open(
                        sequence_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    )
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    temporary_path.unlink(missing_ok=True)
                _task_id_counter = next_value
                return _AI_TASK_ID_BASE + next_value
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class Pose:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    qx: float = 0.0
    qy: float = 0.0
    qz: float = 0.0
    qw: float = 1.0

    def to_dict(self) -> dict:
        return {
            "position":    {"x": self.x,  "y": self.y,  "z": self.z},
            "orientation": {"x": self.qx, "y": self.qy, "z": self.qz, "w": self.qw},
        }


@dataclass
class SysTaskCmd:
    """ROS 2 SysTaskCmd.msg 完整结构体"""
    task_type: int
    task_id: int = field(default_factory=generate_task_id)
    frame_id: str = "odom"
    priority: int = 15
    pos_target: List[Pose] = field(default_factory=list)
    params: List[float] = field(default_factory=list)
    fail_stop: bool = True

    def to_dict(self) -> dict:
        return {
            "task_type":  self.task_type,
            "task_id":    self.task_id,
            "frame_id":   self.frame_id,
            "priority":   self.priority,
            "pos_target": [p.to_dict() for p in self.pos_target],
            "params":     self.params,
            "fail_stop":  self.fail_stop,
        }


@dataclass
class TaskStatusItem:
    """TaskStatus 任务状态项（来自 /task/system_status task_list）"""
    task_id: int
    task_type: int
    status: int
    status_name: str = ""

    def is_finished(self) -> bool:
        return self.status in (TaskStatus.FINISH, TaskStatus.FAIL)

    def is_active(self) -> bool:
        return self.status in (TaskStatus.PLAN, TaskStatus.ENTER, TaskStatus.ONGOING)


# ============================================================================
# SEAgent TaskIntent v2 → SysTaskCmd 转换函数
# ============================================================================

_MANIPULATOR_TASK_TYPES = {
    TaskType.CLAMP_CABLE,
    TaskType.CLAMP_PIN,
    TaskType.INSERT_PLUG,
}
_TARGET_REQUIRED_MANAGEMENT_ACTIONS = {
    TaskManageAction.SUSPEND,
    TaskManageAction.RESUME,
    TaskManageAction.DELETE,
}


def _intent_details(intent: Dict[str, Any]) -> Dict[str, Any]:
    task = intent.get("task") or {}
    if not isinstance(task, dict):
        raise ProtocolValidationError("TaskIntent.task 必须是对象")
    details = task.get("details") or {}
    if not isinstance(details, dict):
        raise ProtocolValidationError("TaskIntent.task.details 必须是对象")
    return details


def _intent_coordinate(
    intent: Dict[str, Any], details: Dict[str, Any], field_name: str
) -> Optional[Dict[str, Any]]:
    value = details.get(field_name)
    return value if isinstance(value, dict) else None


def _water_depth(intent: Dict[str, Any]) -> float:
    location = intent.get("location") or {}
    if not isinstance(location, dict):
        raise ProtocolValidationError("TaskIntent.location 必须是对象")
    depth_value = location.get("water_depth_m")
    if depth_value is None:
        raise ProtocolValidationError("TaskIntent 缺少有效 water_depth_m")
    try:
        depth = float(depth_value)
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError("TaskIntent 缺少有效 water_depth_m") from exc
    if depth < 0:
        raise ProtocolValidationError("water_depth_m 必须是非负数")
    return depth


def _coordinate_pose(
    coordinate: Dict[str, Any],
    default_depth: float,
    use_geodetic: bool,
    origin: Optional[LocalOrigin],
    field_name: str,
) -> Pose:
    latitude = coordinate.get("latitude")
    longitude = coordinate.get("longitude")
    if latitude is None or longitude is None:
        raise ProtocolValidationError(f"{field_name} 必须包含 latitude/longitude")
    try:
        depth = float(coordinate.get("depth", default_depth))
        latitude_value = float(latitude)
        longitude_value = float(longitude)
        yaw = float(coordinate.get("yaw", 0.0))
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError(f"{field_name} 坐标或水深不是有效数值") from exc
    if depth < 0:
        raise ProtocolValidationError(f"{field_name}.depth 必须是非负数")
    if use_geodetic:
        east, north, z = geodetic_to_odom_position(
            latitude=latitude_value,
            longitude=longitude_value,
            water_depth_m=depth,
            origin=origin,
        )
        return Pose(
            x=east, y=north, z=z,
            qz=math.sin(yaw / 2.0), qw=math.cos(yaw / 2.0),
        )
    return Pose(
        x=longitude_value, y=latitude_value, z=-depth,
        qz=math.sin(yaw / 2.0), qw=math.cos(yaw / 2.0),
    )


def validate_sys_task_cmd(cmd: SysTaskCmd) -> SysTaskCmd:
    """Fail closed when a generated command violates the ROS group's layout."""
    try:
        task_type = TaskType(int(cmd.task_type))
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError(f"不支持的 task_type: {cmd.task_type}") from exc
    cmd.task_type = int(task_type)
    cmd.task_id = validate_task_id(cmd.task_id)
    cmd.priority = validate_priority(cmd.priority)
    if not isinstance(cmd.fail_stop, bool):
        raise ProtocolValidationError("fail_stop 必须是布尔值")

    if task_type == TaskType.TASK_MANAGE:
        if cmd.frame_id or cmd.pos_target or cmd.priority != 0:
            raise ProtocolValidationError(
                "TASK_MANAGE 必须使用空 frame_id、空 pos_target 和 priority=0"
            )
        if not 1 <= len(cmd.params) <= 2:
            raise ProtocolValidationError("TASK_MANAGE params 必须包含动作及可选目标 ID")
        try:
            action_value = float(cmd.params[0])
            action = TaskManageAction(int(action_value))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProtocolValidationError(
                f"不支持的 TASK_MANAGE action: {cmd.params[0]}"
            ) from exc
        if action_value != float(action):
            raise ProtocolValidationError(
                f"TASK_MANAGE action 必须是整数枚举值: {cmd.params[0]}"
            )
        target_required = action in _TARGET_REQUIRED_MANAGEMENT_ACTIONS
        if target_required and len(cmd.params) != 2:
            raise ProtocolValidationError(
                f"TASK_MANAGE {action.name} 必须包含目标任务 ID"
            )
        if not target_required and len(cmd.params) != 1:
            raise ProtocolValidationError(
                f"TASK_MANAGE {action.name} 不得包含目标任务 ID"
            )
        if target_required:
            target_value = float(cmd.params[1])
            target_id = validate_uint32(target_value, "target_task_id")
            if target_value != float(target_id):
                raise ProtocolValidationError("target_task_id 必须是整数")
    elif task_type in _MANIPULATOR_TASK_TYPES:
        if cmd.frame_id or len(cmd.pos_target) != 1 or cmd.params:
            raise ProtocolValidationError(
                f"{task_type.name} 必须使用空 frame_id、一个 pos_target 和空 params"
            )
    elif task_type == TaskType.SEARCH_CABLE:
        if cmd.frame_id not in {"odom", "base_link"}:
            raise ProtocolValidationError("SEARCH_CABLE frame_id 必须是 odom 或 base_link")
        if len(cmd.pos_target) != 2 or cmd.params:
            raise ProtocolValidationError(
                "SEARCH_CABLE 必须包含起点和终点两个 pos_target，且 params 为空"
            )
    elif task_type == TaskType.MOVE_TASK:
        if cmd.frame_id not in {"odom", "base_link"}:
            raise ProtocolValidationError("MOVE_TASK frame_id 必须是 odom 或 base_link")
        if len(cmd.pos_target) != 1 or cmd.params:
            raise ProtocolValidationError(
                "MOVE_TASK 必须包含一个 pos_target，且 params 为空"
            )
    elif task_type == TaskType.CTRL_TASK:
        if cmd.frame_id or cmd.pos_target or len(cmd.params) != 2:
            raise ProtocolValidationError(
                "CTRL_TASK 必须使用空 frame_id、空 pos_target 和两个 params"
            )
    elif task_type == TaskType.AUV_TASK:
        if cmd.frame_id not in {"odom", "base_link"}:
            raise ProtocolValidationError("AUV_TASK frame_id 必须是 odom 或 base_link")
        if not cmd.pos_target or len(cmd.params) != 6:
            raise ProtocolValidationError(
                "AUV_TASK 必须包含航线关键点和六个 params"
            )
        if float(cmd.params[3]) not in {0.0, 1.0}:
            raise ProtocolValidationError("AUV_TASK params[3] 必须是 0 或 1")
    return cmd


def intent_to_syscmd(
    intent: Dict[str, Any],
    task_id: Optional[int] = None,
    use_geodetic: bool = False,
    origin: Optional[LocalOrigin] = None,
) -> SysTaskCmd:
    """
    将 SEAgent 落盘的 TaskIntent v2 字典转换为 SysTaskCmd 结构体。
    """
    if not isinstance(intent, dict):
        raise ProtocolValidationError("TaskIntent 必须是对象")
    details = _intent_details(intent)
    task = intent.get("task") or {}
    candidates = (
        intent.get("task_type_key"),
        intent.get("task_type"),
        task.get("type"),
    )
    ros2_type_enum = next(
        (SEAGENT_TO_ROS2_TASK_TYPE.get(value) for value in candidates if value),
        None,
    )
    if ros2_type_enum is None:
        raw_type = next((value for value in candidates if value), None)
        raise ProtocolValidationError(f"不支持的 SEAgent task_type: {raw_type}")

    depth = _water_depth(intent)
    should_use_geo = use_geodetic or bool(
        (intent.get("location") or {}).get("use_geodetic", False)
    )
    target = details.get("target")

    pos_targets: List[Pose] = []
    params: List[float] = []
    frame_id = str(details.get("frame_id") or intent.get("frame_id") or "odom")

    if ros2_type_enum == TaskType.SEARCH_CABLE:
        start_point = _intent_coordinate(intent, details, "start_point")
        end_point = _intent_coordinate(intent, details, "end_point")
        waypoints = details.get("waypoints") or []
        if (start_point is None or end_point is None) and len(waypoints) >= 2:
            start_point, end_point = waypoints[0], waypoints[1]
        if start_point is None or end_point is None:
            raise ProtocolValidationError(
                "SEARCH_CABLE 必须同时提供 start_point 和 end_point"
            )
        pos_targets = [
            _coordinate_pose(start_point, depth, should_use_geo, origin, "start_point"),
            _coordinate_pose(end_point, depth, should_use_geo, origin, "end_point"),
        ]
    elif ros2_type_enum == TaskType.AUV_TASK:
        waypoints = details.get("waypoints") or []
        if not isinstance(waypoints, list) or not waypoints:
            raise ProtocolValidationError("AUV_TASK 必须提供非空 waypoints")
        pos_targets = [
            _coordinate_pose(wp, depth, should_use_geo, origin, f"waypoints[{index}]")
            for index, wp in enumerate(waypoints)
        ]
        auv_params = details.get("auv_params") or {}
        required_param_names = (
            ("speed_mps", "speed_ms"),
            ("dive_angle_rad", "dive_angle"),
            ("ascent_angle_rad", "ascend_angle"),
            ("auto_return",),
            ("return_depth_m", "return_depth"),
            ("return_speed_mps", "return_speed"),
        )
        values = []
        for aliases in required_param_names:
            value = next((auv_params.get(name) for name in aliases if name in auv_params), None)
            if value is None:
                raise ProtocolValidationError(
                    f"AUV_TASK 缺少参数 {aliases[0]}"
                )
            values.append(float(value))
        params = [
            values[0],
            values[1],
            values[2],
            values[3],
            values[4],
            values[5],
        ]
    elif ros2_type_enum == TaskType.CTRL_TASK:
        control = details.get("control") or {}
        if "device_id" not in control or "value" not in control:
            raise ProtocolValidationError("CTRL_TASK 必须提供 device_id 和 value")
        frame_id = ""
        params = [float(control["device_id"]), float(control["value"])]
    else:
        if target is None:
            raise ProtocolValidationError(
                f"{ros2_type_enum.name} 必须提供 target 位姿坐标"
            )
        pos_targets = [
            _coordinate_pose(target, depth, should_use_geo, origin, "target")
        ]
        if ros2_type_enum in _MANIPULATOR_TASK_TYPES:
            frame_id = ""

    cmd = SysTaskCmd(
        task_type=int(ros2_type_enum),
        task_id=task_id if task_id is not None else generate_task_id(),
        frame_id=frame_id,
        priority=int(intent.get("priority", 15)),
        pos_target=pos_targets,
        params=params,
        fail_stop=bool(intent.get("fail_stop", True)),
    )
    return validate_sys_task_cmd(cmd)


def build_task_manage_cmd(
    action: TaskManageAction,
    target_task_id: Optional[int] = None,
    cmd_task_id: Optional[int] = None,
) -> SysTaskCmd:
    """
    构造 TASK_MANAGE 指令（任务管理）。

    params[0] = action 编号
    params[1] = target_task_id（若 action 需要）
    """
    try:
        normalized_action = TaskManageAction(int(action))
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError(f"不支持的 TASK_MANAGE action: {action}") from exc
    target_required = normalized_action in _TARGET_REQUIRED_MANAGEMENT_ACTIONS
    if target_required and target_task_id is None:
        raise ProtocolValidationError(
            f"{normalized_action.name} 必须提供 target_task_id"
        )
    if not target_required and target_task_id is not None:
        raise ProtocolValidationError(
            f"{normalized_action.name} 不使用 target_task_id"
        )
    params = [float(normalized_action)]
    if target_task_id is not None:
        params.append(float(validate_uint32(target_task_id, "target_task_id")))
    cmd = SysTaskCmd(
        task_type=int(TaskType.TASK_MANAGE),
        task_id=cmd_task_id if cmd_task_id is not None else generate_task_id(),
        frame_id="",
        priority=0,  # 任务管理最高优先级
        pos_target=[],
        params=params,
        fail_stop=False,
    )
    return validate_sys_task_cmd(cmd)


def _legacy_task_payload(cmd: SysTaskCmd, intent: Optional[Dict[str, Any]] = None) -> dict:
    """Adapt a rich SysTaskCmd to simulator's single-target legacy message."""
    legacy_task = {
        int(TaskType.CLAMP_CABLE): 0,
        int(TaskType.SEARCH_CABLE): 1,
        int(TaskType.INSERT_PLUG): 2,
        int(TaskType.CLAMP_PIN): 2,
    }.get(int(cmd.task_type), 3)
    target = cmd.pos_target[0] if cmd.pos_target else Pose()
    yaw = 2.0 * math.atan2(float(target.qz), float(target.qw))
    # TaskIntent v2 payloads may carry details at the top level or under the
    # canonical ``task.details`` envelope.  Keep the legacy adapter lossless
    # for pose fields (especially yaw) in both layouts.
    intent_data = intent or {}
    details = (
        intent_data.get("task_details")
        or intent_data.get("details")
        or ((intent_data.get("task") or {}).get("details") if isinstance(intent_data.get("task"), dict) else {})
        or {}
    )
    hole_id = (intent or {}).get("hole_id", details.get("hole_id", 0))
    try:
        hole_id = max(0, min(255, int(hole_id)))
    except (TypeError, ValueError):
        hole_id = 0
    return {
        "task": legacy_task,
        "hole_id": hole_id,
        "x": float(target.x),
        "y": float(target.y),
        "z": float(target.z),
        "roll": 0.0,
        "pitch": 0.0,
        "yaw": yaw,
    }


# ============================================================================
# 生产级 rosbridge WebSocket 客户端
# ============================================================================

class RosbridgeClient:
    """
    SEAgent 生产级 rosbridge WebSocket 客户端。
    连接到支持船 Topside 的 rosbridge_server (ws://host:9090)。
    线程安全，支持订阅回调与任务状态轮询。
    """

    DEFAULT_PORT = 9090
    HEARTBEAT_INTERVAL = 15.0  # 秒

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 connect_timeout: float = 5.0):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._url = f"ws://{host}:{port}"
        self._ws: Optional[websocket.WebSocket] = None
        self._lock = threading.RLock()
        self._subscriptions: Dict[str, List[Callable]] = {}
        self._listener_thread: Optional[threading.Thread] = None
        self._running = False
        self._pending_service_calls: Dict[str, Dict[str, Any]] = {}
        self._advertised_topics: Dict[str, str] = {}
        self._last_transport_error: Optional[str] = None

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """建立 WebSocket 连接并启动后台监听线程"""
        with self._lock:
            if self._ws and self._ws.connected:
                return
            self._ws = websocket.create_connection(
                self._url, timeout=self.connect_timeout
            )
            logger.info(f"[RosbridgeClient] 已连接: {self._url}")
        self._running = True
        self._listener_thread = threading.Thread(
            target=self._listen_loop, daemon=True, name="rosbridge-listener"
        )
        self._listener_thread.start()

    def disconnect(self) -> None:
        """关闭连接与监听线程"""
        self._running = False
        with self._lock:
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
        logger.info("[RosbridgeClient] 已断开")

    def is_connected(self) -> bool:
        with self._lock:
            return bool(
                self._running
                and self._listener_thread
                and self._listener_thread.is_alive()
                and self._ws
                and self._ws.connected
            )

    @property
    def last_transport_error(self) -> Optional[str]:
        with self._lock:
            return self._last_transport_error

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    # ------------------------------------------------------------------
    # 底层 rosbridge v2.0 发送
    # ------------------------------------------------------------------

    def _send(self, message: dict) -> None:
        with self._lock:
            if not self._ws or not self._ws.connected:
                raise ConnectionError(f"rosbridge 未连接: {self._url}")
            self._ws.send(json.dumps(message))

    def call_service(
        self,
        service: str,
        args: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        """调用 rosbridge service，并按请求 ID 等待对应响应。"""
        call_id = f"seagent-{uuid.uuid4().hex}"
        pending = {"event": threading.Event(), "response": None}
        with self._lock:
            self._pending_service_calls[call_id] = pending
        try:
            self._send({
                "op": "call_service",
                "service": service,
                "args": args or {},
                "id": call_id,
            })
            wait_timeout = timeout if timeout is not None else self.connect_timeout
            if not pending["event"].wait(wait_timeout):
                raise TimeoutError(f"rosbridge service 调用超时: {service}")
            if pending["response"] is None:
                raise ConnectionError(f"rosbridge 连接在 service 响应前断开: {service}")
            response = pending["response"]
            if response.get("result") is False:
                raise RuntimeError(
                    response.get("values", {}).get("message")
                    or response.get("message")
                    or f"rosbridge service 调用失败: {service}"
                )
            return response
        finally:
            with self._lock:
                self._pending_service_calls.pop(call_id, None)

    @staticmethod
    def _normalize_ros_type(msg_type: str) -> str:
        return str(msg_type).replace("/msg/", "/")

    def advertise(self, topic: str, msg_type: str) -> None:
        """声明发布话题并通过 rosapi 验证 ROS typesupport 已实际加载。"""
        normalized = self._normalize_ros_type(msg_type)
        with self._lock:
            if self._advertised_topics.get(topic) == normalized:
                return

        self._send({"op": "advertise", "topic": topic, "type": msg_type})
        deadline = time.monotonic() + self.connect_timeout
        observed_type = ""
        while time.monotonic() < deadline:
            response = self.call_service(
                "/rosapi/topic_type", {"topic": topic}, timeout=self.connect_timeout
            )
            observed_type = str((response.get("values") or {}).get("type") or "")
            if self._normalize_ros_type(observed_type) == normalized:
                # Existing simulator topics already have a concrete ROS type.
                # rosbridge may not list itself as a publisher until after the
                # first publish, so type agreement is sufficient here.
                with self._lock:
                    self._advertised_topics[topic] = normalized
                return
            time.sleep(0.05)

        self._last_transport_error = (
            f"ROS 2 话题 {topic} 未成功声明为 {msg_type}; "
            f"rosapi 返回 {observed_type or '空类型'}，且未确认 rosbridge publisher"
        )
        raise RuntimeError(self._last_transport_error)

    def publish(self, topic: str, msg_type: str, payload: dict) -> None:
        """在 typesupport 预检通过后发布 ROS 消息。"""
        self.advertise(topic, msg_type)
        self._send({
            "op": "publish",
            "topic": topic,
            "type": msg_type,
            "msg": payload,
        })

    # ------------------------------------------------------------------
    # 任务下发接口
    # ------------------------------------------------------------------

    def publish_task_cmd(
        self,
        task_intent: Dict[str, Any],
        task_id: Optional[int] = None,
        use_geodetic: bool = False,
        origin: Optional[LocalOrigin] = None,
    ) -> int:
        """
        将 SEAgent TaskIntent v2 转换为 SysTaskCmd 并发布到 /task_cmd。
        返回实际使用的 task_id。
        """
        cmd = intent_to_syscmd(task_intent, task_id=task_id, use_geodetic=use_geodetic, origin=origin)
        if LEGACY_MSGMANAGEMENT:
            self._last_legacy_task_id = cmd.task_id
            self.publish(TASK_TOPIC, TASK_MESSAGE_TYPE, _legacy_task_payload(cmd, task_intent))
            logger.info(
                "[RosbridgeClient] 下发兼容任务: task=%s task_id=0x%X",
                _legacy_task_payload(cmd, task_intent)["task"], cmd.task_id,
            )
            return cmd.task_id
        self.publish(
            TASK_TOPIC,
            TASK_MESSAGE_TYPE,
            cmd.to_dict(),
        )
        logger.info(
            f"[RosbridgeClient] 下发任务: task_type={cmd.task_type} "
            f"task_id=0x{cmd.task_id:X} depth={-cmd.pos_target[0].z if cmd.pos_target else 'N/A'}m"
        )
        return cmd.task_id

    def publish_syscmd_raw(self, cmd: SysTaskCmd) -> None:
        """直接发布预构建的 SysTaskCmd 结构体"""
        validate_sys_task_cmd(cmd)
        if LEGACY_MSGMANAGEMENT:
            self._last_legacy_task_id = cmd.task_id
            self.publish(TASK_TOPIC, TASK_MESSAGE_TYPE, _legacy_task_payload(cmd))
            return
        self.publish(
            TASK_TOPIC,
            TASK_MESSAGE_TYPE,
            cmd.to_dict(),
        )

    # ------------------------------------------------------------------
    # 任务管理接口（TASK_MANAGE）
    # ------------------------------------------------------------------

    def task_manage(self, action: TaskManageAction,
                    target_task_id: Optional[int] = None) -> int:
        """
        发送任务管理指令（挂起/恢复/删除/查询/清除阻塞）。
        返回本条管理指令的 task_id。
        """
        cmd = build_task_manage_cmd(action, target_task_id)
        self.publish_syscmd_raw(cmd)
        action_name = action.name
        if target_task_id:
            logger.info(f"[RosbridgeClient] 任务管理: {action_name} -> task 0x{target_task_id:X}")
        else:
            logger.info(f"[RosbridgeClient] 任务管理: {action_name}")
        return cmd.task_id

    def suspend_task(self, target_task_id: int) -> int:
        """挂起指定任务"""
        return self.task_manage(TaskManageAction.SUSPEND, target_task_id)

    def resume_task(self, target_task_id: int) -> int:
        """恢复指定任务"""
        return self.task_manage(TaskManageAction.RESUME, target_task_id)

    def delete_task(self, target_task_id: int) -> int:
        """删除指定任务"""
        return self.task_manage(TaskManageAction.DELETE, target_task_id)

    def suspend_all(self) -> int:
        """挂起所有任务"""
        return self.task_manage(TaskManageAction.SUSPEND_ALL)

    def resume_all(self) -> int:
        """恢复所有任务"""
        return self.task_manage(TaskManageAction.RESUME_ALL)

    def delete_all(self) -> int:
        """删除所有任务"""
        return self.task_manage(TaskManageAction.DELETE_ALL)

    def clear_block(self) -> int:
        """清除当前阻塞状态（Emergency Clear）"""
        return self.task_manage(TaskManageAction.CLEAR_BLOCK)

    # ------------------------------------------------------------------
    # 灯/继电器控制接口（CTRL_TASK）
    # ------------------------------------------------------------------

    def ctrl_task(self, device_id: int, value: float,
                  priority: int = 15, fail_stop: bool = False) -> int:
        """
        发送设备控制指令（灯光/继电器）。
        device_id: 设备编号（如灯的编号=1）
        value: 设置值（如 PWM 占空比 50 = 50%）
        """
        cmd = SysTaskCmd(
            task_type=int(TaskType.CTRL_TASK),
            task_id=generate_task_id(),
            frame_id="",
            priority=priority,
            pos_target=[],
            params=[float(device_id), float(value)],
            fail_stop=fail_stop,
        )
        self.publish_syscmd_raw(cmd)
        logger.info(f"[RosbridgeClient] 设备控制: device={device_id} value={value}")
        return cmd.task_id

    # ------------------------------------------------------------------
    # 系统配置（SysConfig -> /task/sys_config）
    # ------------------------------------------------------------------

    def set_pilot_mode(self, mode: PilotMode) -> None:
        """设置飞行器控制模式"""
        if LEGACY_MSGMANAGEMENT:
            self.publish(
                CONFIG_TOPIC,
                CONFIG_MESSAGE_TYPE,
                {"task_type": 3, "task_src": 1, "plan_threshold": 0.0, "ctr_mode": int(mode)},
            )
            logger.info(f"[RosbridgeClient] 设置兼容控制模式: {mode.name}")
            return
        self.publish(
            CONFIG_TOPIC,
            CONFIG_MESSAGE_TYPE,
            {"ctr_mode": int(mode)},
        )
        logger.info(f"[RosbridgeClient] 设置控制模式: {mode.name}")

    # ------------------------------------------------------------------
    # 遥测订阅
    # ------------------------------------------------------------------

    def subscribe(self, topic: str, msg_type: str,
                  callback: Callable[[dict], None]) -> None:
        """订阅 ROS 2 话题，消息到达时异步回调"""
        with self._lock:
            if topic not in self._subscriptions:
                self._subscriptions[topic] = []
                self._send({
                    "op":    "subscribe",
                    "topic": topic,
                    "type":  msg_type,
                })
                logger.info(f"[RosbridgeClient] 已订阅: {topic}")
            self._subscriptions[topic].append(callback)

    def subscribe_from_config(
        self,
        callbacks: Optional[Dict[str, Callable[[dict], None]]] = None,
    ) -> List[str]:
        """Register every enabled subscription from ``ros2_protocol_spec.yaml``.

        The YAML is intentionally the source of topic/type wiring.  Callers may
        provide callbacks keyed by the YAML entry name; entries without a
        callback are still subscribed and safely discarded until a consumer is
        attached through one of the typed ``subscribe_*`` helpers.
        """
        callbacks = callbacks or {}
        registered: List[str] = []
        spec = load_protocol_spec()
        subscriptions = spec.get("subscriptions", {})
        if not isinstance(subscriptions, dict):
            return registered
        for name, entry in subscriptions.items():
            if not isinstance(entry, dict) or not entry.get("enabled", True):
                continue
            topic = entry.get("topic")
            msg_type = entry.get("type")
            if not isinstance(topic, str) or not isinstance(msg_type, str):
                logger.warning("忽略无效 ROS 订阅配置: %s", name)
                continue
            callback = callbacks.get(name)
            if callback is None:
                callback = lambda _msg: None
            self.subscribe(topic, msg_type, callback)
            registered.append(name)
        return registered

    def subscribe_system_status(self, callback: Callable[[dict], None]) -> None:
        """订阅 /task/system_status 遥测（ROV → 云端）"""
        if LEGACY_MSGMANAGEMENT:
            self.subscribe(
                STATUS_TOPIC,
                STATUS_MESSAGE_TYPE,
                lambda msg: callback({**msg, "_seagent_task_id": getattr(self, "_last_legacy_task_id", 0)}),
            )
            return
        self.subscribe(
            STATUS_TOPIC,
            STATUS_MESSAGE_TYPE,
            callback,
        )

    def subscribe_compressed_image(self, callback: Callable[[dict], None]) -> None:
        """订阅协议中保留既有拼写的 /vision/compressd_image。"""
        self.subscribe(COMPRESSED_IMAGE_TOPIC, COMPRESSED_IMAGE_TYPE, callback)

    def subscribe_image(self, callback: Callable[[dict], None]) -> None:
        """订阅 /vision/image 原始图像。"""
        self.subscribe(IMAGE_TOPIC, IMAGE_TYPE, callback)

    def subscribe_keypoints(self, callback: Callable[[dict], None]) -> None:
        """订阅 /vision/keypoints 视觉关键点话题"""
        self.subscribe(KEYPOINTS_TOPIC, KEYPOINTS_TYPE, callback)

    def subscribe_plug_hole(self, callback: Callable[[dict], None]) -> None:
        """订阅 /vision/plug_hole 采油树插头与插孔位姿。"""
        self.subscribe(PLUG_HOLE_TOPIC, PLUG_HOLE_TYPE, callback)

    def subscribe_depth_status(self, callback: Callable[[dict], None]) -> None:
        """订阅 /sensor/depth 深度传感器状态话题 (DepthStatus.msg)"""
        self.subscribe(
            "/sensor/depth",
            "sealien_ctrlpilot_msgmanagement/msg/DepthStatus",
            callback,
        )

    def subscribe_imu_dvl_status(self, callback: Callable[[dict], None]) -> None:
        """订阅 /sensor/imu_dvl IMU与DVL惯导传感器话题 (ImuDvlStatus.msg)"""
        self.subscribe(
            "/sensor/imu_dvl",
            "sealien_ctrlpilot_msgmanagement/msg/ImuDvlStatus",
            callback,
        )

    def subscribe_thruster_status(self, callback: Callable[[dict], None]) -> None:
        """订阅 /sensor/thruster_status 推进器工况状态话题 (ThrusterStatus.msg)"""
        self.subscribe(
            "/sensor/thruster_status",
            "sealien_ctrlpilot_msgmanagement/msg/ThrusterStatus",
            callback,
        )

    def subscribe_heartbeat(self, callback: Callable[[dict], None]) -> None:
        """订阅 /system/heartbeat 系统心跳状态话题 (HeartbeatStatus.msg)"""
        self.subscribe(
            "/system/heartbeat",
            "sealien_ctrlpilot_msgmanagement/msg/HeartbeatStatus",
            callback,
        )

    # ------------------------------------------------------------------
    # 特化指令下发 (JoystickCmd, ThrusterCmd, ConnectChristmasTreePlug, RoboticArmRequest)
    # ------------------------------------------------------------------

    def publish_joystick_cmd(self, msg_payload: dict) -> None:
        """发布手柄摇杆底层控制指令 (/cmd/joystick -> JoystickCmd.msg)"""
        self.publish("/cmd/joystick", "sealien_ctrlpilot_msgmanagement/msg/JoystickCmd", msg_payload)

    def publish_thruster_cmd(self, msg_payload: dict) -> None:
        """发布推进器推力底层控制指令 (/cmd/thruster -> ThrusterCmd.msg)"""
        self.publish("/cmd/thruster", "sealien_ctrlpilot_msgmanagement/msg/ThrusterCmd", msg_payload)

    def publish_robotic_arm_request(self, msg_payload: dict) -> None:
        """发布水下机械臂控制请求 (/cmd/robotic_arm -> RoboticArmRequest.msg)"""
        self.publish(
            "/cmd/robotic_arm",
            "sealien_ctrlpilot_msgmanagement/msg/RoboticArmRequest",
            msg_payload,
        )

    def publish_christmas_tree_plug_cmd(self, msg_payload: dict) -> None:
        """发布采油树水下插拔控制指令 (/cmd/christmas_tree_plug -> ConnectChristmasTreePlug.msg)"""
        self.publish(
            "/cmd/christmas_tree_plug",
            "sealien_ctrlpilot_msgmanagement/msg/ConnectChristmasTreePlug",
            msg_payload,
        )

    # ------------------------------------------------------------------
    # 后台监听循环
    # ------------------------------------------------------------------

    def _listen_loop(self) -> None:
        """后台线程：持续接收 rosbridge 推送消息并分发到订阅回调。
        关键：recv() 为阻塞调用，必须在锁外执行，否则与 _send() 死锁。
        """
        while self._running:
            try:
                # 仅在锁内获取 ws 引用和设置超时，立即释放锁
                with self._lock:
                    ws = self._ws
                    if not ws or not ws.connected:
                        break
                    ws.settimeout(1.0)

                # 在锁外执行阻塞 recv()
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception as e:
                    if self._running:
                        logger.warning(f"[RosbridgeClient] recv 异常: {e}")
                    break

                msg = json.loads(raw)
                op = msg.get("op")
                if op == "service_response":
                    call_id = msg.get("id")
                    with self._lock:
                        pending = self._pending_service_calls.get(call_id)
                        if pending is not None:
                            pending["response"] = msg
                            pending["event"].set()
                elif op == "publish":
                    topic = msg.get("topic", "")
                    payload = msg.get("msg", {})
                    with self._lock:
                        callbacks = list(self._subscriptions.get(topic, []))
                    for cb in callbacks:
                        try:
                            cb(payload)
                        except Exception as e:
                            logger.error(f"[RosbridgeClient] 回调异常 [{topic}]: {e}")
            except Exception as e:
                if self._running:
                    logger.warning(f"[RosbridgeClient] 监听循环异常: {e}")
                break
        with self._lock:
            self._running = False
            pending_calls = list(self._pending_service_calls.values())
        for pending in pending_calls:
            pending["event"].set()
        logger.info("[RosbridgeClient] 监听线程退出")
