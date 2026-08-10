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
import json
import logging
import math
import re
import threading
import os
import stat
import uuid
import logging
import threading
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
from .knowledge_retriever import KnowledgeBase, RobotSelectionDataError
from .extractor import ParameterExtractor
from .normalizer import FieldNormalizer
from .output_builder import OutputBuilder
from .validator import TaskValidator, Violation, ValidationResult
from .prompts import (
    build_responder_messages,
    build_general_chat_messages,
    build_knowledge_responder_messages,
    build_status_responder_messages,
)
from .task_intent_builder import TaskIntentBuilder
from .simulated_time import get_current_datetime
from .time_context import get_time_context, is_standalone_time_query
from .coord_parser import parse_coordinate_updates
from .oilfield_linker import OilfieldEntityLinker, _UNSET
from . import task_intent_builder as _ti_builder_module
from .id_sequence import validate_intent_id, validate_task_id, validate_task_id_for_task_type, next_daily_id
from .slot_store import (
    BASE_SLOT_TYPES,
    Slot,
    SlotStore,
    SnapshotValidationError,
    ValidationAcknowledgement,
    normalize_slot_value_type,
    validate_specification_object,
    validate_specification_selector_input,
)

from .exceptions import TaskPersistenceError, IntentIdConflict, IdReservationError, TaskRollbackError
from .intent_router import IntentRouter, IntentRouteResult
from .task_request_guard import analyze_task_request
from .result_paths import get_task_dir


HARD_REFUSAL_LIMIT = 4   # 连续拒绝上限


FIELD_LABELS = {
    "task_id":             "任务编号",
    "task_type":           "任务类型",
    "start_time":          "开始时间",
    "end_time":            "结束时间",
    "cable_position":      "管缆位置",
    "cable_type":          "管缆类型",
    "start_point":         "起始点经纬度",
    "end_point":           "结束点经纬度",
    "water_depth":         "水深（米）",
    "equipment_family":    "机器人系列",
    "equipment_type":      "设备型号",
    "equipment_name":      "设备全称",
    "payload":             "携带工具",
    "support_vessel":      "支持船编号",
    "oilfield_name":       "油田名称",
    "oilfield_coordinates":"油田经纬度",
    "wellhead_id":         "井口编号",
    # 采油树不再区分立式/卧式，停用该状态标签。
    # "tree_type":           "采油树类型",
}

# 软约束忽略关键词
SOFT_IGNORE_KEYWORDS = {"忽略", "继续", "确认", "无视", "不管", "没关系", "ok", "好的", "是"}


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

        # 对话核心状态
        self.conversation_history: list[dict] = []
        self.slot_store = SlotStore(kb)
        self.task_state: dict = self.slot_store.get_task_state()
        self.mode: str = "normal"
        self.phase: str = "collecting"
        self.final_result: dict | None = None
        self.awaiting_final_confirm = False
        self.task_start_now = False

        # 约束管理状态
        self._blocking_violations: list[Violation] = []
        self._soft_whitelist: set[tuple[str, str, str]] = set()
        self._hard_refusal_counts: dict[str, int] = {}

        # ROV候选暂存
        self._pending_rov_candidates: list[dict] = []

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
        if old_mode != new_mode:
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
        if old_mode != new_mode:
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
            reply = self._process_internal(user_message, request_id)
            self._run_session_state_shadow_check(checkpoint="process", request_id=request_id)
            return reply

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

    def _handle_non_task_route(self, user_message: str, route: IntentRouteResult, request_id: str) -> str:
        # 1. 记录前置快照镜像（用于严格的只读状态不变性断言）
        initial_version = self.slot_store.version
        initial_snapshot = copy.deepcopy(self.slot_store.export_snapshot())
        initial_unresolved = list(self.slot_store.unresolved)
        initial_task_state = copy.deepcopy(self.task_state)
        initial_built_json = copy.deepcopy(self._last_built_json)
        initial_missing = copy.deepcopy(self._last_missing)
        initial_phase = self.phase
        initial_mode = self.mode
        initial_rov_candidates = copy.deepcopy(self._pending_rov_candidates)

        query_intent = route.query_intent

        if query_intent in ("TOOL_QUERY", "DEVICE_CAPABILITY", "KNOWLEDGE_QA"):
            reply = self._handle_knowledge_query(user_message, route)
        elif query_intent in ("TASK_STATUS", "DEVICE_STATUS", "ENVIRONMENT_QUERY"):
            reply = self._handle_status_query(user_message, route)
        elif query_intent in ("GENERAL_CHAT", "CLARIFICATION"):
            reply = self._handle_general_chat(user_message, route)
        elif query_intent == "UNKNOWN":
            reply = self._handle_unknown_intent(user_message, route)
        else:
            reply = self._handle_unknown_intent(user_message, route)

        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})

        # 2. 状态不变性断言与校验
        v_ok = (self.slot_store.version == initial_version)
        s_ok = (self.slot_store.export_snapshot() == initial_snapshot)
        u_ok = (self.slot_store.unresolved == initial_unresolved)
        t_ok = (self.task_state == initial_task_state)
        b_ok = (self._last_built_json == initial_built_json)
        m_ok = (self._last_missing == initial_missing)
        p_ok = (self.phase == initial_phase)
        mo_ok = (self.mode == initial_mode)
        r_ok = (self._pending_rov_candidates == initial_rov_candidates)

        if not (v_ok and s_ok and u_ok and t_ok and b_ok and m_ok and p_ok and mo_ok and r_ok):
            logger.critical(
                f"[CRITICAL] State invariance violation in non-task route '{route.query_intent}'! "
                f"ver_ok={v_ok}, snap_ok={s_ok}, unres_ok={u_ok}, state_ok={t_ok}, built_ok={b_ok}, miss_ok={m_ok}"
            )
            raise RuntimeError(f"State invariance violation in non-task route {route.query_intent}")

        return reply

    def _build_knowledge_fallback(self, kb_evidence: dict) -> str:
        query_type = kb_evidence.get("query_type")
        query_mode = kb_evidence.get("query_mode")
        if query_type == "TOOL_QUERY" or query_mode == "tool_list":
            matched_payloads = []
            tools = []
            for item in kb_evidence.get("results", []):
                if isinstance(item, dict):
                    if item.get("category") == "payload_catalog" and item.get("matched_payloads"):
                        matched_payloads = item.get("matched_payloads")
                    elif item.get("category") == "all_supported_tools":
                        tools = item.get("tools", [])

            if matched_payloads:
                p_desc = "；".join(f"【{p.get('name')}】：{p.get('description')}" for p in matched_payloads if p.get("name"))
                if p_desc:
                    return f"已查询到相关载荷说明：{p_desc}"

            if tools:
                return "当前机器人可搭载的工具与负载包括：" + "、".join(map(str, tools)) + "。"

        if (
            query_type == "DEVICE_CAPABILITY"
            and query_mode == "device_list"
        ):
            results = kb_evidence.get("results", [])
            names = [
                item.get("full_name") or item.get("display_name") or item.get("robot_class_name")
                for item in results
                if isinstance(item, dict) and (item.get("full_name") or item.get("display_name") or item.get("robot_class_name"))
            ]
            unique_names = list(dict.fromkeys(names))
            if unique_names:
                return "当前可查询的设备包括：" + "、".join(unique_names) + "。"

        return "当前知识库已检索到相关信息，但暂时无法生成完整回答。"

    def _safe_llm_chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1500,
        role: ModelRole | str | None = None,
    ) -> str:
        try:
            return self.llm.chat(messages, temperature=temperature, max_tokens=max_tokens, role=role)
        except TypeError as exc:
            if not _is_unsupported_role_keyword_error(exc):
                raise
            return self.llm.chat(messages, temperature=temperature, max_tokens=max_tokens)

    def _safe_llm_filter_reply(
        self,
        reply: Any,
        role: ModelRole | str | None = None,
    ) -> str:
        try:
            return self.llm.filter_reply(reply, role=role)
        except TypeError as exc:
            if not _is_unsupported_role_keyword_error(exc):
                raise
            return self.llm.filter_reply(reply)

    def _handle_knowledge_query(
        self,
        user_message: str,
        route: IntentRouteResult,
        request_id: str = "req_default",
    ) -> str:
        context = {
            "task_type_key": self.task_state.get("task_type_key"),
            "equipment_type": (
                self.task_state.get("equipment_type")
                or self.task_state.get("equipment_name")
            ),
            "phase": self.phase,
            "mode": self.mode,
            "user_requirements": self.slot_store.get_built_json(),
            "missing_slots": [m.get("label") for m in self._last_missing if isinstance(m, dict)],
        }
        kb_evidence = self.kb.execute_typed_query(route.query_intent, user_message, context=context)
        if not kb_evidence.get("found"):
            reason = kb_evidence.get("reason")
            if reason == "device_not_resolved":
                return "项目知识库中未找到该设备信息，请说明具体的机器人型号或名称；您也可以查询当前支持的所有机器人。"

            elif reason == "ambiguous_device_alias":
                alias = kb_evidence.get("matched_alias", "该设备")
                cands = kb_evidence.get("candidate_entities", [])
                return f"设备别名【{alias}】对应多个候选设备，请明确说明具体型号系列。"
            elif reason == "no_matching_device":
                return "当前知识库中未检索到符合您询问条件的机器人设备。"
            elif reason == "unsupported_relation":
                return "当前暂不支持该维度的查询，您可以查询机器人的能力、载荷、所属系列或适合作业水深。"
            else:
                return "当前知识库未提供该信息。"

        if route.query_intent == "DEVICE_CAPABILITY" and kb_evidence.get("query_mode") == "device_check":
            results = kb_evidence.get("results", [])
            depth_cond = kb_evidence.get("depth_condition", {})
            target_depth = depth_cond.get("depth_m")
            unmet_devices = [r for r in results if r.get("matches_depth_condition") is False]
            all_devices_unmet = bool(results) and len(unmet_devices) == len(results)
            if all_devices_unmet and target_depth:
                dev = unmet_devices[0]
                dev_name = dev.get("robot_class_name") or dev.get("full_name") or "目标设备"
                max_d = dev.get("max_depth_m")
                return f"已识别设备【{dev_name}】，其最大作业水深为 {max_d}米，无法满足您询问的 {target_depth}米 作业要求。"

        messages = build_knowledge_responder_messages(kb_evidence, self.conversation_history, user_message)
        reply = self._safe_llm_chat(messages, temperature=0.1, role=ModelRole.KNOWLEDGE_QA)
        result_items = kb_evidence.get("results", [])
        all_devices_unmet = bool(result_items) and all(
            item.get("matches_depth_condition") is False
            for item in result_items
        )
        if not reply or not reply.strip() or ("符合条件" in reply and all_devices_unmet):
            if route.query_intent == "DEVICE_CAPABILITY" and kb_evidence.get("query_mode") == "device_check":
                if all_devices_unmet:
                    dev = result_items[0]
                    dev_name = dev.get("robot_class_name") or dev.get("full_name") or "目标设备"
                    max_d = dev.get("max_depth_m")
                    target_d = kb_evidence.get("depth_condition", {}).get("depth_m")
                    return f"已识别设备【{dev_name}】，其最大作业水深为 {max_d}米，无法满足您询问的 {target_d}米 作业要求。"
            if kb_evidence.get("found"):
                return self._build_knowledge_fallback(kb_evidence)
            return "当前知识库未提供该信息。"
        return self._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)


    def _handle_status_query(self, user_message: str, route: IntentRouteResult) -> str:
        if route.query_intent == "TASK_STATUS":
            status_evidence = {
                "query_type": "TASK_STATUS",
                "phase": self.phase,
                "mode": self.mode,
                "task_type": self.task_state.get("task_type", "(未确定)"),
                "collected_slots": self._last_built_json,
                "missing_slots": [m.get("label") for m in self._last_missing if isinstance(m, dict)],
                "found": True,
            }
        else:
            equipment = self.task_state.get("equipment_name") or self.task_state.get("equipment_type")
            if not equipment:
                alias_index = self.kb.get_device_alias_index()
                matched_alias = None
                for alias in sorted(alias_index.keys(), key=len, reverse=True):
                    if len(alias) >= 2 and alias in user_message:
                        matched_alias = alias
                        break
                if matched_alias:
                    equipment = matched_alias
                else:
                    rov_match = self.kb._find_rov(user_message)
                    if rov_match:
                        equipment = rov_match.get("full_name") or rov_match.get("variant_name")
                    else:
                        unit_match = self.kb.resolve_robot_unit(user_message)
                        if unit_match:
                            equipment = unit_match.get("robot", {}).get("full_name") or unit_match.get("unit_id")
            has_realtime = False
            state_dict = None
            if equipment and route.query_intent in ("DEVICE_STATUS", "DEVICE_CAPABILITY", "ENVIRONMENT_QUERY"):
                state_dict = self.kb.get_robot_state_dict(equipment)
                if state_dict and any(v is not None for v in state_dict.values()):
                    has_realtime = True

            if has_realtime:
                status_evidence = {
                    "query_type": route.query_intent,
                    "target": equipment,
                    "state_data": state_dict,
                    "found": True,
                }
            else:
                return "当前实时状态源尚未建立或暂时不可用，无法确认设备/环境的最新状态。"

        messages = build_status_responder_messages(status_evidence, self.conversation_history, user_message)
        reply = self._safe_llm_chat(messages, temperature=0.1, role=ModelRole.KNOWLEDGE_QA)
        if not reply or not reply.strip():
            return f"当前任务处于【{self.phase}】阶段，已收集 {len(self._last_built_json)} 个字段。"
        return self._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)

    def _handle_general_chat(self, user_message: str, route: IntentRouteResult) -> str:
        messages = build_general_chat_messages(self.conversation_history, user_message)
        reply = self._safe_llm_chat(messages, temperature=0.7, role=ModelRole.KNOWLEDGE_QA)
        if not reply or not reply.strip():
            reply = "您好！我是水下多智能体任务决策大模型。请问有什么可以帮您的？"
        return self._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)

    def _handle_unknown_intent(self, user_message: str, route: IntentRouteResult) -> str:
        return "对不起，我没有完全理解您的意思。请问您是要新建水下任务、修改任务参数，还是查询设备工具与系统功能？"

    # --------------------------------------------------------------------------
    # TASK_CONFIRM 独立控制指令处理（彻底隔离于槽位抽取流水线）
    # --------------------------------------------------------------------------

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
            if ack.task_version != validation_result.task_version:
                continue
            if ack.validation_version != validation_result.validation_version:
                continue
            if ack.validation_fingerprint != validation_result.validation_fingerprint:
                continue
            if ack.status_ref != status_ref:
                continue
            if ack.state_version != state_version:
                continue
            if ack.constraint_id in violation_map:
                v = violation_map[ack.constraint_id]
                if ack.value != getattr(v, "observed_value", None) and ack.field not in getattr(v, "related_fields", []):
                    continue
            valid_acks.append(ack)
        return valid_acks

    def _handle_soft_warning_confirmation(self, user_message: str, request_id: str) -> str:
        """blocked_soft 阶段的确认/忽略处理。

        将已确认忽略的软警告录入 SlotStore.validation_acknowledgements 绑定快照版本，
        清除 _blocking_violations，然后根据缺失槽位决定进入 collecting 或 confirming。
        """
        res = self._refresh_validation(purpose="interactive")
        status_ref = res.state_snapshot.get("status_ref") if res.state_snapshot else None
        state_ver = res.state_snapshot.get("state_version") if res.state_snapshot else None

        if self._blocking_violations:
            for v in self._blocking_violations:
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
                    if ack not in self.slot_store.validation_acknowledgements:
                        self.slot_store.validation_acknowledgements.append(ack)
                for f in v.related_fields:
                    val = self.task_state.get(f)
                    if val is not None:
                        self._soft_whitelist.add((f, str(val), v.constraint_id))
            self._blocking_violations = []

        # 重新检查约束（使用白名单过滤后的结果）
        res = self._refresh_validation(purpose="interactive")
        all_violations = res.violations
        remaining_soft = [v for v in all_violations
                          if v.severity == "soft" and not self._is_whitelisted(v)]
        remaining_hard = [v for v in all_violations if v.severity == "hard"]

        if remaining_hard:
            self._transition_phase("blocked_hard", reason="hard_constraint_detected")
            self._blocking_violations = remaining_hard
        elif remaining_soft:
            self._transition_phase("blocked_soft", reason="soft_warning_detected")
            self._blocking_violations = remaining_soft
        else:
            # 检查是否有缺失槽位
            task_type_key = self.task_state.get("task_type_key")
            if task_type_key:
                req_schema = self.builder.get_schema(task_type_key, self.mode)
                user_req_schema = [f for f in req_schema if f.get("type") not in ("auto", "fixed")]
                missing = self.slot_store.get_missing_slots(
                    user_req_schema,
                    allowed_values_resolver=lambda field: self.builder.resolve_allowed_values(
                        field,
                        task_type_key,
                        self.task_state,
                    ),
                )
                self._last_missing = missing
                if not missing:
                    self._transition_phase("confirming", reason="required_slots_complete")
                else:
                    self._transition_phase("collecting", reason="required_slots_missing")
            else:
                self._transition_phase("collecting", reason="task_type_missing")

        # 生成回复
        knowledge_context = self.kb.get_context_for_state(self.task_state)
        built = self._last_built_json
        missing = self._last_missing
        constraint_context = {"type": "none", "violations": [], "hard_refusal_counts": {}}
        if remaining_hard:
            constraint_context = {"type": "hard", "violations": remaining_hard, "hard_refusal_counts": {}}
        elif remaining_soft:
            constraint_context = {"type": "soft", "violations": remaining_soft, "hard_refusal_counts": {}}

        messages = build_responder_messages(
            task_state=self.task_state,
            built_json=built,
            missing_fields=missing,
            mode=self.mode,
            phase=self.phase,
            knowledge_context=knowledge_context,
            constraint_context=constraint_context,
            conversation_history=self.conversation_history,
            latest_user_message=user_message,
            ROV2type=self.kb.ROV2type,
            support_task=self.kb.get_supported_task(),
            slot_snapshot=self.slot_store.get_slot_snapshot(),
        )
        reply = self._safe_llm_chat(messages, temperature=0.7, max_tokens=1500, role=ModelRole.TASK_RESPONDER)
        reply = self._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)
        reply = self._ensure_constraint_details(reply, constraint_context)

        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    def _handle_final_publish_confirmation(self, user_message: str, request_id: str) -> str:
        """confirming 阶段的唯一正式确认发布处理。

        使用已有 SlotStore 内的 valid intent_id 关联并发布文件。
        不重新调用 extractor，不修改 SlotStore。
        """
        if self.phase != "confirming":
            reply = "当前没有处于等待确认状态的任务。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        prev_phase = self.phase
        prev_snap = self.slot_store.export_snapshot()
        prev_whitelist = copy.deepcopy(self._soft_whitelist)
        prev_missing = copy.deepcopy(self._last_missing)
        prev_pending_rov = copy.deepcopy(self._pending_rov_candidates)
        prev_blocking_violations = copy.deepcopy(self._blocking_violations)
        prev_hist = list(self.conversation_history)
        prev_task_start_now = self.task_start_now

        task_type_key = self.task_state.get("task_type_key")
        cand_state = copy.deepcopy(self.task_state)
        cand_built = copy.deepcopy(self._last_built_json)

        # 运行时设备可用性重新校验 (Issue #12)
        unit_id = cand_state.get("equipment_unit_id") or cand_built.get("equipment_unit_id")
        if not unit_id and self.slot_store.slots.get("equipment_unit_id"):
            unit_slot = self.slot_store.slots.get("equipment_unit_id")
            if unit_slot and unit_slot.status == "valid":
                unit_id = unit_slot.value

        if unit_id:
            runtime_res = self.kb.state_info.check_runtime_availability(str(unit_id))
            if not runtime_res.get("available"):
                self._transition_phase("blocked_hard", reason="runtime_equipment_unavailable")
                reply = runtime_res.get("message") or f"无法发布任务：机器人 {unit_id} 当前不可用。"
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply

        # 最终约束全量检查
        val_res = self._refresh_validation(purpose="publish")
        all_violations = val_res.violations
        has_hard = self.validator.has_hard_violations(all_violations) or val_res.overall_status == "validation_error"
        unwhitelisted_soft = [v for v in all_violations if v.severity == "soft" and not self._is_whitelisted(v)]

        # 检查缺失（排除 auto 和 fixed 等由系统自动管理的字段，如 task_id）
        if task_type_key:
            req_schema = self.builder.get_schema(task_type_key, self.mode)
            user_req_schema = [f for f in req_schema if f.get("type") not in ("auto", "fixed")]
            missing = self.slot_store.get_missing_slots(user_req_schema)
        else:
            missing = [{"key": "task_type", "label": "任务类型"}]

        if missing or has_hard or unwhitelisted_soft:
            if has_hard:
                self._transition_phase("blocked_hard", reason="publish_hard_constraint_detected")
                hard_violations = [v for v in all_violations if v.severity == "hard"]
                self._blocking_violations = hard_violations
                if hard_violations:
                    reply = "\n".join(v.message for v in hard_violations)
                else:
                    reply = val_res.error.get("message") if (val_res and val_res.error) else "当前任务参数包含硬性约束冲突，无法发布。"
            elif unwhitelisted_soft:
                self._transition_phase("blocked_soft", reason="publish_soft_warning_detected")
                self._blocking_violations = unwhitelisted_soft
                reply = "\n".join(v.message for v in unwhitelisted_soft)
            else:
                self._transition_phase("collecting", reason="publish_slots_missing")
                reply = "当前任务参数不满足发布条件，请补充或修正参数。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # 检查 intent_id 是否在 SlotStore/built_json 中有效存在 (Fail Closed)
        intent_id = cand_built.get("intent_id") or cand_state.get("intent_id")
        intent_slot = self.slot_store.slots.get("intent_id")
        if not intent_id or not intent_slot or intent_slot.status != "valid" or not validate_intent_id(intent_slot.value):
            reply = "当前任务缺少唯一任务标识(intent_id)，无法完成确认发布。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # 准备发布：正式且唯一预约任务业务编号（在跨进程锁内原子递增）。
        # reserve_task_id() 的返回值是唯一权威正式编号，必须覆盖任何草稿阶段的 preview。
        # 整个发布流程（reserve -> commit -> prepare -> staging -> publish）包含在单一 try...except 回滚保护块内。
        try:
            official_task_id = self.builder.reserve_task_id(task_type_key)

            # 正式编号通过 SlotStore 单一事务写入工作副本，维持 SSOT 与乐观锁 (store_version / version)
            publish_slots = copy.deepcopy(self.slot_store.slots)
            if "task_id" not in publish_slots:
                publish_slots["task_id"] = Slot("task_id")
            publish_slots["task_id"].value = official_task_id
            publish_slots["task_id"].status = "valid"
            publish_slots["task_id"].source = "auto_reserved"
            publish_slots["task_id"].candidate_value = None
            publish_slots["task_id"].raw_value = None
            publish_slots["task_id"].value_type = "string"
            publish_slots["task_id"].validation_error = None

            expected_version = self.slot_store.version
            self.slot_store.commit_transaction(
                publish_slots,
                list(self.slot_store.unresolved),
                request_id=request_id,
                expected_version=expected_version,
            )

            # 提交后从 SlotStore 统一重新派生权威状态，绝对不手工篡改各缓存副本
            self.task_state = self.slot_store.get_task_state()
            self._last_built_json = self.slot_store.get_built_json()

            cand_state = dict(self.task_state)
            cand_built = dict(self._last_built_json)

            # TOCTOU 防线：在最终写盘发布前核对 state_version
            if unit_id and val_res and getattr(val_res, "state_snapshot", None):
                try:
                    current_state_snap = self.kb.get_unit_state_snapshot(str(unit_id))
                    if current_state_snap.get("state_version") != val_res.state_snapshot.get("state_version"):
                        re_val_res = self._refresh_validation(purpose="publish")
                        # 重新校验后执行全量门禁检查：
                        # validation_error / blocked_hard -> 阻断并回滚
                        if re_val_res.overall_status in ("blocked_hard", "validation_error"):
                            raise TaskPersistenceError(f"单机 {unit_id} 的状态遥测在确认发布过程中发生变更，触发阻断告警。")
                        elif re_val_res.overall_status == "blocked_soft":
                            valid_acks = self._get_valid_acknowledgements(re_val_res)
                            unacked = [
                                v for v in (re_val_res.violations or [])
                                if getattr(v, "severity", "") == "soft" and not any(a.constraint_id == v.constraint_id for a in valid_acks)
                            ]
                            if unacked:
                                self._transition_phase("blocked_soft", reason="telemetry_soft_warning_detected")
                                self._blocking_violations = unacked
                                raise TaskPersistenceError(f"单机 {unit_id} 的状态遥测在确认发布过程中发生变更，触发未确认的软性告警。")
                        elif re_val_res.overall_status == "pending_runtime_validation" and is_now:
                            raise TaskPersistenceError(f"单机 {unit_id} 的状态遥测在确认发布过程中发生变更，实时任务无法在缺乏遥测时发布。")
                        val_res = re_val_res
                except Exception as check_exc:
                    if isinstance(check_exc, TaskPersistenceError):
                        raise check_exc
                    raise TaskPersistenceError(f"发布前单机状态复核失败 (fail closed): {check_exc}") from check_exc

            # 准备发布：仅传入匹配当前 validation_result 的有效确认
            valid_acknowledgements = self._get_valid_acknowledgements(val_res)
            ti_builder = TaskIntentBuilder(self.kb)
            ti_json_artifact = ti_builder.prepare(
                task_state=cand_state,
                built_json=cand_built,
                mode=self.mode,
                task_type_key=task_type_key,
                intent_id=intent_id,
                validation_result=val_res,
                validation_acknowledgements=valid_acknowledgements,
            )
            staging_file = ti_builder.create_staging(ti_json_artifact)

            expected_state_ver = val_res.state_snapshot.get("state_version") if (val_res and val_res.state_snapshot) else None
            if unit_id and expected_state_ver is not None and hasattr(self.kb, "state_info") and hasattr(self.kb.state_info, "guard_unit_state_version"):
                with self.kb.state_info.guard_unit_state_version(str(unit_id), expected_state_ver):
                    ti_builder.publish_staging(staging_file, ti_json_artifact)
            else:
                ti_builder.publish_staging(staging_file, ti_json_artifact)
        except Exception as exc:
            # 回滚：包含 reserve, commit_transaction, prepare, create_staging, publish_staging 在内的全流程失败保护
            target_phase = self.phase if self.phase == "blocked_soft" else prev_phase
            target_blocking = copy.deepcopy(self._blocking_violations) if self.phase == "blocked_soft" else prev_blocking_violations
            self._transition_phase(target_phase, reason="publish_rollback")
            self.final_result = None

            rollback_failed = False
            rollback_err = None
            if prev_snap:
                try:
                    self.slot_store.restore_snapshot(prev_snap)
                except Exception as rb_e:
                    rollback_failed = True
                    rollback_err = rb_e

            self.task_state = self.slot_store.get_task_state()
            self._last_built_json = self.slot_store.get_built_json()
            self._soft_whitelist = prev_whitelist
            self._pending_rov_candidates = prev_pending_rov
            self._blocking_violations = target_blocking
            self.conversation_history = prev_hist
            self.task_start_now = prev_task_start_now

            self._last_missing = prev_missing

            logger.error(
                "TaskIntent publish failed: request_id=%s, task_id=%s, intent_id=%s, err_type=%s, err=%s, rollback_failed=%s",
                request_id,
                cand_built.get("task_id", "unknown"),
                intent_id,
                type(exc).__name__,
                exc,
                rollback_failed,
                exc_info=True,
            )

            if rollback_failed:
                raise TaskRollbackError(f"TaskIntent publish failed ({exc}) and rollback error occurred: {rollback_err}") from exc
            if isinstance(exc, (TaskPersistenceError, IntentIdConflict, IdReservationError)):
                raise exc
            else:
                raise TaskPersistenceError(f"TaskIntent publish failed: {exc}") from exc

        # 发布成功
        self._transition_phase("done", reason="publish_success")
        self.task_state = self.slot_store.get_task_state()
        self._last_built_json = self.slot_store.get_built_json()
        self.final_result = self._last_built_json
        self.task_start_now = self.is_start_time_near_now()
        if self.task_start_now:
            reply = (f"✅ 信息收集完成，当前为【立即执行任务】，任务已生成并下发。\n"
                     f"{json.dumps(cand_built, ensure_ascii=False, indent=2)}")
        else:
            reply = (f"✅ 信息收集完成，当前为【未来规划任务】，已加入计划池。\n"
                     f"{json.dumps(cand_built, ensure_ascii=False, indent=2)}")
        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})
        return reply

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

        if self._is_business_identity_query(user_message):
            self._switch_dialogue_mode("knowledge_qa", source="fast_path", reason="通用身份/常规对话问答")
            reply = "我是一个专业的水下多智能体任务决策大模型，可以协助您进行水下任务规划、参数收集与可行性验证。请描述您的水下任务需求，我会继续帮您完善任务参数。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply


        if is_standalone_time_query(user_message):
            self._switch_dialogue_mode("knowledge_qa", source="fast_path", reason="系统时间/环境状态查询")
            reply = get_time_context().user_reply
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        if self.phase == "done" and (
            self._is_confirmation_only(user_message)
            or self._is_final_publish_confirmation(user_message)
        ):
            self._switch_dialogue_mode("task_collection", source="user_confirmation", reason="已发布任务重复确认")
            intent_id = self.task_state.get("intent_id") or self._last_built_json.get("intent_id")
            intent_detail = f"（intent_id: {intent_id}）" if intent_id else ""
            reply = f"任务已发布成功{intent_detail}，无需重复发布。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        pending_reply = self._resolve_pending_oilfield_confirmation(user_message, request_id=request_id)
        if pending_reply is not None:
            self._switch_dialogue_mode("task_collection", source="user_confirmation", reason="待确认油田消解")
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": pending_reply})
            return pending_reply

        # 控制动作由 DialogueManager 当前阶段处理，不再交给 IntentRouter 判断。
        if self.phase == "blocked_hard" and (
            self._is_confirmation_only(user_message)
            or self._is_soft_warning_acknowledgement(user_message)
            or self._is_final_publish_confirmation(user_message)
        ):
            return self._reject_hard_constraint_bypass(user_message)

        if self.phase == "blocked_soft":
            if self._is_soft_warning_acknowledgement(user_message):
                return self._handle_task_confirm(user_message, request_id)
            elif self._is_final_publish_confirmation(user_message) or self._is_confirmation_only(user_message):
                reply = "当前仍存在软警告。请先修改相关参数，或明确回复‘忽略警告’后继续。"
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply

        if self.phase == "confirming":
            if self._is_final_publish_confirmation(user_message):
                return self._handle_task_confirm(user_message, request_id)
            elif self._is_confirmation_only(user_message):
                reply = "当前任务尚未发布。如确认无误，请回复‘确认发布’；如需调整，可直接说明要修改的参数。"
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply

        # ── 独立意图路由分流阶段 ──
        expected_slots = [m["key"] for m in self._last_missing if isinstance(m, dict) and "key" in m]
        route = self.intent_router.route(
            user_message=user_message,
            conversation_history=self.conversation_history,
            task_state=self.task_state,
            phase=self.phase,
            expected_slots=expected_slots,
        )

        self._switch_dialogue_mode(
            route.dialogue_mode,
            source=route.source,
            confidence=route.confidence,
            reason=route.reason,
        )

        if route.dialogue_mode == "emergency_intervention":
            return self._handle_emergency_intervention(user_message, route, request_id)

        if route.dialogue_mode == "knowledge_qa":
            return self._handle_non_task_route(user_message, route, request_id)

        compound_request = analyze_task_request(
            user_message,
            self.kb.get_all_task_type_values(),
        )
        if compound_request.should_block:
            reply = compound_request.build_reply()
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # 3. Parameter Extraction & Processing Pipeline (Atomic Transaction with Optimistic Lock)
        task_patch_v2_active = is_task_patch_v2_enabled()
        norm_v2_active = is_normalization_contract_v2_enabled()
        validate_normalization_runtime_flags(task_patch_v2_active, norm_v2_active)

        new_slots, new_unresolved, expected_version = self.slot_store.snapshot()

        task_type_key = new_slots.get("task_type_key").value if new_slots.get("task_type_key") else None
        had_task_type_key_at_turn_start = task_type_key is not None
        current_state = self.slot_store.get_task_state()
        state_before_turn = dict(current_state)

        merged_updates = {}
        merged_updates_meta = {}
        payload_mutation_failed = False
        mutation_failure_result = None
        list_mutations = []

        extraction_res = {}
        proposed_pending_rov = list(self._pending_rov_candidates)
        turn_unresolved: list = []

        def record_unresolved(result: dict) -> None:
            for item in result.get("unresolved", []):
                if item not in turn_unresolved:
                    turn_unresolved.append(item)
                if item not in new_unresolved:
                    new_unresolved.append(item)

        def reply_write_without_candidates() -> str:
            """Stage1 提取失败：引导用户明确选择支持的任务类型。"""
            supported = self.kb.get_all_task_type_values()
            supported_str = "、".join(supported) if supported else "管缆巡检、管缆埋设、采油树控制面板插入/拔出"
            # 通过 Responder LLM 生成自然的引导回复，避免冷硬错误提示
            guide_messages = [
                {
                    "role": "system",
                    "content": (
                        "你是一个专业的水下多智能体任务决策大模型。"
                        "用户希望创建任务，但系统暂时无法识别任务类型。"
                        f"当前系统支持的任务类型有：{supported_str}。"
                        "请友好、简洁地引导用户明确说明想要进行哪种具体任务类型，"
                        "并列出所有支持的任务类型供用户选择。不要透露底座模型或实现细节。"
                    ),
                },
                *self.conversation_history[-4:],
                {"role": "user", "content": user_message},
            ]
            try:
                reply = self._safe_llm_chat(guide_messages, temperature=0.5, max_tokens=300, role=ModelRole.TASK_RESPONDER)
                reply = self._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)
            except Exception:
                reply = (
                    f"您好！请问您想进行哪种水下作业任务？"
                    f"当前系统支持：{supported_str}。请告知具体任务类型，我将帮您进一步填写参数。"
                )
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        if task_type_key is None:
            # Stage 1: Extract task type
            extraction_res = self.extractor.extract_updates(
                user_message, current_state,
                task_type_key=None,
                task_type_map=self.kb.get_task_type_map(),
                required=None,
                conversation_history=self.conversation_history,
            )

            if is_task_patch_v2_enabled():
                allowed_stage1 = {"task_type", "task_type_key", "emergency_mode"}
                patch = build_task_patch(extraction_res, allowed_keys=allowed_stage1)
                stage1_updates, _, patch_unresolved = task_patch_to_legacy_updates(patch)
                for u in patch_unresolved:
                    if u not in turn_unresolved:
                        turn_unresolved.append(u)
                    if u not in new_unresolved:
                        new_unresolved.append(u)
                if not stage1_updates:
                    if new_unresolved:
                        self.slot_store.commit_transaction(
                            new_slots,
                            new_unresolved,
                            request_id=request_id,
                            expected_version=expected_version,
                        )
                    return reply_write_without_candidates()
                for k, cand_info in stage1_updates.items():
                    merged_updates[k] = cand_info["value"]
                    merged_updates_meta[k] = cand_info
            else:
                if not extraction_res.get("slot_candidates"):
                    record_unresolved(extraction_res)
                    if new_unresolved:
                        self.slot_store.commit_transaction(
                            new_slots,
                            new_unresolved,
                            request_id=request_id,
                            expected_version=expected_version,
                        )
                    return reply_write_without_candidates()

                stage1_updates = {}
                for candidate in extraction_res.get("slot_candidates", []):
                    k = candidate["canonical_key"]
                    v = candidate["normalized_value"]
                    cand_info = {
                        "value": v,
                        "raw_value": candidate.get("raw_value"),
                        "confidence": candidate.get("confidence", 1.0),
                        "source": self._source_for_resolution_method(candidate.get("resolution_method"))
                    }
                    stage1_updates[k] = cand_info
                    merged_updates[k] = v
                    merged_updates_meta[k] = cand_info
                record_unresolved(extraction_res)

            self._apply_updates_in_transaction(stage1_updates, new_slots)

            task_type_key = new_slots.get("task_type_key").value if new_slots.get("task_type_key") else None

        should_extract_task_parameters = (
            bool(task_type_key)
            and (
                had_task_type_key_at_turn_start
                or self._message_may_contain_task_parameters(user_message)
            )
        )

        apply_plan: NormalizationApplyPlan | None = None

        if should_extract_task_parameters:
            # Stage 2: Extract task parameters
            current_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
            required = self.builder.get_required(task_type_key, self.mode, current_state)
            extraction_res = self.extractor.extract_updates(
                user_message, current_state,
                task_type_key=task_type_key,
                task_type_map=self.kb.get_task_type_map(),
                required=required,
                ROV2type=self.kb.ROV2type,
                conversation_history=self.conversation_history,
            )

            if task_patch_v2_active:
                allowed_stage2 = self.extractor._allowed_candidate_keys(task_type_key, required)
                patch = build_task_patch(extraction_res, allowed_keys=allowed_stage2)

                if norm_v2_active:
                    field_defs = self.builder.get_schema(task_type_key, self.mode)
                    current_state_dict = {
                        k: s.value for k, s in new_slots.items()
                        if s.status == "valid" and s.value is not None
                    }

                    def allowed_resolver(fdef: dict[str, Any], state: dict[str, Any]) -> list[Any] | None:
                        return self.builder._resolve_allowed(fdef, task_type_key, state)

                    normalized_patch = normalize_task_patch(
                        patch,
                        field_defs,
                        current_state_dict,
                        allowed_resolver,
                        passthrough_keys=NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS,
                    )
                    apply_plan = normalized_task_patch_to_apply_plan(normalized_patch)

                    stage2_updates = {}
                    for succ in apply_plan.successful_updates:
                        stage2_updates[succ.key] = {
                            "value": succ.value,
                            "raw_value": succ.raw_value,
                            "confidence": succ.confidence,
                            "source": succ.source,
                        }
                        merged_updates[succ.key] = succ.value
                        merged_updates_meta[succ.key] = stage2_updates[succ.key]

                    for p in apply_plan.passthrough_slot_updates:
                        stage2_updates[p.key] = {
                            "value": p.candidate_value,
                            "raw_value": p.raw_value,
                            "confidence": p.confidence,
                            "source": p.source,
                        }
                        merged_updates[p.key] = p.candidate_value
                        merged_updates_meta[p.key] = stage2_updates[p.key]

                    for u in apply_plan.unresolved:
                        if u not in turn_unresolved:
                            turn_unresolved.append(u)
                        if u not in new_unresolved:
                            new_unresolved.append(u)

                    list_mutations = [
                        {
                            "op": m.operation,
                            "operation": m.operation,
                            "field": m.field,
                            "items": list(m.items) if m.items else [],
                            "target_items": list(m.target_items) if m.target_items else [],
                            "raw_text": m.raw_text,
                            "confidence": m.confidence,
                            "source": m.source,
                        }
                        for m in apply_plan.list_mutations
                    ]
                else:
                    stage2_updates, list_mutations, patch_unresolved = task_patch_to_legacy_updates(patch)
                    for u in patch_unresolved:
                        if u not in turn_unresolved:
                            turn_unresolved.append(u)
                        if u not in new_unresolved:
                            new_unresolved.append(u)
                    for k, cand_info in stage2_updates.items():
                        merged_updates[k] = cand_info["value"]
                        merged_updates_meta[k] = cand_info
            else:
                record_unresolved(extraction_res)
                stage2_updates = {}
                for candidate in extraction_res.get("slot_candidates", []):
                    k = candidate["canonical_key"]
                    v = candidate["normalized_value"]
                    if k == "equipment_model":
                        k = "equipment_type"
                    cand_info = {
                        "value": v,
                        "raw_value": candidate.get("raw_value"),
                        "confidence": candidate.get("confidence", 1.0),
                        "source": self._source_for_resolution_method(candidate.get("resolution_method"))
                    }
                    stage2_updates[k] = cand_info
                    merged_updates[k] = v
                    merged_updates_meta[k] = cand_info
                list_mutations = extraction_res.get("list_mutations", [])
            payload_mutation_failed = False
            mutation_failure_result = None

            if list_mutations:
                for mutation in list_mutations:
                    m_field = mutation.get("field")
                    if m_field == "payload":
                        stage2_updates.pop("payload", None)
                        merged_updates.pop("payload", None)
                        merged_updates_meta.pop("payload", None)

                        mut_res = self.slot_store.apply_list_mutation(
                            new_slots,
                            mutation,
                            required_schema=required,
                            payload_catalog=self.kb.assets.get("payload_catalog"),
                        )
                        if mut_res.get("success"):
                            new_payload_val = mut_res.get("new_value")
                            merged_updates["payload"] = new_payload_val
                            merged_updates_meta["payload"] = {
                                "value": new_payload_val,
                                "raw_value": mutation.get("raw_text"),
                                "confidence": mutation.get("confidence", 0.95),
                                "source": mutation.get("source", "user_input"),
                            }
                        else:
                            payload_mutation_failed = True
                            mutation_failure_result = mut_res
                            break

            raw_stage2 = self._merge_coordinate_updates(user_message, {k: v.get("value") if isinstance(v, dict) else v for k, v in stage2_updates.items()}, required)
            for k, v in raw_stage2.items():
                if k not in stage2_updates:
                    c_info = {"value": v, "raw_value": user_message, "confidence": 1.0, "source": "rule_parser"}
                    stage2_updates[k] = c_info
                    merged_updates_meta[k] = c_info
                merged_updates[k] = v

            raw_linked = self._link_oilfield_update_in_transaction({k: v.get("value") if isinstance(v, dict) else v for k, v in stage2_updates.items()}, new_slots)
            if "oilfield_name" in stage2_updates and "oilfield_name" not in raw_linked:
                stage2_updates.pop("oilfield_name", None)
                merged_updates.pop("oilfield_name", None)
                merged_updates_meta.pop("oilfield_name", None)

            for k, v in raw_linked.items():
                if k.startswith("__"):
                    stage2_updates[k] = v
                    continue
                old_info = stage2_updates.get(k)
                old_raw = old_info.get("raw_value") if isinstance(old_info, dict) else None
                c_info = {
                    "value": v,
                    "raw_value": old_raw if old_raw is not None else str(v),
                    "confidence": old_info.get("confidence", 1.0) if isinstance(old_info, dict) else 1.0,
                    "source": old_info.get("source", "entity_linker") if isinstance(old_info, dict) else "entity_linker",
                }
                stage2_updates[k] = c_info
                merged_updates_meta[k] = c_info
                merged_updates[k] = v

            _has_conflict = any(s.status == "conflict" for s in new_slots.values())
            has_successful_mutation = any(m.get("field") == "payload" for m in list_mutations)
            if not stage2_updates and not _has_conflict and not turn_unresolved and not has_successful_mutation and (apply_plan is None or not apply_plan.failures):
                if new_unresolved:
                    self.slot_store.commit_transaction(
                        new_slots,
                        new_unresolved,
                        request_id=request_id,
                        expected_version=expected_version,
                    )
                return reply_write_without_candidates()

            # Scoped & Negation-Safe Conflict resolution check
            slot_name_aliases = {
                "support_vessel": ["支持船", "船", "工作船", "母船"],
                "equipment_type": ["设备", "机器人", "rov", "auv"],
                "water_depth": ["水深", "深度"],
                "cable_type": ["管缆类型", "缆线", "电缆"],
                "payload": ["载荷", "工具", "传感器", "抓手", "配备"],
                "oilfield_name": ["油田", "油田名称"],
            }
            has_negation_confirm = any(nc in user_message for nc in ["不确认", "不修改", "不要修改", "先不确认"])
            has_explicit_upd = bool(stage2_updates)

            conflict_slots = [k for k, s in new_slots.items() if s.status == "conflict" and s.candidate_value is not None]
            is_ambiguous_global_confirm = (
                len(conflict_slots) >= 2
                and user_message.strip() in ("确认这个修改", "确认修改", "好的", "确认", "确定修改")
                and not has_explicit_upd
            )

            if not is_ambiguous_global_confirm:
                for k, slot in list(new_slots.items()):
                    if slot.status == "conflict" and slot.candidate_value is not None:
                        raw_ext = stage2_updates.get(k)
                        extracted_cand_v = raw_ext.get("value") if isinstance(raw_ext, dict) else raw_ext
                        # 1. 显式输入与 candidate_value 完全一致
                        if extracted_cand_v is not None and extracted_cand_v == slot.candidate_value:
                            slot.value = slot.candidate_value
                            slot.status = "valid"
                            slot.candidate_value = None
                            slot.validation_error = None
                            continue

                        # 2. 如果包含其他新槽位修改（如"水深改成500米"），不得顺带确认本冲突槽位
                        if has_explicit_upd and k not in stage2_updates and not any(alias in user_message for alias in slot_name_aliases.get(k, [k])):
                            continue

                        # 3. 检查针对具体槽位 k 的定向确认/取消
                        k_aliases = slot_name_aliases.get(k, [k])
                        msg_targets_k = any(alias in user_message for alias in k_aliases) or (slot.candidate_value and str(slot.candidate_value) in user_message)

                        if msg_targets_k:
                            is_cancel_k = any(c_kw in user_message for c_kw in ["取消", "放弃", "不要", "不修改", "不用"])
                            is_confirm_k = any(c_kw in user_message for c_kw in ["确认", "确定", "好的", "可以", "使用", "改为"]) and not is_cancel_k

                            if is_confirm_k and not has_negation_confirm:
                                slot.value = slot.candidate_value
                                slot.status = "valid"
                                slot.candidate_value = None
                                slot.validation_error = None
                                if k in stage2_updates:
                                    del stage2_updates[k]
                            elif is_cancel_k:
                                slot.status = "valid"
                                slot.candidate_value = None
                                slot.validation_error = None
                                if k in stage2_updates:
                                    del stage2_updates[k]

            if apply_plan is not None:
                self._apply_normalized_plan_in_transaction(
                    apply_plan,
                    new_slots,
                    allow_overwrite=had_task_type_key_at_turn_start,
                )
                task_type_updates = {
                    k: v for k, v in stage2_updates.items()
                    if k in ("task_type", "task_type_key")
                }
                for k, info in task_type_updates.items():
                    val = info.get("value") if isinstance(info, dict) else info
                    if val is not None and val != "":
                        self._handle_task_type_update_in_transaction(k, val, new_slots)

                extra_updates = {
                    k: v for k, v in stage2_updates.items()
                    if k not in apply_plan.normalized_schema_keys
                    and k not in ("task_type", "task_type_key")
                }
                if extra_updates:
                    self._apply_updates_in_transaction(
                        extra_updates,
                        new_slots,
                        allow_overwrite=had_task_type_key_at_turn_start,
                    )
            else:
                self._apply_updates_in_transaction(
                    stage2_updates,
                    new_slots,
                    allow_overwrite=had_task_type_key_at_turn_start,
                )

            if "rov_description" in stage2_updates:
                all_rovs = self.kb.get_all_rovs()
                proposed_pending_rov = self.extractor.resolve_rov_description(
                    stage2_updates["rov_description"].get("value") if isinstance(stage2_updates["rov_description"], dict) else str(stage2_updates["rov_description"]),
                    all_rovs,
                    new_slots.get("task_type_key").value if new_slots.get("task_type_key") else None
                )
        else:
            if extraction_res.get("unresolved"):
                for u in extraction_res["unresolved"]:
                    if u not in new_unresolved:
                        new_unresolved.append(u)

        # Compute proposed mode change without mutating self.mode before commit
        proposed_mode = self.mode
        if merged_updates.get("emergency_mode"):
            proposed_mode = "emergency"

        # Compute changed fields based on proposed updates
        changed_fields = set()
        for k, v in merged_updates.items():
            if k not in ("emergency_mode", "rov_description", "__clear_oilfield_name", "__clear_pending_oilfield") and v is not None and v != "":
                old_val = self.slot_store.slots.get(k).value if self.slot_store.slots.get(k) else None
                if old_val != v:
                    changed_fields.add(k)

        proposed_whitelist = {item for item in self._soft_whitelist if item[0] not in changed_fields}

        # Normalize and validate inside transaction working dict new_slots
        curr_task_type_key = new_slots.get("task_type_key").value if new_slots.get("task_type_key") else None
        skip_keys = apply_plan.normalized_schema_keys if apply_plan is not None else None
        self._normalize_and_validate_in_transaction(new_slots, curr_task_type_key, skip_schema_keys=skip_keys)

        curr_task_type_key = new_slots.get("task_type_key").value if (new_slots.get("task_type_key") and new_slots.get("task_type_key").status == "valid") else None

        # Auto-generate internal_id (UUIDv4) and task_id inside new_slots BEFORE commit
        if curr_task_type_key:
            internal_id_slot = new_slots.get("internal_id")
            if not internal_id_slot or internal_id_slot.status != "valid" or not internal_id_slot.value:
                new_uuid = str(uuid.uuid4())
                if "internal_id" not in new_slots:
                    new_slots["internal_id"] = Slot("internal_id")
                new_slots["internal_id"].value = new_uuid
                new_slots["internal_id"].status = "valid"
                new_slots["internal_id"].source = "auto"
                new_slots["internal_id"].raw_value = None
                new_slots["internal_id"].value_type = "string"

            task_id_slot = new_slots.get("task_id")
            # 草稿阶段仅预览编号：不消耗正式序号，不写 valid status。
            # 只有当前没有任何 preview 或已有 valid 编号（来自正式 reserve）时才刷新预览。
            # 正式编号在用户确认发布时由 _publish_confirmed_task 调用 reserve_task_id() 分配。
            existing_valid = (
                task_id_slot
                and task_id_slot.status == "valid"
                and task_id_slot.value is not None
                and validate_task_id_for_task_type(str(task_id_slot.value), curr_task_type_key, self.kb.task_schemas)
                and task_id_slot.source == "auto_reserved"  # 只有正式预约的才保留
            )
            if not existing_valid:
                try:
                    preview_id = self.builder.preview_task_id(curr_task_type_key)
                except IdReservationError:
                    # 底层 ID 序列配置错误或 counter 损坏时 Fail Closed，不吞异常
                    raise
                except Exception as _preview_err:
                    logger.warning(
                        "[DM] non-fatal preview_task_id failed for %s: %s", curr_task_type_key, _preview_err
                    )
                    preview_id = None
                if preview_id is not None:
                    if "task_id" not in new_slots:
                        new_slots["task_id"] = Slot("task_id")
                    # candidate_value: 预估编号，仅供展示，不进入 TaskIntent
                    new_slots["task_id"].candidate_value = preview_id
                    new_slots["task_id"].status = "candidate"
                    new_slots["task_id"].source = "auto_preview"
                    new_slots["task_id"].value = None          # 正式值为空，防止被 prepare() 误用
                    new_slots["task_id"].raw_value = None
                    new_slots["task_id"].value_type = "string"
                    new_slots["task_id"].validation_error = None

        proposed_phase = self.phase

        # Check required missing in working new_slots
        if curr_task_type_key:
            required_schema = self.builder.get_schema(curr_task_type_key, proposed_mode)
            user_req_schema = [f for f in required_schema if f.get("type") not in ("auto", "fixed")]
            built = self.slot_store.get_built_json()
            missing = self.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: self.builder.resolve_allowed_values(
                    field,
                    curr_task_type_key,
                    self.task_state,
                ),
            )
            self._last_missing = missing
            cand_missing = [f for f in required_schema if f.get("type") not in ("auto", "fixed") and (not new_slots.get(f["key"]) or new_slots[f["key"]].status != "valid" or new_slots[f["key"]].value is None)]
        else:
            built = {}
            missing = [{"key": "task_type", "label": "任务类型", "type": "string",
                        "allowed_values": self.kb.get_all_task_type_values()}]
            self._last_missing = missing
            cand_missing = missing

        # 维持严格 SSOT：_last_built_json 完全由 self.slot_store.get_built_json() 派生
        self._last_built_json = built

        # Auto-generate intent_id inside new_slots BEFORE commit when all required slots are present or when revising a done task
        if old_phase == "done" or (curr_task_type_key and not cand_missing):
            intent_id_slot = new_slots.get("intent_id")
            if old_phase == "done" or not intent_id_slot or intent_id_slot.status != "valid" or not intent_id_slot.value:
                today = get_current_datetime().strftime("%Y%m%d")
                from .task_intent_builder import get_task_dir
                task_dir = get_task_dir(create=False)
                from .id_sequence import next_daily_id
                ti_intent_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])
                if "intent_id" not in new_slots:
                    new_slots["intent_id"] = Slot("intent_id")
                new_slots["intent_id"].value = ti_intent_id
                new_slots["intent_id"].status = "valid"
                new_slots["intent_id"].source = "auto"
                new_slots["intent_id"].raw_value = None

        if old_phase == "done":
            proposed_phase = "confirming" if not cand_missing else "collecting"
        elif not cand_missing and proposed_phase not in ("blocked_hard", "blocked_soft", "confirming", "done"):
            proposed_phase = "confirming"

        # Atomic single commit with optimistic version validation
        self.slot_store.commit_transaction(
            new_slots,
            new_unresolved,
            request_id=request_id,
            expected_version=expected_version,
        )

        if old_phase == "done":
            self.final_result = None

        # Apply proposed instance state AFTER successful commit
        self.mode = proposed_mode
        self._transition_phase(proposed_phase, reason="task_modified")
        self._soft_whitelist = proposed_whitelist
        self._pending_rov_candidates = proposed_pending_rov

        # Re-derive from slot_store (SSOT)
        self.task_state = self.slot_store.get_task_state()
        if curr_task_type_key:
            required_schema = self.builder.get_schema(curr_task_type_key, self.mode)
            user_req_schema = [f for f in required_schema if f.get("type") not in ("auto", "fixed")]
            built = self.slot_store.get_built_json()
            missing = self.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: self.builder.resolve_allowed_values(
                    field,
                    curr_task_type_key,
                    self.task_state,
                ),
            )
            self._last_missing = missing
        else:
            built = {}
            missing = [{"key": "task_type", "label": "任务类型", "type": "string",
                        "allowed_values": self.kb.get_all_task_type_values()}]
            self._last_missing = missing
        self._last_built_json = built

        self.task_start_now = self.is_start_time_near_now()


        pending_oilfield_reply = self._build_pending_oilfield_reply()
        if pending_oilfield_reply:
            self._transition_phase("collecting", reason="oilfield_clarification_needed")
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": pending_oilfield_reply})
            return pending_oilfield_reply

        # 处理软约束忽略（blocked_soft阶段）
        if self.phase == "blocked_soft":
            user_ignore = self._is_soft_warning_acknowledgement(user_message)
            if self._blocking_violations:
                soft_related_fields = set()
                for v in self._blocking_violations:
                    soft_related_fields.update(v.related_fields)
                if user_ignore and not (soft_related_fields & changed_fields):
                    for v in self._blocking_violations:
                        for f in v.related_fields:
                            val = self.task_state.get(f)
                            if val is not None:
                                self._soft_whitelist.add((f, str(val), v.constraint_id))
                    self._transition_phase("collecting", reason="soft_warning_acknowledged")
                    self._blocking_violations = []

        # 约束检查
        ALL_FIELDS = {"task_type", "start_time", "end_time", "cable_position", "cable_type", "start_point", "end_point",
                      "water_depth", "equipment_family", "equipment_type", "equipment_name", "equipment_unit_id",
                      "payload", "support_vessel", "oilfield_name",
                      "oilfield_coordinates", "wellhead_id"}

        if not missing and self.phase not in ("blocked_hard", "blocked_soft"):
            constraint_context = self._run_constraint_check(ALL_FIELDS)
        elif not missing and self.phase == "blocked_soft":
            constraint_context = self._run_constraint_check(changed_fields)
        elif not missing and self.phase == "blocked_hard":
            constraint_context = self._run_constraint_check(ALL_FIELDS)
        else:
            constraint_context = self._run_constraint_check(changed_fields)

        # 知识上下文
        knowledge_context = self.kb.get_context_for_state(self.task_state)
        accepted_updates = self._get_committed_turn_updates(
            merged_updates,
            state_before_turn,
        )

        # 生成回复
        messages = build_responder_messages(
            task_state=self.task_state,
            built_json=built,
            missing_fields=missing,
            mode=self.mode,
            phase=self.phase,
            knowledge_context=knowledge_context,
            constraint_context=constraint_context,
            conversation_history=self.conversation_history,
            latest_user_message=user_message,
            ROV2type=self.kb.ROV2type,
            support_task=self.kb.get_supported_task(),
            slot_snapshot=self.slot_store.get_slot_snapshot(),
            accepted_updates=accepted_updates,
            unresolved_inputs=turn_unresolved,
        )
        reply = self._safe_llm_chat(messages, temperature=0.7, max_tokens=1500, role=ModelRole.TASK_RESPONDER)
        reply = self._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)
        reply = self._ensure_constraint_details(reply, constraint_context)

        if payload_mutation_failed and mutation_failure_result:
            err_msg = mutation_failure_result.get("error") or "载荷修改操作失败。"
            if accepted_updates:
                reply = f"{reply}\n注意：载荷操作失败：{err_msg}"
            else:
                reply = f"操作失败：{err_msg}"

        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    # --------------------------------------------------------------------------
    # 参数更新与规范化
    # --------------------------------------------------------------------------
    @staticmethod
    def _message_may_contain_task_parameters(user_message: str) -> bool:
        """
        判断首轮建任务输入是否除了任务类型外还可能携带业务参数。

        目的不是替代 extractor 做字段抽取，而是在“刚识别出 task_type”的首轮
        避免把纯任务类型短句再次送进 Stage 2，从而减少一次不必要的 LLM 调用。
        如果文本里出现时间、水深、坐标、设备、工具、母船等参数线索，仍保留
        Stage 2，避免用户一口气给完整任务信息时丢字段。
        """
        text = str(user_message or "").strip()
        if not text:
            return False

        parameter_cues = (
            "开始时间",
            "结束时间",
            "时间",
            "现在",
            "小时后",
            "分钟后",
            "明天",
            "今天",
            "水深",
            "深度",
            "起始点",
            "结束点",
            "坐标",
            "经纬度",
            "管缆类型",
            "油气管道",
            "电力电缆",
            "光纤通信缆",
            "使用",
            "选用",
            "型号",
            "编号",
            "机器人",
            "工作级",
            "观察级",
            "AUV",
            "ROV",
            "工具",
            "携带",
            "载荷",
            "母船",
            "支持船",
            "油田",
            "井口",
        )
        if any(cue in text for cue in parameter_cues):
            return True

        if re.search(r"\([-+]?\d+(?:\.\d+)?,\s*[-+]?\d+(?:\.\d+)?\)", text):
            return True
        if re.search(r"\d+(?:\.\d+)?\s*(?:米|m|小时|分钟|号)", text, re.IGNORECASE):
            return True

        return False

    def _get_committed_turn_updates(
        self,
        proposed_updates: dict,
        state_before_turn: dict,
    ) -> dict:
        """返回本轮已由 SlotStore 提交的用户字段更新。"""
        if not proposed_updates:
            return {}

        ignored_keys = {
            "task_id",
            "intent_id",
            "internal_id",
            "emergency_mode",
            "rov_description",
            "pending_oilfield_candidates",
            "__clear_oilfield_name",
            "__clear_pending_oilfield",
        }
        accepted: dict = {}
        for key, value in self.task_state.items():
            if key in ignored_keys or key.startswith("__") or value is None:
                continue
            if key not in proposed_updates and state_before_turn.get(key) == value:
                continue
            slot = self.slot_store.slots.get(key)
            if slot and slot.status == "valid":
                accepted[key] = value
        return accepted



    def _link_oilfield_update_in_transaction(self, updates: dict, new_slots: dict) -> dict:
        raw_name = updates.get("oilfield_name") or updates.get("raw_oilfield_name")
        if isinstance(raw_name, dict):
            raw_name = raw_name.get("value")
        if not raw_name:
            return updates

        coords = (
            updates.get("oilfield_coordinates")
            or updates.get("start_point")
            or updates.get("cable_position")
            or (new_slots.get("oilfield_coordinates").value if new_slots.get("oilfield_coordinates") else None)
            or (new_slots.get("start_point").value if new_slots.get("start_point") else None)
            or (new_slots.get("cable_position").value if new_slots.get("cable_position") else None)
        )
        match = self.oilfield_linker.link(str(raw_name), coords)
        linked = dict(updates)

        for k in ("raw_oilfield_name", "oilfield_match_status", "oilfield_match_confidence", "oilfield_match_evidence", "oilfield_match_candidates"):
            if k not in new_slots:
                new_slots[k] = Slot(slot_name=k)

        new_slots["raw_oilfield_name"].value = match.raw
        new_slots["raw_oilfield_name"].status = "valid"
        new_slots["oilfield_match_status"].value = match.status
        new_slots["oilfield_match_status"].status = "valid"
        new_slots["oilfield_match_confidence"].value = match.confidence
        new_slots["oilfield_match_confidence"].status = "valid"
        new_slots["oilfield_match_evidence"].value = match.evidence
        new_slots["oilfield_match_evidence"].status = "valid"
        new_slots["oilfield_match_candidates"].value = match.candidates
        new_slots["oilfield_match_candidates"].status = "valid"

        if match.status == "accepted" and match.standard_name:
            linked["oilfield_name"] = match.standard_name
            if "oilfield_name" not in new_slots:
                new_slots["oilfield_name"] = Slot("oilfield_name")
            new_slots["oilfield_name"].value = match.standard_name
            new_slots["oilfield_name"].status = "valid"
            if "oilfield_entity_id" not in new_slots:
                new_slots["oilfield_entity_id"] = Slot("oilfield_entity_id")
            new_slots["oilfield_entity_id"].value = match.entity_id
            new_slots["oilfield_entity_id"].status = "valid"
            linked["__clear_pending_oilfield"] = True
        else:
            linked.pop("oilfield_name", None)
            for k in ("pending_oilfield_name", "pending_oilfield_candidates"):
                if k not in new_slots:
                    new_slots[k] = Slot(slot_name=k)
            new_slots["pending_oilfield_name"].value = match.raw
            new_slots["pending_oilfield_name"].status = "valid"
            new_slots["pending_oilfield_candidates"].value = match.candidates
            new_slots["pending_oilfield_candidates"].status = "valid"
            linked["__clear_oilfield_name"] = True
        return linked

    def _apply_updates_in_transaction(
        self,
        updates: dict,
        new_slots: dict,
        allow_overwrite: bool = False,
    ):
        # main extractor 会携带 raw/confidence/source；LHL 归一化器只接收值本身。
        # 在事务入口拆开二者，既保留确定性归一化，也保留槽位审计信息。
        update_meta: dict[str, dict] = {}
        plain_updates: dict = {}
        # equipment_specification 是结构化 typed dict，其 "value" 字段是规格量值，不是 meta 包装。
        # 所有 equipment_specification 值应直接透传，不拆包。
        _spec_passthrough_keys = set()
        for key, item in updates.items():
            if key in _spec_passthrough_keys:
                plain_updates[key] = item
            elif isinstance(item, dict) and "value" in item:
                value = item.get("value")
                plain_updates[key] = value
                update_meta[key] = {
                    "raw_value": item.get("raw_value", value),
                    "confidence": item.get("confidence", 1.0),
                    "source": item.get("source", "user_input"),
                }
            else:
                plain_updates[key] = item
        updates = plain_updates

        if updates.get("__clear_oilfield_name"):
            if "oilfield_name" in new_slots:
                new_slots["oilfield_name"].value = None
                new_slots["oilfield_name"].status = "missing"
            if "oilfield_entity_id" in new_slots:
                new_slots["oilfield_entity_id"].value = None
                new_slots["oilfield_entity_id"].status = "missing"
        if updates.get("__clear_pending_oilfield"):
            if "pending_oilfield_name" in new_slots:
                new_slots["pending_oilfield_name"].value = None
                new_slots["pending_oilfield_name"].status = "missing"
            if "pending_oilfield_candidates" in new_slots:
                new_slots["pending_oilfield_candidates"].value = None
                new_slots["pending_oilfield_candidates"].status = "missing"

        equipment_keys = {
            "equipment_class",
            "equipment_family",
            "equipment_type",
            "equipment_name",
            "equipment_unit_id",
        }
        passthrough_keys = {
            "emergency_mode",
            "rov_description",
            "oilfield_name",
            "__clear_oilfield_name",
            "__clear_pending_oilfield",
            "task_id",
            "intent_id",
            "internal_id",
        }

        task_type_slot = new_slots.get("task_type_key")
        task_type_key = task_type_slot.value if task_type_slot else None
        failures = {}
        if task_type_key:
            current_state = {
                key: slot.value
                for key, slot in new_slots.items()
                if slot.value is not None
            }
            schema_updates = {
                k: v for k, v in updates.items()
                if k not in equipment_keys and k not in passthrough_keys
            }
            norm_res = self.normalizer.normalize_updates_with_failures(
                schema_updates,
                self.builder.get_schema(task_type_key, self.mode),
                current_state,
                lambda field_def, state: self.builder._resolve_allowed(
                    field_def,
                    task_type_key,
                    state,
                ),
            )
            norm_schema = norm_res.normalized_updates
            failures = norm_res.failures
            eq_updates = {k: v for k, v in updates.items() if k in equipment_keys}
            pass_updates = {k: v for k, v in updates.items() if k in passthrough_keys}
            updates = {**norm_schema, **pass_updates, **eq_updates}

        skip = {
            "emergency_mode",
            "rov_description",
            "__clear_oilfield_name",
            "__clear_pending_oilfield",
            "task_id",
            "intent_id",
            "internal_id",
            *equipment_keys,
        }

        for key, value in updates.items():
            if key in skip or value is None or value == "":
                continue
            if key in ("task_type", "task_type_key"):
                self._handle_task_type_update_in_transaction(key, value, new_slots)
                continue
            self._apply_slot_update_in_transaction(
                key,
                value,
                new_slots,
                allow_overwrite,
            )
            slot = new_slots.get(key)
            meta = update_meta.get(key)
            if slot and meta:
                slot.raw_value = meta["raw_value"]
                slot.confidence = meta["confidence"]
                slot.source = meta["source"]

        for key, failure in failures.items():
            slot = new_slots.get(key)
            meta = update_meta.get(key)
            raw_val = failure.raw_value
            msg = failure.message

            candidate_val = raw_val
            original_raw = (
                meta.get("raw_value")
                if meta and meta.get("raw_value") is not None
                else (str(raw_val) if raw_val is not None else "")
            )

            if slot and slot.status in ("valid", "conflict") and slot.value is not None:
                slot.status = "conflict"
                slot.candidate_value = candidate_val
                slot.raw_value = str(original_raw)
                slot.validation_error = msg
            else:
                if slot is None:
                    slot = Slot(slot_name=key)
                    new_slots[key] = slot
                slot.value = None
                slot.status = "invalid"
                slot.candidate_value = candidate_val
                slot.raw_value = str(original_raw)
                slot.validation_error = msg

            if meta:
                slot.confidence = meta.get("confidence", 1.0)
                slot.source = meta.get("source", "user_input")

        if updates.get("emergency_mode"):
            if "emergency_mode" not in new_slots:
                new_slots["emergency_mode"] = Slot("emergency_mode")
            new_slots["emergency_mode"].value = True
            new_slots["emergency_mode"].status = "valid"

        self._handle_equipment_updates_in_transaction(
            updates,
            new_slots,
            allow_overwrite,
        )
        for key in (
            "equipment_class",
            "equipment_family",
            "equipment_type",
            "equipment_name",
            "equipment_unit_id",
        ):
            slot = new_slots.get(key)
            meta = update_meta.get(key)
            if slot and meta:
                slot.raw_value = meta["raw_value"]
                slot.confidence = meta["confidence"]
                slot.source = meta["source"]

    def _apply_normalized_plan_in_transaction(
        self,
        plan: NormalizationApplyPlan,
        new_slots: dict,
        allow_overwrite: bool = False,
    ) -> None:
        """根据 NormalizationApplyPlan 修改 working dict new_slots。"""
        # 1. 成功 outcomes 写入 new_slots
        for succ in plan.successful_updates:
            key = succ.key
            value = succ.value
            slot = new_slots.get(key)

            if (
                slot
                and slot.status == "valid"
                and slot.value is not None
                and slot.value != value
                and not allow_overwrite
            ):
                slot.status = "conflict"
                slot.candidate_value = value
                slot.raw_value = str(succ.raw_value) if succ.raw_value is not None else str(value)
                slot.confidence = succ.confidence
                slot.source = succ.source
                slot.validation_error = None
            else:
                if slot is None:
                    slot = Slot(slot_name=key)
                    new_slots[key] = slot

                slot.value = value
                slot.status = "valid"
                slot.candidate_value = None
                slot.raw_value = str(succ.raw_value) if succ.raw_value is not None else str(value)
                slot.confidence = succ.confidence
                slot.source = succ.source
                slot.validation_error = None

        # 2. 失败 outcomes 写入 new_slots
        for failure in plan.failures:
            key = failure.key
            slot = new_slots.get(key)
            cand_val = failure.candidate_value
            raw_val = failure.raw_value
            raw_str = str(raw_val) if raw_val is not None else ""

            if slot and slot.status in ("valid", "conflict") and slot.value is not None:
                slot.status = "conflict"
                slot.candidate_value = cand_val
                slot.raw_value = raw_str
                slot.confidence = failure.confidence
                slot.source = failure.source
                slot.validation_error = failure.error_message
            else:
                if slot is None:
                    slot = Slot(slot_name=key)
                    new_slots[key] = slot
                slot.value = None
                slot.status = "invalid"
                slot.candidate_value = cand_val
                slot.raw_value = raw_str
                slot.confidence = failure.confidence
                slot.source = failure.source
                slot.validation_error = failure.error_message

    @staticmethod
    def _source_for_resolution_method(resolution_method: str | None) -> str:
        source_map = {
            "canonical_exact": "user_input",
            "alias_exact": "alias_mapping",
            "llm_semantic": "llm_semantic_match",
            "type_normalization": "user_input",
        }
        return source_map.get(resolution_method, "user_input")

    @staticmethod
    def _apply_slot_update_in_transaction(
        key: str,
        value: Any,
        new_slots: dict,
        allow_overwrite: bool,
    ) -> None:
        """把一个候选值写入临时槽位；正式状态只能由后续 commit 生效。"""
        slot = new_slots.get(key)
        if (
            slot
            and slot.status == "valid"
            and slot.value is not None
            and slot.value != value
            and not allow_overwrite
        ):
            slot.status = "conflict"
            slot.candidate_value = value
            slot.raw_value = str(value)
            slot.validation_error = None
            return

        if slot is None:
            slot = Slot(slot_name=key)
            new_slots[key] = slot

        slot.value = value
        slot.status = "candidate"
        slot.candidate_value = None
        slot.raw_value = str(value)
        slot.validation_error = None

    def _handle_equipment_updates_in_transaction(
        self,
        updates: dict,
        new_slots: dict,
        allow_overwrite: bool,
    ) -> None:
        """统一处理机器人类别、系列、型号和单机编号的四级层级联动与依赖失效。"""
        import copy
        from src.slot_store import (
            ROBOT_CASCADE_DEPENDENCIES,
            reset_slot_to_missing,
            invalidate_robot_cascade_dependents,
        )

        EQUIPMENT_KEYS = (
            "equipment_class",
            "equipment_family",
            "equipment_type",
            "equipment_unit_id",
            "equipment_name",
        )

        equipment_updates = {}
        for key in EQUIPMENT_KEYS:
            val = updates.get(key)
            if isinstance(val, dict):
                if "value" in val and len(val) <= 4 and ("raw_value" in val or "source" in val or "confidence" in val):
                    val = val.get("value")
            if val not in (None, ""):
                if isinstance(val, str):
                    val = val.strip()
                equipment_updates[key] = val

        # 兼容旧设备规格输入: 如果没有 equipment_type，但提供了 equipment_specification（含 variant_id），映射为 equipment_type
        if "equipment_type" not in equipment_updates and "equipment_specification" in updates:
            spec_val = updates.get("equipment_specification")
            if isinstance(spec_val, dict) and "variant_id" in spec_val:
                vid = spec_val.get("variant_id")
                var_info = self.kb.get_rov(vid) if hasattr(self, "kb") and self.kb else None
                if var_info and var_info.get("full_name"):
                    equipment_updates["equipment_type"] = var_info.get("full_name")
                elif vid:
                    equipment_updates["equipment_type"] = vid

        if not equipment_updates:
            return

        # 保存 5 槽完整前置快照
        equipment_before = {
            k: copy.deepcopy(new_slots[k])
            for k in EQUIPMENT_KEYS
            if k in new_slots
        }

        task_type = (
            new_slots.get("task_type_key").value
            if new_slots.get("task_type_key")
            else None
        )

        def _unwrap(v):
            return v.get("value") if isinstance(v, dict) else v

        # 辅助函数：校验/推演失败时，原子回滚 new_slots 并标记目标 Slot
        def _rollback_and_fail(target_key: str, candidate_val: Any, error_msg: str, force_conflict: bool = False):
            for k in EQUIPMENT_KEYS:
                if k in equipment_before:
                    new_slots[k] = copy.deepcopy(equipment_before[k])
                elif k in new_slots:
                    del new_slots[k]

            prior_slot = equipment_before.get(target_key)
            has_prior_valid_value = (
                prior_slot is not None
                and prior_slot.status in ("valid", "conflict")
                and prior_slot.value is not None
            )
            if force_conflict or has_prior_valid_value:
                target_slot = copy.deepcopy(prior_slot) if prior_slot else Slot(slot_name=target_key)
                target_slot.status = "conflict"
                target_slot.candidate_value = candidate_val
                target_slot.validation_error = error_msg
                new_slots[target_key] = target_slot
            else:
                default_vtype = BASE_SLOT_TYPES.get(target_key, "string")
                target_slot = copy.deepcopy(prior_slot) if prior_slot else Slot(slot_name=target_key, value_type=default_vtype)
                target_slot.status = "invalid"
                target_slot.value = None
                target_slot.value_type = default_vtype
                target_slot.candidate_value = candidate_val
                target_slot.validation_error = error_msg
                new_slots[target_key] = target_slot

        # Conflict Fence
        if not allow_overwrite:
            highest_conflict_key = None
            highest_candidate_val = None
            highest_conflict_reason = None

            # 1. 检查 equipment_class
            cls_in = equipment_updates.get("equipment_class")
            if cls_in:
                active_cls_slot = equipment_before.get("equipment_class")
                if active_cls_slot and active_cls_slot.status in ("valid", "conflict") and active_cls_slot.value is not None:
                    res_cls_id = self.kb._resolve_class_key(str(cls_in))
                    if res_cls_id and res_cls_id != active_cls_slot.value:
                        highest_conflict_key = "equipment_class"
                        highest_candidate_val = cls_in
                        highest_conflict_reason = f"Robot class '{cls_in}' conflicts with active valid class '{active_cls_slot.value}'"

            # 2. 检查 equipment_family (若 class 未冲突)
            if not highest_conflict_key:
                fam_in = equipment_updates.get("equipment_family")
                if fam_in:
                    active_fam_slot = equipment_before.get("equipment_family")
                    if active_fam_slot and active_fam_slot.status in ("valid", "conflict") and active_fam_slot.value is not None:
                        res_fam = self.kb.resolve_robot_family(str(fam_in), task_type)
                        if res_fam:
                            active_fam_id = self.kb.resolve_robot_family_id(str(active_fam_slot.value), task_type)
                            if res_fam.get("family_id") != active_fam_id:
                                highest_conflict_key = "equipment_family"
                                highest_candidate_val = fam_in
                                highest_conflict_reason = f"Robot family '{fam_in}' conflicts with active valid family '{active_fam_slot.value}'"

            # 3. 检查 equipment_type (若 class/family 未冲突)
            if not highest_conflict_key:
                type_in = equipment_updates.get("equipment_type")
                if type_in and "equipment_unit_id" not in equipment_updates:
                    active_type_slot = equipment_before.get("equipment_type")
                    if active_type_slot and active_type_slot.status in ("valid", "conflict") and active_type_slot.value is not None:
                        res_var = self.kb.get_rov_for_task(str(type_in), task_type)
                        if res_var and res_var.get("full_name") != active_type_slot.value:
                            highest_conflict_key = "equipment_type"
                            highest_candidate_val = type_in
                            highest_conflict_reason = f"Robot variant '{type_in}' conflicts with active valid type '{active_type_slot.value}'"

            # 4. 检查 equipment_unit_id
            if not highest_conflict_key:
                unit_in = equipment_updates.get("equipment_unit_id")
                if unit_in:
                    active_unit_slot = equipment_before.get("equipment_unit_id")
                    if active_unit_slot and active_unit_slot.status in ("valid", "conflict") and active_unit_slot.value is not None:
                        res_unit = self.kb.resolve_robot_unit(str(unit_in), task_type)
                        if res_unit and res_unit.get("unit_id") != active_unit_slot.value:
                            highest_conflict_key = "equipment_unit_id"
                            highest_candidate_val = unit_in
                            highest_conflict_reason = f"Robot unit '{unit_in}' conflicts with active valid unit '{active_unit_slot.value}'"

            if highest_conflict_key:
                _rollback_and_fail(
                    highest_conflict_key,
                    highest_candidate_val,
                    highest_conflict_reason,
                    force_conflict=True,
                )
                return

        # 在沙盒中推演
        sandbox_slots = copy.deepcopy(new_slots)
        changed_parents = []

        # 1. equipment_class 更新
        class_update = equipment_updates.get("equipment_class")
        if class_update:
            resolved_class_id = self.kb._resolve_class_key(str(class_update))
            try:
                if not resolved_class_id:
                    classes = self.kb.list_robot_classes(task_type)
                    for c in classes:
                        if c.get("class_id") == class_update or c.get("display_name") == class_update:
                            resolved_class_id = c.get("class_id")
                            break

                if task_type:
                    allowed_classes = [c.get("class_id") for c in self.kb.list_robot_classes(task_type)]
                    if resolved_class_id not in allowed_classes:
                        resolved_class_id = None
            except RobotSelectionDataError as _err:
                _rollback_and_fail(
                    "equipment_class",
                    class_update,
                    f"{_err.error_code}: {_err}",
                )
                return

            if resolved_class_id:
                class_slot = sandbox_slots.get("equipment_class")
                old_class = (
                    class_slot.value
                    if class_slot and class_slot.status in ("valid", "candidate")
                    else None
                )
                if allow_overwrite and old_class and old_class != resolved_class_id:
                    changed_parents.append("equipment_class")
                self._apply_slot_update_in_transaction(
                    "equipment_class",
                    resolved_class_id,
                    sandbox_slots,
                    allow_overwrite,
                )
            else:
                _rollback_and_fail(
                    "equipment_class",
                    class_update,
                    f"Robot class '{class_update}' is unknown or not allowed for task '{task_type}'",
                )
                return

        # 2. equipment_family 更新
        family_update = equipment_updates.get("equipment_family")
        resolved_family = None
        if family_update:
            resolved_family = self.kb.resolve_robot_family(str(family_update), task_type)
            if not resolved_family and not task_type:
                resolved_family = self.kb.resolve_robot_family(str(family_update), None)
            if resolved_family:
                explicit_class_in_turn = "equipment_class" in equipment_updates
                active_class_slot = sandbox_slots.get("equipment_class")
                active_class = (
                    active_class_slot.value
                    if active_class_slot and active_class_slot.status in ("valid", "candidate")
                    else None
                )
                target_class = resolved_family.get("robot_class")

                if explicit_class_in_turn and active_class and target_class != active_class:
                    f_slot = sandbox_slots.get("equipment_family") or Slot(slot_name="equipment_family")
                    f_slot.status = "invalid"
                    f_slot.value = None
                    f_slot.candidate_value = family_update
                    f_slot.validation_error = f"Family '{family_update}' does not belong to selected class '{active_class}'"
                    sandbox_slots["equipment_family"] = f_slot
                elif not allow_overwrite and active_class and target_class != active_class:
                    _rollback_and_fail(
                        "equipment_family",
                        family_update,
                        f"Family '{family_update}' conflicts with active class '{active_class}'",
                        force_conflict=True,
                    )
                    return
                else:
                    if active_class and active_class != target_class:
                        changed_parents.append("equipment_class")
                    self._apply_slot_update_in_transaction(
                        "equipment_class",
                        target_class,
                        sandbox_slots,
                        allow_overwrite,
                    )
                    family_slot = sandbox_slots.get("equipment_family")
                    current_family_id = (
                        self.kb.resolve_robot_family_id(str(family_slot.value), task_type)
                        if family_slot and family_slot.value and family_slot.status in ("valid", "candidate")
                        else None
                    )
                    if (
                        allow_overwrite
                        and current_family_id
                        and current_family_id != resolved_family.get("family_id")
                    ):
                        changed_parents.append("equipment_family")
                    self._apply_slot_update_in_transaction(
                        "equipment_family",
                        resolved_family.get("full_name", family_update),
                        sandbox_slots,
                        allow_overwrite,
                    )
            else:
                _rollback_and_fail(
                    "equipment_family",
                    family_update,
                    f"Unknown robot family '{family_update}' for task '{task_type}'",
                )
                return

        # 3. equipment_type (model_variant) 更新
        variant_update = equipment_updates.get("equipment_type")
        selected_variant = None
        if variant_update:
            active_fam_slot = sandbox_slots.get("equipment_family")
            active_family = (
                active_fam_slot.value
                if active_fam_slot and active_fam_slot.status in ("valid", "candidate")
                else None
            )
            active_fam_info = self.kb.resolve_robot_family(str(active_family), task_type) if active_family else None
            active_fam_id = active_fam_info.get("family_id") if active_fam_info else None

            # 全局解算 target variant
            selected_variant = self.kb.get_rov_for_task(
                str(variant_update),
                task_type,
                active_fam_id,
            )
            if not selected_variant:
                selected_variant = self.kb.get_rov_for_task(
                    str(variant_update),
                    task_type,
                    None,
                )
            if not selected_variant and not task_type:
                selected_variant = self.kb.get_rov_for_task(
                    str(variant_update),
                    None,
                    None,
                )
            if not selected_variant:
                selected_variant = self.kb.get_rov(str(variant_update))

            if selected_variant:
                robot_cls = selected_variant.get("robot_class")
                fam_id = selected_variant.get("family_id")
                fam_full = selected_variant.get("family_full_name")

                explicit_fam_in_turn = "equipment_family" in equipment_updates
                explicit_fam_id = (
                    resolved_family.get("family_id") if resolved_family else None
                )
                if explicit_fam_in_turn and explicit_fam_id and explicit_fam_id != fam_id:
                    t_slot = sandbox_slots.get("equipment_type") or Slot(slot_name="equipment_type")
                    t_slot.status = "invalid"
                    t_slot.value = None
                    t_slot.candidate_value = variant_update
                    t_slot.validation_error = f"Variant '{variant_update}' does not belong to selected family '{family_update}'"
                    sandbox_slots["equipment_type"] = t_slot

                    # 清理/作废旧下级槽位，防止形成跨类目混合状态
                    for key_to_clear in ("equipment_unit_id", "equipment_name"):
                        if key_to_clear in sandbox_slots:
                            s = sandbox_slots[key_to_clear]
                            s.value = None
                            s.status = "missing"
                            s.validation_error = None

                    for k in EQUIPMENT_KEYS:
                        if k in sandbox_slots:
                            new_slots[k] = sandbox_slots[k]
                    return
                elif not allow_overwrite and active_fam_id and active_fam_id != fam_id:
                    _rollback_and_fail(
                        "equipment_type",
                        variant_update,
                        f"Variant '{variant_update}' conflicts with active family '{active_family}'",
                        force_conflict=True,
                    )
                    return

                old_variant_slot = sandbox_slots.get("equipment_type")
                old_variant_val = (
                    old_variant_slot.value
                    if old_variant_slot and old_variant_slot.status in ("valid", "candidate")
                    else None
                )
                new_variant_val = selected_variant.get("full_name", variant_update)
                if allow_overwrite and old_variant_val and old_variant_val != new_variant_val:
                    changed_parents.append("equipment_type")
                self._apply_slot_update_in_transaction(
                    "equipment_class",
                    robot_cls,
                    sandbox_slots,
                    allow_overwrite,
                )
                self._apply_slot_update_in_transaction(
                    "equipment_family",
                    fam_full,
                    sandbox_slots,
                    allow_overwrite,
                )
                self._apply_slot_update_in_transaction(
                    "equipment_type",
                    new_variant_val,
                    sandbox_slots,
                    allow_overwrite,
                )
            else:
                _rollback_and_fail(
                    "equipment_type",
                    variant_update,
                    f"Unknown model variant '{variant_update}'",
                )
                return

        # 4. equipment_unit_id / equipment_name 更新
        unit_update = (
            _unwrap(equipment_updates.get("equipment_unit_id"))
            or _unwrap(equipment_updates.get("equipment_name"))
        )
        if unit_update:
            variant_slot = sandbox_slots.get("equipment_type")
            variant_context = (
                selected_variant.get("full_name")
                if selected_variant
                else (
                    variant_slot.value
                    if variant_slot and variant_slot.status in ("valid", "candidate")
                    else None
                )
            )
            resolved_unit = self.kb.resolve_robot_unit(
                str(unit_update),
                task_type,
                str(variant_context) if variant_context else None,
            )
            if not resolved_unit and not task_type:
                resolved_unit = self.kb.resolve_robot_unit(
                    str(unit_update),
                    None,
                )

            if resolved_unit:
                unit_variant = resolved_unit["robot"]
                unit_robot_cls = unit_variant.get("robot_class")
                unit_fam_id = unit_variant.get("family_id")
                unit_fam_full = unit_variant.get("family_full_name")
                unit_vid = unit_variant.get("variant_id")
                unit_variant_full = unit_variant.get("full_name")

                explicit_class_in_turn = equipment_updates.get("equipment_class")
                explicit_family_in_turn = equipment_updates.get("equipment_family")
                explicit_type_in_turn = equipment_updates.get("equipment_type")

                explicit_cls_mismatch = (
                    explicit_class_in_turn is not None
                    and self.kb._resolve_class_key(str(explicit_class_in_turn)) != unit_robot_cls
                )
                explicit_fam_mismatch = False
                if explicit_family_in_turn is not None:
                    explicit_fam_resolved = self.kb.resolve_robot_family_id(str(explicit_family_in_turn), task_type)
                    explicit_fam_mismatch = (
                        explicit_fam_resolved is not None
                        and explicit_fam_resolved != unit_fam_id
                    )
                explicit_type_mismatch = False
                if explicit_type_in_turn is not None:
                    exp_v = self.kb.get_rov(str(explicit_type_in_turn))
                    exp_vid = exp_v.get("variant_id") if exp_v else explicit_type_in_turn
                    if exp_vid and exp_vid != unit_vid:
                        explicit_type_mismatch = True

                parent_mismatch = explicit_cls_mismatch or explicit_fam_mismatch or explicit_type_mismatch

                if parent_mismatch:
                    _rollback_and_fail(
                        "equipment_unit_id",
                        unit_update,
                        f"Unit '{unit_update}' belongs to variant '{unit_variant_full}' but explicitly selected parent is mismatched",
                    )
                    return

                # 四级组合权威校验
                try:
                    self.kb.validate_static_robot_selection(
                        unit_robot_cls,
                        unit_fam_id,
                        unit_variant_full,
                        resolved_unit["unit_id"],
                        task_type,
                    )
                except RobotSelectionDataError as _v_exc:
                    _rollback_and_fail(
                        "equipment_unit_id",
                        unit_update,
                        f"{_v_exc.error_code}: {_v_exc}",
                        force_conflict=True,
                    )
                    return

                # 四级校验通过，更新 sandbox
                self._apply_slot_update_in_transaction(
                    "equipment_class",
                    unit_robot_cls,
                    sandbox_slots,
                    allow_overwrite,
                )
                self._apply_slot_update_in_transaction(
                    "equipment_family",
                    unit_fam_full,
                    sandbox_slots,
                    allow_overwrite,
                )
                self._apply_slot_update_in_transaction(
                    "equipment_type",
                    unit_variant_full,
                    sandbox_slots,
                    allow_overwrite,
                )
                self._apply_slot_update_in_transaction(
                    "equipment_unit_id",
                    resolved_unit["unit_id"],
                    sandbox_slots,
                    allow_overwrite,
                )
                if "equipment_name" in sandbox_slots:
                    self._apply_slot_update_in_transaction(
                        "equipment_name",
                        resolved_unit.get("display_name", resolved_unit["unit_id"]),
                        sandbox_slots,
                        allow_overwrite,
                    )
            else:
                _rollback_and_fail(
                    "equipment_unit_id",
                    unit_update,
                    f"Unknown fleet unit '{unit_update}'",
                )
                return

        # 执行层级依赖失效
        if changed_parents:
            invalidate_robot_cascade_dependents(
                sandbox_slots,
                changed_parents,
                preserve_keys=equipment_updates.keys(),
            )

        # 事务生效
        for k in EQUIPMENT_KEYS:
            if k in sandbox_slots:
                new_slots[k] = sandbox_slots[k]


    def _handle_task_type_update_in_transaction(self, key: str, value: str, new_slots: dict):
        task_type_map = self.kb.get_task_type_map()
        templates = self.kb.task_schemas.get("task_templates", {})

        target_key = None
        if value in task_type_map:
            target_key = task_type_map[value]
        elif key == "task_type_key" and value in templates:
            target_key = value

        existing_task_id = new_slots.get("task_id")
        old_task_type_key = new_slots.get("task_type_key").value if new_slots.get("task_type_key") else None

        # 如果已有 valid 的 task_id，禁止原地跨类别修改任务类型 (Lock task category)
        if existing_task_id and existing_task_id.status == "valid" and existing_task_id.value:
            if target_key and old_task_type_key and target_key != old_task_type_key:
                err_msg = f"任务编号已锁定 ({existing_task_id.value})，无法直接修改任务类别。如需更换类别，请先取消或新建任务。"
                logger.warning(
                    "[DialogueManager] Rejecting task category modification from %s to %s because task_id %s is already locked.",
                    old_task_type_key,
                    target_key,
                    existing_task_id.value,
                )
                if "task_type_key" in new_slots:
                    new_slots["task_type_key"].validation_error = err_msg
                return

        if target_key:
            if value in task_type_map:
                new_slots["task_type"].value = value
                new_slots["task_type"].status = "valid"
                new_slots["task_type_key"].value = target_key
                new_slots["task_type_key"].status = "valid"
            elif key == "task_type_key" and value in templates:
                new_slots["task_type_key"].value = value
                new_slots["task_type_key"].status = "valid"
                values = templates[value].get("task_type_values", [])
                if len(values) == 1:
                    new_slots["task_type"].value = values[0]
                    new_slots["task_type"].status = "valid"

        if target_key:
            required_fields = self.builder.get_schema(target_key, self.mode)
            schema_keys = {f["key"] for f in required_fields}

            # Clean up old dynamic slots in new_slots that do not belong to BASE_SLOT_TYPES, schema_keys, or ALLOWED_INTERNAL_SLOTS
            from .slot_store import BASE_SLOT_TYPES, ALLOWED_INTERNAL_SLOTS
            to_remove = [
                k for k in list(new_slots.keys())
                if k not in BASE_SLOT_TYPES and k not in schema_keys and k not in ALLOWED_INTERNAL_SLOTS
            ]
            for k in to_remove:
                del new_slots[k]

            for f in required_fields:
                fkey = f["key"]
                ftype = f.get("type", "string")
                if fkey not in new_slots:
                    new_slots[fkey] = Slot(slot_name=fkey, value_type=ftype)
                else:
                    new_slots[fkey].value_type = ftype

    def _handle_rov_description_in_transaction(self, description: str, new_slots: dict):
        all_rovs = self.kb.get_all_rovs()
        task_type_key = new_slots.get("task_type_key").value if new_slots.get("task_type_key") else None
        candidates = self.extractor.resolve_rov_description(
            description, all_rovs, task_type_key
        )
        self._pending_rov_candidates = candidates
        if candidates:
            new_slots["_rov_candidates"].value = [
                {"model": r["model"], "full_name": r["full_name"],
                 "category": r["category"], "available": True}
                for r in candidates[:3]
            ]
            new_slots["_rov_candidates"].status = "valid"

    def _normalize_and_validate_in_transaction(
        self,
        new_slots: dict,
        task_type_key: str | None,
        skip_schema_keys: set[str] | frozenset[str] | None = None,
    ):
        if not task_type_key:
            return

        schema = self.builder.get_schema(task_type_key, self.mode)

        for field_def in schema:
            key = field_def["key"]
            if skip_schema_keys and key in skip_schema_keys:
                continue
            ftype = field_def["type"]
            slot = new_slots.get(key)
            if not slot or slot.status in ("fixed", "auto", "conflict", "invalid") or key.startswith("equipment_"):
                continue

            target_val = slot.candidate_value if slot.candidate_value is not None else slot.value
            if target_val is None or (isinstance(target_val, list) and len(target_val) == 0):
                if isinstance(target_val, list) and len(target_val) == 0 and slot.status != "conflict":
                    slot.status = "missing"
                continue

            temp_state = {k: (s.candidate_value if s.candidate_value is not None else s.value) for k, s in new_slots.items() if s.status not in ("invalid", "missing") and (s.value is not None or s.candidate_value is not None)}

            allowed = self.builder._resolve_allowed(field_def, task_type_key, temp_state)
            if allowed:
                raw = target_val
                if ftype == "list":
                    normalized = self.normalizer.normalize(raw, allowed, ftype)
                else:
                    normalized = self.normalizer.normalize(str(raw), allowed, ftype)

                if normalized is not None:
                    slot.value = normalized
                    slot.candidate_value = None
                    slot.status = "valid"
                    if key != "payload" or slot.validation_error is None:
                        slot.validation_error = None
                else:
                    slot.status = "invalid"
                    slot.candidate_value = raw
                    slot.validation_error = f"Value '{raw}' could not be normalized to allowed options: {allowed}"
            else:
                if ftype == "datetime":
                    val_str = str(target_val)
                    pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$"
                    if re.match(pattern, val_str):
                        slot.value = val_str
                        slot.candidate_value = None
                        slot.status = "valid"
                        slot.validation_error = None
                    else:
                        slot.status = "invalid"
                        slot.candidate_value = target_val
                        slot.validation_error = f"Invalid datetime format: {val_str}. Expected YYYY-MM-DDTHH:MM:SS"
                elif ftype == "coord":
                    coord = self.builder._validate_coord(target_val)
                    if coord:
                        slot.value = coord
                        slot.candidate_value = None
                        slot.status = "valid"
                        slot.validation_error = None
                    else:
                        slot.status = "invalid"
                        slot.candidate_value = target_val
                        slot.validation_error = f"Invalid coordinate format: {target_val}"
                elif ftype == "number":
                    num = self.builder._validate_number(target_val)
                    if num is not None:
                        slot.value = num
                        slot.candidate_value = None
                        slot.status = "valid"
                        slot.validation_error = None
                    else:
                        slot.status = "invalid"
                        slot.candidate_value = target_val
                        slot.validation_error = f"Invalid number: {target_val}"
                else:
                    slot.value = target_val
                    slot.candidate_value = None
                    slot.status = "valid"
                    slot.validation_error = None

        for eq_key in (
            "equipment_class",
            "equipment_family",
            "equipment_type",
            "equipment_name",
            "equipment_unit_id",
        ):
            eq_slot = new_slots.get(eq_key)
            if (
                eq_slot
                and eq_slot.status == "candidate"
                and eq_slot.value is not None
                and not eq_slot.validation_error
            ):
                eq_slot.status = "valid"



        # 字段自身的格式/候选合法性与任务组合约束是两类状态：
        # 例如“水深 600m”和“最大水深 500m 的设备”均可被正确录入，
        # 但二者组合会触发硬约束。硬约束由对话阶段 blocked_hard 管理，
        # 不能把已合法录入的关联字段重新标记为 invalid，否则前端会误报缺失。

    def _resolve_pending_oilfield_confirmation(
        self,
        user_message: str,
        request_id: str = "req_default",
    ) -> str | None:
        pending_slot = self.slot_store.slots.get("pending_oilfield_name")
        if not pending_slot or not pending_slot.value or pending_slot.status != "valid":
            return None
        if self._user_cancelled_oilfield(user_message):
            oil_slot = self.slot_store.slots.get("oilfield_name")
            clear_oil = ("oilfield_name", "oilfield_entity_id") if (not oil_slot or oil_slot.status != "valid") else ()
            self._commit_internal_slot_values(
                {},
                clear_keys=(
                    "pending_oilfield_name",
                    "pending_oilfield_candidates",
                    *clear_oil,
                ),
            )
            self._rebuild_cache()
            return "已取消当前待确认油田名称，请提供标准的油田名称（例如：流花11-1油田、陵水17-2油田等），或补充油田坐标。"


        if not self._user_confirmed_oilfield(user_message):
            return None

        candidate = self._top_pending_oilfield_candidate(user_message)
        if not candidate:
            return self._build_pending_oilfield_reply()

        confirmed_name = candidate.get("name")
        self._commit_internal_slot_values(
            {
                "oilfield_name": confirmed_name,
                "raw_oilfield_name": confirmed_name,
                "oilfield_entity_id": candidate.get("id"),
                "oilfield_match_status": "accepted",
                "oilfield_match_confidence": candidate.get("confidence"),
                "oilfield_match_evidence": candidate.get("evidence", []),
            },
            clear_keys=(
                "pending_oilfield_name",
                "pending_oilfield_candidates",
            ),
        )
        self._rebuild_cache()
        return f"已确认油田名称为“{confirmed_name}”，我会按这个标准名称继续收集任务信息。"

    def _build_pending_oilfield_reply(self) -> str | None:
        pending_slot = self.slot_store.slots.get("pending_oilfield_name")
        raw_name = pending_slot.value if (pending_slot and pending_slot.status == "valid") else None
        oil_slot = self.slot_store.slots.get("oilfield_name")
        has_oilfield = oil_slot.value if (oil_slot and oil_slot.status == "valid") else None
        if not raw_name or has_oilfield:
            return None

        candidate = self._top_pending_oilfield_candidate()
        if candidate:
            name = candidate.get("name")
            return f"我识别到油田名称“{raw_name}”，疑似为“{name}”。请确认是否采用该标准油田名称？"
        return (
            f"我识别到油田名称“{raw_name}”，但没有匹配到标准油田。"
            "请提供标准的油田名称（例如：流花11-1油田、陵水17-2油田等），或补充油田坐标。"
        )

    def _top_pending_oilfield_candidate(self, user_message: str = "") -> dict | None:
        cand_slot = self.slot_store.slots.get("pending_oilfield_candidates")
        candidates = cand_slot.value if (cand_slot and cand_slot.status == "valid") else None
        if isinstance(candidates, list) and candidates:
            if user_message:
                for c in candidates:
                    if isinstance(c, dict) and c.get("name") and c.get("name") in user_message:
                        return c
            candidate = candidates[0]
            if isinstance(candidate, dict) and candidate.get("name"):
                return candidate
        return None

    def _user_confirmed_oilfield(self, message: str) -> bool:
        keywords = ["是", "对", "就是", "采用", "确认", "确定", "可以", "好的", "ok", "使用"]
        negations = ["不", "别", "不要", "不是", "取消"]
        msg = message.strip().lower()
        if any(neg in msg for neg in negations):
            return False
        return any(kw in msg for kw in keywords)

    def _user_cancelled_oilfield(self, message: str) -> bool:
        msg = message.strip()
        if any(neg in msg for neg in ["不是要取消", "不是取消", "不要取消", "不取消", "别取消", "不要修改", "不修改"]):
            return False
        if any(mod_kw in msg for mod_kw in ["改成", "修改", "水深", "支持船", "管缆", "设备", "载荷"]) and "油田" not in msg:
            return False
        if "任务" in msg or "取消任务" in msg:
            return False

        pending_slot = self.slot_store.slots.get("pending_oilfield_name")
        pending_name = pending_slot.value if (pending_slot and pending_slot.status == "valid") else None
        if pending_name:
            import re
            mentioned_oilfields = re.findall(r"[\u4e00-\u9fa50-9\-]+油田", msg)
            if mentioned_oilfields:
                p_norm = pending_name.replace("油田", "").strip()
                matched_any = any(p_norm in m or m.replace("油田", "").strip() in p_norm for m in mentioned_oilfields)
                if not matched_any:
                    return False

        keywords = ["不是", "不对", "否", "错了", "重新", "取消油田", "不要此油田", "这个油田不对", "不要"]
        return any(kw in msg for kw in keywords) or msg in ("不要", "取消", "不对", "不是")



    # --------------------------------------------------------------------------
    # 约束检查（硬解除后检查软）
    # --------------------------------------------------------------------------

    def _merge_oilfield_context_violations(self, new_violations: list[Violation]) -> list[Violation]:
        entity_id = self.task_state.get("oilfield_entity_id")
        if not entity_id:
            return new_violations

        coords = self.task_state.get("oilfield_coordinates") or self.task_state.get("start_point")
        water_depth = self.task_state.get("water_depth")

        try:
            ctx_res = self.oilfield_linker.evaluate_context(
                entity_id=entity_id,
                coordinates=coords if coords is not None else _UNSET,
                water_depth=water_depth if water_depth is not None else _UNSET,
            )
            if ctx_res and ctx_res.issues:
                merged = list(new_violations)
                existing_ids = {v.constraint_id for v in merged}
                for issue in ctx_res.issues:
                    if issue.constraint_id not in existing_ids:
                        merged.append(
                            Violation(
                                constraint_id=issue.constraint_id,
                                constraint_name=issue.constraint_name,
                                check_type=issue.check_type,
                                severity=issue.severity,
                                message=issue.message,
                                related_fields=list(issue.related_fields),
                            )
                        )
                return merged
        except Exception as exc:
            merged = list(new_violations)
            merged.append(
                Violation(
                    constraint_id="C029",
                    constraint_name="油田上下文计算异常",
                    check_type="oilfield_context_failure",
                    severity="hard",
                    message=f"油田上下文计算失败，安全熔断: {exc}",
                    related_fields=["oilfield_name"],
                )
            )
            return merged

        return new_violations

    def _run_constraint_check(self, changed_fields: set[str]) -> dict:
        """执行约束检查，返回上下文"""
        if not changed_fields and self.phase not in ("blocked_hard", "blocked_soft"):
            return {"type": "none", "violations": [], "hard_refusal_counts": {}}

        val_res = self._refresh_validation(purpose="interactive", changed_fields=changed_fields)
        new_violations = self._merge_oilfield_context_violations(val_res.violations)

        current_hard = [
            v for v in new_violations
            if v.severity == "hard"
        ]
        current_soft = [
            v for v in new_violations
            if v.severity == "soft" and not self._is_whitelisted(v)
        ]
        current_blockers = current_hard + current_soft

        # 处理 soft 阻塞升级为 hard / 维持 / 解除
        if self.phase == "blocked_soft":
            if current_hard:
                self._transition_phase("blocked_hard", reason="soft_upgraded_to_hard")
                self._blocking_violations = current_blockers
                for v in current_hard:
                    if v.constraint_id not in self._hard_refusal_counts:
                        self._hard_refusal_counts[v.constraint_id] = 0

                return {
                    "type": "hard",
                    "violations": current_blockers,
                    "hard_refusal_counts": dict(self._hard_refusal_counts),
                }

            if current_soft:
                self._blocking_violations = current_soft
                return {
                    "type": "soft",
                    "violations": current_soft,
                    "hard_refusal_counts": {},
                }

            self._blocking_violations = []
            self._transition_phase("collecting", reason="soft_warning_resolved")
            return {
                "type": "none",
                "violations": [],
                "hard_refusal_counts": {},
            }

        # 处理 hard 阻塞维持 / 降级为 soft / 解除
        if self.phase == "blocked_hard":
            if current_hard:
                self._blocking_violations = current_blockers
                for v in current_hard:
                    self._hard_refusal_counts[v.constraint_id] = \
                        self._hard_refusal_counts.get(v.constraint_id, 0) + 1

                final_ids = {
                    cid for cid, cnt in self._hard_refusal_counts.items()
                    if cnt >= HARD_REFUSAL_LIMIT
                }
                if final_ids:
                    self._transition_phase("rejected", reason="hard_refusal_limit_reached")
                    self._blocking_violations = []
                    return {
                        "type": "hard_rejected",
                        "violations": current_blockers,
                        "hard_refusal_counts": dict(self._hard_refusal_counts),
                    }

                warn_ids = {
                    cid for cid, cnt in self._hard_refusal_counts.items()
                    if cnt == HARD_REFUSAL_LIMIT - 1
                }
                ctx_type = "hard_final_warning" if warn_ids else "hard"
                return {
                    "type": ctx_type,
                    "violations": current_blockers,
                    "hard_refusal_counts": dict(self._hard_refusal_counts),
                }
            else:
                # 硬约束解除，清除计数
                resolved_ids = set(self._hard_refusal_counts.keys())
                for cid in resolved_ids:
                    self._hard_refusal_counts.pop(cid, None)

                if current_soft:
                    self._transition_phase("blocked_soft", reason="hard_downgraded_to_soft")
                    self._blocking_violations = current_soft
                    return {
                        "type": "soft",
                        "violations": current_soft,
                        "hard_refusal_counts": {},
                    }

                self._transition_phase("collecting", reason="hard_constraint_resolved")
                self._blocking_violations = []
                return {
                    "type": "none",
                    "violations": [],
                    "hard_refusal_counts": {},
                }

        # collecting / confirming 状态下的新违规
        if self.phase in ("collecting", "confirming"):
            if current_hard:
                self._transition_phase("blocked_hard", reason="hard_constraint_detected")
                self._blocking_violations = current_blockers
                for v in current_hard:
                    if v.constraint_id not in self._hard_refusal_counts:
                        self._hard_refusal_counts[v.constraint_id] = 0
                return {
                    "type": "hard",
                    "violations": current_blockers,
                    "hard_refusal_counts": dict(self._hard_refusal_counts),
                }

            if current_soft:
                self._transition_phase("blocked_soft", reason="soft_warning_detected")
                self._blocking_violations = current_soft
                return {
                    "type": "soft",
                    "violations": current_soft,
                    "hard_refusal_counts": {},
                }

        return {"type": "none", "violations": [], "hard_refusal_counts": {}}

    # --------------------------------------------------------------------------
    # 工具方法
    # --------------------------------------------------------------------------

    def _merge_coordinate_updates(
        self,
        user_message: str,
        updates: dict,
        required: list[dict] | None,
    ) -> dict:
        coord_fields = {
            item["key"]
            for item in (required or [])
            if item.get("type") == "coord" and item.get("key")
        }
        coord_updates = parse_coordinate_updates(
            user_message,
            coord_fields,
            current_state=self.task_state,
            proposed_updates=updates,
        )
        if not coord_updates:
            return updates
        merged = dict(updates)
        merged.update(coord_updates)
        return merged

    def _invalidate_whitelist(self, changed_fields: set[str]):
        if changed_fields:
            self._soft_whitelist -= {e for e in self._soft_whitelist if e[0] in changed_fields}

    def _is_whitelisted(self, v: Violation) -> bool:
        res = getattr(self.slot_store, "validation_result", None)
        if res is None:
            return False

        curr_fp = getattr(res, "validation_fingerprint", None)
        state_snap = getattr(res, "state_snapshot", None)
        curr_state_ver = state_snap.get("state_version") if isinstance(state_snap, dict) else None
        curr_status_ref = state_snap.get("status_ref") if isinstance(state_snap, dict) else None

        if not curr_fp or curr_state_ver is None:
            return False

        acks = getattr(self.slot_store, "validation_acknowledgements", [])
        for ack in acks:
            ack_cid = getattr(ack, "constraint_id", None) if not isinstance(ack, dict) else ack.get("constraint_id")
            if ack_cid != v.constraint_id:
                continue

            ack_tv = getattr(ack, "task_version", None) if not isinstance(ack, dict) else ack.get("task_version")
            ack_vv = getattr(ack, "validation_version", None) if not isinstance(ack, dict) else ack.get("validation_version")
            ack_fp = getattr(ack, "validation_fingerprint", None) if not isinstance(ack, dict) else ack.get("validation_fingerprint")
            ack_sref = getattr(ack, "status_ref", None) if not isinstance(ack, dict) else ack.get("status_ref")
            ack_sver = getattr(ack, "state_version", None) if not isinstance(ack, dict) else ack.get("state_version")

            if (
                ack_tv == getattr(res, "task_version", 1)
                and ack_vv == getattr(res, "validation_version", 1)
                and ack_fp == curr_fp
                and ack_sref == curr_status_ref
                and ack_sver == curr_state_ver
            ):
                return True

        return False

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

    @classmethod
    def _is_soft_warning_acknowledgement(cls, message: str) -> bool:
        """仅识别明确接受/忽略软警告的独立指令，绝不混入通用确认或最终发布词。"""
        text = re.sub(r"[\s，,。.!！?？、；;：:]+", "", message).lower()
        exact_ack_words = {
            "忽略警告",
            "确认忽略警告",
            "接受警告继续",
            "接受风险继续",
            "继续并接受该警告",
            "确认继续",
            "继续",
            "忽略",
            "忽略风险",
            "无视警告",
            "无视风险",
        }
        if text in exact_ack_words:
            return True

        parameter_cues = (
            "改成", "修改", "水深", "深度", "时间", "支持船", "管缆", "油田",
            "设备", "工具", "改为", "设为", "调整", "增加", "减少", "补充",
        )
        if any(cue in message for cue in parameter_cues):
            return False

        return any(
            kw in text
            for kw in ("忽略警告", "忽略风险", "无视警告", "无视风险", "接受警告", "接受风险", "确认继续")
        )

    def _ensure_constraint_details(self, reply: str, constraint_context: dict) -> str:
        """Append canonical details for violations omitted or paraphrased by the LLM."""
        violations = constraint_context.get("violations") or []
        missing = [
            violation
            for violation in violations
            if violation.message not in reply
        ]
        if not missing:
            return reply

        details = self.validator.format_violations(missing)
        return f"{reply.rstrip()}\n\n{details}" if reply.strip() else details

    def _reject_hard_constraint_bypass(self, user_message: str) -> str:
        """Reject confirmation/ignore commands while hard violations remain."""
        violations = [
            violation
            for violation in self._blocking_violations
            if violation.severity == "hard"
        ]
        if not violations:
            val_res = self._refresh_validation(purpose="interactive")
            violations = [
                v for v in val_res.violations
                if v.severity == "hard"
            ]

        reply = "硬性约束不能通过确认或忽略警告绕过。请先修正以下问题后再发布任务。"
        if violations:
            reply = f"{reply}\n\n{self.validator.format_violations(violations)}"
            self._blocking_violations = violations

        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    @staticmethod
    def _user_cancelled(message: str) -> bool:
        negated_cancel = ["不是要取消", "不是取消", "不要取消", "别取消", "不取消", "免取消"]
        if any(neg in message for neg in negated_cancel):
            return False

        if any(mod_kw in message for mod_kw in ["修改", "参数", "设置", "载荷", "水深", "支持船", "管缆", "油田", "设备", "工具"]) and "任务" not in message:
            return False
        keywords = ["取消任务", "放弃任务", "终止任务", "取消", "放弃", "不要了", "终止", "退出"]
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

        for m in self._last_missing:
            missing_display.append({
                "key": m["key"],
                "label": m["label"],
                "allowed_values": m.get("allowed_values", []),
            })

        return {
            "phase": self.phase,
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

    def is_start_time_near_now(self, time_window_minutes: int = 10) -> bool:
        try:
            start_time_str = self.task_state.get("start_time")
            if not start_time_str:
                return False

            # 使用模拟时间代替系统时间
            now = get_current_datetime()
            now = now.replace(microsecond=0)

            start_time_str = start_time_str.replace("T", " ").replace("：", ":").strip()
            if start_time_str.endswith("Z"):
                start_time_str = start_time_str[:-1] + "+00:00"
            start_time = datetime.fromisoformat(start_time_str)
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            else:
                start_time = start_time.astimezone(ZoneInfo("Asia/Shanghai"))

            delta_seconds = (start_time - now).total_seconds()
            return 0 <= delta_seconds <= time_window_minutes * 60
        except Exception as e:
            print("时间判断出错:", e)
            return False

    # --------------------------------------------------------------------------
    # 缓存重建
    # --------------------------------------------------------------------------

    def _rebuild_cache(self, commit_derived: bool = True) -> None:
        """根据当前 slot_store 重新构建 task_state, _last_built_json 和 _last_missing"""
        self.task_state = self.slot_store.get_task_state()
        task_type_key = self.task_state.get("task_type_key")
        eq_type = self.task_state.get("equipment_type") or self.task_state.get("equipment_name")
        if commit_derived and eq_type and not self.task_state.get("equipment_family"):
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
        """兼容恢复旧版扁平快照和 snapshot_version=2 完整快照。原子验证 schema。"""
        if is_session_state_v2_enabled():
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
            candidate_store.commit_transaction(new_slots, [])


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

        if is_session_state_v2_enabled() and contract_state is not None:
            self._apply_session_state_contract(contract_state)
        else:
            self.mode = mode
            self.dialogue_mode = dialogue_mode
            self.last_mode_transition = copy.deepcopy(last_mode_transition)
            self.mode_transition_history = copy.deepcopy(validated_history)
            self.control_state = control_state
            self.last_control_request = copy.deepcopy(last_control_request)

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
                self.phase = "done"
                self.final_result = _loaded_intent
            else:
                self.phase = "collecting"
                today = get_current_datetime().strftime("%Y%m%d")
                task_dir = _ti_builder_module.get_task_dir(create=False)
                new_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])
                new_slots = self.slot_store.clone_slots()
                if "intent_id" not in new_slots:
                    new_slots["intent_id"] = Slot("intent_id")
                new_slots["intent_id"].value = new_id
                new_slots["intent_id"].status = "valid"
                new_slots["intent_id"].source = "auto"
                self.slot_store.commit_transaction(new_slots, self.slot_store.unresolved)
                self.task_state = self.slot_store.get_task_state()
                self._last_built_json = self.slot_store.get_built_json()
        else:
            self.phase = phase
            if not is_valid_id:
                today = get_current_datetime().strftime("%Y%m%d")
                task_dir = _ti_builder_module.get_task_dir(create=False)
                new_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])
                new_slots = self.slot_store.clone_slots()
                if "intent_id" not in new_slots:
                    new_slots["intent_id"] = Slot("intent_id")
                new_slots["intent_id"].value = new_id
                new_slots["intent_id"].status = "valid"
                new_slots["intent_id"].source = "auto"
                self.slot_store.commit_transaction(new_slots, self.slot_store.unresolved)
                self.task_state = self.slot_store.get_task_state()
                self._last_built_json = self.slot_store.get_built_json()

        if is_session_state_v2_enabled():
            _ = self._build_session_state_contract()

        self._run_session_state_shadow_check(checkpoint="load_snapshot")
