"""
src/handlers/execution_control.py - 紧急干预与机器人执行控制指令生命周期处理器

职责：
1. 统一维护执行控制状态变更入口 (set_execution_control_state)；
2. 清空未发布任务草稿并保留会话审计追踪 (clear_task_draft_preserving_dialogue_audit)；
3. 处理已发布任务的停止/暂停/终止指令及未发布草稿的取消与防御 (handle_emergency_intervention)。
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Optional, Dict

from .base import BaseDialogueHandler, DialogueContext, HandlerResult
from src.intent_router import IntentRouteResult
from src.id_sequence import validate_intent_id
from src.slot_store import SlotStore
from src.session_state import ExecutionControlState, StateContractError

logger = logging.getLogger("src.dialogue_manager")


def _is_v2_active() -> bool:
    import sys
    dm_mod = sys.modules.get("src.dialogue_manager")
    if dm_mod and hasattr(dm_mod, "is_session_state_v2_enabled"):
        try:
            return bool(dm_mod.is_session_state_v2_enabled())
        except Exception:
            pass
    from src.model_profile import is_session_state_v2_enabled
    return is_session_state_v2_enabled()


class ExecutionControlHandler(BaseDialogueHandler):
    """紧急干预与已发布任务执行控制处理器"""

    def can_handle(self, ctx: DialogueContext) -> bool:
        """HSM 前置调度门禁（预留扩展接口）。"""
        return False

    def handle(self, ctx: DialogueContext) -> HandlerResult:
        """HSM 前置处理（预留扩展接口）。"""
        return HandlerResult.not_handled()

    def set_execution_control_state(
        self,
        control_state: str,
        last_control_request: dict | None,
        *,
        reason: str = "",
        source: str = "runtime",
    ) -> None:
        """Issue #10 / G3.3-B 统一 Execution Control 修改入口。

        Runtime 修改 control_state 与 last_control_request 两个字段的唯一入口。
        """
        dm = self.manager
        if _is_v2_active():
            if control_state != "idle" and dm.phase != "done":
                raise StateContractError(
                    f"Cannot set non-idle execution control state '{control_state}' when task phase is '{dm.phase}' (must be 'done')"
                )
            _cand_exec = ExecutionControlState(
                control_state=control_state,
                last_control_request=last_control_request,
            )

        dm.control_state = control_state
        dm.last_control_request = copy.deepcopy(last_control_request) if last_control_request is not None else None

    def clear_task_draft_preserving_dialogue_audit(self) -> None:
        """清空未发布任务草稿与约束，但保留会话历史与模式流转审计。"""
        dm = self.manager
        task_type_key = dm.task_state.get("task_type_key")
        dm.slot_store = SlotStore(dm.kb)
        if task_type_key:
            schema = dm.builder.get_schema(task_type_key, dm.mode)
            dm.slot_store.init_task_slots(schema)

        dm.task_state = dm.slot_store.get_task_state()
        dm.final_result = None
        dm.awaiting_final_confirm = False
        dm.task_start_now = False
        dm._blocking_violations = []
        dm._soft_whitelist = set()
        dm._hard_refusal_counts = {}
        dm._pending_rov_candidates = []
        dm._last_built_json = {}
        dm._last_missing = []
        self.set_execution_control_state("idle", None, reason="clear_task_draft")

    def handle_emergency_intervention(
        self,
        user_message: str,
        route: IntentRouteResult,
        request_id: str = "req_default",
    ) -> str:
        """处理紧急干预指令与取消流程。"""
        dm = self.manager
        action = route.emergency_action
        valid_actions = {"stop", "pause", "abort", "cancel"}
        if not action or action not in valid_actions:
            return dm._handle_non_task_route(user_message, route, request_id)

        action_cn_map = {
            "stop": "停止",
            "pause": "暂停",
            "abort": "终止",
            "cancel": "取消",
        }
        action_cn = action_cn_map.get(action, action)

        if dm.phase == "done":
            target_intent_id = dm.task_state.get("intent_id") or (
                dm._last_built_json.get("intent_id")
                if isinstance(dm._last_built_json, dict)
                else None
            )
            target_task_id = dm.task_state.get("task_id") or (
                dm._last_built_json.get("task_id")
                if isinstance(dm._last_built_json, dict)
                else None
            )
            target_internal_id = dm.task_state.get("internal_id") or (
                dm._last_built_json.get("internal_id")
                if isinstance(dm._last_built_json, dict)
                else None
            )

            if _is_v2_active():
                if not target_intent_id or not validate_intent_id(target_intent_id):
                    raise StateContractError(
                        f"Cannot create execution control request: invalid or missing target_intent_id ({target_intent_id!r}) in phase 'done'"
                    )

            req_dict = {
                "action": action,
                "status": "requested",
                "target_intent_id": target_intent_id,
                "target_task_id": target_task_id,
                "target_internal_id": target_internal_id,
                "source": route.source,
                "confidence": route.confidence,
                "reason": route.reason,
            }
            self.set_execution_control_state(
                f"{action}_requested",
                req_dict,
                reason="emergency_intervention_requested",
                source=route.source,
            )
            reply = f"已识别针对已发布任务的控制指令【{action_cn}】。该控制请求已记录，等待机器人控制适配器对接执行。"
            dm.conversation_history.append({"role": "user", "content": user_message})
            dm.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        has_active_draft = bool(dm.task_state.get("task_type_key")) or any(
            s.status == "valid" and s.value is not None
            for s in dm.slot_store.slots.values()
        ) or bool(dm._last_built_json)

        if has_active_draft:
            if action == "cancel":
                self.clear_task_draft_preserving_dialogue_audit()
                dm._transition_phase("rejected", reason="user_cancelled_draft")
                dm.final_result = None
                reply = "任务已取消。如需重新规划，请重新开始。"
            else:
                reply = (
                    f"当前任务尚未发布，无正在运行的机器人实例可执行【{action_cn}】操作。"
                    f"任务草稿已保留；如需放弃草稿，请明确指示“取消当前任务”。"
                )
            dm.conversation_history.append({"role": "user", "content": user_message})
            dm.conversation_history.append({"role": "assistant", "content": reply})
            return reply
        else:
            reply = "当前没有活动任务或可取消的未发布任务。"
            dm.conversation_history.append({"role": "user", "content": user_message})
            dm.conversation_history.append({"role": "assistant", "content": reply})
            return reply
