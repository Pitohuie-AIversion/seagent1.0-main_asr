"""
src/handlers/task_commit.py - 任务确认与持久化提交生命周期处理器

职责：
1. 已归档任务（done）防护（杜绝重复发布、杜绝非法就地篡改）；
2. 最终确认阶段（confirming）交互决策；
3. 原子发布持久化提交（TaskIntentBuilder, 锁机制与事务隔离）；
4. 会话历史快照与 ROS2 调度集成。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import BaseDialogueHandler, DialogueContext, HandlerResult

logger = logging.getLogger(__name__)


class TaskCommitHandler(BaseDialogueHandler):
    """任务确认与持久化提交生命周期处理器"""

    def can_handle(self, ctx: DialogueContext) -> bool:
        """
        判断是否处于提交相关阶段：
        1. phase == 'done' 时的重复确认或原地修改请求；
        2. phase == 'confirming' 时的最终发布确认或取消/修改；
        3. awaiting_final_confirm 标志位激活时。
        """
        phase = self.manager.phase
        user_message = ctx.user_message

        if phase == "done":
            if (
                self.manager._is_confirmation_only(user_message)
                or self.manager._is_final_publish_confirmation(user_message)
                or self.manager._user_requested_modification(user_message)
            ):
                return True

        if phase == "confirming" or getattr(self.manager, "awaiting_final_confirm", False):
            if (
                self.manager._is_confirmation_only(user_message)
                or self.manager._is_final_publish_confirmation(user_message)
            ):
                return True

        return False

    def handle(self, ctx: DialogueContext) -> HandlerResult:
        """执行已完成任务防护或最终确认发布"""
        phase = self.manager.phase
        user_message = ctx.user_message
        request_id = ctx.request_id

        # 1. phase == "done" 防护
        if phase == "done":
            # 重复确认防护
            if (
                self.manager._is_confirmation_only(user_message)
                or self.manager._is_final_publish_confirmation(user_message)
            ):
                self.manager._switch_dialogue_mode(
                    "task_collection",
                    source="user_confirmation",
                    reason="已发布任务重复确认",
                )
                intent_id = self.manager.task_state.get("intent_id") or self.manager._last_built_json.get("intent_id")
                intent_detail = f"（intent_id: {intent_id}）" if intent_id else ""
                reply = f"任务已发布成功{intent_detail}，无需重复发布。"
                self.manager.conversation_history.append({"role": "user", "content": user_message})
                self.manager.conversation_history.append({"role": "assistant", "content": reply})
                return HandlerResult.success(reply=reply)

            # 就地篡改防护
            if self.manager._user_requested_modification(user_message):
                self.manager._switch_dialogue_mode(
                    "task_collection",
                    source="user_modification",
                    reason="已发布任务原地修改拒绝",
                )
                intent_id = self.manager.task_state.get("intent_id") or (
                    self.manager._last_built_json.get("intent_id")
                    if isinstance(self.manager._last_built_json, dict)
                    else None
                )
                intent_detail = f"（任务ID: {intent_id}）" if intent_id else ""
                reply = f"当前任务已正式确认发布{intent_detail}并归档，无法就地修改参数。如需调整，请点击“重新开始”创建新任务，或提交工单变更申请。"
                self.manager.conversation_history.append({"role": "user", "content": user_message})
                self.manager.conversation_history.append({"role": "assistant", "content": reply})
                return HandlerResult.success(reply=reply)

        # 2. confirming 状态下的最终确认
        if phase == "confirming" or getattr(self.manager, "awaiting_final_confirm", False):
            if self.manager._is_final_publish_confirmation(user_message):
                reply = self.manager._handle_task_confirm(user_message, request_id)
                return HandlerResult.success(reply=reply)
            elif self.manager._is_confirmation_only(user_message):
                reply = "当前任务尚未发布。如确认无误，请回复‘确认发布’；如需调整，可直接说明要修改的参数。"
                self.manager.conversation_history.append({"role": "user", "content": user_message})
                self.manager.conversation_history.append({"role": "assistant", "content": reply})
                return HandlerResult.success(reply=reply)

        return HandlerResult.not_handled()
