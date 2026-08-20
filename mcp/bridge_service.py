"""
bridge_service.py
==================
SEAgent 云端自动化 MCP 桥接与调度服务 (SEAgent MCP Bridge & Dispatcher Service)

无缝无缝衔接 DialogueManager / TaskIntentBuilder 与支持船 Topside rosbridge 网关：

1. **自动任务下发 (Auto-Dispatch)**：
   在 DialogueManager 完成 TaskIntent 原子落盘后，自动解析 Intent 并通过 WebSocket
   发送 SysTaskCmd 到 /task_cmd。

2. **自动状态同步 (Auto-Sync)**：
   后台持续或单次从 /task/system_status 获取 ROV 实时姿态、水深、电量、在线状态，
   原子更新 SEAgent 状态中心 `RobotStateInfo`。

3. **任务生命周期追踪 (Status Tracking)**：
   自动监控下发任务在机器人控制器侧的状态推进（READY → PLAN → ONGOING → FINISH/FAIL），
   提供安全完成或失败等待闭环。
"""

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from rosbridge_client import (
    RosbridgeClient,
    TaskManageAction,
    SysTaskCmd,
    intent_to_syscmd,
)
from task_status_tracker import TaskStatusTracker, ROVTelemetry, TaskStatusItem

logger = logging.getLogger(__name__)


class SEAgentMCPBridgeService:
    """
    SEAgent 云端 MCP 桥接服务。

    用法示例：
        bridge = SEAgentMCPBridgeService(host="127.0.0.1", port=9090)
        bridge.start()

        # 1. 发布任务
        task_id = bridge.dispatch_intent(final_task_intent_dict)

        # 2. 阻塞等待完成
        status = bridge.wait_for_task_finish(task_id, timeout=60.0)

        # 3. 关闭
        bridge.stop()
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9090,
        state_info: Optional[Any] = None,
        connect_timeout: float = 5.0,
    ):
        self.host = host
        self.port = port
        self.state_info = state_info
        self.client = RosbridgeClient(host=host, port=port, connect_timeout=connect_timeout)
        self.tracker = TaskStatusTracker(self.client)
        self._auto_sync_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 服务生命周期管理
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动 WebSocket 连接与状态追踪器"""
        with self._lock:
            if self._running:
                return
            self.client.connect()
            self.tracker.start()
            self._running = True

            # 注册遥测更新回调，自动同步到 RobotStateInfo
            if self.state_info is not None:
                self.tracker.on_telemetry_update(self._sync_telemetry_to_state_info)

            logger.info(f"[MCPBridgeService] 服务已启动 (ws://{self.host}:{self.port})")

    def stop(self) -> None:
        """关闭服务与底层连接"""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self.tracker.stop()
            self.client.disconnect()
            logger.info("[MCPBridgeService] 服务已停止")

    def is_healthy(self) -> bool:
        """服务是否正常连接且正在运行"""
        return self._running and self.client.is_connected()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # ------------------------------------------------------------------
    # 核心业务接口 1：任务下发
    # ------------------------------------------------------------------

    def dispatch_intent(self, task_intent: Dict[str, Any]) -> int:
        """
        下发 SEAgent 的 TaskIntent v2 字典到机器人 ROS 2 控制系统。

        Args:
            task_intent: DialogueManager 导出的 TaskIntent v2 字典

        Returns:
            int: 实际分配给 ROS 2 侧的 task_id (0x8XXXX)
        """
        if not self.is_healthy():
            raise RuntimeError(f"MCPBridgeService 未连接到支持船网关 (ws://{self.host}:{self.port})")

        task_id = self.client.publish_task_cmd(task_intent)
        logger.info(
            f"[MCPBridgeService] 成功下发 TaskIntent: task_type={task_intent.get('task_type')} "
            f"-> ROS2 task_id=0x{task_id:X}"
        )
        return task_id

    # ------------------------------------------------------------------
    # 核心业务接口 2：任务管理指令
    # ------------------------------------------------------------------

    def suspend_task(self, task_id: int) -> int:
        """挂起指定任务"""
        return self.client.suspend_task(task_id)

    def resume_task(self, task_id: int) -> int:
        """恢复指定任务"""
        return self.client.resume_task(task_id)

    def delete_task(self, task_id: int) -> int:
        """删除指定任务"""
        return self.client.delete_task(task_id)

    def emergency_clear_block(self) -> int:
        """紧急清除阻塞状态"""
        return self.client.clear_block()

    def control_device(self, device_id: int, value: float) -> int:
        """控制设备（开关灯/继电器）"""
        return self.client.ctrl_task(device_id=device_id, value=value)

    # ------------------------------------------------------------------
    # 核心业务接口 3：生命周期追踪
    # ------------------------------------------------------------------

    def wait_for_task_finish(
        self, task_id: int, timeout: float = 120.0
    ) -> Optional[TaskStatusItem]:
        """
        阻塞等待任务达到 FINISH 或 FAIL 状态。

        Returns:
            TaskStatusItem: 任务最终状态
            None: 超时
        """
        return self.tracker.wait_for_finish(task_id=task_id, timeout=timeout)

    def get_task_status(self, task_id: int) -> Optional[TaskStatusItem]:
        """获取任务当前执行状态"""
        return self.tracker.get_task_status(task_id)

    # ------------------------------------------------------------------
    # 内部遥测同步实现
    # ------------------------------------------------------------------

    def _sync_telemetry_to_state_info(self, telemetry: ROVTelemetry) -> None:
        """将 ROVTelemetry 同步到 SEAgent 的 RobotStateInfo"""
        if self.state_info is None:
            return

        # 1. 支持标准单机 Pose / Alt
        unit_id = "WROV-250-001"  # 默认工作级 ROV
        params = {
            "status": "online" if telemetry.health == 0 else "degraded",
            "water_depth": telemetry.water_depth,
            "altitude": telemetry.altitude,
            "ctr_mode": telemetry.ctr_mode,
            "update_timestamp": telemetry.received_at,
            "updated_at": telemetry.received_at,
        }
        try:
            self.state_info.set_status(equipment_name=unit_id, params=params)
        except Exception as e:
            logger.debug(f"[MCPBridgeService] 同步 {unit_id} 状态跳过: {e}")

        # 2. 如果包含 Topside 扩展的多机 fleet_status 字典，逐一同步
        raw_msg = getattr(telemetry, "raw_msg", {}) or {}
        fleet = raw_msg.get("fleet_status", {})
        for robot_id, rdata in fleet.items():
            try:
                self.state_info.set_status(equipment_name=robot_id, params={
                    "status": "online" if rdata.get("online") else "offline",
                    "water_depth": rdata.get("current_depth", 0.0),
                    "battery_level": rdata.get("battery_percentage", 100.0),
                    "update_timestamp": telemetry.received_at,
                    "updated_at": telemetry.received_at,
                })
            except Exception:
                pass
