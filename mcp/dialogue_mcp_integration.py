"""
dialogue_mcp_integration.py
==============================
SEAgent 对话管理器与 MCP ROS 2 通信闭环桥接集成器

用于在 DialogueManager 完成确认发布（进入 done 阶段）时，
自动触发 MCP 桥接服务将落盘的 TaskIntent 下发给水下机器人 ROS 2 控制系统。

能力：
1. `attach_mcp_bridge(dialogue_manager, bridge_service)`:
   挂载 MCP 桥接服务到 DialogueManager，实现自动下发与状态追踪。
2. `dispatch_dialogue_result(dialogue_manager, bridge_service)`:
   对已处于 done 阶段的 DialogueManager，手动触发其 final_result 的下发与闭环跟踪。
"""

import logging
from typing import Any, Dict, Optional
from bridge_service import SEAgentMCPBridgeService
from task_status_tracker import TaskStatusItem

logger = logging.getLogger(__name__)


def attach_mcp_bridge(dialogue_manager: Any, bridge_service: SEAgentMCPBridgeService) -> None:
    """
    将 SEAgentMCPBridgeService 绑定到 DialogueManager 实例。
    绑定后，DialogueManager 会持有 mcp_bridge 引用，
    并在确认发布成功后记录已下发的 ROS 2 task_id。
    """
    dialogue_manager.mcp_bridge = bridge_service
    logger.info("[DialogueMCPIntegration] 成功挂载 MCP Bridge 到 DialogueManager")


def dispatch_dialogue_result(
    dialogue_manager: Any,
    bridge_service: Optional[SEAgentMCPBridgeService] = None,
    wait_finish: bool = False,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """
    对 DialogueManager 生成的 final_result 进行 MCP 下发与可选的状态跟踪。

    Args:
        dialogue_manager: DialogueManager 实例（需处于 done 阶段）
        bridge_service: SEAgentMCPBridgeService 实例（为空时取 dialogue_manager.mcp_bridge）
        wait_finish: 是否阻塞等待机器人侧执行完成 (FINISH)
        timeout: 最长等待超时时间（秒）

    Returns:
        Dict[str, Any]: {
            "status": "success" | "error",
            "task_id": int,
            "final_status_item": TaskStatusItem | None,
            "message": str
        }
    """
    service = bridge_service or getattr(dialogue_manager, "mcp_bridge", None)
    if service is None:
        raise RuntimeError("未提供有效的 SEAgentMCPBridgeService 实例，且 DialogueManager 未绑定 mcp_bridge。")

    if dialogue_manager.phase != "done" or not dialogue_manager.final_result:
        raise ValueError(f"DialogueManager 尚未处于 done 阶段（当前阶段: {dialogue_manager.phase}），无法下发。")

    task_intent = dialogue_manager.final_result
    task_id = service.dispatch_intent(task_intent)
    dialogue_manager.dispatched_ros2_task_id = task_id

    final_item = None
    if wait_finish:
        final_item = service.wait_for_task_finish(task_id, timeout=timeout)

    return {
        "status": "success",
        "task_id": task_id,
        "final_status_item": final_item,
        "message": f"TaskIntent 成功下发至 ROS 2 (task_id=0x{task_id:X})",
    }
