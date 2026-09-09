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
from ..slot_store import ValidationAcknowledgement
from ..simulated_time import get_current_datetime

logger = logging.getLogger("src.dialogue_manager")


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

    def _handle_soft_warning_confirmation(self, user_message: str, request_id: str) -> str:
        """blocked_soft 阶段的确认/忽略处理。

        将已确认忽略的软警告录入 SlotStore.validation_acknowledgements 绑定快照版本，
        清除 _blocking_violations，然后根据缺失槽位决定进入 collecting 或 confirming。
        """
        dm = self.manager
        task_type_key = dm.task_state.get("task_type_key")
        missing = []
        if task_type_key:
            req_schema = dm.builder.get_schema(task_type_key, dm.mode)
            user_req_schema = [f for f in req_schema if f.get("type") not in ("auto", "fixed")]
            missing = dm.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: dm.builder.resolve_allowed_values(
                    field,
                    task_type_key,
                    dm.task_state,
                ),
            )
        if not task_type_key:
            target_purpose = "interactive"
        else:
            target_purpose = "preview" if not missing else "interactive"

        res = dm._refresh_validation(purpose=target_purpose)
        status_ref = res.state_snapshot.get("status_ref") if res.state_snapshot else None
        state_ver = res.state_snapshot.get("state_version") if res.state_snapshot else None

        if dm._blocking_violations:
            for v in dm._blocking_violations:
                if getattr(v, "severity", "soft") == "soft":
                    ack = ValidationAcknowledgement(
                        constraint_id=v.constraint_id,
                        acknowledged_at=get_current_datetime().isoformat(timespec="seconds"),
                        task_version=res.task_version,
                        validation_version=res.validation_version,
                        validation_fingerprint=res.validation_fingerprint,
                        status_ref=status_ref or "",
                        state_version=state_ver or 0,
                        field=getattr(v, "related_fields", [""])[0] if getattr(v, "related_fields", None) else "",
                        value=getattr(v, "observed_value", None),
                    )
                    if ack not in dm.slot_store.validation_acknowledgements:
                        dm.slot_store.validation_acknowledgements.append(ack)
                for f in v.related_fields:
                    val = dm.task_state.get(f)
                    if val is not None:
                        dm._soft_whitelist.add((f, str(val), v.constraint_id))
            dm._blocking_violations = []

        # 重新检查约束（使用白名单过滤后的结果）
        res = dm._refresh_validation(purpose=target_purpose)
        all_violations = res.violations
        remaining_soft = [v for v in all_violations
                          if v.severity == "soft" and not dm._is_whitelisted(v)]
        remaining_hard = [v for v in all_violations if v.severity == "hard"]

        if remaining_hard:
            dm._transition_phase("blocked_hard", reason="hard_constraint_detected")
            dm._blocking_violations = remaining_hard
        elif remaining_soft:
            dm._transition_phase("blocked_soft", reason="soft_warning_detected")
            dm._blocking_violations = remaining_soft
        else:
            if task_type_key:
                if not missing:
                    dm._transition_phase("confirming", reason="required_slots_complete")
                else:
                    dm._transition_phase("collecting", reason="required_slots_missing")
            else:
                dm._transition_phase("collecting", reason="task_type_missing")

        if remaining_hard:
            reply = (
                "未能继续：重新校验发现硬约束，软警告确认不能绕过硬约束。"
                "任务尚未发布，请先修改相关参数。"
            )
        elif remaining_soft:
            reply = (
                "已记录可忽略软警告的确认，但仍有未确认的软警告。"
                "任务保持阻断且尚未发布。"
            )
        elif dm.phase == "confirming":
            reply = (
                "已记录您对当前软警告的确认。任务尚未发布；"
                "所有必填字段已完整，如确认无误，请回复“确认发布”。"
            )
        else:
            labels = [
                item.get("label") or item.get("key")
                for item in dm._last_missing
                if isinstance(item, dict) and (item.get("label") or item.get("key"))
            ]
            missing_text = "、".join(labels)
            suffix = (
                f"请继续补充：{missing_text}。"
                if missing_text
                else "请继续补充任务信息。"
            )
            reply = f"已记录您对当前软警告的确认。任务尚未发布；{suffix}"

        dm.conversation_history.append({"role": "user", "content": user_message})
        dm.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    def _reject_hard_constraint_bypass(self, user_message: str) -> str:
        """Reject confirmation/ignore commands while hard violations remain."""
        dm = self.manager
        violations = [
            violation
            for violation in dm._blocking_violations
            if violation.severity == "hard"
        ]
        if not violations:
            val_res = dm._refresh_validation(purpose="interactive")
            violations = [
                v for v in val_res.violations
                if v.severity == "hard"
            ]

        reply = "硬性约束不能通过确认或忽略警告绕过。请先修正以下问题后再发布任务。"
        if violations:
            reply = f"{reply}\n\n{dm.validator.format_violations(violations)}"
            dm._blocking_violations = violations

        dm.conversation_history.append({"role": "user", "content": user_message})
        dm.conversation_history.append({"role": "assistant", "content": reply})
        return reply
