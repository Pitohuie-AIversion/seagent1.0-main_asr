"""
sealien_protocol.py
===================
SEAgent 协议辅助与高精度数学计算组件
从包 sealien_ctrlpilot_llmbridge-ros-mcp-server/ros_mcp/utils/sealien_protocol.py 提炼。
提供 WGS-84 高精度投影、偏航角/四元数推算及防重复提交守护机制。
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from typing import Any, Dict, Tuple, Optional

AI_TASK_ID_MIN = 0x80000
AI_TASK_ID_MAX = 0x8FFFF

TASK_MANAGE = 0
TASK_CLAMP_CABLE = 1
TASK_SEARCH_CABLE = 2
TASK_CLAMP_PIN = 3
TASK_INSERT_PLUG = 4
TASK_MOVE = 5
TASK_CTRL = 6
TASK_AUV = 10


class ProtocolValidationError(ValueError):
    """当任务无法满足协议约束时抛出"""


class DuplicateRequestError(ProtocolValidationError):
    """当相同请求被重复提交时抛出"""


@dataclass(frozen=True)
class LocalOrigin:
    """WGS-84 局部坐标原点 (缺省为南海某油田作业参考原点)"""
    latitude: float = 22.80169
    longitude: float = 113.52497
    altitude: float = 0.0


def validate_priority(priority: int) -> int:
    """校验并规范化 0--31 之间的优先级范围"""
    try:
        value = int(priority)
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError("priority 必须为 0 到 31 之间的整数") from exc
    if not 0 <= value <= 31:
        raise ProtocolValidationError("priority 必须在 0 到 31 之间")
    return value


def validate_task_id(task_id: int) -> int:
    """校验 AI 预留范围的任务 ID (0x80000..0x8ffff)"""
    try:
        value = int(task_id)
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError("task_id 必须为整数") from exc
    if not AI_TASK_ID_MIN <= value <= AI_TASK_ID_MAX:
        raise ProtocolValidationError("task_id 必须在 AI 预留范围 0x80000..0x8ffff 内")
    return value


def validate_uint32(value: int, field_name: str) -> int:
    """校验 uint32 范围内的参考 ID"""
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError(f"{field_name} 必须为整数") from exc
    if not 0 <= normalized <= 0xFFFFFFFF:
        raise ProtocolValidationError(f"{field_name} 必须在 uint32 范围内")
    return normalized


def geodetic_to_enu(
    latitude: float,
    longitude: float,
    altitude: float,
    origin: Optional[LocalOrigin] = None,
) -> Tuple[float, float, float]:
    """将 WGS-84 经纬度/高度坐标转换为局部 East/North/Up (米)。
    算法与 GeographicLib LocalCartesian 完全对齐。
    """
    if origin is None:
        origin = LocalOrigin()

    lat = float(latitude)
    lon = float(longitude)
    alt = float(altitude)
    if not -90.0 <= lat <= 90.0:
        raise ProtocolValidationError("latitude 必须在 -90 到 90 度之间")
    if not -180.0 <= lon <= 180.0:
        raise ProtocolValidationError("longitude 质必须在 -180 到 180 度之间")

    semi_major_axis = 6378137.0
    flattening = 1.0 / 298.257223563
    eccentricity_squared = flattening * (2.0 - flattening)

    def to_ecef(lat_deg: float, lon_deg: float, height: float) -> Tuple[float, float, float]:
        lat_rad = math.radians(lat_deg)
        lon_rad = math.radians(lon_deg)
        sin_lat = math.sin(lat_rad)
        cos_lat = math.cos(lat_rad)
        radius = semi_major_axis / math.sqrt(1.0 - eccentricity_squared * sin_lat * sin_lat)
        return (
            (radius + height) * cos_lat * math.cos(lon_rad),
            (radius + height) * cos_lat * math.sin(lon_rad),
            (radius * (1.0 - eccentricity_squared) + height) * sin_lat,
        )

    x, y, z = to_ecef(lat, lon, alt)
    origin_x, origin_y, origin_z = to_ecef(origin.latitude, origin.longitude, origin.altitude)
    dx, dy, dz = x - origin_x, y - origin_y, z - origin_z

    origin_lat = math.radians(origin.latitude)
    origin_lon = math.radians(origin.longitude)
    east = -math.sin(origin_lon) * dx + math.cos(origin_lon) * dy
    north = (
        -math.sin(origin_lat) * math.cos(origin_lon) * dx
        - math.sin(origin_lat) * math.sin(origin_lon) * dy
        + math.cos(origin_lat) * dz
    )
    up = (
        math.cos(origin_lat) * math.cos(origin_lon) * dx
        + math.cos(origin_lat) * math.sin(origin_lon) * dy
        + math.sin(origin_lat) * dz
    )
    return east, north, up


def geodetic_to_odom_position(
    latitude: float,
    longitude: float,
    water_depth_m: float,
    origin: Optional[LocalOrigin] = None,
) -> Tuple[float, float, float]:
    """将经纬度和水深转换为 odom 局部坐标 (east, north, -water_depth_m)"""
    try:
        depth = float(water_depth_m)
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError("water_depth_m 必须为数值") from exc
    if depth < 0:
        raise ProtocolValidationError("water_depth_m 必须是非负数")

    east, north, _ = geodetic_to_enu(latitude, longitude, -depth, origin)
    return east, north, -depth


def pose(x: float, y: float, z: float, yaw_rad: float = 0.0) -> Dict[str, Any]:
    """构建标准 ROS geometry_msgs/Pose 字典格式，包含纯 yaw 角推算出来的四元数"""
    yaw = float(yaw_rad)
    return {
        "position": {"x": float(x), "y": float(y), "z": float(z)},
        "orientation": {
            "x": 0.0,
            "y": 0.0,
            "z": math.sin(yaw / 2.0),
            "w": math.cos(yaw / 2.0),
        },
    }


def yaw_between(current_position: Dict[str, Any], target_x: float, target_y: float) -> float:
    """计算当前点到目标点 (target_x, target_y) 的切线方位角 (radians)"""
    try:
        current_x = float(current_position.get("x", 0.0))
        current_y = float(current_position.get("y", 0.0))
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError("当前位置姿态包含无效数据") from exc
    return math.atan2(float(target_y) - current_y, float(target_x) - current_x)


class TaskMessageGuard:
    """按任务 ID 及其关键负载计算签名哈希，防止重复下发完全相同的任务"""

    _COMPARISON_FIELDS = (
        "task_type",
        "task_id",
        "frame_id",
        "priority",
        "pos_target",
        "params",
    )

    def __init__(self) -> None:
        self._last_signature_by_task_id: Dict[int, str] = {}
        self._lock = threading.Lock()

    @classmethod
    def _signature(cls, message: dict) -> Tuple[int, str]:
        if not isinstance(message, dict):
            raise ProtocolValidationError("任务消息必须是字典结构")
        try:
            comparable = {field: message[field] for field in cls._COMPARISON_FIELDS}
        except KeyError as exc:
            raise ProtocolValidationError(f"任务消息缺少必要字段 '{exc.args[0]}'") from exc

        task_id = int(comparable["task_id"])
        comparable["task_id"] = task_id
        try:
            return task_id, json.dumps(
                comparable, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise ProtocolValidationError("任务消息包含不可序列化字段") from exc

    def claim(self, message: dict) -> None:
        """注册并锁止校验。若发现与上次提交完全一致则抛出 DuplicateRequestError"""
        task_id, signature = self._signature(message)
        with self._lock:
            if self._last_signature_by_task_id.get(task_id) == signature:
                raise DuplicateRequestError(
                    f"task_id {task_id} 下发了与上一次完全相同的载荷，自动去重阻断"
                )
            self._last_signature_by_task_id[task_id] = signature


class RequestIdGuard:
    """非任务指令 (如控制模式切换) 的唯一 request_id 去重守护器"""

    def __init__(self) -> None:
        self._request_ids: set[str] = set()
        self._lock = threading.Lock()

    def claim(self, request_id: str) -> None:
        normalized_request_id = str(request_id).strip()
        if not normalized_request_id:
            raise ProtocolValidationError("确认的命令需要提供非空 request_id")
        with self._lock:
            if normalized_request_id in self._request_ids:
                raise DuplicateRequestError(
                    f"request_id '{normalized_request_id}' 已经被提交过，自动去重阻断"
                )
            self._request_ids.add(normalized_request_id)
