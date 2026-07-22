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
import re
import threading
from typing import Any
from zoneinfo import ZoneInfo
from datetime import datetime

logger = logging.getLogger(__name__)   # ✅ 新增导入

from .llm_client import LLMClient
from .knowledge_retriever import KnowledgeBase
from .extractor import ParameterExtractor, MUTATING_INTENTS
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
from .exceptions import TaskPersistenceError, TaskRollbackError, IntentIdConflict, IdReservationError
from .id_sequence import validate_intent_id
from .slot_store import SlotStore, Slot
from .intent_router import IntentRouter, IntentRouteResult

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
    def __init__(self, llm: LLMClient, kb: KnowledgeBase):
        self.llm = llm
        self.kb = kb
        self.extractor = ParameterExtractor(llm)
        # LHL 归一化器采用确定性规则，不依赖 LLM 猜测合法字段值。
        self.normalizer = FieldNormalizer()
        self.builder = OutputBuilder(kb)
        self.validator = TaskValidator(kb)
        self.oilfield_linker = OilfieldEntityLinker(kb.environment)
        # 复用同一个知识库实例，保证路由词表、歧义索引与查询数据版本一致。
        self.intent_router = IntentRouter(llm, device_terms=kb)

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

    # --------------------------------------------------------------------------
    # 主入口
    # --------------------------------------------------------------------------

    def process(self, user_message: str, request_id: str = "req_default") -> str:
        with self._session_lock:
            return self._process_internal(user_message, request_id)

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

        if route.intent in ("TOOL_QUERY", "DEVICE_CAPABILITY", "KNOWLEDGE_QA"):
            reply = self._handle_knowledge_query(user_message, route)
        elif route.intent in ("TASK_STATUS", "DEVICE_STATUS", "ENVIRONMENT_QUERY"):
            reply = self._handle_status_query(user_message, route)
        elif route.intent == "GENERAL_CHAT":
            reply = self._handle_general_chat(user_message, route)
        elif route.intent in ("CLARIFICATION", "UNKNOWN"):
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
                f"[CRITICAL] State invariance violation in non-task route '{route.intent}'! "
                f"ver_ok={v_ok}, snap_ok={s_ok}, unres_ok={u_ok}, state_ok={t_ok}, built_ok={b_ok}, miss_ok={m_ok}"
            )
            raise RuntimeError(f"State invariance violation in non-task route {route.intent}")

        return reply

    def _handle_knowledge_query(self, user_message: str, route: IntentRouteResult) -> str:
        context = {
            "task_type_key": self.task_state.get("task_type_key"),
            "equipment_type": self.task_state.get("equipment_type") or self.task_state.get("equipment_name"),
        }
        kb_evidence = self.kb.execute_typed_query(route.intent, user_message, context=context)
        if not kb_evidence.get("found"):
            return "当前知识库未提供该信息。"

        if route.intent == "DEVICE_CAPABILITY" and kb_evidence.get("query_mode") == "device_check":
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
            if route.intent == "DEVICE_CAPABILITY" and kb_evidence.get("query_mode") == "device_check":
                if all_devices_unmet:
                    dev = result_items[0]
                    dev_name = dev.get("robot_class_name") or dev.get("full_name") or "目标设备"
                    max_d = dev.get("max_depth_m")
                    target_d = kb_evidence.get("depth_condition", {}).get("depth_m")
                    return f"已识别设备【{dev_name}】，其最大作业水深为 {max_d}米，无法满足您询问的 {target_d}米 作业要求。"
            return "当前知识库未提供该信息。"
        return self.llm.filter_reply(reply)

    def _handle_status_query(self, user_message: str, route: IntentRouteResult) -> str:
        if route.intent == "TASK_STATUS":
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
            has_realtime = False
            state_dict = None
            if equipment and route.intent == "DEVICE_STATUS":
                state_dict = self.kb.get_robot_state_dict(equipment)
                if state_dict and any(v is not None for v in state_dict.values()):
                    has_realtime = True

            if has_realtime:
                status_evidence = {
                    "query_type": route.intent,
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

            if staging_file and staging_file.exists():
                try:
                    staging_file.unlink()
                except Exception:
                    pass

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
        self.final_result = cand_built
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

        pending_reply = self._resolve_pending_oilfield_confirmation(user_message, request_id=request_id)
        if pending_reply is not None:
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": pending_reply})
            return pending_reply

        # ── 独立意图路由分流阶段 ──
        expected_slots = [m["key"] for m in self._last_missing if isinstance(m, dict) and "key" in m]
        route = self.intent_router.route(
            user_message=user_message,
            conversation_history=self.conversation_history,
            task_state=self.task_state,
            phase=self.phase,
            expected_slots=expected_slots,
        )

        # ── CONTROL / QUERY / CHAT / AMBIGUOUS 在抽取流水线之前拦截 ──
        if route.interaction_type == "CONTROL":
            if route.control_action in ("CONFIRM", "CONTINUE") or route.intent == "TASK_CONFIRM":
                return self._handle_task_confirm(user_message, request_id)
            if route.control_action == "CANCEL" or route.intent == "TASK_CANCEL":
                self.phase = "rejected"
                self.final_result = None
                reply = "任务已取消。如需重新规划，请重新开始。"
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply

        if route.interaction_type != "WRITE":
            return self._handle_non_task_route(user_message, route, request_id)

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
            # IntentRouter 是意图权威；ParameterExtractor 只负责返回槽位候选。
            intent_str = route.intent

            if intent_str not in MUTATING_INTENTS:
                if intent_str == "GENERAL_CHAT":
                    messages = [
                        {"role": "system", "content": "你是一个水下多智能体任务规划系统助手。请友好专业地回答用户的问候或一般性问题。"},
                        *self.conversation_history[-6:],
                        {"role": "user", "content": user_message}
                    ]
                    reply = self.llm.generate(messages, temperature=0.7)
                    if not reply or reply.strip() in ("", "null"):
                        reply = "您好！我是水下多智能体任务决策大模型。请问有什么可以帮您的？"
                    reply = self.llm.filter_reply(reply)
                    self.conversation_history.append({"role": "user", "content": user_message})
                    self.conversation_history.append({"role": "assistant", "content": reply})
                    return reply

                # UNKNOWN or any illegal intent fails closed
                reply = "对不起，我没有完全理解您的意思。请问您是要新建水下任务、修改任务参数还是查询系统功能？"
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply

            if not extraction_res.get("slot_candidates"):
                record_unresolved(extraction_res)
                return reply_write_without_candidates()

            stage1_updates = {}
            for candidate in extraction_res.get("slot_candidates", []):
                k = candidate["canonical_key"]
                v = candidate["normalized_value"]
                cand_info = {
                    "value": v,
                    "raw_value": candidate.get("raw_value"),
                    "confidence": candidate.get("confidence", 1.0),
                    "source": "user_input"
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
            # IntentRouter 是意图权威；ParameterExtractor 只负责返回槽位候选。
            intent_str = route.intent

            if intent_str not in MUTATING_INTENTS:
                if intent_str == "GENERAL_CHAT":
                    messages = [
                        {"role": "system", "content": "你是一个水下多智能体任务规划系统助手。请友好专业地回答用户的问候或一般性问题。"},
                        *self.conversation_history[-6:],
                        {"role": "user", "content": user_message}
                    ]
                    reply = self.llm.generate(messages, temperature=0.7)
                    if not reply or reply.strip() in ("", "null"):
                        reply = "您好！我是水下多智能体任务决策大模型。请问有什么可以帮您的？"
                    reply = self.llm.filter_reply(reply)
                    self.conversation_history.append({"role": "user", "content": user_message})
                    self.conversation_history.append({"role": "assistant", "content": reply})
                    return reply

                # UNKNOWN or any illegal intent fails closed
                reply = "对不起，我没有完全理解您的意思。请问您是要新建水下任务、修改任务参数还是查询系统功能？"
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply

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
                    "source": "user_input"
                }
                stage2_updates[k] = cand_info
                merged_updates[k] = v
                merged_updates_meta[k] = cand_info

            raw_stage2 = self._merge_coordinate_updates(user_message, {k: v.get("value") if isinstance(v, dict) else v for k, v in stage2_updates.items()}, required)
            for k, v in raw_stage2.items():
                if k not in stage2_updates:
                    stage2_updates[k] = {"value": v, "raw_value": user_message, "confidence": 1.0, "source": "rule_parser"}
                merged_updates[k] = v

            raw_linked = self._link_oilfield_update_in_transaction({k: v.get("value") if isinstance(v, dict) else v for k, v in stage2_updates.items()}, new_slots)
            for k, v in raw_linked.items():
                if k not in stage2_updates:
                    stage2_updates[k] = {"value": v, "raw_value": str(v), "confidence": 1.0, "source": "entity_linker"}
                merged_updates[k] = v

            if not stage2_updates:
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
                allow_overwrite=(route.intent == "TASK_UPDATE"),
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
            built = self.slot_store.get_built_json(required_schema)
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
            user_ignore = any(kw in user_message.lower() for kw in SOFT_IGNORE_KEYWORDS)
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
        raw_name = updates.get("oilfield_name")
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
            if "oilfield_entity_id" not in new_slots:
                new_slots["oilfield_entity_id"] = Slot("oilfield_entity_id")
            new_slots["oilfield_entity_id"].value = match.entity_id
            new_slots["oilfield_entity_id"].status = "valid"
            linked["__clear_pending_oilfield"] = True
        else:
            linked.pop("oilfield_name", None)
            linked["pending_oilfield_name"] = match.raw
            linked["pending_oilfield_candidates"] = match.candidates
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
            updates = self.normalizer.normalize_updates(
                updates,
                self.builder.get_schema(task_type_key, self.mode),
                current_state,
                lambda field_def, state: self.builder._resolve_allowed(
                    field_def,
                    task_type_key,
                    state,
                ),
            )

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
        equipment_updates = {
            key: updates.get(key)
            for key in (
                "equipment_family",
                "equipment_type",
                "equipment_name",
                "equipment_unit_id",
            )
            if updates.get(key) not in (None, "")
        }
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

        # 单机编号和设备名称都只在 fleet_units 层解析。设备名称解析成功后
        # 写入标准 unit_id，并由该单机向上反推型号与系列。
        unit_update = (
            equipment_updates.get("equipment_unit_id")
            or equipment_updates.get("equipment_name")
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
            self._apply_slot_update_in_transaction(
                "equipment_unit_id",
                canonical_unit_id,
                new_slots,
                allow_overwrite,
            )

            if resolved_unit:
                unit_variant = resolved_unit["robot"]
                self._apply_slot_update_in_transaction(
                    "equipment_type",
                    unit_variant.get("full_name"),
                    new_slots,
                    allow_overwrite,
                )
                self._apply_slot_update_in_transaction(
                    "equipment_family",
                    unit_variant.get("family_full_name"),
                    new_slots,
                    allow_overwrite,
                )

                name_slot = new_slots.get("equipment_name")
                if name_slot is None:
                    name_slot = Slot("equipment_name")
                    new_slots["equipment_name"] = name_slot
                name_slot.value = resolved_unit.get("display_name")
                name_slot.status = "valid"
                name_slot.candidate_value = None
                name_slot.raw_value = str(unit_update)
                name_slot.validation_error = None

    def _handle_task_type_update_in_transaction(self, key: str, value: str, new_slots: dict):
        task_type_map = self.kb.get_task_type_map()
        templates = self.kb.task_schemas.get("task_templates", {})

        if value in task_type_map:
            new_slots["task_type"].value = value
            new_slots["task_type"].status = "valid"
            new_slots["task_type_key"].value = task_type_map[value]
            new_slots["task_type_key"].status = "valid"
            required_fields = self.builder.get_schema(task_type_map[value], self.mode)
            self.slot_store.init_task_slots(required_fields)
            for f in required_fields:
                fkey = f["key"]
                if fkey not in new_slots:
                    new_slots[fkey] = Slot(slot_name=fkey, value_type=f.get("type", "string"))
        elif key == "task_type_key" and value in templates:
            new_slots["task_type_key"].value = value
            new_slots["task_type_key"].status = "valid"
            values = templates[value].get("task_type_values", [])
            if len(values) == 1:
                new_slots["task_type"].value = values[0]
                new_slots["task_type"].status = "valid"
            required_fields = self.builder.get_schema(value, self.mode)
            self.slot_store.init_task_slots(required_fields)
            for f in required_fields:
                fkey = f["key"]
                if fkey not in new_slots:
                    new_slots[fkey] = Slot(slot_name=fkey, value_type=f.get("type", "string"))

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
            if not slot or slot.value is None or slot.status in ("fixed", "auto", "conflict"):
                continue
                
            temp_state = {k: s.value for k, s in new_slots.items() if s.value is not None}
            allowed = self.builder._resolve_allowed(field_def, task_type_key, temp_state)
            if allowed:
                raw = slot.value
                if ftype == "list":
                    normalized = self.normalizer.normalize(raw, allowed, ftype)
                else:
                    normalized = self.normalizer.normalize(str(raw), allowed, ftype)
                    
                if normalized is not None:
                    slot.value = normalized
                    slot.status = "valid"
                    slot.validation_error = None
                else:
                    slot.status = "invalid"
                    slot.validation_error = f"Value '{raw}' could not be normalized to allowed options: {allowed}"
            else:
                if ftype == "datetime":
                    val_str = str(slot.value)
                    pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$"
                    if re.match(pattern, val_str):
                        slot.status = "valid"
                        slot.validation_error = None
                    else:
                        slot.status = "invalid"
                        slot.validation_error = f"Invalid datetime format: {val_str}. Expected YYYY-MM-DDTHH:MM:SS"
                elif ftype == "coord":
                    coord = self.builder._validate_coord(slot.value)
                    if coord:
                        slot.value = coord
                        slot.status = "valid"
                        slot.validation_error = None
                    else:
                        slot.status = "invalid"
                        slot.validation_error = f"Invalid coordinate format: {slot.value}"
                elif ftype == "number":
                    num = self.builder._validate_number(slot.value)
                    if num is not None:
                        slot.value = num
                        slot.status = "valid"
                        slot.validation_error = None
                    else:
                        slot.status = "invalid"
                        slot.validation_error = f"Invalid numeric value: {slot.value}"
                else:
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
        if not self.task_state.get("pending_oilfield_name"):
            return None
        if self._user_cancelled_oilfield(user_message):
            self._commit_internal_slot_values(
                {},
                clear_keys=(
                    "pending_oilfield_name",
                    "pending_oilfield_candidates",
                    "oilfield_name",
                    "oilfield_entity_id",
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
        raw_name = self.task_state.get("pending_oilfield_name")
        if not raw_name or self.task_state.get("oilfield_name"):
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
        candidates = self.task_state.get("pending_oilfield_candidates")
        if isinstance(candidates, list) and candidates:
            candidate = candidates[0]
            if isinstance(candidate, dict) and candidate.get("name"):
                return candidate
        return None

    def _user_confirmed_oilfield(self, message: str) -> bool:
        keywords = ["是", "对", "就是", "采用", "确认", "确定", "可以", "好的", "ok"]
        return self._user_confirmed(message) or any(kw in message.lower() for kw in keywords)

    @staticmethod
    def _user_cancelled_oilfield(message: str) -> bool:
        keywords = ["不是", "不对", "否", "错了", "重新", "不要"]
        return any(kw in message for kw in keywords)

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

        # collecting状态下的新违规
        if self.phase == "collecting":
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

    @staticmethod
    def _user_cancelled(message: str) -> bool:
        keywords = ["取消", "放弃", "不要了", "终止", "退出"]
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

    def _rebuild_cache(self) -> None:
        """根据当前 task_state 重新构建 _last_built_json 和 _last_missing"""
        task_type_key = self.task_state.get("task_type_key")
        if task_type_key:
            built, missing = self.builder.build(self.task_state, task_type_key, self.mode)
            if "task_id" in built and not self.task_state.get("task_id"):
                self._commit_internal_slot_values(
                    {"task_id": built["task_id"]}
                )
            self._last_built_json = built
            self._last_missing = missing
        else:
            self._last_built_json = {}
            self._last_missing = [{
                "key": "task_type",
                "label": "任务类型",
                "type": "string",
                "allowed_values": self.kb.get_all_task_type_values()
            }]
        self.task_start_now = self.is_start_time_near_now()

    # --------------------------------------------------------------------------
    # 历史快照恢复
    # --------------------------------------------------------------------------

    def load_snapshot(self, snapshot: dict) -> None:
        """兼容恢复旧版扁平快照和 snapshot_version=2 完整快照。"""
        if not isinstance(snapshot, dict):
            raise ValueError("History snapshot must be a dictionary")

        conversation_history = snapshot.get("conversation_history", [])
        if not isinstance(conversation_history, list):
            raise ValueError("conversation_history must be a list")

        mode = snapshot.get("mode", "normal")
        phase = snapshot.get("phase", "collecting")
        slot_snapshot = snapshot.get("slot_store")
        candidate_store = None

        if snapshot.get("snapshot_version") == 2 and slot_snapshot:
            factory = getattr(SlotStore, "from_snapshot", None)
            if callable(factory):
                candidate_store = factory(slot_snapshot, self.kb)
            else:
                # 当前 LHL SlotStore 尚未合并新版恢复接口，先按同一数据结构兼容恢复。
                if not isinstance(slot_snapshot, dict):
                    raise ValueError("slot_store must be a dictionary")
                store_version = slot_snapshot.get("store_version")
                slots_data = slot_snapshot.get("slots")
                unresolved = slot_snapshot.get("unresolved")
                if (
                    not isinstance(store_version, int)
                    or isinstance(store_version, bool)
                    or store_version < 0
                    or not isinstance(slots_data, dict)
                    or not isinstance(unresolved, list)
                ):
                    raise ValueError("Invalid v2 slot_store snapshot")

                restored_slots = {}
                valid_statuses = {
                    "missing",
                    "candidate",
                    "valid",
                    "invalid",
                    "conflict",
                    "unresolved",
                }
                for key, data in slots_data.items():
                    if not isinstance(key, str) or not isinstance(data, dict):
                        raise ValueError("Invalid slot entry in v2 snapshot")
                    if data.get("slot_name", key) != key:
                        raise ValueError("Slot key does not match slot_name")
                    status = data.get("status", "missing")
                    confidence = data.get("confidence")
                    slot_version = data.get("version", 0)
                    if status not in valid_statuses:
                        raise ValueError(f"Invalid status for slot '{key}'")
                    if (
                        not isinstance(slot_version, int)
                        or isinstance(slot_version, bool)
                        or slot_version < 0
                    ):
                        raise ValueError(f"Invalid version for slot '{key}'")
                    if confidence is not None and (
                        isinstance(confidence, bool)
                        or not isinstance(confidence, (int, float))
                        or not 0.0 <= float(confidence) <= 1.0
                    ):
                        raise ValueError(f"Invalid confidence for slot '{key}'")
                    restored_slots[key] = Slot(
                        slot_name=key,
                        value=copy.deepcopy(data.get("value")),
                        value_type=data.get("value_type", "string"),
                        status=status,
                        source=data.get("source", "user_input"),
                        raw_value=copy.deepcopy(data.get("raw_value")),
                        confidence=confidence,
                        validation_error=data.get("validation_error"),
                        updated_at=data.get("updated_at"),
                        version=slot_version,
                        candidate_value=copy.deepcopy(data.get("candidate_value")),
                    )

                candidate_store = SlotStore(self.kb)
                candidate_store.commit_transaction(
                    restored_slots,
                    copy.deepcopy(unresolved),
                )
                candidate_store.version = store_version

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
                if key in new_slots:
                    new_slots[key].value = copy.deepcopy(value)
                    new_slots[key].status = "valid"
                else:
                    new_slots[key] = Slot(
                        slot_name=key,
                        value=copy.deepcopy(value),
                        status="valid",
                    )
            candidate_store.commit_transaction(new_slots, [])

        # 候选 SlotStore 完整构建后再一次性替换，避免半恢复状态泄漏。
        self.conversation_history = copy.deepcopy(conversation_history)
        self.slot_store = candidate_store
        self.task_state = self.slot_store.get_task_state()
        self.mode = mode
        self.phase = phase
        self.final_result = None
        self.task_start_now = False
        # 清空阻塞与白名单，重新构建缓存
        self._blocking_violations = []
        self._soft_whitelist = set()
        self._hard_refusal_counts = {}
        self._pending_rov_candidates = []
        self._rebuild_cache()
