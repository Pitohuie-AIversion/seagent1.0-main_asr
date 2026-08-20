"""
rosbridge_client.py
====================
SEAgent 生产级 rosbridge WebSocket 客户端

基于 `sealien_ctrlpilot_msgmanagement` 内部消息协议，通过 rosbridge v2.0
WebSocket 协议直连支持船 Topside 网关，实现：

1. 任务下发   publish_task_cmd(intent)         -> /task_cmd
2. 系统配置   publish_sys_config(mode)          -> /task/sys_config
3. 任务管理   task_manage(action, task_id)      -> /task_cmd (TASK_MANAGE)
4. 灯/继电器  ctrl_task(device_id, value)       -> /task_cmd (CTRL_TASK)
5. AUV 任务   auv_task(waypoints, params)        -> /task_cmd (AUV_TASK)
6. 遥测订阅   subscribe_system_status(callback)  <- /task/system_status
7. 视觉信息   subscribe_keypoints(callback)      <- /vision/keypoints

内部协议参考：outside/sealien_ctrlpilot_llmbridge-dev_ros2/UI接口协议.md
"""

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional

import websocket

logger = logging.getLogger(__name__)


# ============================================================================
# 内部协议常量 (sealien_ctrlpilot_msgmanagement)
# ============================================================================

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
    AUTOHEIGHT = 5       # 定高
    AUTODIRECTION = 6    # 定向
    AUTOHOLD1 = 7        # x/y/z 位置保持（定深）
    AUTOHOLD2 = 8        # x/y/z 位置保持（定高）
    MISSION1 = 9         # 路径跟踪（定深）
    MISSION2 = 10        # 路径跟踪（定高）


# SEAgent TaskIntent task_type -> SysTaskCmd TaskType 映射
SEAGENT_TO_ROS2_TASK_TYPE: Dict[str, TaskType] = {
    "pipeline_inspection":  TaskType.SEARCH_CABLE,   # 巡缆/巡线
    "cable_burial":         TaskType.CLAMP_CABLE,    # 夹缆/埋设
    "cable_pin":            TaskType.CLAMP_PIN,      # 夹销
    "valve_operation":      TaskType.INSERT_PLUG,    # 阀门/插销操作
    "tree_valve_operation": TaskType.INSERT_PLUG,    # 采油树阀门
    "underwater_move":      TaskType.MOVE_TASK,      # 移动任务
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
    """生成唯一 AI 任务 ID（0x80001 ~ 0x8FFFF 循环）"""
    global _task_id_counter
    with _task_id_lock:
        _task_id_counter = (_task_id_counter + 1) % 0xFFFF
        return _AI_TASK_ID_BASE + max(_task_id_counter, 1)


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

def intent_to_syscmd(intent: Dict[str, Any], task_id: Optional[int] = None) -> SysTaskCmd:
    """
    将 SEAgent 落盘的 TaskIntent v2 字典转换为 SysTaskCmd 结构体。

    支持任务类型：
      - 巡缆/管道巡检 (SEARCH_CABLE=2)：pos_target[0]=起点, pos_target[1]=终点
      - 夹缆/电缆埋设 (CLAMP_CABLE=1)：pos_target[0]=夹点
      - 采油树阀门/插销 (INSERT_PLUG=4)：pos_target[0]=孔位位姿
      - 移动任务 (MOVE_TASK=5)：pos_target[0]=目标点
      - 灯光控制 (CTRL_TASK=6)：params=[设备编号, 设置值]
      - AUV 任务 (AUV_TASK=10)：pos_target=航线关键点, params=[速度, 下潜角, ...]
    """
    task_type_str = intent.get("task_type", "underwater_move")
    ros2_type = SEAGENT_TO_ROS2_TASK_TYPE.get(task_type_str, TaskType.MOVE_TASK)

    # 提取目标位置（v2 优先，兼容 legacy）
    target_v2 = (intent.get("task", {}).get("details", {}).get("target", {}))
    target_legacy = intent.get("target", {}).get("coordinates", {})
    target = target_v2 if target_v2.get("latitude") is not None else target_legacy

    # 提取水深（v2 优先）
    depth_v2 = intent.get("location", {}).get("water_depth_m")
    depth_legacy = intent.get("target", {}).get("depth")
    depth = float(depth_v2 if depth_v2 is not None else (depth_legacy or 0.0))

    # 默认目标位姿：(经度→x, 纬度→y, -水深→z)
    default_pose = Pose(
        x=float(target.get("longitude", 0.0)),
        y=float(target.get("latitude",  0.0)),
        z=-depth,
    )

    # ---- 任务专属参数拼装 ----
    pos_targets: List[Pose] = []
    params: List[float] = []

    if ros2_type == TaskType.SEARCH_CABLE:
        # 巡缆：起点 + 终点，两个 pos_target
        waypoints = intent.get("task", {}).get("details", {}).get("waypoints", [])
        if len(waypoints) >= 2:
            for wp in waypoints[:2]:
                pos_targets.append(Pose(
                    x=float(wp.get("longitude", 0.0)),
                    y=float(wp.get("latitude",  0.0)),
                    z=-float(wp.get("depth", depth)),
                ))
        else:
            pos_targets = [default_pose, default_pose]

    elif ros2_type == TaskType.AUV_TASK:
        # AUV：多航线关键点 + 6 个 params
        waypoints = intent.get("task", {}).get("details", {}).get("waypoints", [])
        if waypoints:
            for wp in waypoints:
                pos_targets.append(Pose(
                    x=float(wp.get("longitude", 0.0)),
                    y=float(wp.get("latitude",  0.0)),
                    z=-float(wp.get("depth", depth)),
                ))
        else:
            pos_targets = [default_pose]
        auv_params = intent.get("task", {}).get("details", {}).get("auv_params", {})
        params = [
            float(auv_params.get("speed_ms",      1.5)),
            float(auv_params.get("dive_angle",    0.3)),
            float(auv_params.get("ascend_angle",  0.3)),
            float(auv_params.get("auto_return",   0.0)),
            float(auv_params.get("return_depth",  5.0)),
            float(auv_params.get("return_speed",  1.0)),
        ]

    elif ros2_type == TaskType.CTRL_TASK:
        # 灯光/继电器控制：不用 pos_target，用 params[0]=设备编号, params[1]=设置值
        ctrl = intent.get("task", {}).get("details", {}).get("control", {})
        pos_targets = []
        params = [
            float(ctrl.get("device_id", 1)),
            float(ctrl.get("value",     0)),
        ]

    else:
        # 默认：单点位姿任务（CLAMP_CABLE/CLAMP_PIN/INSERT_PLUG/MOVE_TASK）
        pos_targets = [default_pose]
        params = [depth, float(intent.get("task", {}).get("details", {}).get("speed_ms", 1.5))]

    return SysTaskCmd(
        task_type=int(ros2_type),
        task_id=task_id if task_id is not None else generate_task_id(),
        frame_id="odom",
        priority=int(intent.get("priority", 15)),
        pos_target=pos_targets,
        params=params,
        fail_stop=bool(intent.get("fail_stop", True)),
    )


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
    params = [float(action)]
    if target_task_id is not None:
        params.append(float(target_task_id))
    return SysTaskCmd(
        task_type=int(TaskType.TASK_MANAGE),
        task_id=cmd_task_id if cmd_task_id is not None else generate_task_id(),
        frame_id="",
        priority=0,  # 任务管理最高优先级
        pos_target=[],
        params=params,
        fail_stop=False,
    )


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
            return bool(self._ws and self._ws.connected)

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

    # ------------------------------------------------------------------
    # 任务下发接口
    # ------------------------------------------------------------------

    def publish_task_cmd(self, task_intent: Dict[str, Any],
                         task_id: Optional[int] = None) -> int:
        """
        将 SEAgent TaskIntent v2 转换为 SysTaskCmd 并发布到 /task_cmd。
        返回实际使用的 task_id。
        """
        cmd = intent_to_syscmd(task_intent, task_id=task_id)
        self._send({
            "op":    "publish",
            "topic": "/task_cmd",
            "type":  "sealien_ctrlpilot_msgmanagement/SysTaskCmd",
            "msg":   cmd.to_dict(),
        })
        logger.info(
            f"[RosbridgeClient] 下发任务: task_type={cmd.task_type} "
            f"task_id=0x{cmd.task_id:X} depth={-cmd.pos_target[0].z if cmd.pos_target else 'N/A'}m"
        )
        return cmd.task_id

    def publish_syscmd_raw(self, cmd: SysTaskCmd) -> None:
        """直接发布预构建的 SysTaskCmd 结构体"""
        self._send({
            "op":    "publish",
            "topic": "/task_cmd",
            "type":  "sealien_ctrlpilot_msgmanagement/SysTaskCmd",
            "msg":   cmd.to_dict(),
        })

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
        self._send({
            "op":    "publish",
            "topic": "/task/sys_config",
            "type":  "sealien_ctrlpilot_msgmanagement/SysConfig",
            "msg":   {"ctr_mode": int(mode)},
        })
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

    def subscribe_system_status(self, callback: Callable[[dict], None]) -> None:
        """订阅 /task/system_status 遥测（ROV → 云端）"""
        self.subscribe(
            "/task/system_status",
            "sealien_ctrlpilot_msgmanagement/SysStatus",
            callback,
        )

    def subscribe_keypoints(self, callback: Callable[[dict], None]) -> None:
        """订阅 /vision/keypoints 视觉关键点话题"""
        self.subscribe(
            "/vision/keypoints",
            "sealien_ctrlpilot_msgmanagement/Keypoints",
            callback,
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
                if op == "publish":
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
        logger.info("[RosbridgeClient] 监听线程退出")
