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
import copy
import json
import logging
import re
import threading
import os
import stat
import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Callable
from zoneinfo import ZoneInfo
from datetime import datetime

logger = logging.getLogger(__name__)

from .llm_client import LLMClient
from .knowledge_retriever import KnowledgeBase
from .extractor import ParameterExtractor
from .normalizer import FieldNormalizer
from .output_builder import OutputBuilder
from .validator import TaskValidator, Violation
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
from .oilfield_linker import OilfieldEntityLinker
from . import task_intent_builder as _ti_builder_module
from .id_sequence import validate_intent_id, next_daily_id
from .slot_store import SlotStore, Slot, normalize_slot_value_type

from .exceptions import (
    TaskPersistenceError,
    IntentIdConflict,
    IdReservationError,
    ControlAuditPersistenceError,
    ControlAuditConflictError,
    ControlAuditCommitUncertainError,
    ControlAuditCorruptionError,
)
from .intent_router import IntentRouter, IntentRouteResult
from .task_request_guard import analyze_task_request
from .result_paths import get_task_dir, get_history_dir
from .history_manager import (
    compute_request_fingerprint,
    load_control_event,
    get_control_event_path,
    load_latest_session_snapshot,
    _get_request_lock,
    _safe_request_id,
)


@dataclass
class ProcessOutcome:
    reply: str
    control_event_created: bool
    control_action: Optional[str]
    draft_cancelled: bool
    request_id: str
    history_snapshot_required: bool = False
    control_event_data: Optional[Dict[str, Any]] = None
    is_retry: bool = False


import math

HARD_REFUSAL_LIMIT = 4   # 连续拒绝上限

DRAFT_PHASES = {
    "collecting",
    "confirming",
    "blocked_soft",
    "blocked_hard",
}

VALID_DIALOGUE_MODES = {
    "task_collection",
    "knowledge_qa",
    "emergency_intervention",
    "uncertain",
}
VALID_CONTROL_STATES = {
    "idle",
    "pause_requested",
    "stop_requested",
    "abort_requested",
    "cancel_requested",
}
ACTION_TO_CONTROL_STATE = {
    "pause": "pause_requested",
    "stop": "stop_requested",
    "abort": "abort_requested",
    "cancel": "cancel_requested",
}
VALID_CONTROL_SOURCES = {"rule", "llm", "fallback"}
VALID_CONTROL_ACTIONS = {"pause", "stop", "abort", "cancel"}


def _validate_control_snapshot(
    control_state: Any,
    last_control_request: Any,
) -> tuple[str, dict | None]:
    if control_state is None:
        clean_c_state = "idle"
    elif isinstance(control_state, str) and control_state.strip() in VALID_CONTROL_STATES:
        clean_c_state = control_state.strip()
    else:
        raise ValueError(f"Invalid control_state in snapshot: {control_state!r}")

    if clean_c_state == "idle":
        if last_control_request is not None:
            raise ValueError("control_state is idle but last_control_request is not None")
        return ("idle", None)

    # 非 idle 状态
    if not isinstance(last_control_request, dict):
        raise ValueError(f"control_state is {clean_c_state!r} but last_control_request is not a dictionary")

    # 旧快照降级策略：非 idle 但缺少新增元数据字段 (request_id / requested_at / phase_at_request) 时默认降级为 idle，不伪造伪假元数据
    if (
        "request_id" not in last_control_request
        or "requested_at" not in last_control_request
        or "phase_at_request" not in last_control_request
    ):
        return ("idle", None)

    action = last_control_request.get("action")
    status = last_control_request.get("status")
    source = last_control_request.get("source")
    confidence = last_control_request.get("confidence")
    reason = last_control_request.get("reason")
    request_id = last_control_request.get("request_id")
    requested_at = last_control_request.get("requested_at")
    phase_at_request = last_control_request.get("phase_at_request")
    task_id = last_control_request.get("task_id")
    intent_id = last_control_request.get("intent_id")

    if action not in VALID_CONTROL_ACTIONS:
        raise ValueError(f"Invalid action in last_control_request: {action!r}")

    if ACTION_TO_CONTROL_STATE[action] != clean_c_state:
        raise ValueError(f"Action {action!r} does not match control_state {clean_c_state!r}")

    if status != "requested":
        raise ValueError(f"Invalid status in last_control_request: {status!r}")

    if not isinstance(source, str) or source.strip() not in VALID_CONTROL_SOURCES:
        raise ValueError(f"Invalid source in last_control_request: {source!r}")

    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not (0.0 <= float(confidence) <= 1.0)
        or math.isnan(float(confidence))
    ):
        raise ValueError(f"Invalid confidence in last_control_request: {confidence!r}")

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"Invalid or empty reason in last_control_request: {reason!r}")

    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError(f"Invalid or empty request_id in last_control_request: {request_id!r}")

    if not isinstance(requested_at, str) or not requested_at.strip():
        raise ValueError(f"Invalid or empty requested_at in last_control_request: {requested_at!r}")

    try:
        dt = datetime.fromisoformat(requested_at.strip())
        if dt.tzinfo is None:
            raise ValueError(f"requested_at must be timezone-aware: {requested_at!r}")
    except Exception as e:
        if isinstance(e, ValueError) and "timezone" in str(e):
            raise e
        raise ValueError(f"Invalid ISO 8601 requested_at in last_control_request: {requested_at!r}") from e

    if not isinstance(phase_at_request, str) or not phase_at_request.strip():
        raise ValueError(f"Invalid or empty phase_at_request in last_control_request: {phase_at_request!r}")

    task_id = last_control_request.get("task_id")
    if task_id is not None and not isinstance(task_id, str):
        raise ValueError(f"Invalid task_id in last_control_request: {task_id!r}")

    intent_id = last_control_request.get("intent_id")
    if intent_id is not None and not isinstance(intent_id, str):
        raise ValueError(f"Invalid intent_id in last_control_request: {intent_id!r}")

    clean_req = {
        "action": action,
        "status": "requested",
        "source": source.strip(),
        "confidence": float(confidence),
        "reason": reason.strip(),
        "request_id": request_id.strip(),
        "requested_at": requested_at.strip(),
        "phase_at_request": phase_at_request.strip(),
        "task_id": task_id.strip() if isinstance(task_id, str) else None,
        "intent_id": intent_id.strip() if isinstance(intent_id, str) else None,
    }
    return (clean_c_state, clean_req)


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
    def __init__(self, llm: LLMClient, kb: KnowledgeBase):
        self.llm = llm
        self.kb = kb
        self.extractor = ParameterExtractor(llm)
        # LHL 归一化器采用确定性规则，不依赖 LLM 猜测合法字段值。
        self.normalizer = FieldNormalizer()
        self.builder = OutputBuilder(kb)
        self.validator = TaskValidator(kb)
        self.oilfield_linker = OilfieldEntityLinker(kb.environment)
        self.intent_router = IntentRouter(llm)

        # 对话核心状态
        self.conversation_history: list[dict] = []
        self.slot_store = SlotStore(kb)
        self.task_state: dict = self.slot_store.get_task_state()
        self.mode: str = "normal"
        self.dialogue_mode: str = "task_collection"
        self.control_state: str = "idle"
        self.last_control_request: dict | None = None
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

    def _export_runtime_state(self) -> dict[str, Any]:
        """导出纯内存状态快照，用于控制/审计事务失败时的零副作用回滚。"""
        return {
            "conversation_history": copy.deepcopy(self.conversation_history),
            "slot_store_snapshot": self.slot_store.export_snapshot(),
            "task_state": copy.deepcopy(self.task_state),
            "_last_built_json": copy.deepcopy(self._last_built_json),
            "_last_missing": copy.deepcopy(self._last_missing),
            "phase": self.phase,
            "mode": self.mode,
            "dialogue_mode": self.dialogue_mode,
            "control_state": self.control_state,
            "last_control_request": copy.deepcopy(self.last_control_request),
            "final_result": copy.deepcopy(self.final_result),
            "_blocking_violations": copy.deepcopy(self._blocking_violations),
            "_soft_whitelist": copy.deepcopy(self._soft_whitelist),
            "_pending_rov_candidates": copy.deepcopy(self._pending_rov_candidates),
            "_hard_refusal_counts": copy.deepcopy(self._hard_refusal_counts),
            "awaiting_final_confirm": self.awaiting_final_confirm,
            "task_start_now": self.task_start_now,
        }

    def _restore_runtime_state(self, state: dict[str, Any]) -> None:
        """从内存快照完全恢复运行时状态（零磁盘与 ID 副作用）。"""
        self.conversation_history = copy.deepcopy(state["conversation_history"])
        self.slot_store = SlotStore.from_snapshot(state["slot_store_snapshot"], self.kb)
        self.task_state = copy.deepcopy(state["task_state"])
        self._last_built_json = copy.deepcopy(state["_last_built_json"])
        self._last_missing = copy.deepcopy(state["_last_missing"])
        self.phase = state["phase"]
        self.mode = state["mode"]
        self.dialogue_mode = state["dialogue_mode"]
        self.control_state = state["control_state"]
        self.last_control_request = copy.deepcopy(state["last_control_request"])
        self.final_result = copy.deepcopy(state["final_result"])
        self._blocking_violations = copy.deepcopy(state["_blocking_violations"])
        self._soft_whitelist = copy.deepcopy(state["_soft_whitelist"])
        self._pending_rov_candidates = copy.deepcopy(state["_pending_rov_candidates"])
        self._hard_refusal_counts = copy.deepcopy(state["_hard_refusal_counts"])
        self.awaiting_final_confirm = state["awaiting_final_confirm"]
        self.task_start_now = state["task_start_now"]

    def _switch_dialogue_mode(
        self,
        new_mode: str,
        source: str = "rule",
        confidence: float = 1.0,
        reason: str = "",
    ) -> None:
        old_mode = self.dialogue_mode
        if old_mode != new_mode:
            logger.info(
                "[DialogueManager] Mode switch: %s -> %s | source=%s | confidence=%.2f | reason=%s",
                old_mode,
                new_mode,
                source,
                confidence,
                reason,
            )
            self.dialogue_mode = new_mode



    # --------------------------------------------------------------------------
    # 主入口
    # --------------------------------------------------------------------------

    def _find_existing_control_event(
        self,
        request_id: str,
        session_id: str = "default_session",
    ) -> Optional[Dict[str, Any]]:
        """精确路径查找控制事件（(session_id, request_id) 作用域，不使用 glob）。

        返回语义：
        - None: 文件不存在（可安全处理新请求）
        - dict: 文件存在且合法（用于幂等重试或冲突检测）
        - ControlAuditCorruptionError: 文件存在但损坏/不可读 → fail closed，不可当作未找到
        """
        if not request_id:
            return None
        history_dir = get_history_dir(create=False)
        if not history_dir.exists():
            return None
        # intent_id 不参与文件路径，只作为 JSON 元数据
        return load_control_event(history_dir, session_id, request_id)

    def load_session_state(self, session_id: str) -> bool:
        """在 Manager 初始化或服务重启后装载指定 session_id 的最新持久化状态。"""
        with self._session_lock:
            self.session_id = session_id
            latest_snap = load_latest_session_snapshot(session_id)
            if latest_snap:
                self._restore_state_from_snapshot(latest_snap)
                logger.info(
                    "[DialogueManager] Loaded session %s latest state: phase=%s control_state=%s",
                    session_id, self.phase, self.control_state,
                )
                return True
            return False

    def _restore_state_from_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """从 snapshot 字典恢复 DialogueManager 内存状态。"""
        mem_state = {
            "conversation_history": copy.deepcopy(snapshot.get("conversation_history", self.conversation_history)),
            "slot_store_snapshot": snapshot.get("slot_store", self.slot_store.export_snapshot()),
            "task_state": copy.deepcopy(snapshot.get("task_state", self.task_state)),
            "_last_built_json": copy.deepcopy(snapshot.get("built_json", self._last_built_json)),
            "_last_missing": copy.deepcopy(snapshot.get("_last_missing", self._last_missing)),
            "phase": snapshot.get("phase", self.phase),
            "mode": snapshot.get("mode", self.mode),
            "dialogue_mode": snapshot.get("dialogue_mode", self.dialogue_mode),
            "control_state": snapshot.get("control_state", self.control_state),
            "last_control_request": copy.deepcopy(snapshot.get("last_control_request", self.last_control_request)),
            "final_result": copy.deepcopy(snapshot.get("final_result", self.final_result)),
            "_blocking_violations": copy.deepcopy(snapshot.get("_blocking_violations", self._blocking_violations)),
            "_soft_whitelist": copy.deepcopy(snapshot.get("_soft_whitelist", self._soft_whitelist)),
            "_pending_rov_candidates": copy.deepcopy(snapshot.get("_pending_rov_candidates", self._pending_rov_candidates)),
            "_hard_refusal_counts": copy.deepcopy(snapshot.get("_hard_refusal_counts", self._hard_refusal_counts)),
            "awaiting_final_confirm": snapshot.get("awaiting_final_confirm", self.awaiting_final_confirm),
            "task_start_now": snapshot.get("task_start_now", self.task_start_now),
        }
        self._restore_runtime_state(mem_state)

    def _restore_state_from_event_snapshot(self, event: Dict[str, Any]) -> None:
        """兼容接口：从控制审计事件恢复状态。"""
        snapshot = event.get("snapshot") if isinstance(event, dict) else None
        if isinstance(snapshot, dict):
            self._restore_state_from_snapshot(snapshot)

    def process(self, user_message: str, request_id: str = "req_default") -> str:
        with self._session_lock:
            return self._process_internal(user_message, request_id)

    def process_with_audit(
        self,
        user_message: str,
        request_id: str = "req_default",
        session_id: str = "default_session",
        persist_callback: Optional[Callable[["DialogueManager", ProcessOutcome, Dict[str, Any]], None]] = None,
    ) -> ProcessOutcome:
        """全流程控制事务（R9 版）：

        1. 校验 request_id 格式
        2. 获取跨进程 per-request 锁
        3. 精确查重（fail closed on corruption）
        4. 幂等重试：返回保存的结果，绝对不修改/覆盖当前 live Manager 内存状态
        5. 导出 pre-state
        6. 执行状态机
        7. 构造 ProcessOutcome
        8. 提交持久化
        """
        try:
            _safe_request_id(request_id)
        except ValueError:
            raise ControlAuditConflictError(f"Invalid request_id format: {request_id!r}")

        import fcntl as _fcntl
        history_dir = get_history_dir(create=True)
        req_lock = _get_request_lock(history_dir, session_id, request_id)
        try:
            _fcntl.flock(req_lock.fileno(), _fcntl.LOCK_EX)

            with self._session_lock:
                self.session_id = session_id

                try:
                    existing_event = self._find_existing_control_event(request_id, session_id)
                except ControlAuditCorruptionError:
                    raise

                if existing_event is not None:
                    existing_fp = existing_event.get("request_fingerprint")
                    eff_action = existing_event.get("action")

                    incoming_fp = compute_request_fingerprint(
                        session_id=session_id,
                        request_id=request_id,
                        user_message=user_message,
                        action=eff_action,
                    )

                    if existing_fp and existing_fp == incoming_fp:
                        # 步骤 4：幂等重试 — 直接返回最初保存的结果，绝不覆盖/改写 live Manager 状态！
                        saved_reply = existing_event.get("reply") or (existing_event.get("snapshot") or {}).get("reply", "控制请求已处理。")
                        action = existing_event.get("action")
                        return ProcessOutcome(
                            reply=saved_reply,
                            control_event_created=False,
                            control_action=action,
                            draft_cancelled=(existing_event.get("event_type") == "draft_cancel_event"),
                            request_id=request_id,
                            history_snapshot_required=False,
                            control_event_data=existing_event,
                            is_retry=True,
                        )
                    else:
                        raise ControlAuditConflictError(
                            f"Control audit event for request_id={request_id!r} session={session_id!r} "
                            f"already exists with conflicting fingerprint"
                        )

                # 步骤 5：导出 pre-state
                before_state = self._export_runtime_state()
                pre_control_state = before_state["control_state"]
                pre_phase = before_state["phase"]

                try:
                    # 步骤 6：执行状态机（在 per-request 锁内）
                    reply = self.process(user_message, request_id=request_id)

                    # 步骤 7：构造 ProcessOutcome
                    is_control_transition = (self.control_state != pre_control_state)
                    is_draft_cancellation = (
                        pre_phase in ("collecting", "confirming", "blocked_soft", "blocked_hard")
                        and self.phase == "rejected"
                    )
                    requires_control_audit = is_control_transition or is_draft_cancellation

                    # P0 修复：主历史快照只在"普通 done 且无控制事件"时写入。
                    # 控制事件文件已包含完整 snapshot，是控制请求的单一权威提交。
                    # 控制事件成功 + 主历史失败 不再形成半提交。
                    requires_history_snapshot = (
                        self.phase == "done"
                        and not requires_control_audit
                    )

                    control_action = self.last_control_request.get("action") if isinstance(self.last_control_request, dict) else None

                    outcome = ProcessOutcome(
                        reply=reply,
                        control_event_created=requires_control_audit,
                        control_action=control_action,
                        draft_cancelled=is_draft_cancellation,
                        request_id=request_id,
                        history_snapshot_required=requires_history_snapshot,
                    )

                    # 步骤 8：persist_callback（在 per-request 锁和 session 锁内）
                    if persist_callback:
                        persist_callback(self, outcome, before_state)

                    return outcome
                except Exception:
                    # 步骤 9：任何异常（含持久化失败）→ 回滚 Manager 内存状态
                    self._restore_runtime_state(before_state)
                    raise
        finally:
            try:
                _fcntl.flock(req_lock.fileno(), _fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                req_lock.close()
            except Exception:
                pass




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
        elif query_intent == "GENERAL_CHAT":
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

    def _handle_knowledge_query(self, user_message: str, route: IntentRouteResult) -> str:
        context = {
            "task_type_key": self.task_state.get("task_type_key"),
            "equipment_type": self.task_state.get("equipment_type") or self.task_state.get("equipment_name"),
        }
        kb_evidence = self.kb.execute_typed_query(route.query_intent, user_message, context=context)
        if not kb_evidence.get("found"):
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
        reply = self.llm.chat(messages, temperature=0.1)
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
            return "当前知识库未提供该信息。"
        return self.llm.filter_reply(reply)

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
        reply = self.llm.chat(messages, temperature=0.1)
        if not reply or not reply.strip():
            return f"当前任务处于【{self.phase}】阶段，已收集 {len(self._last_built_json)} 个字段。"
        return self.llm.filter_reply(reply)

    def _handle_general_chat(self, user_message: str, route: IntentRouteResult) -> str:
        messages = build_general_chat_messages(self.conversation_history, user_message)
        reply = self.llm.chat(messages, temperature=0.7)
        if not reply or not reply.strip():
            reply = "您好！我是水下多智能体任务决策大模型。请问有什么可以帮您的？"
        return self.llm.filter_reply(reply)

    def _handle_unknown_intent(self, user_message: str, route: IntentRouteResult) -> str:
        return "对不起，我没有完全理解您的意思。请问您是要新建水下任务、修改任务参数，还是查询设备工具与系统功能？"

    def _has_active_task(self) -> bool:
        """判定当前会话是否存在活动任务或有效任务草稿（严格依据阶段与任务数据）。"""
        if self.phase in ("done", "confirming", "blocked_soft", "blocked_hard"):
            return True
        if self.phase == "rejected":
            return False

        valid_slots = [
            s for k, s in self.slot_store.slots.items()
            if getattr(s, "status", None) in ("valid", "pending_confirmation") and getattr(s, "value", None) is not None
        ]
        if valid_slots:
            return True

        if any(k != "intent_id" and v is not None for k, v in self.task_state.items()):
            return True

        if any(k not in ("intent_id", "session_id") and v is not None for k, v in self._last_built_json.items()):
            return True

        return False

    def _clear_draft_state_preserving_history(self) -> None:
        """清空未发布任务草稿数据，但完整保留对话历史。"""
        self.slot_store = SlotStore(self.kb)
        self.task_state = {}
        self._last_built_json = {}
        self._last_missing = []
        self._blocking_violations = []
        self._soft_whitelist = set()
        self._pending_rov_candidates = []
        self._hard_refusal_counts = {}
        self.final_result = None
        self.awaiting_final_confirm = False
        self.task_start_now = False
        self.control_state = "idle"
        self.last_control_request = None

    def _handle_emergency_intervention(
        self, user_message: str, route: IntentRouteResult, request_id: str = "req_default"
    ) -> str:
        action = route.emergency_action
        if not action or action not in ("stop", "pause", "abort", "cancel"):
            self._switch_dialogue_mode(
                "uncertain",
                source=route.source,
                confidence=route.confidence,
                reason="紧急动作不明确，请求用户澄清",
            )
            reply = "请明确您是要停止当前任务、暂停当前任务，还是取消未发布任务草稿。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # ── 1. 无活动任务或草稿时的 fail-closed 拦截 ──
        if not self._has_active_task():
            self.control_state = "idle"
            self.last_control_request = None
            self._switch_dialogue_mode(
                "emergency_intervention",
                source=route.source,
                confidence=route.confidence,
                reason=route.reason,
            )
            if action == "cancel":
                reply = "当前没有可取消的未发布任务。"
            else:
                reply = "当前没有活动任务或可取消的任务草稿，无法执行控制指令。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # ── 2. 未发布草稿阶段的 cancel 处理 ──
        if action == "cancel" and self.phase in DRAFT_PHASES:
            self._clear_draft_state_preserving_history()
            self.phase = "rejected"
            self.control_state = "idle"
            self.last_control_request = None
            self._switch_dialogue_mode(
                "emergency_intervention",
                source=route.source,
                confidence=route.confidence,
                reason=route.reason,
            )
            reply = "当前未发布任务草稿已取消。如需重新规划，请重新开始。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # ── 3. 已处于 rejected 阶段的 cancel 处理 ──
        if action == "cancel" and self.phase == "rejected":
            self.control_state = "idle"
            self.last_control_request = None
            self._switch_dialogue_mode(
                "emergency_intervention",
                source=route.source,
                confidence=route.confidence,
                reason=route.reason,
            )
            reply = "当前没有可取消的未发布任务。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # ── 4. 已发布阶段 (phase == "done") 的 cancel 以及所有阶段的 pause, stop, abort ──
        initial_version = self.slot_store.version
        initial_snapshot = copy.deepcopy(self.slot_store.export_snapshot())
        initial_unresolved = list(self.slot_store.unresolved)
        initial_task_state = copy.deepcopy(self.task_state)
        initial_built_json = copy.deepcopy(self._last_built_json)
        initial_missing = copy.deepcopy(self._last_missing)
        initial_phase = self.phase
        initial_mode = self.mode
        initial_rov_candidates = copy.deepcopy(self._pending_rov_candidates)
        initial_soft_whitelist = copy.deepcopy(self._soft_whitelist)
        initial_blocking_violations = copy.deepcopy(self._blocking_violations)

        tz_now = get_current_datetime()
        if tz_now.tzinfo is None:
            tz_now = tz_now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        iso_requested_at = tz_now.isoformat()

        task_id_val = self.task_state.get("task_id") or (
            self.final_result.get("task_id") if isinstance(self.final_result, dict) else None
        )
        intent_id_val = self.task_state.get("intent_id") or (
            self.final_result.get("intent_id") if isinstance(self.final_result, dict) else None
        )

        self.control_state = f"{action}_requested"
        self.last_control_request = {
            "action": action,
            "status": "requested",
            "source": route.source,
            "confidence": float(route.confidence),
            "reason": route.reason,
            "request_id": request_id,
            "requested_at": iso_requested_at,
            "phase_at_request": self.phase,
            "task_id": str(task_id_val) if task_id_val else None,
            "intent_id": str(intent_id_val) if intent_id_val else None,
        }
        self._switch_dialogue_mode(
            "emergency_intervention",
            source=route.source,
            confidence=route.confidence,
            reason=route.reason,
        )

        if action == "pause":
            reply = "已识别暂停请求。当前系统尚未接入实际机器人执行控制接口，因此尚未确认设备已实际暂停。"
        elif action == "stop":
            reply = "已识别停止请求。当前系统尚未接入实际机器人执行控制接口，因此尚未确认设备已实际停止。"
        elif action == "abort":
            reply = "已识别终止请求。当前系统尚未接入实际机器人执行控制接口，因此尚未确认设备已实际终止。"
        elif action == "cancel":
            reply = "已识别取消请求。当前系统尚未接入实际机器人执行控制接口，因此尚未确认设备已实际取消。"
        else:
            reply = "已记录紧急控制指令。"

        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})

        # 状态不变性校验 (彻底防范数据污染或静默 wipe)
        v_ok = (self.slot_store.version == initial_version)
        s_ok = (self.slot_store.export_snapshot() == initial_snapshot)
        u_ok = (self.slot_store.unresolved == initial_unresolved)
        t_ok = (self.task_state == initial_task_state)
        b_ok = (self._last_built_json == initial_built_json)
        m_ok = (self._last_missing == initial_missing)
        p_ok = (self.phase == initial_phase)
        mo_ok = (self.mode == initial_mode)
        r_ok = (self._pending_rov_candidates == initial_rov_candidates)
        w_ok = (self._soft_whitelist == initial_soft_whitelist)
        bv_ok = (self._blocking_violations == initial_blocking_violations)

        if not (v_ok and s_ok and u_ok and t_ok and b_ok and m_ok and p_ok and mo_ok and r_ok and w_ok and bv_ok):
            logger.critical(
                f"[CRITICAL] State invariance violation in emergency_intervention action '{action}'!"
            )
            raise RuntimeError(f"State invariance violation in emergency_intervention action {action}")

        return reply

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

    def _handle_soft_warning_confirmation(self, user_message: str, request_id: str) -> str:
        """blocked_soft 阶段的确认/忽略处理。

        将已确认忽略的软警告加入白名单，清除 _blocking_violations，
        然后根据缺失槽位决定进入 collecting 或 confirming。
        不触碰 slot_store 或 extractor。
        """
        # 加入白名单
        if self._blocking_violations:
            for v in self._blocking_violations:
                for f in v.related_fields:
                    val = self.task_state.get(f)
                    if val is not None:
                        self._soft_whitelist.add((f, str(val), v.constraint_id))
            self._blocking_violations = []

        # 重新检查约束（使用白名单过滤后的结果）
        all_violations = self.validator.validate(self.task_state)
        remaining_soft = [v for v in all_violations
                          if v.severity == "soft" and not self._is_whitelisted(v)]
        remaining_hard = [v for v in all_violations if v.severity == "hard"]

        if remaining_hard:
            self.phase = "blocked_hard"
            self._blocking_violations = remaining_hard
        elif remaining_soft:
            self.phase = "blocked_soft"
            self._blocking_violations = remaining_soft
        else:
            # 检查是否有缺失槽位
            task_type_key = self.task_state.get("task_type_key")
            if task_type_key:
                req_schema = self.builder.get_schema(task_type_key, self.mode)
                missing = self.slot_store.get_missing_slots(req_schema)
                self._last_missing = missing
                if not missing:
                    self.phase = "confirming"
                else:
                    self.phase = "collecting"
            else:
                self.phase = "collecting"

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
        reply = self.llm.chat(messages, temperature=0.7, max_tokens=1500)
        reply = self.llm.filter_reply(reply)
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
        prev_pending_rov = copy.deepcopy(self._pending_rov_candidates)
        prev_blocking_violations = copy.deepcopy(self._blocking_violations)
        prev_hist = list(self.conversation_history)
        prev_task_start_now = self.task_start_now

        task_type_key = self.task_state.get("task_type_key")
        cand_state = copy.deepcopy(self.task_state)
        cand_built = copy.deepcopy(self._last_built_json)

        # 最终约束全量检查
        all_violations = self.validator.validate(cand_state)
        has_hard = self.validator.has_hard_violations(all_violations)
        unwhitelisted_soft = [v for v in all_violations if v.severity == "soft" and not self._is_whitelisted(v)]

        # 检查缺失
        if task_type_key:
            req_schema = self.builder.get_schema(task_type_key, self.mode)
            missing = self.slot_store.get_missing_slots(req_schema)
        else:
            missing = [{"key": "task_type", "label": "任务类型"}]

        if missing or has_hard or unwhitelisted_soft:
            if has_hard:
                self.phase = "blocked_hard"
                self._blocking_violations = [v for v in all_violations if v.severity == "hard"]
            elif unwhitelisted_soft:
                self.phase = "blocked_soft"
                self._blocking_violations = unwhitelisted_soft
            else:
                self.phase = "collecting"
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

        # 准备发布
        ti_builder = TaskIntentBuilder(self.kb)
        ti_json_artifact = ti_builder.prepare(
            task_state=cand_state,
            built_json=cand_built,
            mode=self.mode,
            task_type_key=task_type_key,
            intent_id=intent_id,
        )
        staging_file = ti_builder.create_staging(ti_json_artifact)

        try:
            ti_builder.publish_staging(staging_file, ti_json_artifact)
        except Exception as exc:
            # 回滚：保持原有回滚和错误处理
            self.phase = prev_phase
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
            self._blocking_violations = prev_blocking_violations
            self.conversation_history = prev_hist
            self.task_start_now = prev_task_start_now

            if task_type_key:
                required_schema = self.builder.get_schema(task_type_key, self.mode)
                self._last_missing = self.slot_store.get_missing_slots(required_schema)

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
        self.phase = "done"
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

    def _process_internal(self, user_message: str, request_id: str = "req_default") -> str:
        old_phase = self.phase

        if self._is_business_identity_query(user_message):
            reply = "我是一个专业的水下多智能体任务决策大模型，可用于辅助水下任务规划、参数收集与可行性验证。请描述您的水下任务需求，我会继续帮您完善任务参数。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        if is_standalone_time_query(user_message):
            reply = get_time_context().user_reply
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        if self.phase == "done" and self._is_confirmation_only(user_message):
            intent_id = self.task_state.get("intent_id") or self._last_built_json.get("intent_id")
            intent_detail = f"（intent_id: {intent_id}）" if intent_id else ""
            reply = f"任务已发布成功{intent_detail}，无需重复发布。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        pending_reply = self._resolve_pending_oilfield_confirmation(user_message, request_id=request_id)
        pending_reply = self._resolve_pending_oilfield_confirmation(user_message, request_id=request_id)
        if pending_reply is not None:
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": pending_reply})
            return pending_reply

        # 控制动作由 DialogueManager 当前阶段处理，不再交给 IntentRouter 判断。
        if self.phase == "blocked_hard" and (
            self._is_confirmation_only(user_message)
            or self._is_soft_warning_acknowledgement(user_message)
        ):
            return self._reject_hard_constraint_bypass(user_message)

        if self.phase in ("blocked_soft", "confirming") and (
            self._is_confirmation_only(user_message)
            or (
                self.phase == "blocked_soft"
                and self._is_soft_warning_acknowledgement(user_message)
            )
        ):
            return self._handle_task_confirm(user_message, request_id)


        # ── 三级独立意图路由分流阶段 ──
        expected_slots = [
            m["key"]
            for m in self._last_missing
            if isinstance(m, dict) and "key" in m
        ]
        route = self.intent_router.route(
            user_message=user_message,
            conversation_history=self.conversation_history,
            task_state=self.task_state,
            phase=self.phase,
            expected_slots=expected_slots,
        )

        # 1. 紧急干预线路拦截 (在 SlotStore 抽取与写入之前拦截)
        if route.dialogue_mode == "emergency_intervention":
            return self._handle_emergency_intervention(user_message, route, request_id)

        # 2. 知识问答线路 (只读，不修改 SlotStore 与任务状态)
        if route.dialogue_mode == "knowledge_qa":
            self._switch_dialogue_mode(
                "knowledge_qa",
                source=route.source,
                confidence=route.confidence,
                reason=route.reason,
            )
            return self._handle_non_task_route(user_message, route, request_id)

        # 3. 安全澄清状态 (uncertain)
        if route.dialogue_mode == "uncertain":
            self._switch_dialogue_mode(
                "uncertain",
                source=route.source,
                confidence=route.confidence,
                reason=route.reason,
            )
            if route.reason and ("紧急" in route.reason or "动作" in route.reason):
                reply = "请明确您是要停止当前任务、暂停当前任务，还是取消未发布任务草稿。"
            else:
                reply = "对不起，我没有完全理解您的需求。请问您是希望【创建/修改任务】，还是想【查询系统与设备知识】？"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # 4. 任务收集线路 (task_collection)
        self._switch_dialogue_mode(
            "task_collection",
            source=route.source,
            confidence=route.confidence,
            reason=route.reason,
        )

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
        new_slots, new_unresolved, expected_version = self.slot_store.snapshot()

        task_type_key = new_slots.get("task_type_key").value if new_slots.get("task_type_key") else None
        had_task_type_key_at_turn_start = task_type_key is not None
        current_state = self.slot_store.get_task_state()
        state_before_turn = dict(current_state)

        merged_updates = {}
        merged_updates_meta = {}

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
            reply = "我判断您可能是在提交任务信息，但本轮没有提取到可写入的合法字段。请换一种方式明确说明要创建的任务或要修改的参数。"
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
                
            self._apply_updates_in_transaction(stage1_updates, new_slots)
            record_unresolved(extraction_res)

            task_type_key = new_slots.get("task_type_key").value if new_slots.get("task_type_key") else None

        should_extract_task_parameters = (
            bool(task_type_key)
            and (
                had_task_type_key_at_turn_start
                or self._message_may_contain_task_parameters(user_message)
            )
        )

        if should_extract_task_parameters:
            # Stage 2: Extract task parameters
            current_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.status == "valid" and s.value is not None}
            required = self.builder.get_required(task_type_key, self.mode, current_state)
            extraction_res = self.extractor.extract_updates(
                user_message, current_state,
                task_type_key=task_type_key,
                task_type_map=self.kb.get_task_type_map(),
                required=required,
                ROV2type=self.kb.ROV2type,
                conversation_history=self.conversation_history,
            )

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

            raw_stage2 = self._merge_coordinate_updates(user_message, {k: v.get("value") if isinstance(v, dict) else v for k, v in stage2_updates.items()}, required)
            for k, v in raw_stage2.items():
                if k not in stage2_updates:
                    c_info = {"value": v, "raw_value": user_message, "confidence": 1.0, "source": "rule_parser"}
                    stage2_updates[k] = c_info
                    merged_updates_meta[k] = c_info
                merged_updates[k] = v

            raw_linked = self._link_oilfield_update_in_transaction({k: v.get("value") if isinstance(v, dict) else v for k, v in stage2_updates.items()}, new_slots)
            for k, v in raw_linked.items():
                if k.startswith("__"):
                    continue
                c_info = {"value": v, "raw_value": str(v), "confidence": 1.0, "source": "entity_linker"}
                stage2_updates[k] = c_info
                merged_updates_meta[k] = c_info
                merged_updates[k] = v

            _has_conflict = any(s.status == "conflict" for s in new_slots.values())
            if not stage2_updates and not _has_conflict and not turn_unresolved:
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
                            elif is_cancel_k:
                                slot.status = "valid"
                                slot.candidate_value = None
                                slot.validation_error = None

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
        curr_task_type_key = new_slots.get("task_type_key").value if (new_slots.get("task_type_key") and new_slots.get("task_type_key").status == "valid") else None
        self._normalize_and_validate_in_transaction(new_slots, curr_task_type_key)

        # Auto-generate task_id inside new_slots BEFORE commit
        if curr_task_type_key:
            task_id_slot = new_slots.get("task_id")
            if not task_id_slot or task_id_slot.status != "valid" or task_id_slot.value is None:
                valid_cand_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
                tid = self.builder._generate_task_id(curr_task_type_key, valid_cand_state)
                if "task_id" not in new_slots:
                    new_slots["task_id"] = Slot("task_id")

        proposed_whitelist = {item for item in self._soft_whitelist if item[0] not in changed_fields}

        # Normalize and validate inside transaction working dict new_slots
        curr_task_type_key = new_slots.get("task_type_key").value if (new_slots.get("task_type_key") and new_slots.get("task_type_key").status == "valid") else None
        self._normalize_and_validate_in_transaction(new_slots, curr_task_type_key)

        # Auto-generate task_id inside new_slots BEFORE commit
        if curr_task_type_key:
            task_id_slot = new_slots.get("task_id")
            if not task_id_slot or task_id_slot.status != "valid" or task_id_slot.value is None:
                valid_cand_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
                tid = self.builder._generate_task_id(curr_task_type_key, valid_cand_state)
                if "task_id" not in new_slots:
                    new_slots["task_id"] = Slot("task_id")
                new_slots["task_id"].value = tid
                new_slots["task_id"].status = "valid"
                new_slots["task_id"].source = "auto"

        proposed_phase = self.phase

        # Check required missing in working new_slots
        if curr_task_type_key:
            req_schema = self.builder.get_schema(curr_task_type_key, proposed_mode)
            cand_missing = [f for f in req_schema if f.get("type") not in ("auto", "fixed") and (not new_slots.get(f["key"]) or new_slots[f["key"]].status != "valid" or new_slots[f["key"]].value is None)]
        else:
            cand_missing = [{"key": "task_type", "label": "任务类型", "type": "string", "allowed_values": self.kb.get_all_task_type_values()}]

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
        self.phase = proposed_phase
        self._soft_whitelist = proposed_whitelist
        self._pending_rov_candidates = proposed_pending_rov

        # Re-derive from slot_store (SSOT)
        self.task_state = self.slot_store.get_task_state()
        if curr_task_type_key:
            required_schema = self.builder.get_schema(curr_task_type_key, self.mode)
            built = self.slot_store.get_built_json()
            missing = self.slot_store.get_missing_slots(
                required_schema,
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
            self.phase = "collecting"
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
                    self.phase = "collecting"
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
        reply = self.llm.chat(messages, temperature=0.7, max_tokens=1500)
        reply = self.llm.filter_reply(reply)
        reply = self.llm.filter_reply(reply)
        reply = self._ensure_constraint_details(reply, constraint_context)

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
        for key, item in updates.items():
            if isinstance(item, dict) and "value" in item:
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

        task_type_slot = new_slots.get("task_type_key")
        task_type_key = task_type_slot.value if task_type_slot else None
        if task_type_key:
            current_state = {
                key: slot.value
                for key, slot in new_slots.items()
                if slot.value is not None
            }
            equipment_keys = {
                "equipment_family",
                "equipment_type",
                "equipment_name",
                "equipment_unit_id",
            }
            non_eq_updates = {k: v for k, v in updates.items() if k not in equipment_keys}
            norm_non_eq = self.normalizer.normalize_updates(
                non_eq_updates,
                self.builder.get_schema(task_type_key, self.mode),
                current_state,
                lambda field_def, state: self.builder._resolve_allowed(
                    field_def,
                    task_type_key,
                    state,
                ),
            )
            eq_updates = {k: v for k, v in updates.items() if k in equipment_keys}
            updates = {**norm_non_eq, **eq_updates}
        else:
            equipment_keys = {
                "equipment_family",
                "equipment_type",
                "equipment_name",
                "equipment_unit_id",
            }

        skip = {
            "emergency_mode",
            "rov_description",
            "__clear_oilfield_name",
            "__clear_pending_oilfield",
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

        if updates.get("emergency_mode"):
            if "emergency_mode" in new_slots:
                new_slots["emergency_mode"].value = True
                new_slots["emergency_mode"].status = "valid"

        self._handle_equipment_updates_in_transaction(
            updates,
            new_slots,
            allow_overwrite,
        )
        for key in (
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
        ):
            if allow_overwrite:
                slot.value = value
                slot.status = "candidate"
                slot.candidate_value = None
                slot.raw_value = str(value)
                slot.validation_error = None
            else:
                slot.status = "conflict"
                slot.candidate_value = value
                slot.raw_value = str(value)
                slot.validation_error = (
                    f"Conflict: existing value '{slot.value}' vs new value '{value}'"
                )
            return

        if slot is None:
            new_slots[key] = Slot(
                slot_name=key,
                value=value,
                status="candidate",
            )
            return

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
        """统一处理机器人系列、型号、设备全称和单机编号的父子联动。"""
        equipment_updates = {}
        for key in (
            "equipment_family",
            "equipment_type",
            "equipment_name",
            "equipment_unit_id",
        ):
            val = updates.get(key)
            if isinstance(val, dict):
                val = val.get("value")
            if val not in (None, ""):
                equipment_updates[key] = val

        if not equipment_updates:
            return


        task_type = (
            new_slots.get("task_type_key").value
            if new_slots.get("task_type_key")
            else None
        )

        def clear_slots(keys: tuple[str, ...]) -> None:
            for key in keys:
                slot = new_slots.get(key)
                if slot is None:
                    continue
                slot.value = None
                slot.status = "missing"
                slot.candidate_value = None
                slot.raw_value = None
                slot.validation_error = None

        family_update = equipment_updates.get("equipment_family")
        family_slot = new_slots.get("equipment_family")
        current_family_id = (
            self.kb.resolve_robot_family_id(str(family_slot.value), task_type)
            if family_slot and family_slot.value is not None
            else None
        )
        resolved_family = (
            self.kb.resolve_robot_family(str(family_update), task_type)
            if family_update
            else None
        )
        requested_family_id = (
            resolved_family.get("family_id") if resolved_family else None
        )

        if (
            family_update
            and allow_overwrite
            and requested_family_id
            and current_family_id != requested_family_id
        ):
            clear_slots(
                ("equipment_type", "equipment_name", "equipment_unit_id")
            )

        if family_update:
            self._apply_slot_update_in_transaction(
                "equipment_family",
                (
                    resolved_family.get("full_name", family_update)
                    if resolved_family
                    else family_update
                ),
                new_slots,
                allow_overwrite,
            )

        # 型号只接受 model_variants 层的标准名称、ID 或 aliases。
        # equipment_name 属于 fleet_units 层，不能再作为型号选择器。
        variant_update = equipment_updates.get("equipment_type")
        selected_family = (
            resolved_family.get("full_name")
            if resolved_family
            else (
                family_slot.value
                if family_slot and family_slot.value is not None
                else None
            )
        )
        selected_variant = None
        if variant_update:
            selected_variant = self.kb.get_rov_for_task(
                str(variant_update),
                task_type,
                str(selected_family) if selected_family else None,
            )
            if not selected_variant and selected_family:
                selected_variant = self.kb.get_rov_for_task(
                    str(variant_update),
                    task_type,
                    None,
                )

            canonical_variant = (
                selected_variant.get("full_name")
                if selected_variant
                else variant_update
            )

            old_type_slot = new_slots.get("equipment_type")
            old_variant = (
                self.kb.get_rov(str(old_type_slot.value))
                if old_type_slot and old_type_slot.value
                else None
            )
            if (
                allow_overwrite
                and selected_variant
                and old_variant
                and old_variant.get("variant_id")
                != selected_variant.get("variant_id")
            ):
                clear_slots(("equipment_unit_id", "equipment_name"))

            self._apply_slot_update_in_transaction(
                "equipment_type",
                canonical_variant,
                new_slots,
                allow_overwrite,
            )

            type_slot = new_slots.get("equipment_type")
            if (
                selected_variant
                and type_slot
                and type_slot.status != "conflict"
                and not family_update
            ):
                derived_family = selected_variant.get("family_full_name")
                if derived_family:
                    self._apply_slot_update_in_transaction(
                        "equipment_family",
                        derived_family,
                        new_slots,
                        allow_overwrite,
                    )

        def _unwrap(v):
            return v.get("value") if isinstance(v, dict) else v

        unit_update = (
            _unwrap(equipment_updates.get("equipment_unit_id"))
            or _unwrap(equipment_updates.get("equipment_name"))
        )
        if unit_update:

            variant_slot = new_slots.get("equipment_type")
            variant_context = (
                selected_variant.get("full_name")
                if selected_variant
                else (
                    variant_slot.value
                    if variant_slot and variant_slot.value is not None
                    else None
                )
            )
            resolved_unit = self.kb.resolve_robot_unit(
                str(unit_update),
                task_type,
                str(variant_context) if variant_context else None,
            )
            if not resolved_unit and allow_overwrite:
                resolved_unit = self.kb.resolve_robot_unit(
                    str(unit_update),
                    task_type,
                )
            if not resolved_unit and allow_overwrite:
                clear_slots(("equipment_name",))

            canonical_unit_id = (
                resolved_unit.get("unit_id") if resolved_unit else unit_update
            )

            if resolved_unit:
                unit_variant = resolved_unit["robot"]

                eq_type_slot = new_slots.get("equipment_type") or Slot("equipment_type")
                eq_type_slot.value = unit_variant.get("full_name")
                eq_type_slot.status = "valid"
                eq_type_slot.candidate_value = None
                eq_type_slot.raw_value = str(unit_update)
                eq_type_slot.validation_error = None
                new_slots["equipment_type"] = eq_type_slot

                eq_fam_slot = new_slots.get("equipment_family") or Slot("equipment_family")
                eq_fam_slot.value = unit_variant.get("family_full_name")
                eq_fam_slot.status = "valid"
                eq_fam_slot.candidate_value = None
                eq_fam_slot.raw_value = str(unit_update)
                eq_fam_slot.validation_error = None
                new_slots["equipment_family"] = eq_fam_slot

                eq_unit_slot = new_slots.get("equipment_unit_id") or Slot("equipment_unit_id")
                eq_unit_slot.value = resolved_unit.get("unit_id")
                eq_unit_slot.status = "valid"
                eq_unit_slot.candidate_value = None
                eq_unit_slot.raw_value = str(unit_update)
                eq_unit_slot.validation_error = None
                new_slots["equipment_unit_id"] = eq_unit_slot

                name_slot = new_slots.get("equipment_name") or Slot("equipment_name")
                name_slot.value = resolved_unit.get("display_name")
                name_slot.status = "valid"
                name_slot.candidate_value = None
                name_slot.raw_value = str(unit_update)
                name_slot.validation_error = None
                new_slots["equipment_name"] = name_slot
            else:
                self._apply_slot_update_in_transaction(
                    "equipment_unit_id",
                    canonical_unit_id,
                    new_slots,
                    allow_overwrite,
                )


    def _handle_task_type_update_in_transaction(self, key: str, value: str, new_slots: dict):
        task_type_map = self.kb.get_task_type_map()
        templates = self.kb.task_schemas.get("task_templates", {})

        target_key = None
        if value in task_type_map:
            new_slots["task_type"].value = value
            new_slots["task_type"].status = "valid"
            target_key = task_type_map[value]
            new_slots["task_type_key"].value = target_key
            new_slots["task_type_key"].status = "valid"
        elif key == "task_type_key" and value in templates:
            target_key = value
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

    def _normalize_and_validate_in_transaction(self, new_slots: dict, task_type_key: str | None):
        if not task_type_key:
            return

        schema = self.builder.get_schema(task_type_key, self.mode)

        for field_def in schema:
            key = field_def["key"]
            ftype = field_def["type"]
            slot = new_slots.get(key)
            if not slot or slot.status in ("fixed", "auto", "conflict"):
                continue

            target_val = slot.candidate_value if slot.candidate_value is not None else slot.value
            if target_val is None:
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

        candidate = self._top_pending_oilfield_candidate()
        if not candidate:
            return self._build_pending_oilfield_reply()

        confirmed_name = candidate.get("name")
        self._commit_internal_slot_values(
            {
                "oilfield_name": confirmed_name,
                "oilfield_entity_id": candidate.get("id"),
                "oilfield_match_status": "confirmed",
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

    def _top_pending_oilfield_candidate(self) -> dict | None:
        cand_slot = self.slot_store.slots.get("pending_oilfield_candidates")
        candidates = cand_slot.value if (cand_slot and cand_slot.status == "valid") else None
        if isinstance(candidates, list) and candidates:
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

    def _run_constraint_check(self, changed_fields: set[str]) -> dict:
        """执行约束检查，返回上下文"""
        if not changed_fields and self.phase not in ("blocked_hard", "blocked_soft"):
            return {"type": "none", "violations": [], "hard_refusal_counts": {}}

        if self.phase in ("blocked_hard", "blocked_soft"):
            new_violations = self.validator.validate(self.task_state)
        else:
            new_violations = self.validator.validate_for_fields(self.task_state, changed_fields)

        # 处理soft阻塞解除/升级为hard
        if self.phase == "blocked_soft":
            current_soft = [v for v in new_violations
                            if v.severity == "soft" and not self._is_whitelisted(v)]
            if not current_soft:
                self._blocking_violations = []
                self.phase = "collecting"
                current_hard = [v for v in new_violations if v.severity == "hard"]
                if current_hard:
                    self.phase = "blocked_hard"
                    self._blocking_violations = current_hard
                    return {"type": "hard", "violations": current_hard, "hard_refusal_counts": {}}
                return {"type": "none", "violations": [], "hard_refusal_counts": {}}
            else:
                self._blocking_violations = current_soft
                return {"type": "soft", "violations": current_soft, "hard_refusal_counts": {}}

        # 处理hard阻塞解除
        if self.phase == "blocked_hard":
            current_hard = [v for v in new_violations if v.severity == "hard"]
            if current_hard:
                self._blocking_violations = current_hard
                for v in current_hard:
                    self._hard_refusal_counts[v.constraint_id] = \
                        self._hard_refusal_counts.get(v.constraint_id, 0) + 1
                final_ids = {cid for cid, cnt in self._hard_refusal_counts.items()
                             if cnt >= HARD_REFUSAL_LIMIT}
                if final_ids:
                    self.phase = "rejected"
                    self._blocking_violations = []
                    return {"type": "hard_rejected", "violations": current_hard,
                            "hard_refusal_counts": dict(self._hard_refusal_counts)}
                warn_ids = {cid for cid, cnt in self._hard_refusal_counts.items()
                            if cnt == HARD_REFUSAL_LIMIT - 1}
                ctx_type = "hard_final_warning" if warn_ids else "hard"
                return {"type": ctx_type, "violations": current_hard,
                        "hard_refusal_counts": dict(self._hard_refusal_counts)}
            else:
                # 硬约束解除，清除计数
                self._blocking_violations = []
                self.phase = "collecting"
                resolved_ids = set(self._hard_refusal_counts.keys()) - {v.constraint_id for v in new_violations if
                                                                        v.severity == "hard"}
                for cid in resolved_ids:
                    self._hard_refusal_counts.pop(cid, None)

                # 硬解除后检查软约束
                current_soft = [v for v in new_violations
                                if v.severity == "soft" and not self._is_whitelisted(v)]
                if current_soft:
                    self.phase = "blocked_soft"
                    self._blocking_violations = current_soft
                    return {"type": "soft", "violations": current_soft,
                            "hard_refusal_counts": {}}
                return {"type": "none", "violations": [], "hard_refusal_counts": {}}

        # collecting / confirming 状态下的新违规
        if self.phase in ("collecting", "confirming"):
            hard_new = [v for v in new_violations if v.severity == "hard"]
            soft_new = [v for v in new_violations
                        if v.severity == "soft" and not self._is_whitelisted(v)]

            if hard_new:
                self.phase = "blocked_hard"
                self._blocking_violations = hard_new
                for v in hard_new:
                    if v.constraint_id not in self._hard_refusal_counts:
                        self._hard_refusal_counts[v.constraint_id] = 0
                return {"type": "hard", "violations": hard_new,
                        "hard_refusal_counts": dict(self._hard_refusal_counts)}
            if soft_new:
                self.phase = "blocked_soft"
                self._blocking_violations = soft_new
                return {"type": "soft", "violations": soft_new, "hard_refusal_counts": {}}

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
        return any(
            (f, str(self.task_state.get(f)), v.constraint_id) in self._soft_whitelist
            for f in v.related_fields
        )

    @staticmethod
    def _is_business_identity_query(message: str) -> bool:
        text = message.strip().lower()
        identity_patterns = (
            "你是什么", "你是谁", "你是啥", "你的身份", "你叫什么",
            "介绍一下你自己", "自我介绍", "what are you", "who are you",
        )
        return any(pattern in text for pattern in identity_patterns)


    @staticmethod
    def _user_confirmed(message: str) -> bool:
        keywords = ["确认", "没问题", "发布", "提交", "ok", "好的", "可以", "确定"]
        return any(kw in message.lower() for kw in keywords)

    @staticmethod
    def _is_confirmation_only(message: str) -> bool:
        """仅识别不携带参数更新的独立确认/发布指令。"""
        text = re.sub(r"[\s，,。.!！?？、；;：:]+", "", message).lower()
        return text in {
            "确认",
            "确认无误",
            "确认发布",
            "确认并发布",
            "确认发布任务",
            "确认开始",
            "开始",
            "开始任务",
            "发布",
            "发布任务",
            "立即发布",
            "现在发布",
            "提交",
            "提交任务",
            "确认提交",
            "确认并提交",
            "确定",
            "没问题",
            "好的",
            "可以",
            "ok",
        }

    @classmethod
    def _is_soft_warning_acknowledgement(cls, message: str) -> bool:
        """Only treat a standalone acknowledgement as consent to ignore soft warnings."""
        if cls._is_confirmation_only(message):
            return True

        parameter_cues = (
            "改成", "修改", "水深", "深度", "时间", "支持船", "管缆", "油田",
            "设备", "工具", "改为", "设为", "调整", "增加", "减少", "补充",
        )
        if any(cue in message for cue in parameter_cues):
            return False
        return any(
            keyword in message.lower()
            for keyword in ("忽略", "继续", "无视", "不管", "没关系")
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
            violations = [
                violation
                for violation in self.validator.validate(self.task_state)
                if violation.severity == "hard"
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
        self.dialogue_mode = "task_collection"
        self.control_state = "idle"
        self.last_control_request = None
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

    # --------------------------------------------------------------------------
    # 历史快照恢复
    # --------------------------------------------------------------------------

    def load_snapshot(self, snapshot: dict) -> None:
        """兼容恢复旧版扁平快照和 snapshot_version=2 完整快照。"""
        if not isinstance(snapshot, dict):
            raise ValueError("History snapshot must be a dictionary")

        raw_d_mode = snapshot.get("dialogue_mode")
        if raw_d_mode is None:
            dialogue_mode = "task_collection"
        elif isinstance(raw_d_mode, str) and raw_d_mode.strip() in VALID_DIALOGUE_MODES:
            dialogue_mode = raw_d_mode.strip()
        else:
            raise ValueError(f"Invalid dialogue_mode in snapshot: {raw_d_mode!r}")

        raw_c_state = snapshot.get("control_state")
        raw_last_req = snapshot.get("last_control_request")
        control_state, clean_last_req = _validate_control_snapshot(raw_c_state, raw_last_req)

        conversation_history = snapshot.get("conversation_history", [])
        if not isinstance(conversation_history, list):
            raise ValueError("conversation_history must be a list")

        mode = snapshot.get("mode", "normal")
        phase = snapshot.get("phase", "collecting")
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

        # 候选 SlotStore 完整构建后再一次性替换，避免半恢复状态泄漏。
        self.conversation_history = copy.deepcopy(conversation_history)
        self.slot_store = candidate_store
        self.task_state = self.slot_store.get_task_state()
        self.mode = mode
        self.dialogue_mode = dialogue_mode
        self.control_state = control_state
        self.last_control_request = copy.deepcopy(clean_last_req)
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
                                if _file_intent_id != intent_id_val:
                                    logger.warning("[load_snapshot] intent_id mismatch in final file")
                                elif not _ti_builder_module.validate_task_intent(_data):
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
