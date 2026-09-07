"""
src/handlers/constraint_decision.py - 约束决策生命周期处理器

职责：
1. 软约束（Soft Warning）提示与用户确认/显式忽略拦截；
2. 硬约束（Hard Constraint）严格拦截与防绕过拦截（fail-closed）；
3. 外部状态（环境海况、机器人遥测）动态约束刷新与阻断解除；
4. 约束白名单跟踪与违规统计。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import BaseDialogueHandler, DialogueContext, HandlerResult

logger = logging.getLogger(__name__)


class ConstraintDecisionHandler(BaseDialogueHandler):
    """约束决策与阻断处理生命周期处理器"""

    def can_handle(self, ctx: DialogueContext) -> bool:
        """
        判断是否处于约束决策阶段：
        1. 当前处于 blocked_hard 阶段且用户尝试确认/忽略；
        2. 当前处于 blocked_soft 阶段且用户输入忽略/确认。
        """
        phase = self.manager.phase
        user_message = ctx.user_message

        if phase == "blocked_hard":
            if (
                self.manager._is_confirmation_only(user_message)
                or self.manager._is_final_publish_confirmation(user_message)
                or self.manager._is_ignore_warning(user_message)
            ):
                return True

        if phase == "blocked_soft":
            if (
                self.manager._is_ignore_warning(user_message)
                or self.manager._is_final_publish_confirmation(user_message)
            ):
                return True

        return False

    def handle(self, ctx: DialogueContext) -> HandlerResult:
        """执行约束相关的拦截与决策流转"""
        phase = self.manager.phase
        user_message = ctx.user_message
        request_id = ctx.request_id

        # 1. 硬约束阻断态下的确认/绕过拦截
        if phase == "blocked_hard":
            prev_val = getattr(self.manager.slot_store, "validation_result", None)
            prev_has_hard = bool(
                (prev_val and getattr(prev_val, "violations", None) and any(getattr(v, "severity", "") == "hard" for v in prev_val.violations))
                or (self.manager._blocking_violations and any(getattr(v, "severity", "") == "hard" for v in self.manager._blocking_violations))
            )

            val_res = self.manager._refresh_validation(purpose="interactive")
            current_hard = [
                v for v in val_res.violations
                if v.severity == "hard"
                and (getattr(self.manager, "mode", "") != "interactive" or getattr(v, "constraint_id", "") not in ("CLASS_NOT_ALLOWED_FOR_TASK", "FAMILY_CLASS_MISMATCH"))
            ]
            if current_hard:
                self.manager._blocking_violations = current_hard
                reply = self.manager._reject_hard_constraint_bypass(user_message)
                return HandlerResult.success(reply=reply)

            is_real_validation = getattr(val_res, "validation_version", 0) > 0
            if prev_has_hard and is_real_validation and getattr(val_res, "overall_status", "") in ("valid", "none"):
                self.manager._blocking_violations = []
                self.manager._hard_refusal_counts.clear()
                self.manager._transition_phase("collecting", reason="external_state_constraint_resolved")
                return HandlerResult.not_handled()
            else:
                reply = self.manager._reject_hard_constraint_bypass(user_message)
                return HandlerResult.success(reply=reply)

        # 2. 软约束阻断态下的忽略与确认
        if phase == "blocked_soft":
            if self.manager._is_ignore_warning(user_message):
                reply = self.manager._handle_soft_warning_confirmation(user_message, request_id)
                return HandlerResult.success(reply=reply)
            if self.manager._is_final_publish_confirmation(user_message):
                reply = "当前仍存在软警告。请先修改相关参数，或明确接受当前软警告后继续。"
                self.manager.conversation_history.append({"role": "user", "content": user_message})
                self.manager.conversation_history.append({"role": "assistant", "content": reply})
                return HandlerResult.success(reply=reply)

        return HandlerResult.not_handled()
