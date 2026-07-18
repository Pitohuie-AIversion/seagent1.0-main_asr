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
import threading
from typing import Any
from zoneinfo import ZoneInfo
from datetime import datetime

logger = logging.getLogger(__name__)   # ✅ 新增导入

from .llm_client import LLMClient
from .knowledge_retriever import KnowledgeBase
from .extractor import ParameterExtractor, MUTATING_INTENTS, NON_MUTATING_INTENTS, normalize_intent
from .normalizer import FieldNormalizer
from .output_builder import OutputBuilder
from .validator import TaskValidator, Violation
from .prompts import build_responder_messages
from .task_intent_builder import TaskIntentBuilder
from .simulated_time import get_current_datetime
from .time_context import get_time_context, is_standalone_time_query
from .coord_parser import parse_coordinate_updates
from .oilfield_linker import OilfieldEntityLinker
from .exceptions import TaskPersistenceError, TaskRollbackError, IntentIdConflict, IdReservationError
from .slot_store import SlotStore, Slot

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
    "equipment_type":      "设备类型",
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
        self.normalizer = FieldNormalizer(llm)
        self.builder = OutputBuilder(kb)
        self.validator = TaskValidator(kb)
        self.oilfield_linker = OilfieldEntityLinker(kb.environment)

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

        # 3. Parameter Extraction & Processing Pipeline (Atomic Transaction with Optimistic Lock)
        new_slots, new_unresolved, expected_version = self.slot_store.snapshot()
        
        task_type_key = new_slots.get("task_type_key").value if new_slots.get("task_type_key") else None
        current_state = self.slot_store.get_task_state()
        
        merged_updates = {}
        merged_updates_meta = {}
        extraction_res = {}
        proposed_pending_rov = list(self._pending_rov_candidates)
        
        if task_type_key is None:
            # Stage 1: Extract task type
            extraction_res = self.extractor.extract_updates(
                user_message, self.conversation_history, current_state,
                task_type_key=None,
                task_type_map=self.kb.get_task_type_map(),
                required=None
            )
            intent_str = normalize_intent(extraction_res.get("intent"))

            if intent_str not in MUTATING_INTENTS and not (old_phase == "confirming" and self._user_confirmed(user_message)):
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
            
            task_type_key = new_slots.get("task_type_key").value if new_slots.get("task_type_key") else None
            
        if task_type_key:
            # Stage 2: Extract task parameters
            current_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
            required = self.builder.get_required(task_type_key, self.mode)
            extraction_res = self.extractor.extract_updates(
                user_message, self.conversation_history, current_state,
                task_type_key=task_type_key,
                task_type_map=self.kb.get_task_type_map(),
                required=required,
                ROV2type=self.kb.ROV2type
            )
            intent_str = normalize_intent(extraction_res.get("intent"))

            if intent_str not in MUTATING_INTENTS and not (old_phase == "confirming" and self._user_confirmed(user_message)):
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
            
            if extraction_res.get("unresolved"):
                for u in extraction_res["unresolved"]:
                    if u not in new_unresolved:
                        new_unresolved.append(u)
                        
            stage2_updates = {}
            for candidate in extraction_res.get("slot_candidates", []):
                k = candidate["canonical_key"]
                v = candidate["normalized_value"]
                if k == "equipment_name" or k == "equipment_model":
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
                
            # Conflict resolution check: if user confirmed or cancelled conflict in next turn
            for k, slot in list(new_slots.items()):
                if slot.status == "conflict" and slot.candidate_value is not None:
                    if self._user_confirmed(user_message) or stage2_updates.get(k) == slot.candidate_value:
                        slot.value = slot.candidate_value
                        slot.status = "valid"
                        slot.candidate_value = None
                        slot.validation_error = None
                    elif self._user_cancelled(user_message):
                        slot.status = "valid"
                        slot.candidate_value = None
                        slot.validation_error = None
                        
            self._apply_updates_in_transaction(stage2_updates, new_slots)
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
        ti_json_artifact = None

        # Check required missing in working new_slots
        if curr_task_type_key:
            req_schema = self.builder.get_schema(curr_task_type_key, proposed_mode)
            cand_missing = [f for f in req_schema if f.get("type") not in ("auto", "fixed") and (not new_slots.get(f["key"]) or new_slots[f["key"]].status != "valid" or new_slots[f["key"]].value is None)]
        else:
            cand_missing = [{"key": "task_type", "label": "任务类型", "type": "string", "allowed_values": self.kb.get_all_task_type_values()}]

        if not cand_missing and proposed_phase not in ("blocked_hard", "blocked_soft", "confirming", "done"):
            proposed_phase = "confirming"

        staging_file = None
        ti_json_artifact = None
        prev_snap = None
        prev_mode = self.mode
        prev_phase = self.phase
        prev_task_state = copy.deepcopy(self.task_state)
        prev_built = copy.deepcopy(self._last_built_json)
        prev_missing = copy.deepcopy(self._last_missing)
        prev_hist = list(self.conversation_history)
        prev_whitelist = copy.deepcopy(self._soft_whitelist)
        prev_pending_rov = copy.deepcopy(self._pending_rov_candidates)
        prev_blocking_violations = copy.deepcopy(self._blocking_violations)
        prev_task_start_now = self.task_start_now

        if old_phase == "confirming" and self._user_confirmed(user_message):
            cand_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
            cand_built = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
            all_violations = self.validator.validate(cand_state)
            if not cand_missing and not self.validator.has_hard_violations(all_violations):
                proposed_phase = "done"
                prev_snap = self.slot_store.export_snapshot()
                ti_builder = TaskIntentBuilder(self.kb)
                ti_json_artifact = ti_builder.prepare(
                    task_state=cand_state,
                    built_json=cand_built,
                    mode=proposed_mode,
                    task_type_key=curr_task_type_key
                )
                staging_file = ti_builder.create_staging(ti_json_artifact)
                ti_intent_id = ti_json_artifact["intent_id"]
                if "intent_id" not in new_slots:
                    new_slots["intent_id"] = Slot("intent_id")
                new_slots["intent_id"].value = ti_intent_id
                new_slots["intent_id"].status = "valid"
                new_slots["intent_id"].source = "auto"

        # Atomic single commit with optimistic version validation
        try:
            self.slot_store.commit_transaction(
                new_slots,
                new_unresolved,
                request_id=request_id,
                expected_version=expected_version,
            )
        except Exception as commit_exc:
            if staging_file and staging_file.exists():
                try:
                    staging_file.unlink()
                except Exception:
                    pass
            raise commit_exc
        
        # Apply proposed instance state AFTER successful commit
        self.mode = proposed_mode
        self.phase = proposed_phase
        self._soft_whitelist = proposed_whitelist
        self._pending_rov_candidates = proposed_pending_rov

        # Re-derive from slot_store (SSOT)
        self.task_state = self.slot_store.get_task_state()
        built = self.slot_store.get_built_json()
        self._last_built_json = built
        
        if curr_task_type_key:
            required_schema = self.builder.get_schema(curr_task_type_key, self.mode)
            missing = self.slot_store.get_missing_slots(required_schema)
            self._last_missing = missing
        else:
            missing = [{"key": "task_type", "label": "任务类型", "type": "string",
                        "allowed_values": self.kb.get_all_task_type_values()}]
            self._last_missing = missing

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
                      "water_depth", "equipment_type", "equipment_name", "payload", "support_vessel", "oilfield_name",
                      "oilfield_coordinates", "wellhead_id"}

        if not missing and self.phase not in ("blocked_hard", "blocked_soft"):
            constraint_context = self._run_constraint_check(ALL_FIELDS)
        elif not missing and self.phase == "blocked_soft":
            constraint_context = self._run_constraint_check(changed_fields)
        elif not missing and self.phase == "blocked_hard":
            constraint_context = self._run_constraint_check(ALL_FIELDS)
        else:
            constraint_context = self._run_constraint_check(changed_fields)

        if self.phase == "done":
            if ti_json_artifact and staging_file:
                ti_builder = TaskIntentBuilder(self.kb)
                try:
                    ti_builder.publish_staging(staging_file, ti_json_artifact)
                except Exception as exc:
                    # 1. Immediately reset phase and final_result so phase NEVER remains "done"
                    self.phase = prev_phase
                    self.final_result = None

                    # 2. Clean staging file
                    if staging_file and staging_file.exists():
                        try:
                            staging_file.unlink()
                        except Exception:
                            pass

                    # 3. Restore slot_store
                    rollback_failed = False
                    rollback_err = None
                    if prev_snap:
                        try:
                            self.slot_store.restore_snapshot(prev_snap)
                        except Exception as rb_e:
                            rollback_failed = True
                            rollback_err = rb_e

                    # 4 & 5. Re-derive manager state
                    self.mode = prev_mode
                    self.task_state = self.slot_store.get_task_state()
                    self._last_built_json = self.slot_store.get_built_json()
                    self._soft_whitelist = prev_whitelist
                    self._pending_rov_candidates = prev_pending_rov
                    self._blocking_violations = prev_blocking_violations
                    self.conversation_history = prev_hist
                    self.task_start_now = prev_task_start_now

                    if curr_task_type_key:
                        required_schema = self.builder.get_schema(curr_task_type_key, self.mode)
                        self._last_missing = self.slot_store.get_missing_slots(required_schema)
                    else:
                        self._last_missing = prev_missing

                    logger.error(
                        "TaskIntent publish failed: request_id=%s, task_id=%s, intent_id=%s, store_version=%d, stage=%s, err_type=%s, err=%s, rollback_failed=%s",
                        request_id,
                        built.get("task_id", "unknown"),
                        ti_json_artifact.get("intent_id", "unknown") if ti_json_artifact else "unknown",
                        self.slot_store.version,
                        "publish_staging",
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

            self.final_result = built
            if self.task_start_now:
                reply = (f"✅ 信息收集完成，当前为【立即执行任务】，任务已生成并下发。\n"
                         f"{json.dumps(built, ensure_ascii=False, indent=2)}")
            else:
                reply = (f"✅ 信息收集完成，当前为【未来规划任务】，已加入计划池。\n"
                         f"{json.dumps(built, ensure_ascii=False, indent=2)}")
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        if self._user_cancelled(user_message):
            self.phase = "rejected"
            self.final_result = None
            reply = "任务已取消。如需重新规划，请重新开始。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # 知识上下文
        knowledge_context = self.kb.get_context_for_state(self.task_state)

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
        )
        reply = self.llm.chat(messages, temperature=0.7, max_tokens=1500)
        reply = self.llm.filter_reply(reply, temperature=0.1, max_tokens=1500)

        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    # --------------------------------------------------------------------------
    # 参数更新与规范化
    # --------------------------------------------------------------------------

    def _link_oilfield_update(self, updates: dict) -> dict:
        raw_name = updates.get("oilfield_name")
        if not raw_name:
            return updates

        coords = (
            updates.get("oilfield_coordinates")
            or updates.get("start_point")
            or updates.get("cable_position")
            or self.task_state.get("oilfield_coordinates")
            or self.task_state.get("start_point")
            or self.task_state.get("cable_position")
        )
        match = self.oilfield_linker.link(str(raw_name), coords)
        linked = dict(updates)
        linked["raw_oilfield_name"] = match.raw
        linked["oilfield_match_status"] = match.status
        linked["oilfield_match_confidence"] = match.confidence
        linked["oilfield_match_evidence"] = match.evidence
        linked["oilfield_match_candidates"] = match.candidates

        if match.status == "accepted" and match.standard_name:
            linked["oilfield_name"] = match.standard_name
            linked["oilfield_entity_id"] = match.entity_id
            linked["__clear_pending_oilfield"] = True
        else:
            linked.pop("oilfield_name", None)
            linked["pending_oilfield_name"] = match.raw
            linked["pending_oilfield_candidates"] = match.candidates
            linked["__clear_oilfield_name"] = True
        return linked

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

    def _apply_updates_in_transaction(self, updates: dict, new_slots: dict):
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

        skip = {"emergency_mode", "rov_description", "__clear_oilfield_name", "__clear_pending_oilfield"}
        for k, item in updates.items():
            if k in skip or item is None or item == "":
                continue

            if isinstance(item, dict) and "value" in item:
                v = item.get("value")
                raw_v = item.get("raw_value", str(v) if v is not None else None)
                conf = item.get("confidence", 1.0)
                src = item.get("source", "user_input")
            else:
                v = item
                raw_v = str(v) if v is not None else None
                conf = 1.0
                src = "user_input"

            if v is None or v == "":
                continue

            if k in ("task_type", "task_type_key"):
                self._handle_task_type_update_in_transaction(k, v, new_slots)
                continue
            
            # Check for conflict or update
            slot = new_slots.get(k)
            if slot and slot.status == "valid" and slot.value is not None and slot.value != v:
                slot.candidate_value = v
                slot.raw_value = raw_v
                slot.confidence = conf
                slot.source = src
                slot.status = "candidate"
                slot.validation_error = None
            else:
                if k in new_slots:
                    new_slots[k].candidate_value = v
                    new_slots[k].raw_value = raw_v
                    new_slots[k].confidence = conf
                    new_slots[k].source = src
                    new_slots[k].status = "candidate"
                    new_slots[k].validation_error = None
                else:
                    new_slots[k] = Slot(
                        slot_name=k,
                        value=None,
                        candidate_value=v,
                        raw_value=raw_v,
                        confidence=conf,
                        source=src,
                        status="candidate"
                    )

        if updates.get("emergency_mode"):
            if "emergency_mode" in new_slots:
                new_slots["emergency_mode"].value = True
                new_slots["emergency_mode"].status = "valid"
        if "rov_description" in updates:
            self._handle_rov_description_in_transaction(updates["rov_description"], new_slots)

        # Auto-synchronize equipment_type and equipment_name when any equipment slot is set/updated
        eq_name_slot = new_slots.get("equipment_name")
        eq_type_slot = new_slots.get("equipment_type")
        eq_unit_slot = new_slots.get("equipment_unit_id")
        eq_val = (eq_unit_slot.value if (eq_unit_slot and eq_unit_slot.status == "valid") else None) or \
                 (eq_unit_slot.candidate_value if eq_unit_slot else None) or \
                 (eq_name_slot.value if (eq_name_slot and eq_name_slot.status == "valid") else None) or \
                 (eq_name_slot.candidate_value if eq_name_slot else None) or \
                 (eq_type_slot.value if (eq_type_slot and eq_type_slot.status == "valid") else None) or \
                 (eq_type_slot.candidate_value if eq_type_slot else None)
        if eq_val:
            task_type = (new_slots.get("task_type_key").value if (new_slots.get("task_type_key") and new_slots.get("task_type_key").status == "valid") else None) or self.task_state.get("task_type_key")
            rov = (self.kb.get_rov_for_task(eq_val, task_type) if task_type else None) or self.kb.get_rov(eq_val)
            if rov:
                full_name = rov["full_name"]
                if "equipment_name" not in new_slots:
                    new_slots["equipment_name"] = Slot("equipment_name")
                new_slots["equipment_name"].value = full_name
                new_slots["equipment_name"].status = "valid"
                new_slots["equipment_name"].candidate_value = None

                if "equipment_type" not in new_slots:
                    new_slots["equipment_type"] = Slot("equipment_type")
                new_slots["equipment_type"].value = full_name
                new_slots["equipment_type"].status = "valid"
                new_slots["equipment_type"].candidate_value = None

                unit_ids = rov.get("unit_ids", [])
                unit_val = (eq_unit_slot.value if (eq_unit_slot and eq_unit_slot.status == "valid") else None) or \
                           (eq_unit_slot.candidate_value if eq_unit_slot else None)
                if unit_val and unit_val in unit_ids:
                    if "equipment_unit_id" not in new_slots:
                        new_slots["equipment_unit_id"] = Slot("equipment_unit_id")
                    new_slots["equipment_unit_id"].value = unit_val
                    new_slots["equipment_unit_id"].status = "valid"
                    new_slots["equipment_unit_id"].candidate_value = None
                elif eq_val in unit_ids:
                    if "equipment_unit_id" not in new_slots:
                        new_slots["equipment_unit_id"] = Slot("equipment_unit_id")
                    new_slots["equipment_unit_id"].value = eq_val
                    new_slots["equipment_unit_id"].status = "valid"
                    new_slots["equipment_unit_id"].candidate_value = None

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

            temp_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
            allowed = self.builder._resolve_allowed(field_def, task_type_key, temp_state)
            if allowed:
                raw = target_val
                if ftype == "list":
                    normalized = self.normalizer.normalize(key, field_def["label"], raw, allowed, ftype)
                else:
                    normalized = self.normalizer.normalize(key, field_def["label"], str(raw), allowed, ftype)
                    
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
                    import re
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
                        slot.validation_error = f"Invalid numeric value: {target_val}"
                else:
                    slot.value = target_val
                    slot.candidate_value = None
                    slot.status = "valid"
                    slot.validation_error = None

        temp_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
        violations = self.validator.validate(temp_state)
        for v in violations:
            if v.severity == "hard":
                for f in v.related_fields:
                    slot = new_slots.get(f)
                    if slot:
                        slot.status = "invalid"
                        slot.validation_error = v.message

    def _apply_updates(self, updates: dict):
        """Deprecated: Compatibility helper routing updates through SlotStore transaction."""
        snap_slots, snap_unresolved, snap_ver = self.slot_store.snapshot()
        self._apply_updates_in_transaction(updates, snap_slots)
        self.slot_store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver)
        self.task_state = self.slot_store.get_task_state()
        self._rebuild_cache()

    def _handle_task_type_update(self, key: str, value: str):
        """Deprecated: Compatibility helper for task type update via SlotStore transaction."""
        snap_slots, snap_unresolved, snap_ver = self.slot_store.snapshot()
        self._handle_task_type_update_in_transaction(key, value, snap_slots)
        self.slot_store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver)
        self.task_state = self.slot_store.get_task_state()
        self._rebuild_cache()

    def _normalize_constrained_fields(self, changed_fields: set[str]):
        """Deprecated: Compatibility helper for normalizing fields via SlotStore transaction."""
        task_type_key = self.task_state.get("task_type_key")
        if not task_type_key:
            return
        snap_slots, snap_unresolved, snap_ver = self.slot_store.snapshot()
        self._normalize_and_validate_in_transaction(snap_slots, task_type_key)
        self.slot_store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver)
        self.task_state = self.slot_store.get_task_state()
        self._rebuild_cache()

    def _handle_rov_description(self, description: str):
        """Deprecated: Compatibility helper for ROV description resolution via SlotStore transaction."""
        snap_slots, snap_unresolved, snap_ver = self.slot_store.snapshot()
        candidates = self._handle_rov_description_in_transaction(description, snap_slots)
        self.slot_store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver)
        self._pending_rov_candidates = candidates
        self.task_state = self.slot_store.get_task_state()
        self._rebuild_cache()

    def _resolve_pending_oilfield_confirmation(self, user_message: str, request_id: str = "req_default") -> str | None:
        pending_slot = self.slot_store.slots.get("pending_oilfield_name")
        raw_name = pending_slot.value if (pending_slot and pending_slot.status == "valid") else None
        if not raw_name:
            return None

        if self._user_cancelled_oilfield(user_message):
            snap_slots, snap_unresolved, snap_ver = self.slot_store.snapshot()
            for k in ("oilfield_name", "oilfield_entity_id", "pending_oilfield_name", "pending_oilfield_candidates"):
                if k in snap_slots:
                    snap_slots[k].value = None
                    snap_slots[k].status = "missing"
                    snap_slots[k].raw_value = None
            self.slot_store.commit_transaction(
                snap_slots, snap_unresolved, request_id=request_id, expected_version=snap_ver
            )
            self.task_state = self.slot_store.get_task_state()
            self._rebuild_cache()
            return "已取消当前待确认油田名称，请提供标准的油田名称（例如：流花11-1油田、陵水17-2油田等），或补充油田坐标。"

        if not self._user_confirmed_oilfield(user_message):
            return None

        candidate = self._top_pending_oilfield_candidate()
        if not candidate:
            return self._build_pending_oilfield_reply()

        confirmed_name = candidate.get("name")
        entity_id = candidate.get("id")

        snap_slots, snap_unresolved, snap_ver = self.slot_store.snapshot()
        for k in ("oilfield_name", "oilfield_entity_id", "oilfield_match_status", "oilfield_match_confidence", "oilfield_match_evidence"):
            if k not in snap_slots:
                snap_slots[k] = Slot(k)

        snap_slots["oilfield_name"].value = confirmed_name
        snap_slots["oilfield_name"].status = "valid"
        snap_slots["oilfield_name"].source = "entity_linker"
        snap_slots["oilfield_name"].raw_value = raw_name
        snap_slots["oilfield_name"].confidence = candidate.get("confidence")

        if "oilfield_entity_id" not in snap_slots:
            snap_slots["oilfield_entity_id"] = Slot("oilfield_entity_id")
        snap_slots["oilfield_entity_id"].value = entity_id
        snap_slots["oilfield_entity_id"].status = "valid"
        snap_slots["oilfield_entity_id"].source = "entity_linker"

        snap_slots["oilfield_match_status"].value = "confirmed"
        snap_slots["oilfield_match_status"].status = "valid"
        snap_slots["oilfield_match_status"].source = "entity_linker"

        snap_slots["oilfield_match_confidence"].value = candidate.get("confidence")
        snap_slots["oilfield_match_confidence"].status = "valid"
        snap_slots["oilfield_match_confidence"].source = "entity_linker"

        snap_slots["oilfield_match_evidence"].value = candidate.get("evidence", [])
        snap_slots["oilfield_match_evidence"].status = "valid"
        snap_slots["oilfield_match_evidence"].source = "entity_linker"

        for k in ("pending_oilfield_name", "pending_oilfield_candidates"):
            if k in snap_slots:
                snap_slots[k].value = None
                snap_slots[k].status = "missing"
                snap_slots[k].raw_value = None

        self.slot_store.commit_transaction(
            snap_slots, snap_unresolved, request_id=request_id, expected_version=snap_ver
        )
        self.task_state = self.slot_store.get_task_state()
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

    def _clear_pending_oilfield(self):
        """Deprecated: Use SlotStore transaction to clear pending oilfield slots."""
        snap_slots, snap_unresolved, snap_ver = self.slot_store.snapshot()
        for key in ("pending_oilfield_name", "pending_oilfield_candidates"):
            if key in snap_slots:
                snap_slots[key].value = None
                snap_slots[key].status = "missing"
        self.slot_store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver)
        self.task_state = self.slot_store.get_task_state()
        self._rebuild_cache()

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

    def _compute_changed_fields(self, updates: dict) -> set[str]:
        changed = set()
        skip = {"emergency_mode", "rov_description", "__clear_oilfield_name", "__clear_pending_oilfield"}
        if updates.get("__clear_oilfield_name") and self.task_state.get("oilfield_name"):
            changed.add("oilfield_name")
        for k, v in updates.items():
            if k in skip or v is None or v == "":
                continue
            if self.task_state.get(k) != v:
                changed.add(k)
        return changed

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
    def _user_cancelled(message: str) -> bool:
        keywords = ["取消", "放弃", "不要了", "终止", "退出"]
        return any(kw in message for kw in keywords)

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
        """根据当前 slot_store 重新构建 task_state, _last_built_json 和 _last_missing"""
        self.task_state = self.slot_store.get_task_state()
        built = self.slot_store.get_built_json()
        task_type_key = self.task_state.get("task_type_key")
        if task_type_key:
            required_schema = self.builder.get_schema(task_type_key, self.mode)
            missing = self.slot_store.get_missing_slots(required_schema)
            self._last_built_json = built
            self._last_missing = missing
        else:
            self._last_built_json = built
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
        """从历史快照恢复对话管理器的状态（先构建、后交换原子恢复模式）"""
        if not isinstance(snapshot, dict):
            raise SnapshotValidationError("Snapshot must be a dictionary.")
        
        conv_hist = snapshot.get("conversation_history")
        if conv_hist is not None and not isinstance(conv_hist, list):
            raise SnapshotValidationError("conversation_history must be a list.")
        conv_hist_copy = copy.deepcopy(conv_hist) if conv_hist is not None else []

        candidate_mode = snapshot.get("mode", "normal")
        if candidate_mode not in ("normal", "emergency"):
            raise SnapshotValidationError(f"Invalid mode '{candidate_mode}'.")

        candidate_phase = snapshot.get("phase", "collecting")
        VALID_PHASES = {"collecting", "blocked_hard", "blocked_soft", "confirming", "done", "rejected"}
        if candidate_phase not in VALID_PHASES:
            raise SnapshotValidationError(f"Invalid phase '{candidate_phase}'.")

        # Resolve slot_store payload
        if "slot_store" in snapshot and isinstance(snapshot["slot_store"], dict):
            slot_store_snap = snapshot["slot_store"]
        elif "slots" in snapshot and "store_version" in snapshot:
            slot_store_snap = snapshot
        elif "task_state" in snapshot and isinstance(snapshot.get("task_state"), dict):
            task_state = snapshot["task_state"]
            legacy_slots = {}
            for k, v in task_state.items():
                vtype = "string"
                if isinstance(v, bool):
                    vtype = "boolean"
                elif isinstance(v, (int, float)):
                    vtype = "number"
                elif isinstance(v, list):
                    vtype = "list"
                elif isinstance(v, dict):
                    vtype = "coord" if ("lat" in v and "lon" in v) else "object"
                elif isinstance(v, str) and len(v) >= 19 and "T" in v:
                    try:
                        datetime.fromisoformat(v.replace("Z", "+00:00"))
                        vtype = "datetime"
                    except Exception:
                        vtype = "string"

                legacy_slots[k] = {
                    "slot_name": k,
                    "value": v,
                    "value_type": vtype,
                    "status": "valid" if v is not None else "missing",
                    "source": "legacy_import",
                    "version": 1
                }
            slot_store_snap = {
                "store_version": 1,
                "slots": legacy_slots,
                "unresolved": snapshot.get("unresolved", [])
            }
        else:
            raise SnapshotValidationError("Snapshot missing valid slot_store or task_state.")

        # Build candidate SlotStore (raises SnapshotValidationError on failure)
        candidate_slot_store = SlotStore.from_snapshot(slot_store_snap, self.kb)

        # Derive facts from candidate_slot_store
        candidate_task_state = candidate_slot_store.get_task_state()
        candidate_built = candidate_slot_store.get_built_json()
        task_type_key = candidate_task_state.get("task_type_key")
        if task_type_key:
            required_schema = self.builder.get_schema(task_type_key, candidate_mode)
            candidate_missing = candidate_slot_store.get_missing_slots(required_schema)
        else:
            candidate_missing = [{"key": "task_type", "label": "任务类型", "type": "string",
                                   "allowed_values": self.kb.get_all_task_type_values()}]

        # ALL STEPS SUCCEEDED - Swap states atomically!
        self.slot_store = candidate_slot_store
        self.conversation_history = conv_hist_copy
        self.mode = candidate_mode
        self.phase = candidate_phase
        self.task_state = candidate_task_state
        self._last_built_json = candidate_built
        self._last_missing = candidate_missing
        self.final_result = candidate_built if candidate_phase == "done" else None
        self.awaiting_final_confirm = False
        self.task_start_now = self.is_start_time_near_now()
        self._blocking_violations = []
        self._soft_whitelist = set()
        self._hard_refusal_counts = {}
        self._pending_rov_candidates = []
