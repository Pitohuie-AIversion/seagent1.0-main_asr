"""
src/handlers/slot_extraction_pipeline.py - 槽位参数抽取、消歧与候选预处理管道

职责：
1. 多源参数抽取与 Schema 范围过滤；
2. 跨任务类型迁移状态推导与双重抽取冲突消歧；
3. 推荐序数限定与设备候选投影兼容；
4. 载荷列表变异过滤与增量应用；
5. 油田实体链接与坐标权威基准匹配；
6. 机器人选择未决列表去重与用户确认冲突消解。
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
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
)
from ..task_patch import build_task_patch, task_patch_to_legacy_updates
from ..slot_store import Slot
from ..constants import FIELD_LABELS

logger = logging.getLogger("src.dialogue_manager")


@dataclass
class ExtractionPipelineResult:
    """参数抽取管道执行结果数据契约"""
    early_return_reply: Optional[str] = None
    extraction_res: Dict[str, Any] = field(default_factory=dict)
    stage2_updates: Dict[str, Any] = field(default_factory=dict)
    list_mutations: List[Dict[str, Any]] = field(default_factory=list)
    proposed_pending_rov: List[Dict[str, Any]] = field(default_factory=list)
    payload_mutation_failed: bool = False
    mutation_failure_result: Optional[Dict[str, Any]] = None
    apply_plan: Optional[NormalizationApplyPlan] = None
    effective_task_type_key: Optional[str] = None
    field_defs: List[Dict[str, Any]] = field(default_factory=list)


class SlotExtractionPipeline(BaseDialogueHandler):
    """槽位参数抽取、消歧与候选预处理管道"""

    def __init__(self, manager: Any, slot_filling_handler: Any = None):
        super().__init__(manager)
        self.slot_filling_handler = slot_filling_handler

    def can_handle(self, ctx: DialogueContext) -> bool:
        return False

    def handle(self, ctx: DialogueContext) -> HandlerResult:
        return HandlerResult.not_handled()

    def run_pipeline(
        self,
        *,
        ctx: DialogueContext,
        task_type_key: str,
        new_slots: Dict[str, Slot],
        current_state: Dict[str, Any],
        turn_unresolved: List[Any],
        new_unresolved: List[Any],
        merged_updates: Dict[str, Any],
        merged_updates_meta: Dict[str, Any],
        route: Any = None,
        plan: Any = None,
        has_acknowledge_action: bool = False,
        task_patch_v2_active: bool = False,
        norm_v2_active: bool = False,
        expected_version: Optional[int] = None,
        had_task_type_key_at_turn_start: bool = False,
    ) -> ExtractionPipelineResult:
        """执行第二阶段任务参数抽取、模式迁移、实体链接与候选变异预处理。"""
        manager = self.manager
        sf = self.slot_filling_handler or getattr(manager, "slot_handler", None)
        user_message = ctx.user_message
        request_id = ctx.request_id

        def reply_write_without_candidates() -> str:
            if sf and hasattr(sf, "reply_write_without_candidates"):
                return sf.reply_write_without_candidates(
                    user_message=user_message,
                    request_id=request_id,
                    turn_unresolved=turn_unresolved,
                    task_type_key=task_type_key,
                    has_acknowledge_action=has_acknowledge_action,
                )
            return "已接收您的输入。"

        field_defs = manager.builder.get_schema(task_type_key, manager.mode)
        required_field_defs = manager.builder.get_required(task_type_key, manager.mode, current_state)
        extraction_res = manager.extractor.extract_updates(
            user_message,
            current_state,
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
            return ExtractionPipelineResult(early_return_reply=reply_write_without_candidates())

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
                return ExtractionPipelineResult(early_return_reply=reply_write_without_candidates())
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
                return ExtractionPipelineResult(early_return_reply=reply_write_without_candidates())
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
                projected = sf.project_legacy_equipment_class_candidate(
                    candidate,
                    effective_task_type_key,
                    transition_state,
                ) if sf else None
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

        if sf and hasattr(sf, "normalize_payload_list_mutations"):
            sf.normalize_payload_list_mutations(extraction_res, user_message, new_slots)

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

        apply_plan: NormalizationApplyPlan | None = None
        list_mutations: List[Dict[str, Any]] = []
        stage2_updates: Dict[str, Any] = {}

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
            for item in extraction_res.get("unresolved", []):
                if item not in turn_unresolved:
                    turn_unresolved.append(item)
                if item not in new_unresolved:
                    new_unresolved.append(item)

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

        raw_stage2 = manager._merge_coordinate_updates(
            user_message,
            {k: v.get("value") if isinstance(v, dict) else v for k, v in stage2_updates.items()},
            required_field_defs,
        )
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
        raw_linked = sf.link_oilfield_update_in_transaction(
            {k: v.get("value") if isinstance(v, dict) else v for k, v in stage2_updates.items()},
            new_slots,
            user_message=user_message,
            extracted_oilfield=extracted_oilfield,
        ) if sf else {}
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

        if sf and hasattr(sf, "filter_robot_selection_unresolved"):
            turn_unresolved[:] = sf.filter_robot_selection_unresolved(
                turn_unresolved,
                stage2_updates,
            )
            new_unresolved[:] = sf.filter_robot_selection_unresolved(
                new_unresolved,
                stage2_updates,
            )

        _has_conflict = any(s.status == "conflict" for s in new_slots.values())
        has_successful_mutation = any(m.get("field") == "payload" for m in list_mutations)
        if (
            not stage2_updates
            and not _has_conflict
            and not turn_unresolved
            and not has_successful_mutation
            and (apply_plan is None or not apply_plan.failures)
        ):
            if new_unresolved:
                manager.slot_store.commit_transaction(
                    new_slots,
                    new_unresolved,
                    request_id=request_id,
                    expected_version=expected_version,
                )
            return ExtractionPipelineResult(early_return_reply=reply_write_without_candidates())

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

        proposed_pending_rov = list(manager._pending_rov_candidates)
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

        return ExtractionPipelineResult(
            early_return_reply=None,
            extraction_res=extraction_res,
            stage2_updates=stage2_updates,
            list_mutations=list_mutations,
            proposed_pending_rov=proposed_pending_rov,
            payload_mutation_failed=payload_mutation_failed,
            mutation_failure_result=mutation_failure_result,
            apply_plan=apply_plan,
            effective_task_type_key=effective_task_type_key,
            field_defs=field_defs,
        )
