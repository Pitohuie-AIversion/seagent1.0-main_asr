"""
src/handlers/slot_filling.py - 槽位填报与消歧生命周期处理器

职责：
1. 搭载工具就地调整/补丁提取；
2. 任务参数抽取（ParameterExtractor）与规范化应用（FieldNormalizer/NormalizationContract）；
3. 槽位状态存储与事务更新（SlotStore, TaskSlotFilter）；
4. 交互式多候选消歧（ROV候选、油田候选、工具组合候选）；
5. 缺失参数指引与下步提问生成；
6. 事实锚点回复（ground_write_reply）及回执展示生成。
"""

from __future__ import annotations

import copy
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import BaseDialogueHandler, DialogueContext, HandlerResult
from ..model_profile import (
    ModelRole,
    is_normalization_contract_v2_enabled,
    is_task_patch_v2_enabled,
)
from ..normalization_contract import (
    NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS,
    NormalizationApplyPlan,
    normalize_task_patch,
    normalized_task_patch_to_apply_plan,
    validate_normalization_runtime_flags,
)
from ..task_patch import build_task_patch, task_patch_to_legacy_updates
from ..slot_store import Slot
from ..simulated_time import get_current_datetime
from ..id_sequence import next_daily_id, IdReservationError, validate_task_id_for_task_type
from ..prompts import build_responder_messages
from .. import coord_parser

logger = logging.getLogger("src.dialogue_manager")

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
    "equipment_class":     "机器人类别",
    "equipment_family":    "机器人系列",
    "equipment_type":      "设备型号",
    "equipment_name":      "设备全称",
    "equipment_unit_id":   "具体机器人编号",
    "payload":             "携带工具",
    "support_vessel":      "支持船编号",
    "oilfield_name":       "油田名称",
    "oilfield_coordinates":"油田经纬度",
    "wellhead_id":         "井口编号",
}


class SlotFillingHandler(BaseDialogueHandler):
    """槽位填报与消歧生命周期处理器"""

    def can_handle(self, ctx: DialogueContext) -> bool:
        """
        判断是否由本处理器在门禁阶段介入：
        当前仅当用户发起载荷就地调整指令时在 Level 3 拦截处理。
        普通槽位抽取由主流程显式调用 execute_slot_filling 完成。
        """
        user_message = ctx.user_message
        return self.is_payload_modification_request(user_message)

    def handle(self, ctx: DialogueContext) -> HandlerResult:
        """执行槽位就地修改（载荷卡片重置等）"""
        user_message = ctx.user_message
        if self.is_payload_modification_request(user_message):
            payload_mod_reply = self.handle_payload_modification(user_message)
            if payload_mod_reply is not None:
                return HandlerResult.success(reply=payload_mod_reply)
        return HandlerResult.not_handled()

    @staticmethod
    def is_payload_modification_request(user_message: str) -> bool:
        """判断用户是否明确请求重新选择/修改/配置载荷（且不属于取消修改指令）。"""
        msg = (user_message or "").strip().lower()
        if any(neg in msg for neg in ["取消", "放弃", "不要", "不修改", "不用"]):
            return False

        direct_keywords = (
            "修改载荷", "重新选择载荷", "重新选载荷", "重新配置载荷",
            "修改payload", "重选载荷", "更换载荷", "换载荷", "重置载荷",
            "清除载荷", "调出载荷卡片", "载荷卡片", "重配置载荷", "修改工具",
            "重新选择工具", "更换工具", "换工具"
        )
        if any(kw in msg for kw in direct_keywords):
            return True

        has_target = any(t in msg for t in ["载荷", "payload", "工具"])
        has_action = any(a in msg for a in ["修改", "重选", "重新选择", "更换", "重新配置", "重置", "清除"])
        return bool(has_target and has_action)

    def handle_payload_modification(self, user_message: str) -> str | None:
        """用户请求重新选择/修改/配置载荷时，重置 payload 槽位为 missing 并调整阶段供前端调出卡片。"""
        manager = self.manager
        payload_slot = manager.slot_store.slots.get("payload")
        task_type_slot = manager.slot_store.slots.get("task_type_key")

        task_type_val = (
            task_type_slot.value if (task_type_slot and task_type_slot.value)
            else manager.task_state.get("task_type_key")
        )

        if payload_slot is None and task_type_val:
            schema = manager.builder.get_schema(task_type_val, manager.mode)
            for f in schema:
                k = f.get("key")
                if k and k not in manager.slot_store.slots:
                    manager.slot_store.slots[k] = Slot(slot_name=k, value_type=f.get("type", "string"), status="missing")
            payload_slot = manager.slot_store.slots.get("payload")

        if payload_slot is None:
            existing_payload = manager.task_state.get("payload")
            payload_slot = Slot(
                slot_name="payload",
                value=None,
                candidate_value=existing_payload,
                value_type="list",
                status="missing",
                source="user",
            )
            manager.slot_store.slots["payload"] = payload_slot

        if payload_slot is not None:
            existing_val = payload_slot.candidate_value or copy.deepcopy(payload_slot.value)
            payload_slot.candidate_value = existing_val if existing_val is not None else None
            payload_slot.status = "missing"
            payload_slot.value = None
            payload_slot.validation_error = None
            payload_slot.source = "user"
            manager.slot_store.slots["payload"] = payload_slot
            manager.slot_store.version += 1

            if manager.phase in ("confirming", "validating", "blocked_soft", "blocked_hard"):
                manager._transition_phase("collecting", reason="user_requested_payload_modification")

            manager._blocking_violations = []
            manager._switch_dialogue_mode("task_collection", source="user_payload_modification", reason="用户请求重新配置/修改载荷")

            if task_type_slot and task_type_slot.value:
                req_fields = manager.builder.get_required(task_type_slot.value, manager.mode, manager.slot_store.get_task_state())
                manager._last_missing = manager.slot_store.get_missing_slots(req_fields)

            reply = "已为您重新调出载荷配置卡片。请在下方对话区域或卡片中重新选择与配置要携带的工具。"
            manager.conversation_history.append({"role": "user", "content": user_message})
            manager.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        return None

    def normalize_payload_list_mutations(
        self,
        extraction_res: dict,
        user_message: str,
        current_slots: dict,
    ) -> None:
        """兜底防护：当 LLM 抽取的 extraction_res 将 payload 误放入 slot_candidates 时，
        基于用户增量/减量意图或现有槽位，自动转换为 list_mutations（op: add/remove），
        防止列表字段被整体覆盖。
        """
        mutations = extraction_res.get("list_mutations")
        if not isinstance(mutations, list):
            mutations = []
            extraction_res["list_mutations"] = mutations

        has_payload_mutation = any(
            isinstance(m, dict) and m.get("field") == "payload"
            for m in mutations
        )
        if has_payload_mutation:
            return

        candidates = extraction_res.get("slot_candidates")
        if not isinstance(candidates, list):
            return

        payload_cands = [
            c for c in candidates
            if isinstance(c, dict) and c.get("canonical_key") == "payload"
        ]
        if not payload_cands:
            return

        items = []
        for cand in payload_cands:
            val = cand.get("normalized_value")
            if val is None:
                val = cand.get("raw_value")
            if isinstance(val, list):
                for item in val:
                    if item and item not in items:
                        items.append(item)
            elif val and val not in items:
                items.append(val)
        if not items:
            return

        msg = str(user_message or "")
        add_kws = ("添加", "加装", "增加", "加上", "还要", "补充", "带上", "携带", "配置", "配合", "还要带", "加个")
        remove_kws = ("删除", "去掉", "移除", "不要", "取消", "别带")
        replace_kws = ("替换", "改成", "换成", "重置", "覆盖")

        is_add = any(kw in msg for kw in add_kws)
        is_remove = any(kw in msg for kw in remove_kws)
        is_replace = any(kw in msg for kw in replace_kws)

        payload_slot = current_slots.get("payload")
        has_existing_payload = bool(
            payload_slot
            and payload_slot.status == "valid"
            and isinstance(payload_slot.value, list)
            and len(payload_slot.value) > 0
        )

        max_confidence = max((c.get("confidence", 0.95) for c in payload_cands if isinstance(c, dict)), default=0.95)

        if is_add or (has_existing_payload and not is_replace and not is_remove):
            extraction_res["slot_candidates"] = [
                c for c in candidates
                if isinstance(c, dict) and c.get("canonical_key") != "payload"
            ]
            mutations.append({
                "field": "payload",
                "operation": "add",
                "items": items,
                "target_items": [],
                "raw_text": msg,
                "confidence": max_confidence,
                "source": "user_input",
            })
        elif is_remove:
            extraction_res["slot_candidates"] = [
                c for c in candidates
                if isinstance(c, dict) and c.get("canonical_key") != "payload"
            ]
            mutations.append({
                "field": "payload",
                "operation": "remove",
                "items": items,
                "target_items": [],
                "raw_text": msg,
                "confidence": max_confidence,
                "source": "user_input",
            })

    @staticmethod
    def filter_robot_selection_unresolved(
        unresolved_items: list,
        accepted_updates: dict | None,
    ) -> list:
        """清理机器人选择迁移后的 unresolved 噪声。

        equipment_class 已经是内部派生元数据，不再是 schema 采集字段；同一个
        raw phrase 如果已被 family/type/unit 成功写入，下游层级对同 raw 的失败
        fan-out 也不应继续展示给用户。
        """
        if not unresolved_items:
            return []

        accepted_raws: set[str] = set()
        for key, info in (accepted_updates or {}).items():
            if key not in {
                "equipment_family",
                "equipment_type",
                "equipment_unit_id",
                "equipment_name",
            }:
                continue
            raw = info.get("raw_value") if isinstance(info, dict) else None
            value = info.get("value") if isinstance(info, dict) else info
            for item in (raw, value):
                if item is not None and str(item).strip():
                    accepted_raws.add(str(item).strip())

        filtered: list = []
        for item in unresolved_items:
            text = str(item)
            if "equipment_class" in text:
                continue
            match = re.search(
                r"(equipment_family|equipment_type|equipment_unit_id|equipment_name) 表达“([^”]+)”",
                text,
            )
            if match and match.group(2).strip() in accepted_raws:
                continue
            if item not in filtered:
                filtered.append(item)
        return filtered

    def project_legacy_equipment_class_candidate(
        self,
        candidate: dict,
        task_type_key: str | None,
        task_state: dict | None,
    ) -> dict | None:
        """Map a legacy equipment_class candidate to family when unambiguous."""
        manager = self.manager
        raw_value = candidate.get("raw_value", candidate.get("normalized_value"))
        normalized_value = candidate.get("normalized_value", raw_value)
        class_id = manager.kb._resolve_class_key(str(normalized_value or ""))
        if not class_id and raw_value is not None:
            class_id = manager.kb._resolve_class_key(str(raw_value))
        if not class_id or not task_type_key:
            return None
        try:
            domain = manager.kb.get_feasible_robot_selection_domain(
                task_type_key,
                task_state,
            )
        except Exception:
            return None
        class_node = next(
            (
                item
                for item in domain.get("classes", [])
                if item.get("class_id") == class_id
            ),
            None,
        )
        families = class_node.get("families", []) if class_node else []
        if len(families) != 1:
            return None
        family = families[0]
        projected = dict(candidate)
        projected["canonical_key"] = "equipment_family"
        projected["normalized_value"] = family.get("full_name") or family.get("family_id")
        projected.setdefault("raw_value", raw_value)
        projected["resolution_method"] = "legacy_class_to_single_family"
        return projected

    def get_committed_update_display_values(self, accepted_updates: dict) -> dict:
        """从领域配置生成写入回执的展示值，不改变 SlotStore 标准值。"""
        manager = self.manager
        display = {}
        for k, v in (accepted_updates or {}).items():
            if k == "equipment_class" and v is not None:
                class_config = manager.kb.get_robot_classes().get(str(v), {})
                display["equipment_class"] = class_config.get(
                    "full_name",
                    v,
                )
            else:
                display[k] = coord_parser.format_slot_display_value(k, v)
        return display

    def get_committed_turn_updates(
        self,
        proposed_updates: dict,
        state_before_turn: dict,
    ) -> dict:
        """返回本轮已由 SlotStore 提交的用户字段更新。"""
        manager = self.manager
        if not proposed_updates:
            return {}

        # 内部元数据字段：由 oilfield linker 等中间件写入，仅供后端推理，不得面向用户展示
        _INTERNAL_METADATA_KEYS = {
            "raw_oilfield_name",
            "oilfield_match_status",
            "oilfield_match_confidence",
            "oilfield_match_evidence",
            "oilfield_match_candidates",
            "oilfield_entity_id",
            "pending_oilfield_name",
            "pending_oilfield_candidates",
        }
        ignored_keys = {
            "task_id",
            "intent_id",
            "internal_id",
            "task_type_key",
            "emergency_mode",
            "rov_description",
            "__clear_oilfield_name",
            "__clear_pending_oilfield",
        } | _INTERNAL_METADATA_KEYS
        accepted: dict = {}
        for key, value in manager.task_state.items():
            if key in ignored_keys or key.startswith("__") or value is None:
                continue
            if key not in proposed_updates and state_before_turn.get(key) == value:
                continue
            slot = manager.slot_store.slots.get(key)
            if slot and slot.status == "valid":
                accepted[key] = value
        return accepted

    @staticmethod
    def ground_write_reply(
        self_or_reply: object = "",
        model_reply: str = "",
        *,
        accepted_updates: dict,
        unresolved_inputs: list,
        missing_fields: list[dict] | None = None,
        display_updates: dict | None = None,
    ) -> str:
        """在 LLM 自然语言回复后追加事实锚点摘要，防止回复内容与实际写入状态不一致。

        设计原则：
        - ``model_reply`` 作为主体自然语言回复，原样保留，不得丢弃。
        - 仅将 FIELD_LABELS 中有中文标签的用户可见字段写入摘要，内部元数据字段不得出现。
        - 当 ``accepted_updates`` 非空时，在回复末尾追加一行简明的字段确认摘要。
        - 当 LLM 回复为空时，退化为纯摘要模式（兜底）。
        """
        if isinstance(self_or_reply, str):
            self_obj = None
            actual_model_reply = self_or_reply
        else:
            self_obj = self_or_reply
            actual_model_reply = model_reply

        # 只展示 FIELD_LABELS 中有中文标签的用户可见字段
        user_visible_updates = {
            key: value
            for key, value in accepted_updates.items()
            if key in FIELD_LABELS
        }
        resolved_labels = {
            FIELD_LABELS[key]
            for key in user_visible_updates
        }
        unresolved = []
        for item in unresolved_inputs:
            text = str(item).strip()
            if not text:
                continue
            if (
                any(label in text for label in resolved_labels)
                and ("无法解析" in text or "Invalid datetime format" in text)
            ):
                continue
            unresolved.append(text)

        suffix_parts: list[str] = []
        if user_visible_updates:
            committed = []
            for key, value in user_visible_updates.items():
                label = FIELD_LABELS[key]
                display_value = (display_updates or {}).get(key, value)
                rendered = coord_parser.format_slot_display_value(key, display_value)
                committed.append(f"{label}：{rendered}")
            suffix_parts.append("✅ 已记录：" + "；".join(committed) + "。")

        if unresolved:
            suffix_parts.append("⚠️ 未写入或仍需确认：" + "；".join(unresolved) + "。")

        if missing_fields is not None:
            labels = [
                str(item.get("label") or item.get("key"))
                for item in missing_fields
                if isinstance(item, dict) and (item.get("label") or item.get("key"))
            ]
            if labels:
                next_labels = labels[:3]
                suffix_parts.append("仍需补充：" + "、".join(next_labels) + "。")
                if any(isinstance(item, dict) and item.get("key") == "payload" for item in missing_fields[:3]):
                    eq_type = str(
                        (accepted_updates or {}).get("equipment_type")
                        or ((display_updates or {}).get("equipment_type"))
                        or ""
                    )
                    manager_obj = getattr(self_obj, "manager", self_obj)
                    if not eq_type and manager_obj and hasattr(manager_obj, "slot_store") and manager_obj.slot_store:
                        eq_slot = manager_obj.slot_store.slots.get("equipment_type")
                        if eq_slot and eq_slot.status == "valid" and eq_slot.value:
                            eq_type = str(eq_slot.value)
                    if eq_type and manager_obj and hasattr(manager_obj, "kb") and manager_obj.kb:
                        robot = manager_obj.kb.get_rov(eq_type)
                        if robot:
                            ob_list = robot.get("onboard_payloads", [])
                            if ob_list and hasattr(manager_obj, "capability_adapter"):
                                guidance_msg = manager_obj.capability_adapter.format_payload_guidance(
                                    "",
                                    [{"key": "payload", "equipment_type": eq_type, "onboard_payloads": ob_list}]
                                )
                                suffix_parts.append(guidance_msg)
            elif user_visible_updates:
                suffix_parts.append("所有必填字段已收集完成，任务尚未发布。")

        suffix = "\n".join(suffix_parts)

        # 区分有提交和无提交的响应安全规则：
        # 1. 若 LLM 回复为空，退化为纯摘要模式（兜底）。
        if not actual_model_reply or not str(actual_model_reply).strip():
            if not accepted_updates:
                reply = "本轮没有任务字段通过验证，因此未写入任务状态。"
                if suffix:
                    reply = f"{reply}\n{suffix}"
                return reply
            return suffix if suffix else "已写入本轮通过验证的字段。"

        # 2. 若有 LLM 回复，以 LLM 自然语言回复为主体，末尾追加规则生成的客观校验/记录摘要。
        model_reply_str = str(actual_model_reply)
        if not accepted_updates:
            for false_claim in ("已创建", "指令已下发", "已经设置"):
                if false_claim in model_reply_str:
                    model_reply_str = model_reply_str.replace(false_claim, "未写入任务状态")
            if "未写入" not in model_reply_str and not any("未写入" in p for p in suffix_parts):
                suffix_parts.insert(0, "⚠️ 本轮未写入任务状态。")

        def _normalize(text: object) -> str:
            if not isinstance(text, str):
                text = str(text)
            return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)

        norm_reply = _normalize(model_reply_str)
        deduped_parts = []
        for part in suffix_parts:
            norm_part = _normalize(part)
            # 检查整段是否已存在（严格或去空）
            if part in model_reply_str or (norm_part and norm_part in norm_reply):
                continue
            # 检查特定模式
            if part.startswith("✅ 已记录：") and ("✅ 已记录：" in model_reply_str or "✅已记录：" in model_reply_str):
                labels_in_reply = all(
                    FIELD_LABELS[k] in model_reply_str
                    for k in user_visible_updates
                )
                if labels_in_reply:
                    continue
            if part.startswith("仍需补充：") and ("仍需补充：" in model_reply_str or "仍需补充" in model_reply_str):
                continue
            if part.startswith("⚠️ 未写入或仍需确认：") and ("⚠️ 未写入或仍需确认：" in model_reply_str or "未写入或仍需确认" in model_reply_str):
                continue
            if part.startswith("【提示】") or "已搭载" in part:
                if any(kw in model_reply_str for kw in ("已搭载", "自带载荷", "已具备", "【提示】", "替换、增加或减少")):
                    continue
            deduped_parts.append(part)

        deduped_suffix = "\n".join(deduped_parts)
        if deduped_suffix:
            if isinstance(actual_model_reply, str):
                return f"{model_reply_str.rstrip()}\n\n{deduped_suffix}".rstrip()
            return f"{model_reply_str}\n\n{deduped_suffix}"
        return model_reply_str.strip() if isinstance(actual_model_reply, str) else model_reply_str

    def execute_slot_filling(
        self,
        ctx: DialogueContext,
        *,
        route: Any = None,
        plan: Any = None,
        has_acknowledge_action: bool = False,
    ) -> str:
        """执行端到端槽位抽取、规范化过滤、消歧、原子事务提交与事实锚点回复落地。"""
        manager = self.manager
        user_message = ctx.user_message
        request_id = ctx.request_id
        old_phase = ctx.old_phase

        # 3. Parameter Extraction & Processing Pipeline (Atomic Transaction with Optimistic Lock)
        import sys
        _dm_mod = sys.modules.get("src.dialogue_manager")
        task_patch_v2_active = getattr(_dm_mod, "is_task_patch_v2_enabled", is_task_patch_v2_enabled)()
        norm_v2_active = getattr(_dm_mod, "is_normalization_contract_v2_enabled", is_normalization_contract_v2_enabled)()
        validate_normalization_runtime_flags(task_patch_v2_active, norm_v2_active)

        new_slots, _previous_unresolved, expected_version = manager.slot_store.snapshot()
        new_unresolved: list = []

        task_type_slot = new_slots.get("task_type_key")
        task_type_key = (
            task_type_slot.value
            if task_type_slot
            and task_type_slot.status == "valid"
            and task_type_slot.value is not None
            else None
        )
        had_task_type_key_at_turn_start = task_type_key is not None
        current_state = manager.slot_store.get_task_state()
        state_before_turn = dict(current_state)

        if task_type_key:
            schema = manager.builder.get_schema(task_type_key, manager.mode)
            for f in schema:
                k = f.get("key")
                if k and k not in new_slots:
                    new_slots[k] = Slot(slot_name=k, value_type=f.get("type", "string"), status="missing")

        merged_updates = {}
        merged_updates_meta = {}
        payload_mutation_failed = False
        mutation_failure_result = None
        list_mutations = []

        extraction_res = {}
        proposed_pending_rov = list(manager._pending_rov_candidates)
        turn_unresolved: list = []

        def record_unresolved(result: dict) -> None:
            for item in result.get("unresolved", []):
                if item not in turn_unresolved:
                    turn_unresolved.append(item)
                if item not in new_unresolved:
                    new_unresolved.append(item)

        def reply_write_without_candidates() -> str:
            """依据事务结果回复，优先通过 LLM 依据处理事实向用户解释原因并引导。"""
            if has_acknowledge_action:
                manager._switch_dialogue_mode(
                    "task_collection",
                    source="interaction_plan",
                    reason="无任务候选时执行结构化软警告确认",
                )
                if manager.phase == "blocked_soft":
                    return manager._handle_soft_warning_confirmation(
                        user_message,
                        request_id,
                    )
                reply = (
                    "当前没有等待确认的软警告，未执行忽略操作。"
                    "任务状态和已填写参数均未改变。"
                )
                manager.conversation_history.append({"role": "user", "content": user_message})
                manager.conversation_history.append({"role": "assistant", "content": reply})
                return reply

            unresolved = list(turn_unresolved)
            if not unresolved:
                unresolved.append("模型没有返回可验证的任务字段候选")

            req_fields = manager.builder.get_required(task_type_key, manager.mode, manager.slot_store.get_task_state()) if task_type_key else []
            missing = manager.slot_store.get_missing_slots(req_fields) if task_type_key else []
            knowledge_context = manager.kb.get_context_for_state(manager.task_state)
            constraint_context = manager._run_constraint_check(set(), purpose="interactive")

            messages = build_responder_messages(
                task_state=manager.task_state,
                built_json=manager._last_built_json,
                missing_fields=missing,
                mode=manager.mode,
                phase=manager.phase,
                knowledge_context=knowledge_context,
                constraint_context=constraint_context,
                conversation_history=manager.conversation_history,
                latest_user_message=user_message,
                ROV2type=manager.kb.ROV2type,
                support_task=manager.kb.get_supported_task(),
                slot_snapshot=manager.slot_store.get_slot_snapshot(),
                accepted_updates={},
                unresolved_inputs=unresolved,
            )
            model_reply = manager._safe_llm_chat(
                messages,
                temperature=0.7,
                max_tokens=1500,
                role=ModelRole.TASK_RESPONDER,
            )
            model_reply = manager._safe_llm_filter_reply(model_reply, role=ModelRole.FILTER_REPLY)
            if task_type_key and (not model_reply or "已就绪" in model_reply):
                tv = manager.kb.task_schemas.get("task_templates", {}).get(task_type_key, {})
                task_name = tv.get("display_name") or tv.get("name") or task_type_key
                model_reply = f"已为您开启【{task_name}】任务规划。请提供具体作业参数（如管缆类型、作业区域与执行机器人）。"

            reply = self.ground_write_reply(
                model_reply,
                accepted_updates={},
                unresolved_inputs=unresolved,
                missing_fields=missing if missing else None,
            )
            if task_type_key is None:
                supported = manager.kb.get_all_task_type_values()
                if (
                    supported
                    and "当前支持的任务类型" not in reply
                    and not any(st in reply for st in supported)
                ):
                    reply += " 当前支持的任务类型：" + "、".join(supported) + "。"
            manager.conversation_history.append({"role": "user", "content": user_message})
            manager.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        if task_type_key is None:
            # Stage 1: Extract task type
            extraction_res = manager.extractor.extract_updates(
                user_message, current_state,
                task_type_key=None,
                task_type_map=manager.kb.get_task_type_map(),
                required=None,
                conversation_history=manager.conversation_history,
                allow_empty_for_side_effect=has_acknowledge_action,
            )

            if getattr(_dm_mod, "is_task_patch_v2_enabled", is_task_patch_v2_enabled)():
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
                        manager.slot_store.commit_transaction(
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
                        manager.slot_store.commit_transaction(
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
                        "source": manager._source_for_resolution_method(candidate.get("resolution_method"))
                    }
                    stage1_updates[k] = cand_info
                    merged_updates[k] = v
                    merged_updates_meta[k] = cand_info
                record_unresolved(extraction_res)

            manager._apply_updates_in_transaction(stage1_updates, new_slots)

            task_type_slot = new_slots.get("task_type_key")
            task_type_key = (
                task_type_slot.value
                if task_type_slot
                and task_type_slot.status == "valid"
                and task_type_slot.value is not None
                else None
            )

        # WRITE 已由 TurnPlanner 结合上下文判定。不要再根据原句关键词决定是否
        # 调用参数抽取，否则自然表达会在模型判断后被第二道语义门静默丢弃。
        should_extract_task_parameters = bool(task_type_key)
        apply_plan: NormalizationApplyPlan | None = None

        if should_extract_task_parameters:
            # Stage 2: Extract task parameters
            current_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
            field_defs = manager.builder.get_schema(task_type_key, manager.mode)
            required_field_defs = manager.builder.get_required(task_type_key, manager.mode, current_state)
            extraction_res = manager.extractor.extract_updates(
                user_message, current_state,
                task_type_key=task_type_key,
                task_type_map=manager.kb.get_task_type_map(),
                required=required_field_defs,
                ROV2type=manager.kb.ROV2type,
                conversation_history=manager.conversation_history,
                allow_empty_for_side_effect=has_acknowledge_action,
                allow_task_type_transition=True,
            )
            if task_patch_v2_active:
                build_task_patch(extraction_res, allowed_keys=None)

            (
                extracted_task_type_updates,
                duplicate_task_selector_error,
            ) = manager._task_selector_updates_from_extraction(extraction_res)
            extraction_selector_error = next(
                (
                    item
                    for item in extraction_res.get("unresolved", [])
                    if "同轮具体任务类型互相冲突" in str(item)
                ),
                None,
            )
            (
                pending_task_type_key,
                effective_task_type_key,
                task_type_change_locked,
                task_type_preflight_error,
            ) = manager._resolve_task_type_update_context(
                extracted_task_type_updates,
                new_slots,
            )
            task_type_preflight_error = (
                duplicate_task_selector_error
                or extraction_selector_error
                or task_type_preflight_error
            )
            if task_type_preflight_error:
                manager._record_task_type_update_error(
                    new_slots,
                    task_type_preflight_error,
                )
                if task_type_preflight_error not in turn_unresolved:
                    turn_unresolved.append(task_type_preflight_error)
                if task_type_preflight_error not in new_unresolved:
                    new_unresolved.append(task_type_preflight_error)
                manager.slot_store.commit_transaction(
                    new_slots,
                    new_unresolved,
                    request_id=request_id,
                    expected_version=expected_version,
                )
                return reply_write_without_candidates()

            effective_task_type_key = effective_task_type_key or task_type_key
            transition_state = dict(current_state)
            transition_state_active = False
            if (
                pending_task_type_key
                and pending_task_type_key != task_type_key
                and not task_type_change_locked
            ):
                transition_state_active = True
                transition_state = manager._build_task_transition_state(
                    current_state,
                    task_type_key,
                    pending_task_type_key,
                )
                transition_shared_keys = manager._task_transition_shared_field_keys(
                    task_type_key,
                    pending_task_type_key,
                )
                (
                    discovery_updates,
                    discovery_touched_keys,
                ) = manager._normalize_transition_discovery_candidates(
                    extraction_res,
                    pending_task_type_key,
                    transition_state,
                    transition_shared_keys,
                )
                for touched_key in discovery_touched_keys:
                    transition_state.pop(touched_key, None)
                transition_state.update(discovery_updates)
                target_required = manager.builder.get_required(
                    pending_task_type_key,
                    manager.mode,
                    transition_state,
                )
                target_extraction = manager.extractor.extract_updates(
                    user_message,
                    transition_state,
                    task_type_key=pending_task_type_key,
                    task_type_map=manager.kb.get_task_type_map(),
                    required=target_required,
                    ROV2type=manager.kb.ROV2type,
                    conversation_history=manager.conversation_history,
                    allow_empty_for_side_effect=has_acknowledge_action,
                )
                if task_patch_v2_active:
                    build_task_patch(target_extraction, allowed_keys=None)
                (
                    _target_task_type_updates,
                    duplicate_target_selector_error,
                ) = manager._task_selector_updates_from_extraction(
                    target_extraction,
                )
                duplicate_target_selector_error = (
                    duplicate_target_selector_error
                    or next(
                        (
                            item
                            for item in target_extraction.get("unresolved", [])
                            if "同轮具体任务类型互相冲突" in str(item)
                        ),
                        None,
                    )
                )
                if duplicate_target_selector_error:
                    manager._record_task_type_update_error(
                        new_slots,
                        duplicate_target_selector_error,
                    )
                    if duplicate_target_selector_error not in turn_unresolved:
                        turn_unresolved.append(duplicate_target_selector_error)
                    if duplicate_target_selector_error not in new_unresolved:
                        new_unresolved.append(duplicate_target_selector_error)
                    manager.slot_store.commit_transaction(
                        new_slots,
                        new_unresolved,
                        request_id=request_id,
                        expected_version=expected_version,
                    )
                    return reply_write_without_candidates()
                extraction_res = manager._merge_task_transition_extractions(
                    extraction_res,
                    target_extraction,
                    transition_shared_keys,
                )
                (
                    final_task_type_updates,
                    duplicate_final_selector_error,
                ) = manager._task_selector_updates_from_extraction(extraction_res)
                (
                    _final_pending_task_type_key,
                    final_effective_task_type_key,
                    _final_task_type_change_locked,
                    final_preflight_error,
                ) = manager._resolve_task_type_update_context(
                    final_task_type_updates,
                    new_slots,
                )
                final_preflight_error = (
                    duplicate_final_selector_error or final_preflight_error
                )
                if (
                    final_preflight_error
                    or final_effective_task_type_key != pending_task_type_key
                ):
                    error = final_preflight_error or (
                        "目标任务二次抽取结果与首次任务类型不一致，请只指定一个任务类型。"
                    )
                    manager._record_task_type_update_error(new_slots, error)
                    if error not in turn_unresolved:
                        turn_unresolved.append(error)
                    if error not in new_unresolved:
                        new_unresolved.append(error)
                    manager.slot_store.commit_transaction(
                        new_slots,
                        new_unresolved,
                        request_id=request_id,
                        expected_version=expected_version,
                    )
                    return reply_write_without_candidates()
                effective_task_type_key = final_effective_task_type_key
                (
                    transition_updates,
                    transition_touched_keys,
                ) = manager._normalize_transition_discovery_candidates(
                    extraction_res,
                    pending_task_type_key,
                    transition_state,
                    transition_shared_keys,
                )
                for touched_key in transition_touched_keys:
                    transition_state.pop(touched_key, None)
                transition_state.update(transition_updates)

            extraction_res = manager._scope_confirmed_recommendation(
                extraction_res,
                plan,
                user_message,
            )
            extraction_res = manager._scope_visible_ordinal_selections(
                extraction_res,
                user_message,
                manager.builder.get_required(
                    effective_task_type_key,
                    manager.mode,
                    transition_state,
                ),
            )
            field_defs = manager.builder.get_schema(effective_task_type_key, manager.mode)
            effective_schema_keys = {
                str(field.get("key"))
                for field in field_defs
                if field.get("key")
            }
            # Pre-filter equipment_class candidates compatibility promotion
            raw_candidates = extraction_res.get("slot_candidates", [])
            filtered_candidates = []
            for candidate in raw_candidates:
                if isinstance(candidate, dict) and candidate.get("canonical_key") == "equipment_class":
                    projected = self.project_legacy_equipment_class_candidate(
                        candidate,
                        effective_task_type_key,
                        transition_state,
                    )
                    if projected is not None:
                        filtered_candidates.append(projected)
                else:
                    filtered_candidates.append(candidate)

            projected_candidates, filter_unresolved = manager.slot_filter.filter_candidates(
                task_type_key=effective_task_type_key,
                effective_schema_keys=effective_schema_keys,
                candidates=filtered_candidates,
                task_type_change_locked=task_type_change_locked,
                pending_task_type_key=pending_task_type_key,
                active_task_type_key=task_type_key,
            )
            extraction_res["slot_candidates"] = projected_candidates
            if filter_unresolved:
                extraction_res.setdefault("unresolved", []).extend(filter_unresolved)

            mention_guidance = manager.slot_filter.check_non_template_oilfield_mention(
                task_type_key=effective_task_type_key,
                effective_schema_keys=effective_schema_keys,
                user_message=user_message,
                unresolved_list=extraction_res.get("unresolved", []),
                oilfield_linker=manager.oilfield_linker,
            )
            if mention_guidance:
                extraction_res.setdefault("unresolved", []).append(mention_guidance)

            self.normalize_payload_list_mutations(extraction_res, user_message, new_slots)

            filtered_mutations = []
            for mutation in extraction_res.get("list_mutations", []):
                if (
                    not isinstance(mutation, dict)
                    or not isinstance(mutation.get("field"), str)
                    or not mutation.get("field", "").strip()
                ):
                    filtered_mutations.append(mutation)
                    continue
                mutation_key = mutation["field"].strip()
                if mutation_key in effective_schema_keys:
                    filtered_mutations.append(mutation)
                    continue
                message = (
                    f"列表字段 {mutation_key or '未知字段'} 不属于目标任务 "
                    f"{effective_task_type_key}，未写入。"
                )
                extraction_res.setdefault("unresolved", []).append(message)
            extraction_res["list_mutations"] = filtered_mutations

            evaluation_slots, effective_state = (
                manager._build_post_update_evaluation_context(
                    new_slots,
                    effective_task_type_key,
                    transition_state,
                    extraction_res,
                    transition_state_active=transition_state_active,
                )
            )
            required_field_defs = manager.builder.get_required(
                effective_task_type_key,
                manager.mode,
                effective_state,
            )

            if task_patch_v2_active:
                allowed_stage2 = manager.extractor._allowed_candidate_keys(
                    effective_task_type_key,
                    field_defs,
                )
                patch = build_task_patch(extraction_res, allowed_keys=allowed_stage2)

                if norm_v2_active:
                    current_state_dict = dict(effective_state)

                    def allowed_resolver(fdef: dict[str, Any], state: dict[str, Any]) -> list[Any] | None:
                        return manager.builder._resolve_allowed(
                            fdef,
                            effective_task_type_key,
                            state,
                        )

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
                        "source": manager._source_for_resolution_method(candidate.get("resolution_method"))
                    }
                    stage2_updates[k] = cand_info
                    merged_updates[k] = v
                    merged_updates_meta[k] = cand_info
                list_mutations = extraction_res.get("list_mutations", [])

            payload_mutation_failed = False
            mutation_failure_result = None

            if list_mutations:
                mutation_slots = copy.deepcopy(evaluation_slots)
                for mutation in list_mutations:
                    m_field = mutation.get("field")
                    if m_field == "payload":
                        stage2_updates.pop("payload", None)
                        merged_updates.pop("payload", None)
                        merged_updates_meta.pop("payload", None)

                        mut_res = manager.slot_store.apply_list_mutation(
                            mutation_slots,
                            mutation,
                            required_schema=field_defs,
                            payload_catalog=manager.kb.assets.get("payload_catalog"),
                            allowed_values_resolver=lambda f: manager.builder.resolve_allowed_values(
                                f,
                                effective_task_type_key,
                                effective_state,
                            ),
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
                            stage2_updates["payload"] = merged_updates_meta["payload"]
                        else:
                            payload_mutation_failed = True
                            mutation_failure_result = mut_res
                            payload_slot = new_slots.get("payload")
                            if payload_slot is None:
                                payload_slot = Slot(
                                    "payload",
                                    value_type="list",
                                    status="missing",
                                )
                                new_slots["payload"] = payload_slot
                            payload_slot.raw_value = mutation.get("raw_text")
                            payload_slot.source = mutation.get(
                                "source",
                                "user_input",
                            )
                            payload_slot.confidence = mutation.get(
                                "confidence",
                                0.95,
                            )
                            payload_slot.validation_error = mut_res.get("error")
                            break
                    else:
                        payload_mutation_failed = True
                        mutation_failure_result = {
                            "error": f"不支持的列表字段 '{m_field}'",
                        }
                        break

            raw_stage2 = manager._merge_coordinate_updates(user_message, {k: v.get("value") if isinstance(v, dict) else v for k, v in stage2_updates.items()}, required_field_defs)
            for k, v in raw_stage2.items():
                if k not in stage2_updates:
                    c_info = {"value": v, "raw_value": user_message, "confidence": 1.0, "source": "rule_parser"}
                    stage2_updates[k] = c_info
                    merged_updates_meta[k] = c_info
                merged_updates[k] = v

            if transition_state_active:
                manager._clear_non_inherited_transition_slots(new_slots)
            extracted_oilfield = next(
                (
                    str(c.get("raw_value") or c.get("normalized_value"))
                    for c in (filtered_candidates or [])
                    if isinstance(c, dict) and c.get("canonical_key") in ("oilfield_name", "raw_oilfield_name")
                ),
                None,
            )
            raw_linked = self.link_oilfield_update_in_transaction(
                {k: v.get("value") if isinstance(v, dict) else v for k, v in stage2_updates.items()},
                new_slots,
                user_message=user_message,
                extracted_oilfield=extracted_oilfield,
            )
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

            turn_unresolved = self.filter_robot_selection_unresolved(
                turn_unresolved,
                stage2_updates,
            )
            new_unresolved = self.filter_robot_selection_unresolved(
                new_unresolved,
                stage2_updates,
            )

            _has_conflict = any(s.status == "conflict" for s in new_slots.values())
            has_successful_mutation = any(m.get("field") == "payload" for m in list_mutations)
            if not stage2_updates and not _has_conflict and not turn_unresolved and not has_successful_mutation and (apply_plan is None or not apply_plan.failures):
                if new_unresolved:
                    manager.slot_store.commit_transaction(
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
                        if extracted_cand_v is not None and extracted_cand_v == slot.candidate_value:
                            slot.value = slot.candidate_value
                            slot.status = "valid"
                            slot.candidate_value = None
                            slot.validation_error = None
                            continue

                        if has_explicit_upd and k not in stage2_updates and not any(alias in user_message for alias in slot_name_aliases.get(k, [k])):
                            continue

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
                manager._apply_normalized_plan_in_transaction(
                    apply_plan,
                    new_slots,
                    allow_overwrite=had_task_type_key_at_turn_start,
                    transition_from_task_type_key=(
                        task_type_key
                        if effective_task_type_key != task_type_key
                        else None
                    ),
                    transition_to_task_type_key=(
                        effective_task_type_key
                        if effective_task_type_key != task_type_key
                        else None
                    ),
                )
                task_type_updates = {
                    k: v for k, v in stage2_updates.items()
                    if k in ("task_type", "task_type_key")
                }
                for k, info in task_type_updates.items():
                    val = info.get("value") if isinstance(info, dict) else info
                    if val is not None and val != "":
                        manager._handle_task_type_update_in_transaction(k, val, new_slots)

                applied_keys = {outcome.key for outcome in apply_plan.successful_updates} | {f.key for f in apply_plan.failures}
                extra_updates = {
                    k: v for k, v in stage2_updates.items()
                    if (k.startswith("equipment_") or k not in applied_keys)
                    and k not in ("task_type", "task_type_key")
                }
                if extra_updates:
                    manager._apply_updates_in_transaction(
                        extra_updates,
                        new_slots,
                        allow_overwrite=had_task_type_key_at_turn_start,
                    )
            else:
                manager._apply_updates_in_transaction(
                    stage2_updates,
                    new_slots,
                    allow_overwrite=had_task_type_key_at_turn_start,
                    transition_slots_already_cleared=transition_state_active,
                )

            for key in stage2_updates:
                slot = new_slots.get(key)
                if (
                    slot
                    and slot.status in ("invalid", "conflict")
                    and slot.validation_error
                ):
                    detail = f"{FIELD_LABELS.get(key, key)}：{slot.validation_error}"
                    if detail not in turn_unresolved:
                        turn_unresolved.append(detail)

            if "rov_description" in stage2_updates:
                all_rovs = manager.kb.get_all_rovs()
                proposed_pending_rov = manager.extractor.resolve_rov_description(
                    stage2_updates["rov_description"].get("value") if isinstance(stage2_updates["rov_description"], dict) else str(stage2_updates["rov_description"]),
                    all_rovs,
                    (
                        new_slots["task_type_key"].value
                        if new_slots.get("task_type_key")
                        and new_slots["task_type_key"].status == "valid"
                        else None
                    )
                )
        else:
            if extraction_res.get("unresolved"):
                for u in extraction_res["unresolved"]:
                    if u not in new_unresolved:
                        new_unresolved.append(u)

        # 强制保障：当油田已标准识别且用户未输入显式坐标时，强制使用油田权威基准坐标，消除大模型幻觉坐标
        of_id_slot = new_slots.get("oilfield_entity_id")
        if of_id_slot and of_id_slot.status == "valid" and of_id_slot.value:
            has_explicit_coord = False
            if user_message:
                kw = ["北纬", "南纬", "东经", "西经", "纬度", "经度", "坐标", "lat", "lon", "coord"]
                if any(k in user_message.lower() for k in kw) or re.search(r"[（(]\s*[-+]?\d+(?:\.\d*)?\s*[,，、/]\s*[-+]?\d+(?:\.\d*)?\s*[）)]", user_message):
                    has_explicit_coord = True
            if not has_explicit_coord:
                try:
                    ctx_res = manager.oilfield_linker.evaluate_context(entity_id=str(of_id_slot.value))
                    if ctx_res and ctx_res.default_coordinates:
                        if "oilfield_coordinates" not in new_slots:
                            new_slots["oilfield_coordinates"] = Slot("oilfield_coordinates")
                        new_slots["oilfield_coordinates"].value = ctx_res.default_coordinates
                        new_slots["oilfield_coordinates"].status = "valid"
                        new_slots["oilfield_coordinates"].source = "oilfield_default"
                except Exception:
                    pass

        # Compute proposed mode change without mutating manager.mode before commit
        proposed_mode = manager.mode
        if merged_updates.get("emergency_mode") is True:
            proposed_mode = "emergency"
        elif merged_updates.get("emergency_mode") is False:
            proposed_mode = "normal"

        # Compute changed fields based on proposed updates
        changed_fields = set()
        for k, v in merged_updates.items():
            if k not in ("emergency_mode", "rov_description", "__clear_oilfield_name", "__clear_pending_oilfield") and v is not None and v != "":
                old_val = manager.slot_store.slots.get(k).value if manager.slot_store.slots.get(k) else None
                if old_val != v:
                    changed_fields.add(k)

        proposed_whitelist = {item for item in manager._soft_whitelist if item[0] not in changed_fields}

        # Normalize and validate inside transaction working dict new_slots
        curr_task_type_slot = new_slots.get("task_type_key")
        curr_task_type_key = (
            curr_task_type_slot.value
            if curr_task_type_slot
            and curr_task_type_slot.status == "valid"
            and curr_task_type_slot.value is not None
            else None
        )
        skip_keys = None
        if apply_plan is not None:
            dynamic_keys = manager._dynamic_allowed_schema_keys(field_defs)
            skip_keys = set(apply_plan.normalized_schema_keys) - dynamic_keys
        manager._normalize_and_validate_in_transaction(new_slots, curr_task_type_key, skip_schema_keys=skip_keys)

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
            existing_valid = (
                task_id_slot
                and task_id_slot.status == "valid"
                and task_id_slot.value is not None
                and validate_task_id_for_task_type(str(task_id_slot.value), curr_task_type_key, manager.kb.task_schemas)
                and task_id_slot.source == "auto_reserved"
            )
            if not existing_valid:
                try:
                    preview_id = manager.builder.preview_task_id(curr_task_type_key)
                except IdReservationError:
                    raise
                except Exception as _preview_err:
                    logger.warning(
                        "[DM] non-fatal preview_task_id failed for %s: %s", curr_task_type_key, _preview_err
                    )
                    preview_id = None
                if preview_id is not None:
                    if "task_id" not in new_slots:
                        new_slots["task_id"] = Slot("task_id")
                    new_slots["task_id"].candidate_value = preview_id
                    new_slots["task_id"].status = "candidate"
                    new_slots["task_id"].source = "auto_preview"
                    new_slots["task_id"].value = None
                    new_slots["task_id"].raw_value = None
                    new_slots["task_id"].value_type = "string"
                    new_slots["task_id"].validation_error = None

        proposed_phase = manager.phase

        # Check required missing in working new_slots
        if curr_task_type_key:
            required_schema = manager.builder.get_schema(curr_task_type_key, proposed_mode)
            user_req_schema = [f for f in required_schema if f.get("type") not in ("auto", "fixed")]
            built = manager.slot_store.get_built_json()
            missing = manager.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: manager.builder.resolve_allowed_values(
                    field,
                    curr_task_type_key,
                    manager.task_state,
                ),
            )
            manager._last_missing = missing
            cand_missing = [f for f in required_schema if f.get("type") not in ("auto", "fixed") and (not new_slots.get(f["key"]) or new_slots[f["key"]].status != "valid" or new_slots[f["key"]].value is None)]
        else:
            built = {}
            missing = [{"key": "task_type", "label": "任务类型", "type": "string",
                        "allowed_values": manager.kb.get_all_task_type_values()}]
            manager._last_missing = missing
            cand_missing = missing

        # 维持严格 SSOT：_last_built_json 完全由 manager.slot_store.get_built_json() 派生
        manager._last_built_json = built

        # Auto-generate intent_id inside new_slots BEFORE commit when all required slots are present or when revising a done task
        if old_phase == "done" or (curr_task_type_key and not cand_missing):
            intent_id_slot = new_slots.get("intent_id")
            if old_phase == "done" or not intent_id_slot or intent_id_slot.status != "valid" or not intent_id_slot.value:
                today = get_current_datetime().strftime("%Y%m%d")
                from ..task_intent_builder import get_task_dir
                task_dir = get_task_dir(create=False)
                from ..id_sequence import next_daily_id
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
        manager.slot_store.commit_transaction(
            new_slots,
            new_unresolved,
            request_id=request_id,
            expected_version=expected_version,
        )

        if old_phase == "done":
            manager.final_result = None

        # Apply proposed instance state AFTER successful commit
        manager.mode = proposed_mode
        manager._transition_phase(proposed_phase, reason="task_modified")
        manager._soft_whitelist = proposed_whitelist
        manager._pending_rov_candidates = proposed_pending_rov

        # Re-derive from slot_store (SSOT)
        manager.task_state = manager.slot_store.get_task_state()
        if curr_task_type_key:
            required_schema = manager.builder.get_schema(curr_task_type_key, manager.mode)
            user_req_schema = [f for f in required_schema if f.get("type") not in ("auto", "fixed")]
            built = manager.slot_store.get_built_json()
            missing = manager.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: manager.builder.resolve_allowed_values(
                    field,
                    curr_task_type_key,
                    manager.task_state,
                ),
            )
            manager._last_missing = missing
        else:
            built = {}
            missing = [{"key": "task_type", "label": "任务类型", "type": "string",
                        "allowed_values": manager.kb.get_all_task_type_values()}]
            manager._last_missing = missing
        manager._last_built_json = built

        manager.task_start_now = manager.is_start_time_near_now()

        pending_oilfield_reply = manager._build_pending_oilfield_reply()
        if pending_oilfield_reply:
            manager._transition_phase("collecting", reason="oilfield_clarification_needed")
            manager.conversation_history.append({"role": "user", "content": user_message})
            manager.conversation_history.append({"role": "assistant", "content": pending_oilfield_reply})
            return pending_oilfield_reply

        # 约束检查
        ALL_FIELDS = {"task_type", "start_time", "end_time", "cable_position", "cable_type", "start_point", "end_point",
                      "water_depth", "equipment_family", "equipment_type", "equipment_name", "equipment_unit_id",
                      "payload", "support_vessel", "oilfield_name",
                      "oilfield_coordinates", "wellhead_id"}

        if not missing and manager.phase not in ("blocked_hard", "blocked_soft"):
            constraint_context = manager._run_constraint_check(ALL_FIELDS, purpose="preview")
        elif not missing and manager.phase == "blocked_soft":
            constraint_context = manager._run_constraint_check(changed_fields, purpose="preview")
        elif not missing and manager.phase == "blocked_hard":
            constraint_context = manager._run_constraint_check(ALL_FIELDS, purpose="preview")
        else:
            constraint_context = manager._run_constraint_check(changed_fields, purpose="interactive")

        if (
            not missing
            and manager.phase == "collecting"
            and constraint_context.get("type") == "none"
        ):
            manager._transition_phase(
                "confirming",
                reason="constraints_resolved_and_required_slots_complete",
            )

        # 知识上下文
        knowledge_context = manager.kb.get_context_for_state(manager.task_state)
        accepted_updates = self.get_committed_turn_updates(
            merged_updates,
            state_before_turn,
        )

        # 生成回复
        messages = build_responder_messages(
            task_state=manager.task_state,
            built_json=built,
            missing_fields=missing,
            mode=manager.mode,
            phase=manager.phase,
            knowledge_context=knowledge_context,
            constraint_context=constraint_context,
            conversation_history=manager.conversation_history,
            latest_user_message=user_message,
            ROV2type=manager.kb.ROV2type,
            support_task=manager.kb.get_supported_task(),
            slot_snapshot=manager.slot_store.get_slot_snapshot(),
            accepted_updates=accepted_updates,
            unresolved_inputs=turn_unresolved,
        )
        reply = manager._safe_llm_chat(messages, temperature=0.7, max_tokens=1500, role=ModelRole.TASK_RESPONDER)
        reply = manager._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)
        reply = self.ground_write_reply(
            reply,
            accepted_updates=accepted_updates,
            unresolved_inputs=turn_unresolved,
            missing_fields=missing,
            display_updates=self.get_committed_update_display_values(
                accepted_updates
            ),
        )
        reply = manager._ensure_constraint_details(reply, constraint_context)

        if payload_mutation_failed and mutation_failure_result:
            err_msg = mutation_failure_result.get("error") or "载荷修改操作失败。"
            if accepted_updates:
                reply = f"{reply}\n注意：载荷操作失败：{err_msg}"
            else:
                reply = f"操作失败：{err_msg}"

        manager.conversation_history.append({"role": "user", "content": user_message})
        manager.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    def resolve_pending_oilfield_confirmation(
        self,
        user_message: str,
        request_id: str = "req_default",
        pending_action: str | None = None,
        subject_text: str | None = None,
    ) -> str | None:
        manager = self.manager
        pending_slot = manager.slot_store.slots.get("pending_oilfield_name")
        if not pending_slot or not pending_slot.value or pending_slot.status != "valid":
            return None
        if pending_action == "reject" or (
            pending_action is None and self.user_cancelled_oilfield(user_message)
        ):
            oil_slot = manager.slot_store.slots.get("oilfield_name")
            clear_oil = ("oilfield_name", "oilfield_entity_id") if (not oil_slot or oil_slot.status != "valid") else ()
            manager._commit_internal_slot_values(
                {},
                clear_keys=(
                    "pending_oilfield_name",
                    "pending_oilfield_candidates",
                    *clear_oil,
                ),
            )
            manager._rebuild_cache()
            return "已取消当前待确认油田名称，请提供标准的油田名称（例如：流花11-1油田、陵水17-2油田等），或补充油田坐标。"

        if pending_action not in {"confirm", None}:
            return None
        if pending_action is None and not self.user_confirmed_oilfield(user_message):
            return None

        candidate = self.top_pending_oilfield_candidate(subject_text or user_message)
        if not candidate:
            return self.build_pending_oilfield_reply()

        confirmed_name = candidate.get("name")
        manager._commit_internal_slot_values(
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
        manager._rebuild_cache()
        return f"已确认油田名称为“{confirmed_name}”，我会按这个标准名称继续收集任务信息。"

    def build_pending_oilfield_reply(self) -> str | None:
        manager = self.manager
        task_type_slot = manager.slot_store.slots.get("task_type_key")
        task_type_key = task_type_slot.value if task_type_slot and task_type_slot.status == "valid" else None
        if task_type_key:
            field_defs = manager.builder.get_schema(task_type_key, manager.mode)
            schema_keys = {str(field.get("key")) for field in field_defs if field.get("key")}
            if not manager.slot_filter.supports_oilfield_slots(schema_keys):
                return None

        pending_slot = manager.slot_store.slots.get("pending_oilfield_name")
        raw_name = pending_slot.value if (pending_slot and pending_slot.status == "valid") else None
        oil_slot = manager.slot_store.slots.get("oilfield_name")
        has_oilfield = oil_slot.value if (oil_slot and oil_slot.status == "valid") else None
        if not raw_name or has_oilfield:
            return None

        candidate = self.top_pending_oilfield_candidate()
        if candidate:
            name = candidate.get("name")
            return f"我识别到油田名称“{raw_name}”，疑似为“{name}”。请确认是否采用该标准油田名称？"
        return (
            f"我识别到油田名称“{raw_name}”，但没有匹配到标准油田。"
            "请提供标准的油田名称（例如：流花11-1油田、陵水17-2油田等），或补充油田坐标。"
        )

    def top_pending_oilfield_candidate(self, user_message: str = "") -> dict | None:
        cand_slot = self.manager.slot_store.slots.get("pending_oilfield_candidates")
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

    def user_confirmed_oilfield(self, message: str) -> bool:
        keywords = ["是", "对", "就是", "采用", "确认", "确定", "可以", "好的", "ok", "使用"]
        negations = ["不", "别", "不要", "不是", "取消"]
        msg = message.strip().lower()
        if any(neg in msg for neg in negations):
            return False
        return any(kw in msg for kw in keywords)

    def user_cancelled_oilfield(self, message: str) -> bool:
        msg = message.strip()
        if any(neg in msg for neg in ["不是要取消", "不是取消", "不要取消", "不取消", "别取消", "不要修改", "不修改"]):
            return False
        if any(mod_kw in msg for mod_kw in ["改成", "修改", "水深", "支持船", "管缆", "设备", "载荷"]) and "油田" not in msg:
            return False
        if "任务" in msg or "取消任务" in msg:
            return False

        pending_slot = self.manager.slot_store.slots.get("pending_oilfield_name")
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

    def process_referential_carryover(self, user_message: str) -> str:
        manager = self.manager
        # 四大实体 (Task, Robot, Oilfield, Payload) 全量上下文提取与暂存
        r_ent = manager._extract_robot_entity_from_text(user_message)
        if r_ent:
            manager._last_discussed_robot = r_ent

        o_ent = manager._extract_oilfield_entity_from_text(user_message)
        if o_ent:
            manager._last_discussed_oilfield = o_ent

        p_ent = manager._extract_payload_entity_from_text(user_message)
        if p_ent:
            manager._last_discussed_payload = p_ent

        # 1. 任务类型指代继承
        REFERENTIAL_START_TRIGGERS = (
            "那开始这个任务", "开始这个任务", "就做这个任务", "就安排这个", "开始创建",
            "就这个吧", "安排这个任务", "创建这个任务", "就按这个做", "开始做这个",
            "开启这个任务", "按这个开始", "就选这个任务", "那就这个", "开始这个",
            "开始该任务", "就做这个", "安排这个", "创建这个", "选这个任务", "那开始这个",
            "开始吧", "开始该作业", "就按这个", "开始任务"
        )
        is_referential_start = any(kw in user_message for kw in REFERENTIAL_START_TRIGGERS) or (
            ("开始" in user_message or "做" in user_message or "安排" in user_message or "创建" in user_message)
            and ("这个" in user_message or "任务" in user_message)
            and "不" not in user_message and "别" not in user_message and "取消" not in user_message
        )
        if is_referential_start and not manager.task_state.get("task_type_key") and manager._last_discussed_task_type:
            schema = manager.builder.get_schema(manager._last_discussed_task_type, manager.mode)
            manager.slot_store.init_task_slots(schema)
            manager.slot_store.slots["task_type_key"] = Slot(
                slot_name="task_type_key",
                value=manager._last_discussed_task_type,
                value_type="string",
                status="valid",
                source="user",
            )
            manager._transition_phase("collecting", reason="referential_carryover")
            manager._switch_dialogue_mode("task_collection", source="referential_carryover", reason="继承上一轮讨论的任务类型")
            user_message = f"开启{manager._last_discussed_task_type}任务"

        # 2. 机器人实体指代继承
        ROBOT_TRIGGERS = ("就用这个机器人", "选这个机器人", "用这个设备", "就用这款", "选这个型", "安排这个机", "就用它", "用它", "选这个设备", "用这个机器人", "就这个机器人")
        is_robot_ref = any(kw in user_message for kw in ROBOT_TRIGGERS) or (
            ("用" in user_message or "选" in user_message or "安排" in user_message)
            and ("这个机器人" in user_message or "该设备" in user_message or "这款" in user_message or "它" in user_message)
        )
        if is_robot_ref and manager._last_discussed_robot:
            r_val = manager._last_discussed_robot
            if any(f in r_val for f in ["座", "天鹰", "金牛", "御夫", "奇点", "双子", "凤凰"]):
                manager.slot_store.slots["robot_family"] = Slot(slot_name="robot_family", value=r_val, value_type="string", status="valid", source="user")
            elif any(c in r_val for c in ["观察级", "工作级", "履带式"]):
                manager.slot_store.slots["robot_class"] = Slot(slot_name="robot_class", value=r_val, value_type="string", status="valid", source="user")
            else:
                manager.slot_store.slots["specific_robot_id"] = Slot(slot_name="specific_robot_id", value=r_val, value_type="string", status="valid", source="user")
            if manager.phase not in ("collecting", "confirming"):
                manager._transition_phase("collecting", reason="robot_referential_carryover")
            manager._switch_dialogue_mode("task_collection", source="referential_carryover", reason="继承上一轮讨论的机器人")

        # 3. 油田海域实体指代继承
        OILFIELD_TRIGGERS = ("就去这个油田", "选这个油田", "去这个海域", "选这个区域", "就在这做", "去这里", "就选这个油田", "去这个油田", "在这做")
        is_oilfield_ref = any(kw in user_message for kw in OILFIELD_TRIGGERS) or (
            ("去" in user_message or "选" in user_message or "在" in user_message)
            and ("这个油田" in user_message or "该海域" in user_message or "这个区域" in user_message or "这里" in user_message)
        )
        if is_oilfield_ref and manager._last_discussed_oilfield:
            manager.slot_store.slots["raw_oilfield_name"] = Slot(slot_name="raw_oilfield_name", value=manager._last_discussed_oilfield, value_type="string", status="valid", source="user")
            manager.slot_store.slots["oilfield_name"] = Slot(slot_name="oilfield_name", value=manager._last_discussed_oilfield, value_type="string", status="valid", source="user")
            if manager.phase not in ("collecting", "confirming"):
                manager._transition_phase("collecting", reason="oilfield_referential_carryover")
            manager._switch_dialogue_mode("task_collection", source="referential_carryover", reason="继承上一轮讨论的油田")

        # 4. 载荷工具实体指代继承
        PAYLOAD_TRIGGERS = ("就用这个工具", "带上这个", "选这个载荷", "挂载这个", "就带这个", "就用这个载荷", "装上这个", "用这个工具", "带这个")
        is_payload_ref = any(kw in user_message for kw in PAYLOAD_TRIGGERS) or (
            ("用" in user_message or "带" in user_message or "挂载" in user_message or "装" in user_message)
            and ("这个工具" in user_message or "该载荷" in user_message or "这个传感器" in user_message)
        )
        if is_payload_ref and manager._last_discussed_payload:
            current_payloads = manager.slot_store.slots.get("onboard_payloads").value if manager.slot_store.slots.get("onboard_payloads") else []
            if not isinstance(current_payloads, list):
                current_payloads = [current_payloads] if current_payloads else []
            if manager._last_discussed_payload not in current_payloads:
                current_payloads.append(manager._last_discussed_payload)
            manager.slot_store.slots["onboard_payloads"] = Slot(slot_name="onboard_payloads", value=current_payloads, value_type="list", status="valid", source="user")
            if manager.phase not in ("collecting", "confirming"):
                manager._transition_phase("collecting", reason="payload_referential_carryover")
            manager._switch_dialogue_mode("task_collection", source="referential_carryover", reason="继承上一轮讨论的载荷工具")

        return user_message

    def link_oilfield_update_in_transaction(
        self,
        updates: dict,
        new_slots: dict,
        user_message: str = "",
        extracted_oilfield: str | None = None,
    ) -> dict:
        manager = self.manager
        existing_task_id = new_slots.get("task_id")
        is_task_id_locked = bool(existing_task_id and existing_task_id.status == "valid" and existing_task_id.value)
        current_tt = (
            (new_slots["task_type_key"].value if new_slots.get("task_type_key") and new_slots["task_type_key"].value else None)
            or manager.task_state.get("task_type_key")
        )

        tt_val = updates.get("task_type_key")
        if isinstance(tt_val, dict):
            tt_val = tt_val.get("value")

        if is_task_id_locked and current_tt:
            task_type_key = current_tt
        else:
            task_type_key = tt_val or current_tt
        supports_oilfield = True
        if task_type_key:
            field_defs = manager.builder.get_schema(task_type_key, manager.mode)
            schema_keys = {str(field.get("key")) for field in field_defs if field.get("key")}
            supports_oilfield = manager.slot_filter.supports_oilfield_slots(schema_keys)

        raw_name = (
            updates.get("oilfield_name")
            or updates.get("raw_oilfield_name")
            or extracted_oilfield
        )
        if isinstance(raw_name, dict):
            raw_name = raw_name.get("value")

        if not raw_name and user_message and any(kw in user_message for kw in ("油田", "气田", "海域")):
            m = manager.oilfield_linker.link(user_message)
            if m and m.status == "accepted" and m.standard_name:
                raw_name = m.standard_name

        is_task_switching = bool(current_tt and tt_val and tt_val != current_tt)
        is_locked_switch_rejected = bool(is_task_id_locked and current_tt and tt_val and tt_val != current_tt)
        if (is_locked_switch_rejected and not supports_oilfield) or (is_task_switching and not supports_oilfield and not raw_name):
            linked = dict(updates)
            linked.pop("oilfield_name", None)
            linked.pop("raw_oilfield_name", None)
            for k in (
                "oilfield_name",
                "raw_oilfield_name",
                "oilfield_match_status",
                "oilfield_match_confidence",
                "oilfield_match_evidence",
                "oilfield_match_candidates",
                "oilfield_entity_id",
                "pending_oilfield_name",
                "pending_oilfield_candidates",
            ):
                if k in new_slots:
                    new_slots[k].value = None
                    new_slots[k].status = "missing"
            return linked

        coords = (
            updates.get("oilfield_coordinates")
            or updates.get("start_point")
            or updates.get("cable_position")
            or next(
                (
                    new_slots[key].value
                    for key in (
                        "oilfield_coordinates",
                        "start_point",
                        "cable_position",
                    )
                    if new_slots.get(key)
                    and new_slots[key].status == "valid"
                    and new_slots[key].value is not None
                ),
                None,
            )
        )
        linked = dict(updates)

        # 反向映射：检查坐标是否包含在知识库某油田范围内
        matched_entity_by_coords = manager.oilfield_linker.find_entity_by_coords(coords) if coords else None

        if not raw_name:
            if matched_entity_by_coords:
                # 坐标落入已知油田，反向自动跟随推导油田名称
                raw_name = matched_entity_by_coords.get("name")
            else:
                # 坐标不落入任何已知油田，且用户未提供油田名称：
                # 若坐标被更新且原槽位绑定了知识库油田，解绑原知识库油田，让用户提供名称占位
                existing_entity_id_slot = new_slots.get("oilfield_entity_id")
                if (
                    "oilfield_coordinates" in updates
                    or "start_point" in updates
                    or "cable_position" in updates
                ) and existing_entity_id_slot and existing_entity_id_slot.value is not None:
                    linked.pop("oilfield_name", None)
                    if "oilfield_name" in new_slots:
                        new_slots["oilfield_name"].value = None
                        new_slots["oilfield_name"].status = "missing"
                    if "oilfield_entity_id" in new_slots:
                        new_slots["oilfield_entity_id"].value = None
                        new_slots["oilfield_entity_id"].status = "missing"
                    linked["__clear_oilfield_name"] = True
                return linked

        match = manager.oilfield_linker.link(str(raw_name), coords)

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

        if not supports_oilfield:
            linked.pop("oilfield_name", None)
            linked.pop("raw_oilfield_name", None)
            if "oilfield_name" in new_slots:
                new_slots["oilfield_name"].value = None
                new_slots["oilfield_name"].status = "missing"
            if "oilfield_entity_id" in new_slots:
                new_slots["oilfield_entity_id"].value = None
                new_slots["oilfield_entity_id"].status = "missing"
            return linked

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

            # 自动映射油田坐标（若用户未上报自定义坐标）
            existing_coord_slot = new_slots.get("oilfield_coordinates")
            has_user_custom_coord = (
                "oilfield_coordinates" in updates
                or (
                    existing_coord_slot
                    and existing_coord_slot.status == "valid"
                    and existing_coord_slot.value is not None
                    and getattr(existing_coord_slot, "source", None) != "oilfield_default"
                )
            )
            if not has_user_custom_coord and match.entity_id:
                try:
                    ctx_res = manager.oilfield_linker.evaluate_context(entity_id=match.entity_id)
                    if ctx_res and ctx_res.default_coordinates:
                        default_coord = ctx_res.default_coordinates
                        linked["oilfield_coordinates"] = default_coord
                        if "oilfield_coordinates" not in new_slots:
                            new_slots["oilfield_coordinates"] = Slot("oilfield_coordinates")
                        new_slots["oilfield_coordinates"].value = default_coord
                        new_slots["oilfield_coordinates"].status = "valid"
                        new_slots["oilfield_coordinates"].source = "oilfield_default"
                except Exception:
                    pass
        elif match.raw and not matched_entity_by_coords and not match.candidates:
            # 用户显式输入了自定义名称（如“自设A区”），且坐标不属于知识库任何油田：
            # 允许自定义名称作为 oilfield_name 生效（自定义名称占位），entity_id 为 None
            linked["oilfield_name"] = match.raw
            if "oilfield_name" not in new_slots:
                new_slots["oilfield_name"] = Slot("oilfield_name")
            new_slots["oilfield_name"].value = match.raw
            new_slots["oilfield_name"].status = "valid"
            new_slots["oilfield_name"].source = "user_input"
            if "oilfield_entity_id" not in new_slots:
                new_slots["oilfield_entity_id"] = Slot("oilfield_entity_id")
            new_slots["oilfield_entity_id"].value = None
            new_slots["oilfield_entity_id"].status = "missing"
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

