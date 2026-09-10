"""
dialogue_manager.py - 对话主控制器

协调提取、验证、知识检索、响应生成的完整流程。

阶段状态机:
  collecting
    → blocked_hard   (硬违规阻塞)
    → blocked_soft   (软违规阻塞)
    → confirming     (字段齐全无阻塞，等待确认)
    → done           (确认，输出最终JSON)
    → rejected       (拒绝)

约束检查策略:
  - 字段变化后增量检查
  - Hard违规阻塞，连续失败达上限则拒绝
  - Soft违规询问一次，用户可忽略并加入白名单
  - 白名单key: (field, str(value), constraint_id)，字段值变化时失效
"""

import copy
import dataclasses
import json
import logging
import math
import re
import threading
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from .llm_client import LLMClient
from .model_profile import (
    ModelRole,
    _is_unsupported_role_keyword_error,
    is_normalization_contract_v2_enabled,
    is_session_state_v2_enabled,
    is_shadow_compare_enabled,
    is_task_patch_v2_enabled,
)
from .session_state_shadow import (
    compare_session_state_shadow,
    record_shadow_metric,
    should_run_session_state_shadow,
)
from .session_state import (
    ConversationState,
    ExecutionControlState,
    SessionState,
    StateContractError,
    TaskLifecycleState,
    VALID_DIALOGUE_MODES,
    VALID_PHASES,
    VALID_TASK_MODES,
    session_state_from_legacy_snapshot,
    session_state_to_legacy_fields,
    validate_task_phase_transition,
)
from .normalization_contract import (
    NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS,
    NormalizationApplyPlan,
    normalize_task_patch,
    normalized_task_patch_to_apply_plan,
    validate_normalization_runtime_flags,
)
from .task_patch import build_task_patch, task_patch_to_legacy_updates
from .knowledge_retriever import KnowledgeBase, RobotSelectionDataError, format_seabed_type, format_telemetry_value
from .extractor import ParameterExtractor
from .task_slot_filter import TaskSlotFilter
from .task_capability_adapter import TaskCapabilityAdapter
from .handlers import (
    BaseDialogueHandler,
    DialogueContext,
    HandlerResult,
    ConversationRouterHandler,
    SlotFillingHandler,
    ConstraintDecisionHandler,
    TaskCommitHandler,
)
from .handlers.task_commit import (
    sanitize_user_facing_json,
    _USER_FACING_EXCLUDED_KEYS,
)

from .normalizer import FieldNormalizer
from .output_builder import OutputBuilder
from .validator import TaskValidator, Violation, ValidationResult
from .prompts import (
    OFF_TOPIC_REJECT_TEMPLATE,
    build_responder_messages,
    build_general_chat_messages,
    build_knowledge_responder_messages,
    build_status_responder_messages,
)
from .task_intent_builder import TaskIntentBuilder
from .simulated_time import get_current_datetime
from .time_context import get_time_context, is_standalone_time_query
from .coord_parser import parse_coordinate_updates
from . import coord_parser
from .oilfield_linker import OilfieldEntityLinker, _UNSET
from . import task_intent_builder as _ti_builder_module
from .constants import (
    FIELD_LABELS,
)
from .id_sequence import validate_intent_id, validate_task_id, validate_task_id_for_task_type, next_daily_id
from .slot_store import (
    BASE_SLOT_TYPES,
    Slot,
    SlotStore,
    SnapshotValidationError,
    ValidationAcknowledgement,
    normalize_slot_value_type,
    reset_slot_to_missing,
)

from .exceptions import TaskPersistenceError, IntentIdConflict, IdReservationError, TaskRollbackError
from .intent_router import IntentRouter, IntentRouteResult
from .task_request_guard import analyze_task_request
from .result_paths import get_task_dir


class DialogueManager:
    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        kb: Optional[KnowledgeBase] = None,
        session_id: str | None = None,
    ):
        if kb is None:
            kb = KnowledgeBase()
        if llm is None:
            llm = LLMClient(None, None)
        self.llm = llm
        self.kb = kb
        self.session_id = session_id
        self.extractor = ParameterExtractor(llm)
        # LHL 归一化器采用确定性规则，不依赖 LLM 猜测合法字段值。
        self.normalizer = FieldNormalizer()
        self.builder = OutputBuilder(kb)
        self.validator = TaskValidator(kb)
        self.oilfield_linker = OilfieldEntityLinker(kb.environment, getattr(kb, "constraints", None))
        self.intent_router = IntentRouter(llm)
        self.slot_filter = TaskSlotFilter(kb.task_schemas)
        self.capability_adapter = TaskCapabilityAdapter(kb.task_schemas)

        # 对话核心状态
        self.conversation_history: list[dict] = []
        self.slot_store = SlotStore(kb)
        self.task_state: dict = self.slot_store.get_task_state()
        self.mode: str = "normal"
        self.phase: str = "collecting"
        self.final_result: dict | None = None
        self.awaiting_final_confirm = False
        self.task_start_now = False
        self._last_visible_catalog_items: list[dict] = []

        # 约束管理状态
        self._blocking_violations: list[Violation] = []
        self.ignored_soft_warning_ids: set[str] = set()
        self.pending_warning_violations: list[Violation] = []
        self._soft_whitelist: set[tuple[str, str, str]] = set()
        self._hard_refusal_counts: dict[str, int] = {}

        # ROV候选暂存
        self._pending_rov_candidates: list[dict] = []
        self._last_discussed_task_type: str | None = None
        self._last_discussed_robot: str | None = None
        self._last_discussed_oilfield: str | None = None
        self._last_discussed_payload: str | None = None

        # 缓存构建结果
        self._last_built_json: dict = {}
        self._last_missing: list[dict] = []

        # 会话锁（按 session 隔离并发控制）
        self._session_lock = threading.RLock()

        # 内存控制状态（Issue #10 运行期控制请求记录）
        self.control_state: str = "idle"
        self.last_control_request: dict | None = None

        # 会话模式状态（Issue #10 会话模式管理）
        self.dialogue_mode: str = "task_collection"
        self.last_mode_transition: dict | None = None
        self.mode_transition_history: list[dict] = []

        # 分层状态机（HSM）生命周期处理器
        self.router_handler = ConversationRouterHandler(self)
        self.constraint_handler = ConstraintDecisionHandler(self)
        self.commit_handler = TaskCommitHandler(self)
        self.slot_handler = SlotFillingHandler(self)

    def __getattr__(self, name: str):
        if name == "router_handler":
            handler = ConversationRouterHandler(self)
            self.__dict__["router_handler"] = handler
            return handler
        if name == "constraint_handler":
            handler = ConstraintDecisionHandler(self)
            self.__dict__["constraint_handler"] = handler
            return handler
        if name == "commit_handler":
            handler = TaskCommitHandler(self)
            self.__dict__["commit_handler"] = handler
            return handler
        if name == "slot_handler":
            handler = SlotFillingHandler(self)
            self.__dict__["slot_handler"] = handler
            return handler
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def _ensure_payload_guidance(self, text: str, missing_fields: list) -> str:
        return self.capability_adapter.format_payload_guidance(text, missing_fields)

    @property
    def task_id_preview(self) -> str | None:
        """只读预览属性：草稿阶段预估的下一个任务业务编号（仅供 UI/API 展示）。"""
        if self.slot_store:
            slot = self.slot_store.slots.get("task_id")
            if slot and slot.status == "candidate" and slot.candidate_value is not None:
                return slot.candidate_value
        return None

    def _switch_dialogue_mode(
        self,
        new_mode: str,
        *,
        source: str = "rule",
        confidence: float = 1.0,
        reason: str = "",
        restore_transition_state: tuple[dict | None, list[dict]] | None = None,
    ) -> None:
        """Issue #10 统一模式切换方法：记录切换元数据与历史轨迹。"""
        old_mode = getattr(self, "dialogue_mode", "task_collection")
        if is_session_state_v2_enabled():
            if old_mode not in VALID_DIALOGUE_MODES:
                raise StateContractError(f"Invalid old dialogue_mode in runtime: {old_mode!r}")

        changed_at = datetime.now(timezone.utc).isoformat()
        transition = {
            "from": old_mode,
            "to": new_mode,
            "source": source,
            "confidence": confidence,
            "reason": reason,
            "changed_at": changed_at,
        }

        cand_mode = new_mode
        if restore_transition_state is not None:
            restored_last, restored_history = restore_transition_state
            cand_last_transition = copy.deepcopy(restored_last)
            cand_history = copy.deepcopy(restored_history)
        elif old_mode != new_mode:
            cand_last_transition = transition
            hist = list(getattr(self, "mode_transition_history", []) or [])
            hist.append(transition)
            if len(hist) > 50:
                hist.pop(0)
            cand_history = hist
        else:
            cand_last_transition = getattr(self, "last_mode_transition", None)
            cand_history = list(getattr(self, "mode_transition_history", []) or [])

        if is_session_state_v2_enabled():
            _cand_conv = ConversationState(
                dialogue_mode=cand_mode,
                last_mode_transition=cand_last_transition,
                mode_transition_history=cand_history,
            )

        self.dialogue_mode = cand_mode
        if old_mode != new_mode or restore_transition_state is not None:
            self.last_mode_transition = cand_last_transition
            self.mode_transition_history = cand_history

    def _set_execution_control_state(
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
        if is_session_state_v2_enabled():
            if control_state != "idle" and self.phase != "done":
                raise StateContractError(
                    f"Cannot set non-idle execution control state '{control_state}' when task phase is '{self.phase}' (must be 'done')"
                )
            _cand_exec = ExecutionControlState(
                control_state=control_state,
                last_control_request=last_control_request,
            )

        self.control_state = control_state
        self.last_control_request = copy.deepcopy(last_control_request) if last_control_request is not None else None

    def _transition_phase(
        self,
        new_phase: str,
        *,
        reason: str = "",
        source: str = "runtime",
    ) -> None:
        """Issue #10 / G3.3-A & G3.4-A 统一 Task Phase 修改入口。

        只负责 phase 状态迁移。
        在 session_state_v2=true 时增加 old_phase, new_phase 及 transition edge 的合法性校验。
        """
        if is_session_state_v2_enabled():
            old_phase = getattr(self, "phase", "collecting")
            validate_task_phase_transition(old_phase, new_phase)

        self.phase = new_phase

    def _build_session_state_contract(self) -> SessionState:
        """从 DialogueManager 当前 Runtime 内存字段构造 SessionState 合约。无副作用，纯只读校验。"""
        conv = ConversationState(
            dialogue_mode=getattr(self, "dialogue_mode", "task_collection"),
            last_mode_transition=getattr(self, "last_mode_transition", None),
            mode_transition_history=getattr(self, "mode_transition_history", []),
        )
        task = TaskLifecycleState(
            phase=getattr(self, "phase", "collecting"),
            mode=getattr(self, "mode", "normal"),
            awaiting_final_confirm=getattr(self, "awaiting_final_confirm", False),
        )
        exec_ctrl = ExecutionControlState(
            control_state=getattr(self, "control_state", "idle"),
            last_control_request=getattr(self, "last_control_request", None),
        )
        return SessionState(
            schema_version=2,
            conversation=conv,
            task=task,
            execution=exec_ctrl,
        )

    def _apply_session_state_contract(self, state: SessionState) -> None:
        """将已校验通过的 SessionState 合约对象字段写入 DialogueManager 内存状态。"""
        fields = session_state_to_legacy_fields(state)
        self.phase = fields["phase"]
        self.mode = fields["mode"]
        self.awaiting_final_confirm = fields["awaiting_final_confirm"]
        self.dialogue_mode = fields["dialogue_mode"]
        self.last_mode_transition = copy.deepcopy(fields["last_mode_transition"])
        self.mode_transition_history = [copy.deepcopy(t) for t in fields["mode_transition_history"]]
        self.control_state = fields["control_state"]
        self.last_control_request = copy.deepcopy(fields["last_control_request"])




    # --------------------------------------------------------------------------
    # 主入口
    # --------------------------------------------------------------------------

    def process(self, user_message: str, request_id: str = "req_default") -> str:
        with self._session_lock:
            request_snapshot = copy.deepcopy(self.slot_store.export_snapshot())
            request_task_state = copy.deepcopy(self.task_state)
            request_built_json = copy.deepcopy(self._last_built_json)
            request_missing = copy.deepcopy(self._last_missing)
            request_phase = self.phase
            request_soft_whitelist = copy.deepcopy(self._soft_whitelist)
            request_pending_rov = copy.deepcopy(self._pending_rov_candidates)
            request_blocking_violations = copy.deepcopy(self._blocking_violations)
            request_history = list(self.conversation_history)
            request_task_start_now = self.task_start_now

            try:
                reply = self._process_internal(user_message, request_id)
                self._run_session_state_shadow_check(checkpoint="process", request_id=request_id)
                return reply
            except (TaskPersistenceError, IntentIdConflict, IdReservationError) as exc:
                if self.phase == "blocked_soft":
                    raise

                try:
                    self.slot_store.restore_snapshot(request_snapshot)
                    self.task_state = request_task_state
                    self._last_built_json = request_built_json
                    self._last_missing = request_missing
                    self._transition_phase(request_phase, reason="request_rollback")
                    self._soft_whitelist = request_soft_whitelist
                    self._pending_rov_candidates = request_pending_rov
                    self._blocking_violations = request_blocking_violations
                    self.conversation_history = request_history
                    self.task_start_now = request_task_start_now
                    self.final_result = None
                except Exception as rb_exc:
                    raise TaskRollbackError(
                        f"Request failed ({exc}) and request rollback failed: {rb_exc}"
                    ) from exc
                raise

    def _run_session_state_shadow_check(
        self,
        checkpoint: str,
        request_id: str | None = None,
    ) -> None:
        """在稳定边界运行 SessionState V2 Shadow 旁路比较（只读、不干预、异常隔离、日志脱敏）。"""
        if not should_run_session_state_shadow(self.session_id):
            return

        try:
            snapshot = self.export_snapshot()
            result = compare_session_state_shadow(snapshot, checkpoint=checkpoint, request_id=request_id)
            if result.classification == "PARITY":
                record_shadow_metric("parity")
                logger.info(
                    "[SESSION_STATE_SHADOW_PARITY] checkpoint=%s request_id=%s",
                    checkpoint,
                    request_id,
                )
            elif result.classification == "STRICT_REJECTED":
                record_shadow_metric("strict_rejected")
                logger.warning(
                    "[SESSION_STATE_SHADOW_STRICT_REJECTED] checkpoint=%s request_id=%s exc_type=%s",
                    checkpoint,
                    request_id,
                    result.exception_type,
                )
            elif result.classification == "MISMATCH":
                record_shadow_metric("mismatch")
                logger.warning(
                    "[SESSION_STATE_SHADOW_MISMATCH] checkpoint=%s request_id=%s diff_fields=%s",
                    checkpoint,
                    request_id,
                    result.diff_fields,
                )
        except Exception as exc:
            record_shadow_metric("error")
            logger.warning(
                "[SESSION_STATE_SHADOW_ERROR] checkpoint=%s request_id=%s exc_type=%s",
                checkpoint,
                request_id,
                type(exc).__name__,
            )

    # --------------------------------------------------------------------------
    # 非任务路由与知识检索处理集群（委托至 ConversationRouterHandler）
    # --------------------------------------------------------------------------

    def _handle_non_task_route(self, user_message: str, route: IntentRouteResult, request_id: str) -> str:
        return self.router_handler._handle_non_task_route(user_message, route, request_id)

    def _build_knowledge_fallback(self, kb_evidence: dict) -> str:
        return self.router_handler._build_knowledge_fallback(kb_evidence)

    def _missing_field_definition(self, key: str) -> dict | None:
        return self.router_handler._missing_field_definition(key)

    def _build_grounded_recommendation(self, route: IntentRouteResult, user_message: str | None = None) -> str | None:
        return self.router_handler._build_grounded_recommendation(route, user_message)

    def _resolve_project_robot_classes(self, text: str) -> list[tuple[str, str]]:
        return self.router_handler._resolve_project_robot_classes(text)

    def _extract_robot_entity_from_text(self, text: str) -> str | None:
        return self.router_handler._extract_robot_entity_from_text(text)

    def _extract_oilfield_entity_from_text(self, text: str) -> str | None:
        return self.router_handler._extract_oilfield_entity_from_text(text)

    def _extract_payload_entity_from_text(self, text: str) -> str | None:
        return self.router_handler._extract_payload_entity_from_text(text)

    def _extract_task_type_from_text(self, text: str) -> str | None:
        return self.router_handler._extract_task_type_from_text(text)

    def _build_grounded_single_task_introduction(self, task_type_key: str) -> str:
        return self.router_handler._build_grounded_single_task_introduction(task_type_key)

    def _build_grounded_fleet_introduction(self) -> str:
        return self.router_handler._build_grounded_fleet_introduction()

    def _build_grounded_task_catalog_introduction(self) -> str:
        return self.router_handler._build_grounded_task_catalog_introduction()

    def _build_grounded_tool_catalog_introduction(self) -> str:
        return self.router_handler._build_grounded_tool_catalog_introduction()

    def _build_grounded_oilfield_catalog_introduction(self) -> str:
        return self.router_handler._build_grounded_oilfield_catalog_introduction()

    def _build_grounded_rule_catalog_introduction(self) -> str:
        return self.router_handler._build_grounded_rule_catalog_introduction()

    def _build_grounded_device_class_answer(self, user_message: str, route: IntentRouteResult) -> str | None:
        return self.router_handler._build_grounded_device_class_answer(user_message, route)

    def _safe_llm_chat(self, *args, **kwargs) -> str:
        return self.router_handler._safe_llm_chat(*args, **kwargs)

    def _safe_llm_filter_reply(self, *args, **kwargs) -> str:
        return self.router_handler._safe_llm_filter_reply(*args, **kwargs)

    def _handle_knowledge_query(self, user_message: str, route: IntentRouteResult, request_id: str = "req_default") -> str:
        return self.router_handler._handle_knowledge_query(user_message, route, request_id)

    def _is_environment_status_query(self, user_message: str, route: IntentRouteResult) -> bool:
        return self.router_handler._is_environment_status_query(user_message, route)

    def _handle_status_query(self, user_message: str, route: IntentRouteResult, as_environment_status: bool = False) -> str:
        return self.router_handler._handle_status_query(user_message, route, as_environment_status=as_environment_status)

    def _build_environment_status_reply(self, equipment: str, state_dict: dict) -> str:
        return self.router_handler._build_environment_status_reply(equipment, state_dict)

    def _align_status_reply_with_backend_facts(self, reply: str, state_dict: dict | None) -> str:
        return self.router_handler._align_status_reply_with_backend_facts(reply, state_dict)

    def _handle_general_chat(self, user_message: str, route: IntentRouteResult) -> str:
        return self.router_handler._handle_general_chat(user_message, route)

    def _handle_clarification(self, user_message: str, route: IntentRouteResult) -> str:
        return self.router_handler._handle_clarification(user_message, route)

    def _handle_unknown_intent(self, user_message: str, route: IntentRouteResult) -> str:
        return self.router_handler._handle_unknown_intent(user_message, route)

    def _handle_task_confirm(self, user_message: str, request_id: str = "req_default") -> str:
        """处理 TASK_CONFIRM 控制指令。

        不得调用 extractor.extract_updates / slot normalization /
        _apply_updates_in_transaction / slot_store.commit_transaction。
        只修改控制状态（phase / _soft_whitelist / _blocking_violations）。
        """
        if self.phase == "blocked_soft":
            return self._handle_soft_warning_confirmation(user_message, request_id)
        elif self.phase == "confirming":
            return self._handle_final_publish_confirmation(user_message, request_id)
        else:
            # 非 confirming/blocked_soft 阶段出现确认指令 → 澄清
            reply = "当前没有待确认的任务。请先创建或补充任务参数。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

    def _refresh_validation(
        self,
        purpose: str = "interactive",
        changed_fields: Optional[set[str]] = None,
    ) -> Any:
        """DialogueManager 的单一权威校验刷新入口。"""
        task_ver = getattr(self.slot_store, "version", 1)
        prev_res = getattr(self.slot_store, "validation_result", None)
        res = self.validator.validate_task(
            self.task_state,
            task_version=task_ver,
            previous_result=prev_res,
            purpose=purpose,
        )
        self.slot_store.validation_result = res
        return res

    def _task_uses_status_ref(self, status_ref: str | None) -> bool:
        """Return whether the current task is tied to a specific robot state ref."""
        if not status_ref:
            return True

        selectors = [
            self.task_state.get("equipment_unit_id"),
            self._last_built_json.get("equipment_unit_id"),
        ]
        unit_slot = self.slot_store.slots.get("equipment_unit_id")
        if unit_slot and unit_slot.status == "valid" and unit_slot.value is not None:
            selectors.append(unit_slot.value)

        for selector in selectors:
            if selector is None or selector == "":
                continue
            try:
                resolved_ref = self.kb.state_info.resolve_status_ref(str(selector))
            except Exception:
                resolved_ref = None
            if resolved_ref == status_ref or str(selector) == status_ref:
                return True
        return False

    def refresh_external_state_constraints(self, status_ref: str | None = None) -> dict:
        """Refresh validation after external robot telemetry/state changes.

        This does not publish or edit task slots; it only synchronizes phase,
        blockers, validation_result, and missing fields with current evidence.
        """
        if self.phase in ("done", "rejected"):
            return {"refreshed": False, "reason": "terminal_phase"}
        if not self._task_uses_status_ref(status_ref):
            return {"refreshed": False, "reason": "unrelated_status_ref"}

        task_type_key = self.task_state.get("task_type_key")
        missing: list[dict] = []
        if task_type_key:
            schema = self.builder.get_schema(task_type_key, self.mode)
            user_req_schema = [
                field for field in schema
                if field.get("type") not in ("auto", "fixed")
            ]
            missing = self.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: self.builder.resolve_allowed_values(
                    field,
                    task_type_key,
                    self.task_state,
                ),
            )
            self._last_missing = missing
        else:
            self._last_missing = [{
                "key": "task_type",
                "label": "任务类型",
                "type": "string",
                "allowed_values": self.kb.get_all_task_type_values(),
            }]
            missing = self._last_missing

        purpose = "preview" if task_type_key and not missing else "interactive"
        val_res = self._refresh_validation(purpose=purpose)
        violations = self._merge_oilfield_context_violations(val_res.violations)
        hard = [v for v in violations if v.severity == "hard"]
        soft = [
            v for v in violations
            if v.severity == "soft" and not self._is_whitelisted(v)
        ]

        if hard or val_res.overall_status == "validation_error":
            self._transition_phase("blocked_hard", reason="external_state_hard_detected")
            self._blocking_violations = hard
        elif soft:
            self._transition_phase("blocked_soft", reason="external_state_soft_detected")
            self._blocking_violations = soft
        else:
            self._blocking_violations = []
            self._hard_refusal_counts.clear()
            if task_type_key and not missing:
                self._transition_phase("confirming", reason="external_state_constraints_resolved")
            else:
                self._transition_phase("collecting", reason="external_state_constraints_resolved")

        return {
            "refreshed": True,
            "phase": self.phase,
            "overall_status": val_res.overall_status,
            "hard_violations": len(hard),
            "soft_violations": len(soft),
            "missing": [m.get("key") for m in missing if isinstance(m, dict)],
        }

    def _get_valid_acknowledgements(
        self,
        validation_result: ValidationResult | None,
    ) -> list[ValidationAcknowledgement]:
        """
        过滤并返回与当前 validation_result 完全匹配的有效确认。
        至少匹配：
        - constraint_id
        - task_version
        - validation_version
        - validation_fingerprint
        - status_ref
        - state_version
        - observed_value (or field/value)
        """
        if not validation_result or not self.slot_store.validation_acknowledgements:
            return []

        status_ref = (
            validation_result.state_snapshot.get("status_ref", "")
            if validation_result.state_snapshot
            else ""
        )
        state_version = (
            validation_result.state_snapshot.get("state_version", 0)
            if validation_result.state_snapshot
            else 0
        )

        violation_map = {
            v.constraint_id: v for v in (validation_result.violations or [])
        }

        valid_acks = []
        for ack in self.slot_store.validation_acknowledgements:
            if not isinstance(ack, ValidationAcknowledgement):
                continue
            # ack 的创建版本不能晚于当前 validation_result
            if ack.task_version > validation_result.task_version:
                continue
            if ack.status_ref != status_ref:
                continue
            if ack.state_version != state_version:
                continue
            # 必须对应当前实际存在的软警告 Violation
            if ack.constraint_id not in violation_map:
                continue
            v = violation_map[ack.constraint_id]
            if ack.value != getattr(v, "observed_value", None) and ack.field not in getattr(v, "related_fields", []):
                continue
            valid_acks.append(ack)
        return valid_acks

    def _handle_soft_warning_confirmation(self, user_message: str, request_id: str) -> str:
        return self.constraint_handler._handle_soft_warning_confirmation(user_message, request_id)

    def _handle_final_publish_confirmation(self, user_message: str, request_id: str) -> str:
        return self.commit_handler._handle_final_publish_confirmation(user_message, request_id)

    def _clear_task_draft_preserving_dialogue_audit(self) -> None:
        """清空未发布任务草稿与约束，但保留会话历史与模式流转审计。"""
        task_type_key = self.task_state.get("task_type_key")
        self.slot_store = SlotStore(self.kb)
        if task_type_key:
            schema = self.builder.get_schema(task_type_key, self.mode)
            self.slot_store.init_task_slots(schema)

        self.task_state = self.slot_store.get_task_state()
        self.final_result = None
        self.awaiting_final_confirm = False
        self.task_start_now = False
        self._blocking_violations = []
        self._soft_whitelist = set()
        self._hard_refusal_counts = {}
        self._pending_rov_candidates = []
        self._last_built_json = {}
        self._last_missing = []
        self._set_execution_control_state("idle", None, reason="clear_task_draft")

    def _handle_emergency_intervention(
        self,
        user_message: str,
        route: IntentRouteResult,
        request_id: str = "req_default",
    ) -> str:
        action = route.emergency_action
        valid_actions = {"stop", "pause", "abort", "cancel"}
        if not action or action not in valid_actions:
            return self._handle_non_task_route(user_message, route, request_id)

        action_cn_map = {
            "stop": "停止",
            "pause": "暂停",
            "abort": "终止",
            "cancel": "取消",
        }
        action_cn = action_cn_map.get(action, action)

        if self.phase == "done":
            target_intent_id = self.task_state.get("intent_id") or (self._last_built_json.get("intent_id") if isinstance(self._last_built_json, dict) else None)
            target_task_id = self.task_state.get("task_id") or (self._last_built_json.get("task_id") if isinstance(self._last_built_json, dict) else None)
            target_internal_id = self.task_state.get("internal_id") or (self._last_built_json.get("internal_id") if isinstance(self._last_built_json, dict) else None)

            if is_session_state_v2_enabled():
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
            self._set_execution_control_state(
                f"{action}_requested",
                req_dict,
                reason="emergency_intervention_requested",
                source=route.source,
            )
            reply = f"已识别针对已发布任务的控制指令【{action_cn}】。该控制请求已记录，等待机器人控制适配器对接执行。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        has_active_draft = bool(self.task_state.get("task_type_key")) or any(
            s.status == "valid" and s.value is not None
            for s in self.slot_store.slots.values()
        ) or bool(self._last_built_json)

        if has_active_draft:
            if action == "cancel":
                self._clear_task_draft_preserving_dialogue_audit()
                self._transition_phase("rejected", reason="user_cancelled_draft")
                self.final_result = None
                reply = "任务已取消。如需重新规划，请重新开始。"
            else:
                reply = (
                    f"当前任务尚未发布，无正在运行的机器人实例可执行【{action_cn}】操作。"
                    f"任务草稿已保留；如需放弃草稿，请明确指示“取消当前任务”。"
                )
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply
        else:
            reply = "当前没有活动任务或可取消的未发布任务。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

    def _process_internal(self, user_message: str, request_id: str = "req_default") -> str:
        old_phase = self.phase

        # ----------------------------------------------------------------------
        # 分层状态机（Hierarchical State Machine, HSM）调度层
        # ----------------------------------------------------------------------
        ctx = DialogueContext(
            manager=self,
            user_message=user_message,
            request_id=request_id,
            old_phase=old_phase,
        )

        # Level 1: 全局门禁与快捷路由（离题检测、系统时间查询）
        if self.router_handler.can_handle(ctx):
            res = self.router_handler.handle(ctx)
            if res.handled and res.reply is not None:
                return res.reply

        # Level 2: 任务提交流程与已归档任务防护（done重复确认/就地篡改拦截、confirming最终确认）
        if self.commit_handler.can_handle(ctx):
            res = self.commit_handler.handle(ctx)
            if res.handled and res.reply is not None:
                return res.reply

        # Level 3: 槽位填报特定调整（如搭载工具就地修改补丁）
        if self.slot_handler.can_handle(ctx):
            res = self.slot_handler.handle(ctx)
            if res.handled and res.reply is not None:
                return res.reply

        # Level 4: 约束决策与阻断拦截（blocked_hard 防绕过、blocked_soft 明确忽略）
        if self.constraint_handler.can_handle(ctx):
            res = self.constraint_handler.handle(ctx)
            if res.handled and res.reply is not None:
                return res.reply

        # 指代消解与继承（委托至 SlotFillingHandler）
        user_message = self.slot_handler.process_referential_carryover(user_message)

        # ── 独立意图路由分流阶段 ──
        expected_slots = [m["key"] for m in self._last_missing if isinstance(m, dict) and "key" in m]
        expected_slot_options = [
            {
                "key": item.get("key"),
                "label": item.get("label"),
                "type": item.get("type"),
                "allowed_values": copy.deepcopy(item.get("allowed_values") or []),
                "alias_mappings": copy.deepcopy(item.get("alias_mappings") or {}),
            }
            for item in self._last_missing
            if isinstance(item, dict) and item.get("key")
        ]
        # ════════════════════════════════════════════════════════════════
        # 修复3的关键：路由切换 dialogue_mode 之前快照保存原始 mode
        #   否则 L1207-1212 _switch_dialogue_mode(route.dialogue_mode)
        #   会把原本的 task_collection 先覆盖成 knowledge_qa，
        #   后续 _already_in_task 检测（用 self.dialogue_mode）永远 False。
        route = self.intent_router.route(
            user_message=user_message,
            conversation_history=self.conversation_history,
            task_state=self.task_state,
            phase=self.phase,
            expected_slots=expected_slots,
            expected_slot_options=expected_slot_options,
        )

        self._switch_dialogue_mode(
            route.dialogue_mode,
            source=route.source,
            confidence=route.confidence,
            reason=route.reason,
        )

        plan = route.interaction_plan
        is_ignore_warning_cmd = self._is_ignore_warning(user_message)
        has_acknowledge_action = bool(
            (plan and plan.warning_action == "acknowledge")
            or (self.phase == "blocked_soft" and is_ignore_warning_cmd)
        )

        if has_acknowledge_action:
            if self.phase == "blocked_hard":
                return self._reject_hard_constraint_bypass(user_message)
            # warning_action 是 WRITE 中的次级副作用，不能在字段抽取前抢占整轮。
            # 真实模型可能把“补充参数后继续”同时误标成 acknowledge；执行器必须
            # 先尝试提取并校验字段，只有没有任何任务候选时才执行警告确认。

        pending_reply = self._resolve_pending_oilfield_confirmation(
            user_message,
            request_id=request_id,
            pending_action=plan.pending_action if plan else None,
            subject_text=plan.subject_text if plan else None,
        )
        if pending_reply is not None:
            self._switch_dialogue_mode(
                "task_collection",
                source="interaction_plan",
                reason="结构化待确认油田消解",
            )
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": pending_reply})
            return pending_reply

        if route.dialogue_mode == "emergency_intervention":
            return self._handle_emergency_intervention(user_message, route, request_id)

        if route.interaction_type == "QUERY":
            return self._handle_non_task_route(user_message, route, request_id)

        if self.phase == "done":
            is_new_task = any(kw in user_message for kw in ["重新", "新任务", "创建", "新建", "重置"]) or any(user_message.startswith(kw) for kw in ["安排", "派", "我想做", "开始做"])
            if not is_new_task:
                self._switch_dialogue_mode("task_collection", source="user_input", reason="已发布任务尝试修改")
                intent_id = self.task_state.get("intent_id") or (self._last_built_json.get("intent_id") if isinstance(self._last_built_json, dict) else None)
                intent_detail = f"（任务ID: {intent_id}）" if intent_id else ""
                reply = f"当前任务已正式确认发布{intent_detail}并归档，无法就地修改参数。如需调整，请点击“重新开始”创建新任务，或提交工单变更申请。"
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply

        compound_request = analyze_task_request(
            user_message,
            self.kb.get_all_task_type_values(),
        )
        if compound_request.should_block:
            reply = compound_request.build_reply()
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # 3. 委托至 Level 3 SlotFillingHandler 执行槽位抽取、消歧、原子事务提交与事实锚点落地
        return self.slot_handler.execute_slot_filling(
            ctx,
            route=route,
            plan=plan,
            has_acknowledge_action=has_acknowledge_action,
        )

    # --------------------------------------------------------------------------
    # 参数更新与规范化
    # --------------------------------------------------------------------------
    @classmethod
    def _ground_write_reply(
        cls_or_self,
        self_or_reply: object = "",
        model_reply: str = "",
        *,
        accepted_updates: dict,
        unresolved_inputs: list,
        missing_fields: list[dict] | None = None,
        display_updates: dict | None = None,
    ) -> str:
        """在 LLM 自然语言回复后追加事实锚点摘要，防止回复内容与实际写入状态不一致。"""
        return SlotFillingHandler.ground_write_reply(
            self_or_reply,
            model_reply,
            accepted_updates=accepted_updates,
            unresolved_inputs=unresolved_inputs,
            missing_fields=missing_fields,
            display_updates=display_updates,
        )

    @staticmethod
    def _filter_robot_selection_unresolved(
        unresolved_items: list,
        accepted_updates: dict | None,
    ) -> list:
        """清理机器人选择迁移后的 unresolved 噪声。"""
        return SlotFillingHandler.filter_robot_selection_unresolved(
            unresolved_items,
            accepted_updates,
        )

    def _project_legacy_equipment_class_candidate(
        self,
        candidate: dict,
        task_type_key: str | None,
        task_state: dict | None,
    ) -> dict | None:
        """Map a legacy equipment_class candidate to family when unambiguous."""
        return self.slot_handler.project_legacy_equipment_class_candidate(
            candidate,
            task_type_key,
            task_state,
        )

    def _get_committed_update_display_values(self, accepted_updates: dict) -> dict:
        """从领域配置生成写入回执的展示值，不改变 SlotStore 标准值。"""
        return self.slot_handler.get_committed_update_display_values(accepted_updates)

    def _get_committed_turn_updates(
        self,
        proposed_updates: dict,
        state_before_turn: dict,
    ) -> dict:
        """返回本轮已由 SlotStore 提交的用户字段更新。"""
        return self.slot_handler.get_committed_turn_updates(
            proposed_updates,
            state_before_turn,
        )



    def _link_oilfield_update_in_transaction(
        self,
        updates: dict,
        new_slots: dict,
        user_message: str = "",
        extracted_oilfield: str | None = None,
    ) -> dict:
        return self.slot_handler.link_oilfield_update_in_transaction(
            updates,
            new_slots,
            user_message=user_message,
            extracted_oilfield=extracted_oilfield,
        )


    # --------------------------------------------------------------------------
    # 槽位填报、设备选型与任务迁移集群（委托至 SlotFillingHandler）
    # --------------------------------------------------------------------------

    def _apply_updates_in_transaction(self, *args, **kwargs):
        return self.slot_handler.apply_updates_in_transaction(*args, **kwargs)

    def _apply_normalized_plan_in_transaction(self, *args, **kwargs):
        return self.slot_handler.apply_normalized_plan_in_transaction(*args, **kwargs)

    def _apply_slot_update_in_transaction(self, *args, **kwargs):
        return self.slot_handler.apply_slot_update_in_transaction(*args, **kwargs)

    def _normalize_and_validate_in_transaction(self, *args, **kwargs):
        return self.slot_handler.normalize_and_validate_in_transaction(*args, **kwargs)

    def _source_for_resolution_method(self, *args, **kwargs):
        return self.slot_handler.source_for_resolution_method(*args, **kwargs)

    def _scope_confirmed_recommendation(self, *args, **kwargs):
        return self.slot_handler.scope_confirmed_recommendation(*args, **kwargs)

    def _scope_visible_ordinal_selections(self, *args, **kwargs):
        return self.slot_handler.scope_visible_ordinal_selections(*args, **kwargs)

    def _auto_collapse_robot_cascade(self, *args, **kwargs):
        return self.slot_handler.auto_collapse_robot_cascade(*args, **kwargs)

    def _handle_equipment_updates_in_transaction(self, *args, **kwargs):
        return self.slot_handler.handle_equipment_updates_in_transaction(*args, **kwargs)

    def _project_equipment_updates_for_evaluation(self, *args, **kwargs):
        return self.slot_handler.project_equipment_updates_for_evaluation(*args, **kwargs)

    def _resolve_task_type_target(self, *args, **kwargs):
        return self.slot_handler.resolve_task_type_target(*args, **kwargs)

    def _task_transition_shared_field_keys(self, *args, **kwargs):
        return self.slot_handler.task_transition_shared_field_keys(*args, **kwargs)

    def _build_task_transition_state(self, *args, **kwargs):
        return self.slot_handler.build_task_transition_state(*args, **kwargs)

    def _build_post_update_evaluation_context(self, *args, **kwargs):
        return self.slot_handler.build_post_update_evaluation_context(*args, **kwargs)

    def _dynamic_allowed_schema_keys(self, *args, **kwargs):
        return self.slot_handler.dynamic_allowed_schema_keys(*args, **kwargs)

    def _clear_non_inherited_transition_slots(self, *args, **kwargs):
        return self.slot_handler.clear_non_inherited_transition_slots(*args, **kwargs)

    def _normalize_transition_discovery_candidates(self, *args, **kwargs):
        return self.slot_handler.normalize_transition_discovery_candidates(*args, **kwargs)

    def _merge_task_transition_extractions(self, *args, **kwargs):
        return self.slot_handler.merge_task_transition_extractions(*args, **kwargs)

    def _task_selector_updates_from_extraction(self, *args, **kwargs):
        return self.slot_handler.task_selector_updates_from_extraction(*args, **kwargs)

    def _resolve_task_type_update_context(self, *args, **kwargs):
        return self.slot_handler.resolve_task_type_update_context(*args, **kwargs)

    def _record_task_type_update_error(self, *args, **kwargs):
        return self.slot_handler.record_task_type_update_error(*args, **kwargs)

    def _handle_task_type_update_in_transaction(self, *args, **kwargs):
        return self.slot_handler.handle_task_type_update_in_transaction(*args, **kwargs)

    def _handle_rov_description_in_transaction(self, *args, **kwargs):
        return self.slot_handler.handle_rov_description_in_transaction(*args, **kwargs)
    def _resolve_pending_oilfield_confirmation(
        self,
        user_message: str,
        request_id: str = "req_default",
        pending_action: str | None = None,
        subject_text: str | None = None,
    ) -> str | None:
        return self.slot_handler.resolve_pending_oilfield_confirmation(
            user_message,
            request_id=request_id,
            pending_action=pending_action,
            subject_text=subject_text,
        )

    def _build_pending_oilfield_reply(self) -> str | None:
        return self.slot_handler.build_pending_oilfield_reply()

    def _top_pending_oilfield_candidate(self, user_message: str = "") -> dict | None:
        return self.slot_handler.top_pending_oilfield_candidate(user_message)

    def _user_confirmed_oilfield(self, message: str) -> bool:
        return self.slot_handler.user_confirmed_oilfield(message)

    def _user_cancelled_oilfield(self, message: str) -> bool:
        return self.slot_handler.user_cancelled_oilfield(message)



    # --------------------------------------------------------------------------
    # 约束检查（硬解除后检查软）
    # --------------------------------------------------------------------------


    # --------------------------------------------------------------------------
    # 约束检查与决策集群（委托至 ConstraintDecisionHandler）
    # --------------------------------------------------------------------------

    def _merge_oilfield_context_violations(self, *args, **kwargs):
        return self.constraint_handler.merge_oilfield_context_violations(*args, **kwargs)

    def _is_state_snapshot_stale(self, *args, **kwargs):
        return self.constraint_handler.is_state_snapshot_stale(*args, **kwargs)

    def _run_constraint_check(self, *args, **kwargs):
        return self.constraint_handler.run_constraint_check(*args, **kwargs)

    def _get_kb_alternatives_for_violations(self, *args, **kwargs):
        return self.constraint_handler.get_kb_alternatives_for_violations(*args, **kwargs)

    def _merge_coordinate_updates(self, *args, **kwargs):
        return self.constraint_handler.merge_coordinate_updates(*args, **kwargs)

    def _invalidate_whitelist(self, *args, **kwargs):
        return self.constraint_handler.invalidate_whitelist(*args, **kwargs)

    def _is_whitelisted(self, *args, **kwargs):
        return self.constraint_handler.is_whitelisted(*args, **kwargs)
    @staticmethod
    def _is_business_identity_query(message: str) -> bool:
        text = message.strip().lower()
        identity_patterns = (
            "你是什么", "你是谁", "你是啥", "你的身份", "你叫什么",
            "介绍一下你自己", "自我介绍", "你能做什么", "你有什么功能", "what are you", "who are you",
        )

        return any(pattern in text for pattern in identity_patterns)


    @staticmethod
    def _user_confirmed(message: str) -> bool:
        keywords = ["确认", "没问题", "发布", "提交", "ok", "好的", "可以", "确定"]
        return any(kw in message.lower() for kw in keywords)

    @staticmethod
    def _is_final_publish_confirmation(message: str) -> bool:
        """仅识别明确具有‘发布/提交当前任务’语义的独立指令。"""
        text = re.sub(r"[\s，,。.!！?？、；;：:]+", "", message).lower()
        return text in {
            "确认发布",
            "确认并发布",
            "确认发布任务",
            "发布任务",
            "发布",
            "立即发布",
            "现在发布",
            "确认提交",
            "确认并提交",
            "提交任务",
            "提交",
            "确认开始",
            "确认开始任务",
        }

    @staticmethod
    def _is_confirmation_only(message: str) -> bool:
        """仅识别不携带参数更新的独立泛确认/认可指令。"""
        text = re.sub(r"[\s，,。.!！?？、；;：:]+", "", message).lower()
        return text in {
            "确认",
            "确认无误",
            "确认开始",
            "开始",
            "开始任务",
            "确定",
            "没问题",
            "好的",
            "可以",
            "ok",
            "继续",
        }

    @staticmethod
    def _is_ignore_warning(message: str) -> bool:
        """仅识别明确具有忽略/无视软警告语义的独立控制指令。"""
        text = re.sub(r"[\s，,。.!！?？、；;：:]+", "", message).lower()
        negated = ["不忽略", "不要忽略", "不能忽略", "别忽略", "不无视", "不要无视", "不是忽略"]
        if any(neg in text for neg in negated):
            return False
        return text in {
            "忽略警告",
            "忽略警告继续",
            "忽略软警告",
            "忽略软警告继续",
            "忽略",
            "无视警告",
            "无视软警告",
            "忽略此警告",
            "忽略当前警告",
            "无视此警告",
            "无视当前警告",
            "接受风险",
            "忽略风险",
            "无视",
        }




    def _ensure_constraint_details(self, *args, **kwargs):
        return self.constraint_handler.ensure_constraint_details(*args, **kwargs)
    def _reject_hard_constraint_bypass(self, user_message: str) -> str:
        return self.constraint_handler._reject_hard_constraint_bypass(user_message)

    @staticmethod
    def _user_cancelled(message: str) -> bool:
        negated_cancel = ["不是要取消", "不是取消", "不要取消", "别取消", "不取消", "免取消"]
        if any(neg in message for neg in negated_cancel):
            return False

        if any(mod_kw in message for mod_kw in ["修改", "参数", "设置", "载荷", "水深", "支持船", "管缆", "油田", "设备", "工具"]) and "任务" not in message:
            return False
        keywords = ["取消任务", "放弃任务", "终止任务", "取消", "放弃", "不要了", "终止", "退出", "算了一会儿再做", "先不搞了", "不要创建了", "撤销", "作废", "重新来", "清空", "不做了", "算了不用了", "先退出来", "终止创建", "不搞了", "不用了"]
        return any(kw in message for kw in keywords)


    @staticmethod
    def _user_requested_modification(message: str) -> bool:
        """判断用户是否明确要求覆盖已经录入的参数。"""
        keywords = (
            "修改",
            "改成",
            "改为",
            "改到",
            "更改",
            "更换",
            "换成",
            "换为",
            "调整",
            "重新设置",
            "设置为",
            "设为",
            "替换",
        )
        return any(keyword in message for keyword in keywords)

    @staticmethod
    def _is_payload_modification_request(user_message: str) -> bool:
        """判断用户是否明确请求重新选择/修改/配置载荷（且不属于取消修改指令）。"""
        return SlotFillingHandler.is_payload_modification_request(user_message)

    def _handle_payload_modification_request(self, user_message: str) -> str | None:
        """用户请求重新选择/修改/配置载荷时，重置 payload 槽位为 missing 并调整阶段供前端调出卡片。"""
        return self.slot_handler.handle_payload_modification(user_message)

    def _normalize_payload_list_mutations(
        self,
        extraction_res: dict,
        user_message: str,
        current_slots: dict,
    ) -> None:
        """兜底防护：当 LLM 抽取的 extraction_res 将 payload 误放入 slot_candidates 时，
        基于用户增量/减量意图或现有槽位，自动转换为 list_mutations（op: add/remove），
        防止列表字段被整体覆盖。
        """
        self.slot_handler.normalize_payload_list_mutations(
            extraction_res,
            user_message,
            current_slots,
        )

    # --------------------------------------------------------------------------
    # 状态查询与重置
    # --------------------------------------------------------------------------

    def get_status(self) -> dict:
        filled: dict = {}
        missing_display: list[dict] = []

        for k, v in self._last_built_json.items():
            if k.startswith("_"):
                continue
            label = FIELD_LABELS.get(k, k)
            filled[k] = {"label": label, "value": v}

        missing_source = self._last_missing
        task_type_key = self.task_state.get("task_type_key")
        if task_type_key:
            try:
                schema = self.builder.get_schema(task_type_key, self.mode)
                user_req_schema = [
                    field for field in schema
                    if field.get("type") not in ("auto", "fixed")
                ]
                missing_source = self.slot_store.get_missing_slots(
                    user_req_schema,
                    allowed_values_resolver=lambda field: self.builder.resolve_allowed_values(
                        field,
                        task_type_key,
                        self.task_state,
                    ),
                )
                self._last_missing = missing_source
            except Exception as exc:
                logger.warning(
                    "[DialogueManager] Failed to derive status missing fields from SlotStore: %s",
                    exc,
                )

        for m in missing_source:
            missing_display.append({
                "key": m["key"],
                "label": m["label"],
                "allowed_values": m.get("allowed_values", []),
            })

        return {
            "phase": self.phase,
            "workflow_phase": "validating" if self.phase in ("blocked_soft", "blocked_hard") else self.phase,
            "mode": self.mode,
            "dialogue_mode": self.dialogue_mode,
            "last_mode_transition": copy.deepcopy(self.last_mode_transition),
            "mode_transition_history": copy.deepcopy(self.mode_transition_history),
            "control_state": self.control_state,
            "last_control_request": copy.deepcopy(self.last_control_request),
            "filled": filled,
            "missing": missing_display,
            "whitelisted_soft": sorted({e[2] for e in self._soft_whitelist}),
        }

    def get_final_result(self) -> dict | None:
        return self.final_result

    def reset(self):
        with self._session_lock:
            self.conversation_history = []
            self.slot_store = SlotStore(self.kb)
            self.task_state = self.slot_store.get_task_state()
            self.mode = "normal"
            self.phase = "collecting"
            self.final_result = None
            self.awaiting_final_confirm = False
            self.task_start_now = False
            self._blocking_violations = []
            self._soft_whitelist = set()
            self._hard_refusal_counts = {}
            self._pending_rov_candidates = []
            self._last_built_json = {}
            self._last_missing = []
            self.control_state = "idle"
            self.last_control_request = None
            self.dialogue_mode = "task_collection"
            self.last_mode_transition = None
            self.mode_transition_history = []
            self._run_session_state_shadow_check(checkpoint="reset")

    def _commit_internal_slot_values(
        self,
        values: dict,
        clear_keys: tuple[str, ...] = (),
    ) -> None:
        """提交可信的内部派生值，保持 SlotStore 为唯一状态源。"""
        new_slots = self.slot_store.clone_slots()
        for key in clear_keys:
            slot = new_slots.get(key)
            if slot is None:
                continue
            slot.value = None
            slot.status = "missing"
            slot.candidate_value = None
            slot.raw_value = None
            slot.validation_error = None

        for key, value in values.items():
            if value is None:
                continue
            slot = new_slots.get(key)
            if slot is None:
                slot = Slot(slot_name=key)
                new_slots[key] = slot
            slot.value = value
            slot.status = "valid"
            slot.candidate_value = None
            slot.raw_value = None
            slot.validation_error = None

        self.slot_store.commit_transaction(
            new_slots,
            self.slot_store.unresolved,
        )
        self.task_state = self.slot_store.get_task_state()

    # --------------------------------------------------------------------------
    # 时间判断
    # --------------------------------------------------------------------------

    def is_start_time_near_now(self, time_window_minutes: int = 60) -> bool:
        return self.validator._is_task_start_now(self.task_state, time_window_minutes=time_window_minutes)

    # --------------------------------------------------------------------------
    # 缓存重建
    # --------------------------------------------------------------------------

    def _rebuild_cache(self, commit_derived: bool = True) -> None:
        """根据当前 slot_store 重新构建 task_state, _last_built_json 和 _last_missing"""
        self.task_state = self.slot_store.get_task_state()
        task_type_key = self.task_state.get("task_type_key")
        eq_type = self.task_state.get("equipment_type") or self.task_state.get("equipment_name")
        family_slot = self.slot_store.slots.get("equipment_family")
        family_is_materializable = family_slot is None or (
            family_slot.status == "missing" and family_slot.value is None
        )
        if (
            commit_derived
            and eq_type
            and not self.task_state.get("equipment_family")
            and family_is_materializable
        ):
            rov = self.kb.get_rov(eq_type)
            family = (rov.get("family_full_name") if rov else None) or (rov.get("family") if rov else None) or "ROV"

            self._commit_internal_slot_values({"equipment_family": family})
            self.task_state["equipment_family"] = family

        if task_type_key:
            b_dict, missing = self.builder.build(self.task_state, task_type_key, self.mode)
            if commit_derived and "task_id" in b_dict and not self.task_state.get("task_id"):
                self._commit_internal_slot_values(
                    {"task_id": b_dict["task_id"]}
                )
            self._last_missing = missing
        else:
            self._last_missing = [{
                "key": "task_type",
                "label": "任务类型",
                "type": "string",
                "allowed_values": self.kb.get_all_task_type_values()
            }]
        self.task_state = self.slot_store.get_task_state()
        self._last_built_json = self.slot_store.get_built_json()
        self.task_start_now = self.is_start_time_near_now()

    def export_snapshot(self) -> dict:
        """导出 Issue #10 会话状态快照。"""
        if is_session_state_v2_enabled():
            _ = self._build_session_state_contract()

        return {
            "snapshot_version": 2,
            "session_id": self.session_id,
            "conversation_history": copy.deepcopy(self.conversation_history),
            "phase": self.phase,
            "mode": self.mode,
            "dialogue_mode": self.dialogue_mode,
            "last_mode_transition": copy.deepcopy(self.last_mode_transition),
            "mode_transition_history": copy.deepcopy(self.mode_transition_history),
            "control_state": self.control_state,
            "last_control_request": copy.deepcopy(self.last_control_request),
            "slot_store": self.slot_store.export_snapshot(),
            "task_state": copy.deepcopy(self.task_state),
        }

    # --------------------------------------------------------------------------
    # 历史快照恢复
    # --------------------------------------------------------------------------

    def load_snapshot(self, snapshot: dict) -> None:
        """原子恢复旧版扁平快照和 snapshot_version=2 完整快照。"""
        session_state_v2_active = is_session_state_v2_enabled()

        with self._session_lock:
            candidate = DialogueManager(
                llm=self.llm,
                kb=self.kb,
                session_id=self.session_id,
            )
            # Legacy restore does not own this field, so preserve its existing
            # value unless the SessionState contract explicitly replaces it.
            candidate.awaiting_final_confirm = self.awaiting_final_confirm
            candidate._load_snapshot_in_place(
                snapshot,
                session_state_v2_active=session_state_v2_active,
            )
            self._commit_snapshot_runtime_state(candidate)
            self._run_session_state_shadow_check(checkpoint="load_snapshot")

    def _commit_snapshot_runtime_state(self, candidate: "DialogueManager") -> None:
        """Commit a fully validated candidate restore to the live manager."""
        runtime_fields = (
            "session_id",
            "conversation_history",
            "slot_store",
            "task_state",
            "mode",
            "phase",
            "final_result",
            "awaiting_final_confirm",
            "task_start_now",
            "_blocking_violations",
            "_soft_whitelist",
            "_hard_refusal_counts",
            "_pending_rov_candidates",
            "_last_built_json",
            "_last_missing",
            "control_state",
            "last_control_request",
            "dialogue_mode",
            "last_mode_transition",
            "mode_transition_history",
        )
        for field_name in runtime_fields:
            setattr(self, field_name, getattr(candidate, field_name))


    def _load_snapshot_in_place(
        self,
        snapshot: dict,
        *,
        session_state_v2_active: bool,
    ) -> None:
        """Restore and validate a snapshot on an isolated candidate manager."""
        if session_state_v2_active:
            contract_state = session_state_from_legacy_snapshot(snapshot)
        else:
            contract_state = None

        if not isinstance(snapshot, dict):
            raise ValueError("History snapshot must be a dictionary")

        if "session_id" in snapshot and snapshot["session_id"] is not None:
            self.session_id = str(snapshot["session_id"])

        conversation_history = snapshot.get("conversation_history", [])
        if not isinstance(conversation_history, list):
            raise ValueError("conversation_history must be a list")

        mode = snapshot.get("mode", "normal")
        phase = snapshot.get("phase", "collecting")

        if type(mode) is not str or mode not in VALID_TASK_MODES:
            raise ValueError(f"Invalid task mode in snapshot: {mode}")
        if type(phase) is not str or phase not in VALID_PHASES:
            raise ValueError(f"Invalid task phase in snapshot: {phase}")

        # 校验模式与控制快照字段（原子校验，失败则不更改内存状态）
        valid_modes = {"task_collection", "knowledge_qa", "emergency_intervention", "uncertain"}
        valid_control = {"idle", "stop_requested", "pause_requested", "abort_requested", "cancel_requested"}

        dialogue_mode = snapshot.get("dialogue_mode", "task_collection")
        if not isinstance(dialogue_mode, str) or dialogue_mode not in valid_modes:
            raise ValueError(f"Invalid dialogue_mode in snapshot: {dialogue_mode}")
        if dialogue_mode == "uncertain":
            dialogue_mode = "knowledge_qa"

        control_state = snapshot.get("control_state", "idle")
        if not isinstance(control_state, str) or control_state not in valid_control:
            raise ValueError(f"Invalid control_state in snapshot: {control_state}")

        mode_transition_history = snapshot.get("mode_transition_history", [])
        if not isinstance(mode_transition_history, list):
            raise ValueError("mode_transition_history must be a list")

        def _val_trans(item: Any) -> dict:
            if not isinstance(item, dict):
                raise ValueError("Transition item must be a dictionary")
            from_m = item.get("from")
            to_m = item.get("to")
            if not isinstance(from_m, str) or from_m not in valid_modes:
                raise ValueError(f"Invalid 'from' mode: {from_m}")
            if not isinstance(to_m, str) or to_m not in valid_modes:
                raise ValueError(f"Invalid 'to' mode: {to_m}")

            conf = item.get("confidence", 1.0)
            if isinstance(conf, bool):
                raise ValueError("confidence cannot be bool")
            if not isinstance(conf, (int, float)) or not math.isfinite(float(conf)) or not (0.0 <= float(conf) <= 1.0):
                raise ValueError(f"Invalid confidence: {conf}")

            changed_at = item.get("changed_at")
            if not isinstance(changed_at, str) or not changed_at.strip():
                raise ValueError("Invalid changed_at in transition")

            try:
                parsed = datetime.fromisoformat(changed_at)
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError(f"changed_at must be timezone-aware ISO format: {changed_at}")
            except Exception as e:
                raise ValueError(f"Invalid timezone-aware ISO timestamp '{changed_at}': {e}")

            return item

        validated_history = [_val_trans(t) for t in mode_transition_history]

        last_mode_transition = snapshot.get("last_mode_transition")
        if last_mode_transition is not None:
            last_mode_transition = _val_trans(last_mode_transition)

        last_control_request = snapshot.get("last_control_request")
        if last_control_request is not None:
            if not isinstance(last_control_request, dict):
                raise ValueError("last_control_request must be a dictionary")
            act = last_control_request.get("action")
            if not isinstance(act, str) or act not in {"stop", "pause", "abort", "cancel"}:
                raise ValueError(f"Invalid action in last_control_request: {act}")
            st = last_control_request.get("status")
            if not isinstance(st, str) or st != "requested":
                raise ValueError(f"Invalid status in last_control_request: {st}")

        # 校验 control_state 与 last_control_request 的严格一致性
        if last_control_request is None:
            if control_state != "idle":
                raise ValueError("control_state must be 'idle' when last_control_request is None")
        else:
            act = last_control_request["action"]
            expected_state = f"{act}_requested"
            if control_state != expected_state:
                raise ValueError(
                    f"Mismatched control_state '{control_state}' for action '{act}' (expected '{expected_state}')"
                )

        candidate_store = None

        if "slot_store" in snapshot and isinstance(snapshot.get("slot_store"), dict):
            candidate_store = SlotStore.from_snapshot(snapshot["slot_store"], self.kb)

        if candidate_store is None:
            # 兼容没有 snapshot_version/slot_store 的旧历史记录。
            legacy_state = snapshot.get("task_state", {})
            if not isinstance(legacy_state, dict):
                raise ValueError("task_state must be a dictionary")
            candidate_store = SlotStore(self.kb)
            task_type_key = legacy_state.get("task_type_key")
            if task_type_key:
                required_fields = self.builder.get_schema(task_type_key, mode)
                candidate_store.init_task_slots(required_fields)

            new_slots = candidate_store.clone_slots()
            for key, value in legacy_state.items():
                vtype = normalize_slot_value_type(None, value)
                if key in new_slots:
                    new_slots[key].value = copy.deepcopy(value)
                    new_slots[key].status = "valid"
                    new_slots[key].value_type = vtype
                else:
                    new_slots[key] = Slot(
                        slot_name=key,
                        value=copy.deepcopy(value),
                        status="valid",
                        value_type=vtype,
                    )
            if task_type_key and ("internal_id" not in new_slots or new_slots["internal_id"].value is None):
                new_slots["internal_id"] = Slot(
                    slot_name="internal_id",
                    value=str(uuid.uuid4()),
                    status="valid",
                    value_type="string",
                    source="snapshot_migration",
                )
            # Route the migrated flat state through SlotStore's authoritative
            # snapshot validator before committing it to the candidate
            # manager.  This keeps legacy restore behavior aligned with v2
            # snapshots (including exact selector rules and alias migration)
            # without mutating the live manager on failure.
            migrated_snapshot = candidate_store.export_snapshot()
            migrated_snapshot["snapshot_schema_version"] = None
            migrated_snapshot["slots"] = {
                key: slot.to_dict()
                for key, slot in new_slots.items()
            }
            migrated_snapshot["unresolved"] = []
            candidate_store.restore_snapshot(migrated_snapshot)


        # 候选 SlotStore 校验系统标识合法性与互斥结构完整性
        cand_internal_slot = candidate_store.slots.get("internal_id")
        cand_internal = cand_internal_slot.value if (cand_internal_slot and cand_internal_slot.status == "valid" and cand_internal_slot.value is not None) else None

        cand_task_id_slot = candidate_store.slots.get("task_id")
        cand_task_id = cand_task_id_slot.value if (cand_task_id_slot and cand_task_id_slot.status == "valid" and cand_task_id_slot.value is not None) else None

        cand_task_type_key_slot = candidate_store.slots.get("task_type_key")
        cand_task_type_key = cand_task_type_key_slot.value if (cand_task_type_key_slot and cand_task_type_key_slot.status == "valid" and cand_task_type_key_slot.value is not None) else None

        if cand_internal is not None:
            if not _ti_builder_module.validate_uuid4(str(cand_internal)):
                raise SnapshotValidationError(f"Invalid internal_id UUIDv4 in candidate snapshot: {cand_internal}")
            if cand_task_type_key is None:
                raise SnapshotValidationError(f"internal_id {cand_internal} present in candidate snapshot but task_type_key is missing")

        if cand_task_id is not None:
            if not validate_task_id(str(cand_task_id)):
                raise SnapshotValidationError(f"Invalid task_id format in candidate snapshot: {cand_task_id}")
            if not cand_task_type_key:
                raise SnapshotValidationError(f"task_id {cand_task_id} present in candidate snapshot but task_type_key is missing")
            if not validate_task_id_for_task_type(str(cand_task_id), cand_task_type_key, self.kb.task_schemas):
                raise SnapshotValidationError(f"task_id {cand_task_id} does not match task_type_key {cand_task_type_key} in candidate snapshot")
            if cand_internal is None or cand_task_type_key is None:
                raise SnapshotValidationError("v2 candidate snapshot with valid task_id must contain internal_id, task_id, and task_type_key simultaneously")

        # 候选 SlotStore 完整校验通过后再一次性替换，避免半恢复状态泄漏。
        self.conversation_history = copy.deepcopy(conversation_history)
        self.slot_store = candidate_store
        self.task_state = self.slot_store.get_task_state()

        if session_state_v2_active and contract_state is not None:
            self._apply_session_state_contract(contract_state)
        else:
            self.mode = mode
            self._switch_dialogue_mode(
                dialogue_mode,
                source="snapshot_restore",
                reason="restore validated snapshot state",
                restore_transition_state=(last_mode_transition, validated_history),
            )
            self._set_execution_control_state(
                control_state,
                last_control_request,
                source="snapshot_restore",
                reason="restore validated snapshot state",
            )

        self.final_result = None
        self.task_start_now = False
        # 清空阻塞与白名单，重新构建缓存
        self._blocking_violations = []
        self._soft_whitelist = set()
        self._hard_refusal_counts = {}
        self._pending_rov_candidates = []
        self._rebuild_cache(commit_derived=False)

        # ── 快照 Intent ID 校验与 done 阶段完整性校验 ──
        intent_slot = self.slot_store.slots.get("intent_id")
        intent_id_val = intent_slot.value if (intent_slot and intent_slot.status == "valid") else None
        is_valid_id = bool(intent_id_val and validate_intent_id(str(intent_id_val)))

        _REQUIRED_INTENT_KEYS = {
            "intent_id", "task_type", "priority", "time",
            "location", "task", "equipment", "conditions"
        }
        if phase == "done":
            validated = False
            _loaded_intent = None
            if is_valid_id and intent_id_val:
                try:
                    task_dir = _ti_builder_module.get_task_dir(create=False)
                    with _ti_builder_module.TaskPublishLock(task_dir):
                        pub_file = task_dir / f"task_intent_{intent_id_val}.json"
                        if pub_file.is_symlink():
                            logger.warning("[load_snapshot] final file is a symlink, rejecting done phase")
                        elif pub_file.is_file():
                            with open(pub_file, "r", encoding="utf-8") as _f:
                                _data = json.load(_f)
                            if isinstance(_data, dict) and _REQUIRED_INTENT_KEYS.issubset(_data.keys()):
                                _file_task_type = _data.get("task_type")
                                _file_intent_id = _data.get("intent_id")
                                _file_internal_id = _data.get("internal_id")
                                _file_task_id = _data.get("task_id")
                                _file_ver = _data.get("schema_version")

                                snap_internal_slot = self.slot_store.slots.get("internal_id")
                                snap_internal = snap_internal_slot.value if (snap_internal_slot and snap_internal_slot.status == "valid") else None

                                snap_task_id_slot = self.slot_store.slots.get("task_id")
                                snap_task_id = snap_task_id_slot.value if (snap_task_id_slot and snap_task_id_slot.status == "valid") else None

                                is_snap_v2 = bool(snap_internal or snap_task_id)
                                is_file_v2 = bool(_file_ver == 2 or _file_internal_id or _file_task_id)

                                if is_snap_v2 != is_file_v2:
                                    logger.warning("[load_snapshot] schema version mismatch: is_snap_v2=%s vs is_file_v2=%s", is_snap_v2, is_file_v2)
                                elif _file_intent_id != intent_id_val:
                                    logger.warning("[load_snapshot] intent_id mismatch in final file")
                                elif is_snap_v2 and (not _file_internal_id or snap_internal != _file_internal_id):
                                    logger.warning("[load_snapshot] internal_id mismatch or missing in final file: snap=%s vs file=%s", snap_internal, _file_internal_id)
                                elif is_snap_v2 and (not _file_task_id or snap_task_id != _file_task_id):
                                    logger.warning("[load_snapshot] task_id mismatch or missing in final file: snap=%s vs file=%s", snap_task_id, _file_task_id)
                                elif not _ti_builder_module.validate_task_intent(_data, self.kb.task_schemas):
                                    logger.warning("[load_snapshot] invalid task_type or TaskIntent structure in final file: %r", _file_task_type)
                                else:
                                    validated = True
                                    _loaded_intent = copy.deepcopy(_data)
                            else:
                                logger.warning("[load_snapshot] final file missing required keys")
                        else:
                            logger.warning("[load_snapshot] final file not found: %s", pub_file)
                except Exception as _e:
                    logger.warning("[load_snapshot] done-phase validation error: %s", _e)

            if validated:
                self._transition_phase(
                    "done",
                    source="snapshot_restore",
                    reason="validated published task file",
                )
                self.final_result = _loaded_intent
            else:
                self._transition_phase(
                    "collecting",
                    source="snapshot_restore",
                    reason="published task validation failed",
                )
                today = get_current_datetime().strftime("%Y%m%d")
                task_dir = _ti_builder_module.get_task_dir(create=False)
                new_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])
                new_slots = self.slot_store.clone_slots()
                if "intent_id" not in new_slots:
                    new_slots["intent_id"] = Slot("intent_id")
                new_slots["intent_id"].value = new_id
                new_slots["intent_id"].value_type = "string"
                new_slots["intent_id"].status = "valid"
                new_slots["intent_id"].source = "auto"
                self.slot_store.commit_transaction(new_slots, self.slot_store.unresolved)
                self.task_state = self.slot_store.get_task_state()
                self._last_built_json = self.slot_store.get_built_json()
        else:
            self._transition_phase(
                phase,
                source="snapshot_restore",
                reason="restore validated snapshot phase",
            )
            if not is_valid_id:
                today = get_current_datetime().strftime("%Y%m%d")
                task_dir = _ti_builder_module.get_task_dir(create=False)
                new_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])
                new_slots = self.slot_store.clone_slots()
                if "intent_id" not in new_slots:
                    new_slots["intent_id"] = Slot("intent_id")
                new_slots["intent_id"].value = new_id
                new_slots["intent_id"].value_type = "string"
                new_slots["intent_id"].status = "valid"
                new_slots["intent_id"].source = "auto"
                self.slot_store.commit_transaction(new_slots, self.slot_store.unresolved)
                self.task_state = self.slot_store.get_task_state()
                self._last_built_json = self.slot_store.get_built_json()

        if session_state_v2_active:
            _ = self._build_session_state_contract()
