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
from src.session.intent_router import IntentRouteResult
from src.dispatch.id_sequence import validate_intent_id
from src.slots.slot_store import SlotStore
from src.session.session_state import ExecutionControlState, StateContractError, is_session_state_v2_active
from src.dispatch.execution_state import intent_execution_guard, read_execution_control, write_execution_control
from src.dispatch.task_dispatch import save_dispatch_history

logger = logging.getLogger("src.dialogue_manager")


def _is_v2_active() -> bool:
    return is_session_state_v2_active()


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

    def _perform_archived_control(self, request: dict) -> str:
        """Coordinate a durable intent hold and any targeted ROS control write."""
        dm = self.manager
        intent = dm.final_result
        action = request["action"]
        labels = {"stop": "停止", "pause": "暂停", "abort": "终止", "cancel": "取消"}
        prefix = f"已识别针对已发布任务的控制指令【{labels[action]}】。"
        bridge = getattr(dm, "mcp_bridge", None)
        if not isinstance(intent, dict) or not intent:
            request.update(outcome="failed", message="缺少可核验的归档任务，未发送控制指令。")
            self.set_execution_control_state(f"{action}_requested", request)
            return prefix + request["message"]

        transmitted = False
        try:
            with intent_execution_guard(intent) as path:
                existing = read_execution_control(path, intent)
                if (existing and existing["dispatch_blocked"]
                        and existing["request"].get("outcome") == "not_sent_cancelled"):
                    previous = existing["request"]
                    self.set_execution_control_state(f"{previous['action']}_requested", previous)
                    dm.runtime_execution_review = None
                    dm.ros2_dispatch = {
                        "state": "BLOCKED", "reason": "execution_control",
                        "message": previous["message"], "retry_allowed": False,
                    }
                    return prefix + "该任务已取消下发，原取消状态保持有效。"

                dispatch_record = None
                task_id = None
                uncertain_send = False
                if bridge is not None:
                    reader = getattr(bridge, "get_dispatch_record", None)
                    try:
                        dispatch_record = reader(intent) if callable(reader) else None
                    except Exception as exc:
                        from mcp.core.bridge_service import DispatchOutcomeUnknown
                        if not isinstance(exc, DispatchOutcomeUnknown):
                            raise
                        task_id, uncertain_send = exc.task_id, True
                if isinstance(dispatch_record, dict) and dispatch_record.get("dispatch_state") == "SENT":
                    task_id = int(dispatch_record["task_id"])
                previous_dispatch = getattr(dm, "ros2_dispatch", None) or {}
                if task_id is None and previous_dispatch.get("state") == "SENT":
                    task_id = previous_dispatch.get("task_id")
                if task_id is None and previous_dispatch.get("state") == "UNKNOWN":
                    task_id = previous_dispatch.get("task_id")
                    uncertain_send = True

                definitely_unsent = (
                    isinstance(dispatch_record, dict) and dispatch_record.get("dispatch_state") == "FAILED"
                ) or (
                    dispatch_record is None and not uncertain_send
                    and previous_dispatch.get("state") in {"FAILED", "SCHEDULED", "BLOCKED", "NOT_SENT"}
                )
                if definitely_unsent and task_id is None:
                    paused = action == "pause"
                    message = (
                        "尚未发送的任务已暂停下发；如需恢复，请点击“重新检查并下发”。"
                        if paused else "尚未发送的任务已取消下发，之后不会执行该任务。"
                    )
                    request.update(
                        outcome="not_sent_paused" if paused else "not_sent_cancelled",
                        message=message,
                    )
                    write_execution_control(path, intent, request, blocked=True)
                    self.set_execution_control_state(f"{action}_requested", request)
                    dm.runtime_execution_review = None
                    dm.ros2_dispatch = {
                        "state": "BLOCKED", "reason": "execution_control",
                        "message": message, "retry_allowed": paused,
                    }
                    save_dispatch_history(dm)
                    return prefix + message

                # Unknown delivery with no reliable ROS task ID cannot be
                # turned into either a fabricated cancellation or a bulk stop.
                request.update(outcome="requested", ros_task_id=task_id,
                               message="控制请求待发送，机器人执行状态尚未改变。")
                write_execution_control(path, intent, request, blocked=task_id is None)
                self.set_execution_control_state(f"{action}_requested", request)
                save_dispatch_history(dm)
                if task_id is None:
                    request.update(outcome="unknown", message="无法核验机器人任务编号，未发送控制指令；请核对任务看板。")
                elif bridge is None or not bridge.is_healthy():
                    request.update(outcome="failed", message="桥接未连接，控制指令未发送；请恢复连接后重新提出控制请求。")
                else:
                    method = bridge.suspend_task if action in {"stop", "pause"} else bridge.delete_task
                    try:
                        command_id = method(int(task_id))
                        transmitted = True
                        request.update(
                            outcome="sent", command_task_id=command_id,
                            message="控制指令已写入 ROS 传输，是否停止或暂停须以机器人遥测确认。",
                        )
                    except Exception as exc:
                        from mcp.core.rosbridge_client import PublishOutcomeUnknown
                        uncertain = isinstance(exc, PublishOutcomeUnknown)
                        transmitted = uncertain
                        request.update(
                            outcome="unknown" if uncertain else "failed", error=str(exc),
                            message=("控制指令发送结果未确认，请核对机器人遥测；不会自动重发。"
                                     if uncertain else "控制指令未能发送，请核对连接后重试。"),
                        )
                write_execution_control(path, intent, request, blocked=task_id is None)
                self.set_execution_control_state(f"{action}_requested", request)
                save_dispatch_history(dm)
                return prefix + request["message"]
        except Exception as exc:
            request.update(
                outcome="unknown" if transmitted else "failed",
                error=str(exc), persistence_error=True,
                message=("控制发送可能已发生，但结果记录保存失败；请核对机器人遥测，勿自动重发。"
                         if transmitted else "控制请求未能可靠保存，未发送控制指令；请核对状态后重试。"),
            )
            self.set_execution_control_state(f"{action}_requested", request)
            return prefix + request["message"]

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
            reply = self._perform_archived_control(req_dict)
            dm.conversation_history.append({"role": "user", "content": user_message})
            dm.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        has_active_draft = bool(dm.task_state.get("task_type_key")) or any(
            s.status == "valid" and s.value is not None
            for s in dm.slot_store.slots.values()
        ) or bool(dm._last_built_json)

        if has_active_draft:
            is_cancel_intent = (
                action == "cancel"
                or any(k in user_message for k in ("取消", "放弃", "不要了", "算了", "重置"))
                or "cancel" in str(route.reason).lower()
            )
            if is_cancel_intent:
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
