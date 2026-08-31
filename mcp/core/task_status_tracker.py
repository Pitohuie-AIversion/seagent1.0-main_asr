"""
task_status_tracker.py
========================
SEAgent 任务执行状态追踪器

订阅 /task/system_status 话题，解析 TaskStatus[] task_list，
实时追踪任务生命周期：READY → PLAN → ONGOING → FINISH/FAIL

SysStatus 解析严格按 `sealien_ctrlpilot_llmbridge/msg/SysStatus` 约定进行，
`outside/sealien_ctrlpilot_msgmanagement-dev_rov-msg` 下的同名/同主题候选文件不作为主链路依据。

提供：
  - 同步等待任务完成（wait_for_finish）
  - 异步状态回调（on_status_change）
  - 最新遥测快照（latest_sys_status）
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .rosbridge_client import (
    RosbridgeClient,
    TaskStatus,
    TaskStatusItem,
)

logger = logging.getLogger(__name__)


@dataclass
class ROVTelemetry:
    """ROV 实时遥测快照（来自 /task/system_status）"""
    received_at: str = ""
    pose_x: float = 0.0
    pose_y: float = 0.0
    pose_z: float = 0.0   # 负值表示水深
    vel_x: float = 0.0
    vel_y: float = 0.0
    vel_z: float = 0.0
    altitude: float = 0.0
    ctr_mode: int = 0
    health: int = 0
    task_list: List[TaskStatusItem] = field(default_factory=list)
    raw_msg: Dict[str, Any] = field(default_factory=dict)

    @property
    def water_depth(self) -> float:
        """当前水深（正值，单位 m）"""
        return abs(self.pose_z)


class TaskStatusTracker:
    """
    任务执行状态追踪器。

    通过 RosbridgeClient 订阅 /task/system_status，
    解析 SysStatus.msg 中的 task_list 字段，
    仅按 `sealien_ctrlpilot_llmbridge` 主协议消息处理任务状态，
    提供任务生命周期查询与等待接口。

    用法：
        tracker = TaskStatusTracker(client)
        tracker.start()
        task_id = client.publish_task_cmd(intent)
        result = tracker.wait_for_finish(task_id, timeout=60.0)
    """

    def __init__(self, client: RosbridgeClient):
        self._client = client
        self._latest: Optional[ROVTelemetry] = None
        self._task_history: Dict[int, List[TaskStatusItem]] = {}
        self._lock = threading.Lock()
        self._status_callbacks: List[Callable[[ROVTelemetry], None]] = []
        self._change_callbacks: Dict[int, List[Callable[[TaskStatusItem], None]]] = {}

    def start(self) -> None:
        """开始订阅 /task/system_status"""
        self._client.subscribe_system_status(self._on_sys_status)
        logger.info("[TaskStatusTracker] 开始追踪任务状态")

    def stop(self) -> None:
        """停止追踪（取消订阅由 RosbridgeClient 管理）"""
        logger.info("[TaskStatusTracker] 停止追踪")

    # ------------------------------------------------------------------
    # 状态回调注册
    # ------------------------------------------------------------------

    def on_telemetry_update(self, callback: Callable[["ROVTelemetry"], None]) -> None:
        """注册遥测更新回调（每次收到 system_status 都触发）"""
        with self._lock:
            self._status_callbacks.append(callback)

    def on_task_status_change(
        self, task_id: int, callback: Callable[["TaskStatusItem"], None]
    ) -> None:
        """注册特定任务状态变化回调"""
        with self._lock:
            if task_id not in self._change_callbacks:
                self._change_callbacks[task_id] = []
            self._change_callbacks[task_id].append(callback)

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def latest_telemetry(self) -> Optional[ROVTelemetry]:
        """获取最新 ROV 遥测快照（线程安全）"""
        with self._lock:
            return self._latest

    def get_task_status(self, task_id: int) -> Optional[TaskStatusItem]:
        """查询指定 task_id 的最新状态（来自最近一次 system_status）"""
        with self._lock:
            if self._latest is None:
                return None
            for item in self._latest.task_list:
                if item.task_id == task_id:
                    return item
        return None

    def wait_for_finish(
        self,
        task_id: int,
        timeout: float = 120.0,
        poll_interval: float = 0.5,
    ) -> Optional[TaskStatusItem]:
        """
        阻塞等待任务达到 FINISH 或 FAIL 状态。

        Args:
            task_id: 要等待的任务 ID
            timeout: 最长等待时间（秒），超时返回 None
            poll_interval: 轮询间隔（秒）

        Returns:
            TaskStatusItem: 最终状态（FINISH 或 FAIL）
            None: 超时
        """
        deadline = time.monotonic() + timeout
        logger.info(f"[TaskStatusTracker] 等待任务 0x{task_id:X} 完成 (timeout={timeout}s)")

        while time.monotonic() < deadline:
            item = self.get_task_status(task_id)
            if item is not None and item.is_finished():
                status_name = TaskStatus(item.status).name
                logger.info(f"[TaskStatusTracker] 任务 0x{task_id:X} 已完成: {status_name}")
                return item
            time.sleep(poll_interval)

        logger.warning(f"[TaskStatusTracker] 任务 0x{task_id:X} 等待超时 ({timeout}s)")
        return None

    def is_task_active(self, task_id: int) -> bool:
        """任务是否仍在执行中（PLAN / ENTER / ONGOING）"""
        item = self.get_task_status(task_id)
        return item is not None and item.is_active()

    # ------------------------------------------------------------------
    # 内部回调（由 rosbridge 监听线程调用）
    # ------------------------------------------------------------------

    def _on_sys_status(self, msg: dict) -> None:
        """解析 SysStatus.msg 并更新内部状态"""
        try:
            telemetry = self._parse_sys_status(msg)
            with self._lock:
                self._latest = telemetry
                # 记录任务历史
                for item in telemetry.task_list:
                    if item.task_id not in self._task_history:
                        self._task_history[item.task_id] = []
                    self._task_history[item.task_id].append(item)
                callbacks = list(self._status_callbacks)
                change_cbs: Dict[int, List] = {}
                for item in telemetry.task_list:
                    if item.task_id in self._change_callbacks:
                        change_cbs[item.task_id] = list(self._change_callbacks[item.task_id])

            # 触发遥测回调
            for cb in callbacks:
                try:
                    cb(telemetry)
                except Exception as e:
                    logger.error(f"[TaskStatusTracker] 遥测回调异常: {e}")

            # 触发任务状态变化回调
            for item in telemetry.task_list:
                for cb in change_cbs.get(item.task_id, []):
                    try:
                        cb(item)
                    except Exception as e:
                        logger.error(f"[TaskStatusTracker] 状态回调异常: {e}")

        except Exception as e:
            logger.error(f"[TaskStatusTracker] 解析 SysStatus 失败: {e}")

    @staticmethod
    def _parse_sys_status(msg: dict) -> ROVTelemetry:
        """
        将 rosbridge 推送的 SysStatus.msg JSON 解析为 ROVTelemetry 结构体。

        SysStatus.msg 字段（参考 UI接口协议.md 第 3 节）：
          - pose: geometry_msgs/PoseStamped
          - twist: geometry_msgs/Twist
          - alt: float32
          - task_list: TaskStatus[]
          - ctr_mode: uint8
          - health: uint16
        """
        pose_stamped = msg.get("pose", {})
        pose = pose_stamped.get("pose", {}) or pose_stamped  # 兼容两层嵌套
        position = pose.get("position", {})
        twist = msg.get("twist", {})
        linear = twist.get("linear", {})

        # 解析 task_list
        task_items = []
        for t in msg.get("task_list", []):
            task_cmd = t.get("task", {})
            status_val = int(t.get("status", 0))
            try:
                status_name = TaskStatus(status_val).name
            except ValueError:
                status_name = f"UNKNOWN({status_val})"

            raw_task_id = task_cmd.get("task_id") if isinstance(task_cmd, dict) and "task_id" in task_cmd else t.get("task_id", 0)
            raw_task_type = task_cmd.get("task_type") if isinstance(task_cmd, dict) and "task_type" in task_cmd else t.get("task_type", 0)

            task_items.append(TaskStatusItem(
                task_id=int(raw_task_id or 0),
                task_type=int(raw_task_type or 0),
                status=status_val,
                status_name=status_name,
            ))

        return ROVTelemetry(
            received_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            pose_x=float(position.get("x", 0.0)),
            pose_y=float(position.get("y", 0.0)),
            pose_z=float(position.get("z", 0.0)),
            vel_x=float(linear.get("x", 0.0)),
            vel_y=float(linear.get("y", 0.0)),
            vel_z=float(linear.get("z", 0.0)),
            altitude=float(msg.get("alt", 0.0)),
            ctr_mode=int(msg.get("ctr_mode", 0)),
            health=int(msg.get("health", 0)),
            task_list=task_items,
            raw_msg=msg,
        )
