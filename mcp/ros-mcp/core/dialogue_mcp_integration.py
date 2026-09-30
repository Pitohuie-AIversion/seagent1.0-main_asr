"""
dialogue_mcp_integration.py
==============================
SEAgent 对话管理器与 MCP ROS 2 通信闭环桥接集成器

用于显式下发 DialogueManager 已归档的 TaskIntent，并可选等待机器人终态。
挂载桥接引用不会注册自动发送回调；Web 对话的自动派发由 routes_chat 处理。

能力：
1. `attach_mcp_bridge(dialogue_manager, bridge_service)`:
   挂载 MCP 桥接服务引用，供后续显式派发调用使用。
2. `dispatch_dialogue_result(dialogue_manager, bridge_service)`:
   对已处于 done 阶段的 DialogueManager，手动触发其 final_result 的下发与闭环跟踪。
"""

import logging
from typing import Any, Dict, Optional
from .bridge_service import SEAgentMCPBridgeService
from .task_status_tracker import TaskStatusItem

logger = logging.getLogger(__name__)


def attach_mcp_bridge(dialogue_manager: Any, bridge_service: SEAgentMCPBridgeService) -> None:
    """
    将 SEAgentMCPBridgeService 绑定到 DialogueManager 实例。
    绑定后，DialogueManager 会持有 mcp_bridge 引用。
    本函数不发送任务；显式调用 dispatch_dialogue_result 后才执行派发检查。
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
        wait_finish: SENT 时是否等待机器人终态 (FINISH 或 FAIL)
        timeout: 等待机器人终态的超时时间（秒），不包含派发检查耗时

    Returns:
        Dict[str, Any]: {
            "status": "success" | "pending" | "error",
            "task_id": int | None,
            "final_status_item": TaskStatusItem | None,
            "message": str,
            "ros2_dispatch": dict
        }

        status 表示派发结果：SENT 映射为 success，SCHEDULED/UNKNOWN 为 pending，
        其余为 error。success 不表示机器人执行成功；FINISH/FAIL 见 final_status_item，
        未等待或等待超时时该值为 None。详细门禁原因保留在 ros2_dispatch 中。

    Raises:
        RuntimeError: 未提供且未挂载桥接服务。
        ValueError: 会话不处于 done，或没有 final_result。
    """
    service = bridge_service or getattr(dialogue_manager, "mcp_bridge", None)
    if service is None:
        raise RuntimeError("未提供有效的 SEAgentMCPBridgeService 实例，且 DialogueManager 未绑定 mcp_bridge。")

    if dialogue_manager.phase != "done" or not dialogue_manager.final_result:
        raise ValueError(f"DialogueManager 尚未处于 done 阶段（当前阶段: {dialogue_manager.phase}），无法下发。")

    from contextlib import nullcontext
    from src.dispatch.task_dispatch import dispatch_completed_task, save_dispatch_history
    with getattr(dialogue_manager, "_session_lock", nullcontext()):
        dispatch = dispatch_completed_task(dialogue_manager, service)
        if hasattr(dialogue_manager, "session_id"):
            try:
                save_dispatch_history(dialogue_manager)
            except Exception:
                logger.exception("保存 ROS 2 下发结果失败")
    if dispatch["state"] != "SENT":
        return {"status": "pending" if dispatch["state"] in {"SCHEDULED", "UNKNOWN"} else "error",
                "task_id": dispatch.get("task_id"), "final_status_item": None,
                "message": dispatch["message"], "ros2_dispatch": dispatch}
    task_id = dispatch["task_id"]

    final_item = None
    if wait_finish:
        final_item = service.wait_for_task_finish(task_id, timeout=timeout)

    return {
        "status": "success",
        "task_id": task_id,
        "final_status_item": final_item,
        "message": f"TaskIntent 成功下发至 ROS 2 (task_id=0x{task_id:X})",
        "ros2_dispatch": dispatch,
    }
