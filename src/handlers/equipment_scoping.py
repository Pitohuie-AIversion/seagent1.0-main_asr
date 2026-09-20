"""
src/handlers/equipment_scoping.py - 机器人推荐与界面可见选项范围限定处理器

职责：
1. 候选解析方法映射 (_source_for_resolution_method / source_for_resolution_method)；
2. 上一轮助手推荐授权范围限定 (_scope_confirmed_recommendation)；
3. 界面序数选择与可见选项溯源范围限定 (_scope_visible_ordinal_selections)。
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from ..constants import (
    FIELD_LABELS,
    RECOMMENDATION_FIELD_BY_SUBJECT,
    ROBOT_CASCADE_FIELDS,
)
from src.extraction.extractor import ParameterExtractor
from src.slots.visible_selection_provenance import (
    build_candidate_terms,
    parse_ordinal_reference,
    visible_ordinal_matches_candidate,
)

logger = logging.getLogger("src.dialogue_manager")



class EquipmentScopingHandler:
    """机器人推荐与界面可见选项范围限定处理器"""

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    def __getattr__(self, name: str) -> Any:
        if "manager" in self.__dict__ and self.manager is not None:
            return getattr(self.manager, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

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
        plan_op = plan.get("operation") if isinstance(plan, dict) else getattr(plan, "operation", None)
        plan_rel = plan.get("relation") if isinstance(plan, dict) else getattr(plan, "relation", None)
        if (
            plan is None
            or plan_op != "WRITE"
            or plan_rel != "recommend"
        ):
            return extraction_result
        if parse_ordinal_reference(user_message) is not None:
            # 编号候选由通用可见来源门禁处理，不能套用机器人单一推荐协议。
            return extraction_result

        result = copy.deepcopy(extraction_result or {})
        result.setdefault("slot_candidates", [])
        result.setdefault("list_mutations", [])
        result.setdefault("unresolved", [])

        plan_subj_type = plan.get("subject_type") if isinstance(plan, dict) else getattr(plan, "subject_type", "")
        target_key = RECOMMENDATION_FIELD_BY_SUBJECT.get(plan_subj_type or "")
        if not target_key:
            return extraction_result
        selected = plan.get("subject_text") if isinstance(plan, dict) else getattr(plan, "subject_text", None)

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
        raw_mention = plan.get("raw_mention") if isinstance(plan, dict) else getattr(plan, "raw_mention", None)
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

        plan_conf = plan.get("confidence", 1.0) if isinstance(plan, dict) else getattr(plan, "confidence", 1.0)
        result["slot_candidates"].append(
            {
                "raw_key": "上一轮明确推荐",
                "canonical_key": target_key,
                "raw_value": user_message,
                "normalized_value": resolved_selected or selected,
                "confidence": plan_conf,
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

    # 别名兼容
    source_for_resolution_method = _source_for_resolution_method
    scope_confirmed_recommendation = _scope_confirmed_recommendation
    scope_visible_ordinal_selections = _scope_visible_ordinal_selections
