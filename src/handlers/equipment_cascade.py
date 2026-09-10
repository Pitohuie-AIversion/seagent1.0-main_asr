"""
src/handlers/equipment_cascade.py - 设备选型、级联折叠与消歧子模块

职责：
1. 四级设备级联自动收敛 (_auto_collapse_robot_cascade)；
2. 机器人选型、类别/系列/单元事务更新 (_handle_equipment_updates_in_transaction)；
3. 评估态设备更新投影 (_project_equipment_updates_for_evaluation)；
4. 候选解析方法映射与推荐/序数范围限定。
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ..slot_store import Slot, reset_slot_to_missing, BASE_SLOT_TYPES
from ..knowledge_retriever import RobotSelectionDataError
from ..extractor import ParameterExtractor
from ..constants import (
    FIELD_LABELS,
    RECOMMENDATION_FIELD_BY_SUBJECT,
    ROBOT_CASCADE_FIELDS,
    OILFIELD_CONTEXT_FIELDS,
    TASK_TRANSITION_NON_INHERITED_FIELDS,
)
from ..visible_selection_provenance import (
    build_candidate_terms,
    parse_ordinal_reference,
    visible_ordinal_matches_candidate,
)

logger = logging.getLogger("src.dialogue_manager")


class EquipmentCascadeResolver:
    """设备选型、级联折叠与消歧处理器"""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self.manager, name)

    @staticmethod
    def _source_for_resolution_method(resolution_method: str | None) -> str:
        source_map = {
            "canonical_exact": "user_input",
            "alias_exact": "alias_mapping",
            "llm_semantic": "llm_semantic_match",
            "type_normalization": "user_input",
            "assistant_recommendation": "assistant_recommendation",
            "visible_ordinal_selection": "assistant_option_selection",
        }
        return source_map.get(resolution_method, "user_input")

    def _scope_confirmed_recommendation(
        self,
        extraction_result: dict,
        plan: Any,
        user_message: str,
    ) -> dict:
        """接受推荐时只授权同一机器人层级，其他字段仍按正常抽取处理。"""
        if (
            plan is None
            or plan.operation != "WRITE"
            or plan.relation != "recommend"
        ):
            return extraction_result
        if parse_ordinal_reference(user_message) is not None:
            # 编号候选由通用可见来源门禁处理，不能套用机器人单一推荐协议。
            return extraction_result

        result = copy.deepcopy(extraction_result or {})
        result.setdefault("slot_candidates", [])
        result.setdefault("list_mutations", [])
        result.setdefault("unresolved", [])

        target_key = RECOMMENDATION_FIELD_BY_SUBJECT.get(plan.subject_type or "")
        if not target_key:
            return extraction_result
        selected = plan.subject_text
        field_def = self._missing_field_definition(target_key or "")
        allowed_values = list((field_def or {}).get("allowed_values") or [])
        if not allowed_values and target_key:
            task_key = self.task_state.get("task_type_key")
            allowed_values = list(
                self.builder.resolve_allowed_values(
                    field_def or {"key": target_key},
                    task_key,
                    self.task_state,
                )
                or []
            )
        semantic_field_def = dict(field_def or {})
        task_key = self.task_state.get("task_type_key")
        if target_key and task_key:
            required = self.builder.get_required(
                task_key,
                self.mode,
                self.task_state,
            )
            authoritative = next(
                (
                    item
                    for item in required
                    if isinstance(item, dict) and item.get("key") == target_key
                ),
                {},
            )
            if authoritative.get("alias_mappings"):
                semantic_field_def["alias_mappings"] = authoritative.get("alias_mappings")
        semantic_field_def["allowed_values"] = allowed_values
        resolved_selected = (
            ParameterExtractor._match_allowed_value(selected, allowed_values)
            or ParameterExtractor._match_alias_value(selected, semantic_field_def)
            if selected
            else None
        )

        previous_assistant = (
            self.conversation_history[-1].get("content", "")
            if self.conversation_history
            and self.conversation_history[-1].get("role") == "assistant"
            else ""
        )

        candidate_terms = {selected} if selected else set()
        if resolved_selected:
            candidate_terms.add(resolved_selected)
        raw_mention = getattr(plan, "raw_mention", None)
        if raw_mention:
            candidate_terms.add(raw_mention)
        if user_message:
            candidate_terms.add(user_message.strip())

        kb_inst = getattr(self, "kb", None) or getattr(getattr(self, "validator", None), "kb", None)
        alias_map = getattr(kb_inst, "alias_map", {}) if kb_inst else {}
        for k, v in alias_map.items():
            if k in candidate_terms or v in candidate_terms:
                candidate_terms.add(k)
                candidate_terms.add(v)

        if kb_inst and hasattr(kb_inst, "get_aliases_for_term"):
            for term in list(candidate_terms):
                aliases = kb_inst.get_aliases_for_term(term)
                if aliases:
                    candidate_terms.update(aliases)

        in_allowed = not allowed_values or any(t in allowed_values for t in candidate_terms if t)
        in_previous = any(t in previous_assistant for t in candidate_terms if t and len(t) > 1)

        valid_provenance = bool(
            target_key
            and selected
            and in_allowed
            and in_previous
        )

        if not valid_provenance:
            # 来源校验失败时，不清空 extractor 已抽取的 robot cascade candidates，
            # 让后续正常的 _handle_equipment_updates_in_transaction 流程继续处理。
            # 记录 unresolved 以便告知用户但不阻断写入。
            result["unresolved"].append(
                "无法验证所接受的推荐与紧邻上一轮助手建议及当前合法候选一致"
            )
            return result

        # valid_provenance 通过：才清除 extractor 可能产生的其他 robot cascade candidates，
        # 改用推荐协议注入唯一授权候选，防止 extractor 和推荐协议产生冲突写入。
        result["slot_candidates"] = [
            candidate
            for candidate in result["slot_candidates"]
            if candidate.get("canonical_key") not in ROBOT_CASCADE_FIELDS
        ]

        result["slot_candidates"].append(
            {
                "raw_key": "上一轮明确推荐",
                "canonical_key": target_key,
                "raw_value": user_message,
                "normalized_value": resolved_selected or selected,
                "confidence": plan.confidence,
                "resolution_method": "assistant_recommendation",
            }
        )
        return result

    def _scope_visible_ordinal_selections(
        self,
        extraction_result: dict,
        user_message: str,
        required_fields: list[dict],
    ) -> dict:
        """只授权紧邻助手回复中真实可见的编号候选选择。"""
        result = copy.deepcopy(extraction_result or {})
        candidates = result.setdefault("slot_candidates", [])
        result.setdefault("list_mutations", [])
        unresolved = result.setdefault("unresolved", [])
        required_by_key = {
            str(field.get("key")): field
            for field in required_fields or []
            if isinstance(field, dict) and field.get("key")
        }
        previous_assistant = (
            self.conversation_history[-1].get("content", "")
            if self.conversation_history
            and self.conversation_history[-1].get("role") == "assistant"
            else ""
        )

        authorized: list[dict] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            reference = parse_ordinal_reference(candidate.get("raw_value"))
            if reference is None and len(candidates) == 1:
                reference = parse_ordinal_reference(user_message)
            if reference is None:
                authorized.append(candidate)
                continue

            key = str(candidate.get("canonical_key") or "")
            field_definition = required_by_key.get(key) or {}
            allowed_values = list(field_definition.get("allowed_values") or [])
            if not allowed_values:
                # 数字、时间等非枚举值不依赖候选列表顺序，保持原抽取结果。
                authorized.append(candidate)
                continue

            selected = candidate.get("normalized_value")
            selected_values = selected if isinstance(selected, list) else [selected]
            valid_visible_selection = bool(
                len(selected_values) == 1
                and isinstance(selected_values[0], str)
                and selected_values[0] in allowed_values
                and visible_ordinal_matches_candidate(
                    previous_assistant,
                    reference,
                    selected_values[0],
                    build_candidate_terms(field_definition),
                )
            )
            if valid_visible_selection:
                accepted = copy.deepcopy(candidate)
                accepted["resolution_method"] = "visible_ordinal_selection"
                accepted["source"] = "assistant_option_selection"
                authorized.append(accepted)
                continue

            message = (
                f"{FIELD_LABELS.get(key, key or '该字段')}的编号选择“{reference.raw_text}”"
                "无法对应紧邻上一轮助手明确展示的可见候选，未写入"
            )
            if message not in unresolved:
                unresolved.append(message)

        result["slot_candidates"] = authorized
        return result

    def _auto_collapse_robot_cascade(self, new_slots: dict, allow_overwrite: bool = False) -> None:
        """
        [Issue #40] 四级级联自动收敛处理。
        只在 task_type_key 为 valid 时生效。
        规则：
        1. 前置校验：若已有槽位不属于当前 task 的 feasible_domain，作废该槽位及所有下游依赖。
        2. 逐级计算可行候选数 (candidate count)：
           - count == 0: Fail Closed (若槽位状态非 conflict/invalid，设为 invalid)；
           - count == 1: 自动绑定唯一 canonical value (status="valid", source="auto")，继续下一层推导；
           - count > 1: 停止自动收敛，等待用户选择该层。
        """
        task_type_slot = new_slots.get("task_type_key")
        if not task_type_slot or task_type_slot.status != "valid" or not task_type_slot.value:
            return

        task_type_key = str(task_type_slot.value)
        try:
            task_state = {
                key: copy.deepcopy(slot.value)
                for key, slot in new_slots.items()
                if slot.status == "valid" and slot.value is not None
            }
            domain = self.kb.get_feasible_robot_selection_domain(
                task_type_key,
                task_state,
                purpose="auto_collapse",
            )
            # The admission domain contains only task/class/capability and
            # registry hierarchy rules.  It deliberately excludes mutable
            # task facts (depth/payload) and runtime telemetry.  Keeping this
            # second view lets us distinguish a structurally stale selector
            # from an explicit, structurally valid selector that is currently
            # infeasible and must remain visible to the Validator.
            admission_domain = self.kb.get_feasible_robot_selection_domain(
                task_type_key,
            )
        except Exception as exc:
            logger.warning("[DialogueManager] Failed to build feasible robot domain for task '%s': %s", task_type_key, exc)
            if "equipment_class" not in new_slots:
                new_slots["equipment_class"] = Slot("equipment_class")
            slot = new_slots["equipment_class"]
            slot.status = "invalid"
            slot.value = None
            slot.source = "system_candidate_resolution"
            slot.validation_error = str(exc)
            return

        from src.slot_store import (
            invalidate_robot_cascade_dependents,
            reset_slot_to_missing,
        )

        # Auto-bound values express candidate uniqueness, not a durable user
        # preference. Recompute only the automatic suffix below the deepest
        # explicit user choice. Automatic ancestors of an explicit family,
        # variant, or unit must remain in place so that choice can still be
        # validated against its parent chain.
        cascade_keys = (
            "equipment_class",
            "equipment_family",
            "equipment_type",
            "equipment_unit_id",
        )
        for key in cascade_keys:
            slot = new_slots.get(key)
            if (
                slot
                and slot.status == "invalid"
                and slot.source == "system_candidate_resolution"
            ):
                reset_slot_to_missing(
                    slot,
                    source="system_candidate_recompute",
                )

        explicit_indices = [
            index
            for index, key in enumerate(cascade_keys)
            if (
                (slot := new_slots.get(key))
                and slot.status == "valid"
                and slot.value is not None
                and slot.source != "auto"
            )
        ]
        deepest_explicit = max(explicit_indices, default=-1)
        for index, key in enumerate(cascade_keys):
            slot = new_slots.get(key)
            if (
                index > deepest_explicit
                and slot
                and slot.status == "valid"
                and slot.source == "auto"
            ):
                reset_slot_to_missing(slot, source="system_candidate_recompute")

        # ── 1. 前置校验：分离“静态归属”和“当前可行性” ──
        # 任务类型/父层切换后已不在 admission domain 的旧值必须清理，
        # 无论其是否来自用户。但对仍在 admission domain、只因水深/
        # 载荷/运行状态而不在 feasible domain 的明确用户选择，必须保留
        # 给 Validator 产生硬约束；只重算 source=auto 的候选结论。
        def should_invalidate(
            slot: Any,
            *,
            admitted: bool,
            feasible: bool,
            level_index: int,
        ) -> bool:
            if not admitted:
                return True
            return (
                not feasible
                and bool(slot and slot.source == "auto")
                and level_index > deepest_explicit
            )

        def class_node(selection_domain: dict, class_id: str | None) -> dict | None:
            return next(
                (
                    item
                    for item in selection_domain.get("classes", [])
                    if item.get("class_id") == class_id
                ),
                None,
            )

        def family_node(parent: dict | None, family_id: str | None) -> dict | None:
            return next(
                (
                    item
                    for item in (parent or {}).get("families", [])
                    if item.get("family_id") == family_id
                ),
                None,
            )

        def variant_node(parent: dict | None, variant_id: str | None) -> dict | None:
            return next(
                (
                    item
                    for item in (parent or {}).get("variants", [])
                    if item.get("variant_id") == variant_id
                ),
                None,
            )

        cls_slot = new_slots.get("equipment_class")
        if cls_slot and cls_slot.status == "valid" and cls_slot.value:
            resolved_cls = self.kb._resolve_class_key(str(cls_slot.value))
            admitted_cls = class_node(admission_domain, resolved_cls) is not None
            feasible_cls = class_node(domain, resolved_cls) is not None
            if should_invalidate(
                cls_slot,
                admitted=admitted_cls,
                feasible=feasible_cls,
                level_index=0,
            ):
                invalidate_robot_cascade_dependents(new_slots, ["equipment_class"])
                reset_slot_to_missing(cls_slot, source="system_dependency_invalidation")

        fam_slot = new_slots.get("equipment_family")
        if fam_slot and fam_slot.status == "valid" and fam_slot.value:
            cur_cls = new_slots.get("equipment_class")
            cur_cls_id = (
                self.kb._resolve_class_key(str(cur_cls.value))
                if cur_cls and cur_cls.status == "valid" and cur_cls.value
                else None
            )
            resolved_fam_id = self.kb._resolve_family_key(str(fam_slot.value))
            canonical_cls_id = self.kb.get_ancestor_by_level(resolved_fam_id, "class", source_level="family") if resolved_fam_id else None
            if (cur_cls is None or cur_cls.status not in ("valid", "conflict", "candidate") or not cur_cls.value) and canonical_cls_id:
                if "equipment_class" not in new_slots:
                    new_slots["equipment_class"] = Slot("equipment_class")
                cls_s = new_slots["equipment_class"]
                cls_s.value = canonical_cls_id
                cls_s.status = "valid"
                cls_s.source = "auto"
                cls_s.raw_value = canonical_cls_id
                cls_s.confidence = 1.0
                cls_s.candidate_value = None
                cls_s.validation_error = None
                cur_cls_id = canonical_cls_id

            parent_matches = cur_cls_id is None or cur_cls_id == canonical_cls_id
            admitted_fam = family_node(
                class_node(admission_domain, canonical_cls_id),
                resolved_fam_id,
            ) is not None and parent_matches
            feasible_fam = family_node(
                class_node(domain, canonical_cls_id),
                resolved_fam_id,
            ) is not None and parent_matches
            if should_invalidate(
                fam_slot,
                admitted=admitted_fam,
                feasible=feasible_fam,
                level_index=1,
            ):
                invalidate_robot_cascade_dependents(new_slots, ["equipment_family"])
                reset_slot_to_missing(fam_slot, source="system_dependency_invalidation")

        type_slot = new_slots.get("equipment_type")
        if type_slot and type_slot.status == "valid" and type_slot.value:
            cur_cls = new_slots.get("equipment_class")
            cur_fam = new_slots.get("equipment_family")
            cur_cls_id = (
                self.kb._resolve_class_key(str(cur_cls.value))
                if cur_cls and cur_cls.status == "valid" and cur_cls.value
                else None
            )
            cur_fam_id = (
                self.kb._resolve_family_key(str(cur_fam.value))
                if cur_fam and cur_fam.status == "valid" and cur_fam.value
                else None
            )
            try:
                resolved_variant = self.kb._resolve_robot_variant_exact(
                    str(type_slot.value),
                )
            except RobotSelectionDataError:
                resolved_variant = None
            resolved_variant_id = (
                resolved_variant.get("variant_id") if resolved_variant else None
            )
            canonical_cls_id = self.kb.get_ancestor_by_level(resolved_variant_id, "class", source_level="variant") if resolved_variant_id else None
            canonical_fam_id = self.kb.get_ancestor_by_level(resolved_variant_id, "family", source_level="variant") if resolved_variant_id else None

            if (cur_fam is None or cur_fam.status not in ("valid", "conflict", "candidate") or not cur_fam.value) and canonical_fam_id:
                if "equipment_family" not in new_slots:
                    new_slots["equipment_family"] = Slot("equipment_family")
                fam_s = new_slots["equipment_family"]
                fam_cfg = self.kb.robot_fleet.get("robot_families", {}).get(canonical_fam_id, {})
                fam_name = fam_cfg.get("full_name", canonical_fam_id)
                fam_s.value = fam_name
                fam_s.status = "valid"
                fam_s.source = "auto"
                fam_s.raw_value = fam_name
                fam_s.confidence = 1.0
                fam_s.candidate_value = None
                fam_s.validation_error = None
                cur_fam_id = canonical_fam_id

            if (cur_cls is None or cur_cls.status not in ("valid", "conflict", "candidate") or not cur_cls.value) and canonical_cls_id:
                if "equipment_class" not in new_slots:
                    new_slots["equipment_class"] = Slot("equipment_class")
                cls_s = new_slots["equipment_class"]
                cls_s.value = canonical_cls_id
                cls_s.status = "valid"
                cls_s.source = "auto"
                cls_s.raw_value = canonical_cls_id
                cls_s.confidence = 1.0
                cls_s.candidate_value = None
                cls_s.validation_error = None
                cur_cls_id = canonical_cls_id
            parents_match = (
                (cur_cls_id is None or cur_cls_id == canonical_cls_id)
                and (cur_fam_id is None or cur_fam_id == canonical_fam_id)
            )
            admitted_var = variant_node(
                family_node(
                    class_node(admission_domain, canonical_cls_id),
                    canonical_fam_id,
                ),
                resolved_variant_id,
            ) is not None and parents_match
            feasible_var = variant_node(
                family_node(
                    class_node(domain, canonical_cls_id),
                    canonical_fam_id,
                ),
                resolved_variant_id,
            ) is not None and parents_match
            if should_invalidate(
                type_slot,
                admitted=admitted_var,
                feasible=feasible_var,
                level_index=2,
            ):
                invalidate_robot_cascade_dependents(new_slots, ["equipment_type"])
                reset_slot_to_missing(type_slot, source="system_dependency_invalidation")

        unit_slot = new_slots.get("equipment_unit_id")
        if unit_slot and unit_slot.status == "valid" and unit_slot.value:
            cur_cls = new_slots.get("equipment_class")
            cur_fam = new_slots.get("equipment_family")
            cur_type = new_slots.get("equipment_type")
            cur_cls_id = (
                self.kb._resolve_class_key(str(cur_cls.value))
                if cur_cls and cur_cls.status == "valid" and cur_cls.value
                else None
            )
            cur_fam_id = (
                self.kb._resolve_family_key(str(cur_fam.value))
                if cur_fam and cur_fam.status == "valid" and cur_fam.value
                else None
            )
            try:
                resolved_variant = (
                    self.kb._resolve_robot_variant_exact(str(cur_type.value))
                    if cur_type and cur_type.status == "valid" and cur_type.value
                    else None
                )
                resolved_unit = self.kb._resolve_robot_unit_exact(
                    str(unit_slot.value),
                    task_type_key,
                )
            except RobotSelectionDataError:
                resolved_variant = None
                resolved_unit = None
            resolved_variant_id = (
                resolved_variant.get("variant_id") if resolved_variant else None
            )
            resolved_unit_id = (
                resolved_unit.get("unit_id") if resolved_unit else None
            )
            unit_robot = resolved_unit.get("robot") if resolved_unit else None
            canonical_unit_cls_id = (
                unit_robot.get("robot_class") if unit_robot else None
            )
            canonical_unit_fam_id = (
                unit_robot.get("family_id") if unit_robot else None
            )
            canonical_unit_var_id = (
                unit_robot.get("variant_id") if unit_robot else None
            )
            parents_match = (
                (cur_cls_id is None or cur_cls_id == canonical_unit_cls_id)
                and (cur_fam_id is None or cur_fam_id == canonical_unit_fam_id)
                and (
                    resolved_variant_id is None
                    or resolved_variant_id == canonical_unit_var_id
                )
            )
            admission_var = variant_node(
                family_node(
                    class_node(admission_domain, canonical_unit_cls_id),
                    canonical_unit_fam_id,
                ),
                canonical_unit_var_id,
            )
            feasible_var_node = variant_node(
                family_node(
                    class_node(domain, canonical_unit_cls_id),
                    canonical_unit_fam_id,
                ),
                canonical_unit_var_id,
            )
            admitted_unit = any(
                item.get("unit_id") == resolved_unit_id
                for item in (admission_var or {}).get("units", [])
            ) and parents_match
            feasible_unit = any(
                item.get("unit_id") == resolved_unit_id
                for item in (feasible_var_node or {}).get("units", [])
            ) and parents_match
            if should_invalidate(
                unit_slot,
                admitted=admitted_unit,
                feasible=feasible_unit,
                level_index=3,
            ):
                reset_slot_to_missing(unit_slot, source="system_dependency_invalidation")

        # A valid deeper selector has one authoritative registry lineage.
        # Restore/migration callers may legitimately provide only Family,
        # Variant, or Unit, so materialize only missing ancestors before the
        # normal forward collapse.  Explicit ancestors were already checked
        # above and are never overwritten here.
        canonical_state = {
            key: copy.deepcopy(slot.value)
            for key, slot in new_slots.items()
            if key in cascade_keys
            and slot.status == "valid"
            and slot.value is not None
        }
        if canonical_state:
            try:
                canonical_selection = (
                    self.kb.validate_robot_selection_from_task_state(
                        {
                            "task_type_key": task_type_key,
                            **canonical_state,
                        },
                        require_unit=False,
                    )
                )
            except RobotSelectionDataError:
                canonical_selection = None

            if isinstance(canonical_selection, dict):
                canonical_ancestors = (
                    ("equipment_class", "robot_class"),
                    ("equipment_family", "family_name"),
                    ("equipment_type", "equipment_type"),
                )
                for slot_key, result_key in canonical_ancestors:
                    canonical_value = canonical_selection.get(result_key)
                    current_slot = new_slots.get(slot_key)
                    if (
                        isinstance(canonical_value, str)
                        and canonical_value.strip()
                        and (
                            current_slot is None
                            or (
                                current_slot.status == "missing"
                                and current_slot.value is None
                            )
                        )
                    ):
                        if current_slot is None:
                            current_slot = Slot(slot_key)
                            new_slots[slot_key] = current_slot
                        current_slot.value = canonical_value
                        current_slot.status = "valid"
                        current_slot.source = "auto"
                        current_slot.raw_value = canonical_value
                        current_slot.confidence = 1.0
                        current_slot.candidate_value = None
                        current_slot.validation_error = None

        # ── 2. 逐级四级 auto-collapse ──

        # Level 1: equipment_class
        classes = domain["classes"]
        cls_slot = new_slots.get("equipment_class")
        if cls_slot and cls_slot.status in ("invalid", "conflict", "unresolved", "candidate"):
            return
        if not cls_slot or cls_slot.status != "valid" or not cls_slot.value:
            if len(classes) == 0:
                if "equipment_class" not in new_slots:
                    new_slots["equipment_class"] = Slot("equipment_class")
                slot = new_slots["equipment_class"]
                if slot.status not in ("conflict", "invalid"):
                    slot.status = "invalid"
                    slot.value = None
                    slot.source = "system_candidate_resolution"
                    slot.validation_error = f"No feasible robot class for task '{task_type_key}'"
                return
            elif len(classes) == 1:
                cls_info = classes[0]
                if "equipment_class" not in new_slots:
                    new_slots["equipment_class"] = Slot("equipment_class")
                cls_slot = new_slots["equipment_class"]
                cls_slot.value = cls_info["full_name"]
                cls_slot.status = "valid"
                cls_slot.source = "auto"
                cls_slot.raw_value = cls_info["full_name"]
                cls_slot.confidence = 1.0
                cls_slot.validation_error = None
            else:
                # > 1 候选且未指定 -> 停止自动收敛
                return

        # Level 2: equipment_family
        cur_cls_val = str(new_slots["equipment_class"].value)
        cur_cls_id = self.kb._resolve_class_key(cur_cls_val)
        cls_node = next((c for c in classes if c["class_id"] == cur_cls_id), None)
        if not cls_node:
            return

        families = cls_node["families"]
        fam_slot = new_slots.get("equipment_family")
        if fam_slot and fam_slot.status in ("invalid", "conflict", "unresolved", "candidate"):
            return
        if not fam_slot or fam_slot.status != "valid" or not fam_slot.value:
            if len(families) == 0:
                if "equipment_family" not in new_slots:
                    new_slots["equipment_family"] = Slot("equipment_family")
                slot = new_slots["equipment_family"]
                if slot.status not in ("conflict", "invalid"):
                    slot.status = "invalid"
                    slot.value = None
                    slot.source = "system_candidate_resolution"
                    slot.validation_error = f"No feasible robot family under class '{cur_cls_id}' for task '{task_type_key}'"
                return
            elif len(families) == 1:
                fam_info = families[0]
                if "equipment_family" not in new_slots:
                    new_slots["equipment_family"] = Slot("equipment_family")
                fam_slot = new_slots["equipment_family"]
                fam_slot.value = fam_info["full_name"]
                fam_slot.status = "valid"
                fam_slot.source = "auto"
                fam_slot.raw_value = fam_info["full_name"]
                fam_slot.confidence = 1.0
                fam_slot.validation_error = None
            else:
                # > 1 候选且未指定 -> 停止自动收敛
                return

        # Level 3: equipment_type
        cur_fam_val = str(new_slots["equipment_family"].value)
        cur_fam_id = self.kb.resolve_robot_family_id(cur_fam_val, task_type_key)
        fam_node = next((f for f in families if f["family_id"] == cur_fam_id), None)
        if not fam_node:
            return

        variants = fam_node["variants"]
        type_slot = new_slots.get("equipment_type")
        if type_slot and type_slot.status in ("invalid", "conflict", "unresolved", "candidate"):
            return
        if not type_slot or type_slot.status != "valid" or not type_slot.value:
            if len(variants) == 0:
                if "equipment_type" not in new_slots:
                    new_slots["equipment_type"] = Slot("equipment_type")
                slot = new_slots["equipment_type"]
                if slot.status not in ("conflict", "invalid"):
                    slot.status = "invalid"
                    slot.value = None
                    slot.source = "system_candidate_resolution"
                    slot.validation_error = f"No feasible robot variant under family '{cur_fam_id}' for task '{task_type_key}'"
                return
            elif len(variants) == 1:
                var_info = variants[0]
                if "equipment_type" not in new_slots:
                    new_slots["equipment_type"] = Slot("equipment_type")
                type_slot = new_slots["equipment_type"]
                type_slot.value = var_info["full_name"]
                type_slot.status = "valid"
                type_slot.source = "auto"
                type_slot.raw_value = var_info["full_name"]
                type_slot.confidence = 1.0
                type_slot.validation_error = None
            else:
                # > 1 候选且未指定 -> 停止自动收敛
                return

        # Level 4: equipment_unit_id
        cur_type_val = str(new_slots["equipment_type"].value)
        var_node = next((v for v in variants if v["full_name"] == cur_type_val or v["variant_id"] == cur_type_val), None)
        if not var_node:
            return

        units = var_node["units"]
        unit_slot = new_slots.get("equipment_unit_id")
        if unit_slot and unit_slot.status in ("invalid", "conflict", "unresolved", "candidate"):
            return
        if not unit_slot or unit_slot.status != "valid" or not unit_slot.value:
            if len(units) == 0:
                if "equipment_unit_id" not in new_slots:
                    new_slots["equipment_unit_id"] = Slot("equipment_unit_id")
                slot = new_slots["equipment_unit_id"]
                if slot.status not in ("conflict", "invalid"):
                    slot.status = "invalid"
                    slot.value = None
                    slot.source = "system_candidate_resolution"
                    slot.validation_error = f"No fleet units configured for variant '{cur_type_val}'"
                return
            elif len(units) == 1:
                unit_info = units[0]
                if "equipment_unit_id" not in new_slots:
                    new_slots["equipment_unit_id"] = Slot("equipment_unit_id")
                unit_slot = new_slots["equipment_unit_id"]
                unit_slot.value = unit_info["unit_id"]
                unit_slot.status = "valid"
                unit_slot.source = "auto"
                unit_slot.raw_value = unit_info["unit_id"]
                unit_slot.confidence = 1.0
                unit_slot.validation_error = None
            else:
                # > 1 候选且未指定 -> 停止自动收敛
                return

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

        task_type_slot = new_slots.get("task_type_key")
        task_type = (
            task_type_slot.value
            if task_type_slot
            and task_type_slot.status == "valid"
            and task_type_slot.value is not None
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

        if not task_type:
            # 仅在 task_type 为 missing 时尝试根据设备自动补全唯一关联的任务类型，不得强改 candidate 状态
            task_type_slot = new_slots.get("task_type_key")
            if not task_type_slot or task_type_slot.status == "missing" or task_type_slot.value is None:
                inferred_tt_key = None
                for key in ("equipment_unit_id", "equipment_name", "equipment_type", "equipment_family", "equipment_class"):
                    val = equipment_updates.get(key)
                    if val:
                        val_str = str(val.get("value") if isinstance(val, dict) else val)
                        resolved_unit = self.kb.resolve_robot_unit(val_str, None)
                        resolved_family = self.kb.resolve_robot_family(val_str, None) if not resolved_unit else None
                        cls_id = None
                        family_id = None
                        if resolved_unit:
                            robot = resolved_unit.get("robot") or {}
                            cls_id = robot.get("robot_class")
                            family_id = robot.get("family_id")
                        elif resolved_family:
                            cls_id = resolved_family.get("robot_class")
                            family_id = resolved_family.get("family_id")
                        elif key == "equipment_class":
                            cls_id = self.kb._resolve_class_key(val_str)

                        if cls_id or family_id:
                            families_cfg = self.kb.robot_fleet.get("robot_families", {}) or {}
                            matched = [
                                t_k
                                for t_k, t_c in self.kb.task_schemas.get("task_templates", {}).items()
                                if any(
                                    set(t_c.get("required_capabilities", []) or []).issubset(
                                        set(f_cfg.get("capabilities", []) or [])
                                    )
                                    for f_id, f_cfg in families_cfg.items()
                                    if isinstance(f_cfg, dict)
                                    and (
                                        (family_id and f_id == family_id)
                                        or (
                                            not family_id
                                            and cls_id
                                            and f_cfg.get("robot_class") == cls_id
                                        )
                                    )
                                )
                            ]
                            if len(matched) == 1:
                                inferred_tt_key = matched[0]
                                break

                if inferred_tt_key:
                    self._handle_task_type_update_in_transaction("task_type_key", inferred_tt_key, new_slots)
                    task_type_slot = new_slots.get("task_type_key")
                    task_type = (
                        task_type_slot.value
                        if task_type_slot and task_type_slot.status == "valid"
                        else None
                    )

        if not task_type:
            for target_key in (
                "equipment_unit_id",
                "equipment_name",
                "equipment_type",
                "equipment_family",
                "equipment_class",
            ):
                if target_key in equipment_updates:
                    _rollback_and_fail(
                        target_key,
                        equipment_updates[target_key],
                        "Task type must be confirmed before robot selection",
                    )
                    return

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
                    active_cls_id = self.kb._resolve_class_key(str(active_cls_slot.value)) or str(active_cls_slot.value)
                    if res_cls_id and active_cls_id and res_cls_id != active_cls_id:
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
                            if res_fam.get("family_id") and active_fam_id and res_fam.get("family_id") != active_fam_id:
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
                    if class_slot and class_slot.status == "valid"
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
                sandbox_slots["equipment_class"].status = "valid"
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
                active_class_val = (
                    active_class_slot.value
                    if active_class_slot and active_class_slot.status == "valid"
                    else None
                )
                active_class = self.kb._resolve_class_key(str(active_class_val)) if active_class_val else None
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
                    sandbox_slots["equipment_class"].status = "valid"
                    family_slot = sandbox_slots.get("equipment_family")
                    current_family_id = (
                        self.kb.resolve_robot_family_id(str(family_slot.value), task_type)
                        if family_slot and family_slot.value and family_slot.status == "valid"
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
                    sandbox_slots["equipment_family"].status = "valid"
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
                if active_fam_slot and active_fam_slot.status == "valid"
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

            if selected_variant and task_type and not self.kb.robot_matches_task(selected_variant, task_type):
                selected_variant = None

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
                    if old_variant_slot and old_variant_slot.status == "valid"
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
                sandbox_slots["equipment_class"].status = "valid"
                self._apply_slot_update_in_transaction(
                    "equipment_family",
                    fam_full,
                    sandbox_slots,
                    allow_overwrite,
                )
                sandbox_slots["equipment_family"].status = "valid"
                self._apply_slot_update_in_transaction(
                    "equipment_type",
                    new_variant_val,
                    sandbox_slots,
                    allow_overwrite,
                )
                sandbox_slots["equipment_type"].status = "valid"
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
            variant_context = (
                selected_variant.get("full_name")
                if selected_variant
                else None
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

            if not resolved_unit:
                unit_raw = None
                raw_item = updates.get("equipment_unit_id") or updates.get("equipment_name")
                if isinstance(raw_item, dict):
                    unit_raw = raw_item.get("raw_value")
                if unit_raw and isinstance(unit_raw, str) and unit_raw != str(unit_update):
                    resolved_unit = self.kb.resolve_robot_unit(
                        str(unit_raw),
                        task_type,
                        str(variant_context) if variant_context else None,
                    )
                    if not resolved_unit and not task_type:
                        resolved_unit = self.kb.resolve_robot_unit(
                            str(unit_raw),
                            None,
                        )

            if not resolved_unit and hasattr(self.kb, "resolve_robot_unit_from_text") and getattr(self, "conversation_history", None):
                last_user_msg = next((m.get("content") for m in reversed(self.conversation_history) if isinstance(m, dict) and m.get("role") == "user"), "")
                if last_user_msg:
                    resolved_unit = self.kb.resolve_robot_unit_from_text(last_user_msg, task_type)

            if resolved_unit and task_type and not self.kb.robot_matches_task(resolved_unit.get("robot"), task_type):
                resolved_unit = None

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
                sandbox_slots["equipment_class"].status = "valid"
                self._apply_slot_update_in_transaction(
                    "equipment_family",
                    unit_fam_full,
                    sandbox_slots,
                    allow_overwrite,
                )
                sandbox_slots["equipment_family"].status = "valid"
                self._apply_slot_update_in_transaction(
                    "equipment_type",
                    unit_variant_full,
                    sandbox_slots,
                    allow_overwrite,
                )
                sandbox_slots["equipment_type"].status = "valid"
                self._apply_slot_update_in_transaction(
                    "equipment_unit_id",
                    resolved_unit["unit_id"],
                    sandbox_slots,
                    allow_overwrite,
                )
                sandbox_slots["equipment_unit_id"].status = "valid"
                if "equipment_name" in sandbox_slots:
                    self._apply_slot_update_in_transaction(
                        "equipment_name",
                        resolved_unit.get("display_name", resolved_unit["unit_id"]),
                        sandbox_slots,
                        allow_overwrite,
                    )
                    sandbox_slots["equipment_name"].status = "valid"
            else:
                _rollback_and_fail(
                    "equipment_unit_id",
                    unit_update,
                    f"Unknown fleet unit '{unit_update}'",
                )
                return

        # 执行层级依赖失效
        robot_cascade_preserve_keys = set(equipment_updates.keys())
        if "payload" in updates:
            robot_cascade_preserve_keys.add("payload")
        if changed_parents:
            invalidate_robot_cascade_dependents(
                sandbox_slots,
                changed_parents,
                preserve_keys=robot_cascade_preserve_keys,
            )
            unit_slot = sandbox_slots.get("equipment_unit_id")
            if not (
                unit_slot
                and unit_slot.status == "valid"
                and unit_slot.value not in (None, "")
            ):
                self.slot_store.validation_result = None

        # 事务生效
        for k in EQUIPMENT_KEYS:
            if k in sandbox_slots:
                new_slots[k] = sandbox_slots[k]
        if changed_parents and "payload" in sandbox_slots:
            new_slots["payload"] = sandbox_slots["payload"]

        # 若当前 task_type_key 为空，且设备类别已推导确定，自动推导唯一的关联任务类型
        cur_tt_slot = new_slots.get("task_type_key")
        if not cur_tt_slot or cur_tt_slot.status != "valid" or not cur_tt_slot.value:
            eq_cls_slot = new_slots.get("equipment_class")
            if eq_cls_slot and eq_cls_slot.status == "valid" and eq_cls_slot.value:
                cls_id = self.kb._resolve_class_key(str(eq_cls_slot.value))
                if cls_id:
                    families_cfg = self.kb.robot_fleet.get("robot_families", {}) or {}
                    matched_templates = [
                        t_key
                        for t_key, t_cfg in self.kb.task_schemas.get("task_templates", {}).items()
                        if any(
                            set(t_cfg.get("required_capabilities", []) or []).issubset(
                                set(f_cfg.get("capabilities", []) or [])
                            )
                            for f_cfg in families_cfg.values()
                            if isinstance(f_cfg, dict) and f_cfg.get("robot_class") == cls_id
                        )
                    ]
                    if len(matched_templates) == 1:
                        inferred_key = matched_templates[0]
                        self._handle_task_type_update_in_transaction("task_type_key", inferred_key, new_slots)


    def _project_equipment_updates_for_evaluation(
        self,
        updates: dict,
        sandbox_slots: dict,
        task_type_key: str,
    ) -> bool:
        """Purely project a canonical robot lineage for dynamic-value lookup.

        This is intentionally not the mutating equipment handler.  It invokes
        the same KnowledgeBase static tuple authority on an isolated state and
        materializes only its canonical result, keeping the real equipment
        handler as the single transaction commit path.
        """
        selection_state: dict[str, Any] = {"task_type_key": task_type_key}

        def value_of(value: Any) -> Any:
            return value.get("value") if isinstance(value, dict) else value

        for key in ROBOT_CASCADE_FIELDS:
            value = value_of(updates.get(key))
            if value not in (None, ""):
                selection_state[key] = value
        if "equipment_unit_id" not in selection_state:
            equipment_name = value_of(updates.get("equipment_name"))
            if equipment_name not in (None, ""):
                selection_state["equipment_unit_id"] = equipment_name

        if len(selection_state) == 1:
            return False
        try:
            canonical = self.kb.validate_robot_selection_from_task_state(
                selection_state,
                require_unit=False,
            )
        except (RobotSelectionDataError, TypeError, ValueError):
            return False
        if not isinstance(canonical, dict):
            return False

        # The canonical result describes the deepest explicitly selected
        # level.  Any older descendant not present in that result belongs to
        # the previous selection and must not constrain dynamic values in this
        # evaluation sandbox (for example, Class-only AUV after an old ROV).
        for key in (*ROBOT_CASCADE_FIELDS, "equipment_name"):
            slot = sandbox_slots.get(key)
            if slot is not None:
                reset_slot_to_missing(
                    slot,
                    source="evaluation_projection",
                )

        canonical_values = {
            "equipment_class": canonical.get("robot_class"),
            "equipment_family": canonical.get("family_name"),
            "equipment_type": canonical.get("equipment_type")
            or canonical.get("variant_name"),
            "equipment_unit_id": canonical.get("unit_id"),
            "equipment_name": canonical.get("unit_display_name"),
        }
        for key, value in canonical_values.items():
            if value is None or value == "":
                continue
            slot = sandbox_slots.get(key)
            if slot is None:
                slot = Slot(key, value_type=BASE_SLOT_TYPES.get(key, "string"))
                sandbox_slots[key] = slot
            slot.value = copy.deepcopy(value)
            slot.status = "valid"
            slot.candidate_value = None
            slot.validation_error = None

        # A Class- or Family-only selector can still have exactly one feasible
        # descendant chain.  Collapse that chain now, while still operating on
        # the isolated sandbox, so payload and other dynamic catalogs are
        # evaluated against the same final Variant that the real transaction
        # will auto-bind later.
        self._auto_collapse_robot_cascade(sandbox_slots)
        return True

    # 别名兼容
    source_for_resolution_method = _source_for_resolution_method
    scope_confirmed_recommendation = _scope_confirmed_recommendation
    scope_visible_ordinal_selections = _scope_visible_ordinal_selections
    auto_collapse_robot_cascade = _auto_collapse_robot_cascade
    handle_equipment_updates_in_transaction = _handle_equipment_updates_in_transaction
    project_equipment_updates_for_evaluation = _project_equipment_updates_for_evaluation
