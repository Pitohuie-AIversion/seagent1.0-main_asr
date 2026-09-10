"""
src/handlers/task_transition.py - 任务类型切换与跨任务上下文迁移子模块

职责：
1. 任务类型目标与共享字段解析 (_resolve_task_type_target, _task_transition_shared_field_keys)；
2. 任务切换沙箱状态构建 (_build_task_transition_state, _build_post_update_evaluation_context)；
3. 跨任务槽位清理与候选合并 (_clear_non_inherited_transition_slots, _merge_task_transition_extractions)；
4. 任务切换更新事务执行 (_handle_task_type_update_in_transaction, _handle_rov_description_in_transaction)。
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ..slot_store import Slot, reset_slot_to_missing, BASE_SLOT_TYPES
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


class TaskTransitionManager:
    """任务类型切换与跨任务上下文迁移处理器"""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self.manager, name)

    def _resolve_task_type_target(self, key: str, value: object) -> str | None:
        """Resolve either task selector field through one authoritative rule."""
        if not isinstance(value, str):
            return None
        task_type_map = self.kb.get_task_type_map()
        templates = self.kb.task_schemas.get("task_templates", {})

        if value in task_type_map:
            return task_type_map[value]
        if key == "task_type_key" and value in templates:
            return str(value)
        return None

    def _task_transition_shared_field_keys(
        self,
        current_task_type_key: str,
        target_task_type_key: str,
    ) -> set[str]:
        """Return schema facts that are safe to inherit across a task switch.

        A matching key is not enough: both schemas must declare the same field
        contract.  Task-specific payload and robot selectors are always
        re-evaluated in the target domain even though their YAML keys match.
        """
        current_by_key = {
            str(field.get("key")): field
            for field in self.builder.get_schema(current_task_type_key, self.mode)
            if isinstance(field, dict) and field.get("key")
        }
        target_by_key = {
            str(field.get("key")): field
            for field in self.builder.get_schema(target_task_type_key, self.mode)
            if isinstance(field, dict) and field.get("key")
        }
        ignored_contract_keys = {"label"}

        def comparable(field: dict) -> dict:
            return {
                key: copy.deepcopy(value)
                for key, value in field.items()
                if key not in ignored_contract_keys
            }

        return {
            key
            for key in current_by_key.keys() & target_by_key.keys()
            if key not in TASK_TRANSITION_NON_INHERITED_FIELDS
            and current_by_key[key].get("type") not in {"auto", "fixed"}
            and target_by_key[key].get("type") not in {"auto", "fixed"}
            and comparable(current_by_key[key]) == comparable(target_by_key[key])
        }

    def _build_task_transition_state(
        self,
        current_state: dict,
        current_task_type_key: str,
        target_task_type_key: str,
    ) -> dict:
        """Project current facts into one clean target-task evaluation view."""
        shared_keys = self._task_transition_shared_field_keys(
            current_task_type_key,
            target_task_type_key,
        )
        target_state = {
            key: copy.deepcopy(value)
            for key, value in current_state.items()
            if key in shared_keys and value is not None
        }
        target_state["task_type_key"] = target_task_type_key
        return target_state

    def _build_post_update_evaluation_context(
        self,
        new_slots: dict,
        target_task_type_key: str,
        base_state: dict,
        extraction: dict,
        *,
        transition_state_active: bool,
    ) -> tuple[dict, dict]:
        """Project same-turn robot selectors into an isolated evaluation view.

        Dynamic schema values (notably payload) depend on the selected Variant.
        Reuse the KnowledgeBase static tuple authority to derive a canonical
        Class -> Family -> Variant -> Unit view before normalizing dependent
        fields.  The real transaction and its specialized equipment handler
        are not mutated or invoked here.
        """
        sandbox_slots = copy.deepcopy(new_slots)
        if transition_state_active:
            self._clear_non_inherited_transition_slots(sandbox_slots)

        task_slot = sandbox_slots.get("task_type_key")
        if task_slot is None:
            task_slot = Slot("task_type_key", value_type="string")
            sandbox_slots["task_type_key"] = task_slot
        task_slot.value = target_task_type_key
        task_slot.status = "valid"
        task_slot.candidate_value = None
        task_slot.validation_error = None

        # The transition discovery pass may already have normalized safe
        # shared facts (for example water_depth/start_time) that are not yet
        # committed to ``new_slots``.  Materialize those facts only inside the
        # evaluation sandbox so the robot feasibility domain sees the same
        # target-task context that dependent fields will use.
        for key, value in base_state.items():
            if key == "task_type_key" or value is None:
                continue
            slot = sandbox_slots.get(key)
            if slot is None:
                slot = Slot(key, value_type=BASE_SLOT_TYPES.get(key, "string"))
                sandbox_slots[key] = slot
            if slot.status not in ("missing", "valid"):
                continue
            slot.value = copy.deepcopy(value)
            slot.status = "valid"
            slot.candidate_value = None
            slot.validation_error = None

        equipment_updates: dict[str, dict[str, Any]] = {}
        for candidate in extraction.get("slot_candidates", []):
            if not isinstance(candidate, dict):
                continue
            key = str(candidate.get("canonical_key") or "")
            if key == "equipment_model":
                key = "equipment_type"
            if key not in ROBOT_CASCADE_FIELDS and key != "equipment_name":
                continue
            value = candidate.get("normalized_value")
            if value is None or value == "":
                continue
            equipment_updates[key] = {
                "value": value,
                "raw_value": candidate.get("raw_value", value),
                "confidence": candidate.get("confidence", 1.0),
                "source": self._source_for_resolution_method(
                    candidate.get("resolution_method")
                ),
            }

        projected_equipment = False
        if equipment_updates:
            projected_equipment = self._project_equipment_updates_for_evaluation(
                equipment_updates,
                sandbox_slots,
                target_task_type_key,
            )

        effective_state = {
            key: copy.deepcopy(slot.value)
            for key, slot in sandbox_slots.items()
            if slot.status == "valid" and slot.value is not None
        }
        # Safe shared values extracted in the discovery pass have not yet been
        # committed to sandbox_slots; overlay them for target-schema catalogs.
        effective_state.update(copy.deepcopy(base_state))
        effective_state["task_type_key"] = target_task_type_key

        if equipment_updates:
            # A failed same-turn robot selector must not silently fall back to
            # the previous robot when evaluating its dependent payload.
            for key in (*ROBOT_CASCADE_FIELDS, "equipment_name"):
                effective_state.pop(key, None)
            if projected_equipment:
                for key in (*ROBOT_CASCADE_FIELDS, "equipment_name"):
                    slot = sandbox_slots.get(key)
                    if slot and slot.status == "valid" and slot.value is not None:
                        effective_state[key] = copy.deepcopy(slot.value)

        return sandbox_slots, effective_state

    @staticmethod
    def _dynamic_allowed_schema_keys(field_defs: list[dict]) -> set[str]:
        """Fields whose allowed values depend on the finalized robot tuple."""
        dynamic_refs = {
            "supported_payloads",
            "onboard_payloads",
            "all_payloads",
        }
        result: set[str] = set()
        for field in field_defs:
            if not isinstance(field, dict) or not field.get("key"):
                continue
            ref = str(field.get("allowed_values_ref") or "")
            if ref in dynamic_refs or ref.startswith("payload_options."):
                result.add(str(field["key"]))
        return result

    @staticmethod
    def _clear_non_inherited_transition_slots(new_slots: dict) -> None:
        """Remove stale task-specific facts before target-task updates apply."""
        for key in TASK_TRANSITION_NON_INHERITED_FIELDS:
            slot = new_slots.get(key)
            if slot is not None:
                reset_slot_to_missing(
                    slot,
                    source="task_type_change_invalidation",
                )

    def _normalize_transition_discovery_candidates(
        self,
        extraction: dict,
        target_task_type_key: str,
        base_state: dict,
        shared_keys: set[str],
    ) -> tuple[dict, set[str]]:
        """Normalize safe first-pass siblings before building target catalogs.

        Touched keys are returned separately so an invalid new value can evict
        a stale inherited fact rather than leaving it to constrain the target
        robot domain.
        """
        raw_updates: dict[str, object] = {}
        for candidate in extraction.get("slot_candidates", []):
            if not isinstance(candidate, dict):
                continue
            key = str(candidate.get("canonical_key") or "")
            if key in shared_keys:
                raw_updates[key] = candidate.get("normalized_value")
        if not raw_updates:
            return {}, set()

        normalized = self.normalizer.normalize_updates_with_failures(
            raw_updates,
            self.builder.get_schema(target_task_type_key, self.mode),
            base_state,
            lambda field_def, state: self.builder._resolve_allowed(
                field_def,
                target_task_type_key,
                state,
            ),
        )
        return normalized.normalized_updates, set(raw_updates)

    @staticmethod
    def _merge_task_transition_extractions(
        initial: dict,
        target: dict,
        shared_keys: set[str] | None = None,
    ) -> dict:
        """Merge a selector-discovery pass with a target-schema pass.

        Target-schema ordinary fields and list mutations are authoritative.
        Task selectors from both passes are retained so the shared preflight
        can reject category or concrete-operation disagreement instead of
        silently choosing whichever extraction happened last.
        """
        initial = initial if isinstance(initial, dict) else {}
        target = target if isinstance(target, dict) else {}
        selector_keys = {"task_type", "task_type_key"}
        initial_candidates = [
            copy.deepcopy(candidate)
            for candidate in initial.get("slot_candidates", [])
            if isinstance(candidate, dict)
        ]
        target_candidates = [
            copy.deepcopy(candidate)
            for candidate in target.get("slot_candidates", [])
            if isinstance(candidate, dict)
        ]
        initial_selectors = [
            candidate
            for candidate in initial_candidates
            if candidate.get("canonical_key") in selector_keys
        ]
        target_selectors = [
            candidate
            for candidate in target_candidates
            if candidate.get("canonical_key") in selector_keys
        ]
        target_ordinary = [
            candidate
            for candidate in target_candidates
            if candidate.get("canonical_key") not in selector_keys
        ]
        target_ordinary_keys = {
            str(candidate.get("canonical_key"))
            for candidate in target_ordinary
        }
        candidates = [
            candidate
            for candidate in initial_candidates
            if candidate.get("canonical_key") in (shared_keys or set())
            and str(candidate.get("canonical_key")) not in target_ordinary_keys
        ]
        candidates.extend(target_ordinary)
        for selector_key in ("task_type", "task_type_key"):
            first = [
                candidate
                for candidate in initial_selectors
                if candidate.get("canonical_key") == selector_key
            ]
            second = [
                candidate
                for candidate in target_selectors
                if candidate.get("canonical_key") == selector_key
            ]
            def unique_values(items: list[dict]) -> list[object]:
                values: list[object] = []
                for item in items:
                    value = item.get("normalized_value")
                    if not any(value == existing for existing in values):
                        values.append(value)
                return values

            first_values = unique_values(first)
            second_values = unique_values(second)
            if first and second and first_values != second_values:
                candidates.extend([*first, *second])
            else:
                candidates.extend(second or first)
        return {
            "slot_candidates": candidates,
            "list_mutations": copy.deepcopy(
                target.get("list_mutations", [])
                if isinstance(target.get("list_mutations", []), list)
                else []
            ),
            "unresolved": copy.deepcopy(
                target.get("unresolved", [])
                if isinstance(target.get("unresolved", []), list)
                else []
            ),
        }

    @staticmethod
    def _task_selector_updates_from_extraction(
        extraction: dict,
    ) -> tuple[dict[str, object], str | None]:
        """Return task selectors, rejecting duplicate values in one result."""
        updates: dict[str, object] = {}
        for candidate in extraction.get("slot_candidates", []):
            if not isinstance(candidate, dict):
                continue
            key = candidate.get("canonical_key")
            if key not in {"task_type", "task_type_key"}:
                continue
            value = candidate.get("normalized_value")
            if not isinstance(value, str) or not value.strip():
                return {}, "任务类型字段必须是非空字符串，请重新指定任务类型。"
            value = value.strip()
            if key in updates and updates[key] != value:
                return {}, "同轮具体任务类型互相冲突，请只指定一种任务操作。"
            updates[str(key)] = value
        return updates, None

    def _resolve_task_type_update_context(
        self,
        updates: dict,
        new_slots: dict,
    ) -> tuple[str | None, str | None, bool, str | None]:
        """Preflight task selectors and choose the schema for sibling updates.

        A reserved task ID locks the category, but does not turn ordinary
        sibling updates into an all-or-nothing operation. In that case sibling
        values are evaluated against the current task schema. Conflicting task
        selectors are different: there is no authoritative target schema, so
        the turn must stop before any field is mutated.
        """
        task_type_slot = new_slots.get("task_type_key")
        current_task_type_key = (
            task_type_slot.value
            if task_type_slot
            and task_type_slot.status == "valid"
            and task_type_slot.value is not None
            else None
        )
        resolved_targets = {
            target
            for key in ("task_type", "task_type_key")
            if key in updates
            for target in [self._resolve_task_type_target(key, updates[key])]
            if target is not None
        }
        if len(resolved_targets) > 1:
            logger.warning(
                "[DialogueManager] Rejecting conflicting task type targets: %s",
                sorted(resolved_targets),
            )
            return (
                None,
                current_task_type_key,
                False,
                "同轮任务类型信息互相冲突，请只指定一个任务类型。",
            )

        task_type_map = self.kb.get_task_type_map()
        concrete_task_values = {
            value
            for key in ("task_type", "task_type_key")
            if key in updates
            for value in [updates[key]]
            if isinstance(value, str) and value in task_type_map
        }
        if len(concrete_task_values) > 1:
            logger.warning(
                "[DialogueManager] Rejecting conflicting concrete task values: %s",
                sorted(concrete_task_values),
            )
            return (
                None,
                current_task_type_key,
                False,
                "同轮具体任务类型互相冲突，请只指定一种任务操作。",
            )

        pending_task_type_key = (
            next(iter(resolved_targets)) if resolved_targets else None
        )
        existing_task_id = new_slots.get("task_id")
        task_type_change_locked = bool(
            pending_task_type_key
            and current_task_type_key
            and pending_task_type_key != current_task_type_key
            and existing_task_id
            and existing_task_id.status == "valid"
            and existing_task_id.value
        )
        effective_task_type_key = (
            current_task_type_key
            if task_type_change_locked
            else (pending_task_type_key or current_task_type_key)
        )
        return (
            pending_task_type_key,
            effective_task_type_key,
            task_type_change_locked,
            None,
        )

    @staticmethod
    def _record_task_type_update_error(new_slots: dict, message: str) -> None:
        slot = new_slots.get("task_type_key")
        if slot is None:
            slot = Slot("task_type_key")
            new_slots["task_type_key"] = slot
        slot.validation_error = message

    def _handle_task_type_update_in_transaction(self, key: str, value: str, new_slots: dict):
        task_type_map = self.kb.get_task_type_map()
        templates = self.kb.task_schemas.get("task_templates", {})
        target_key = self._resolve_task_type_target(key, value)

        existing_task_id = new_slots.get("task_id")
        old_task_type_slot = new_slots.get("task_type_key")
        old_task_type_key = (
            old_task_type_slot.value
            if old_task_type_slot
            and old_task_type_slot.status == "valid"
            and old_task_type_slot.value is not None
            else None
        )

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

            # A candidate/conflict/invalid/unresolved robot selector belongs
            # to the task context in which it was produced.  When the task
            # type actually changes, it must not block the new task's
            # admission-domain collapse.  Keep valid committed selectors for
            # the normal cross-task admission check; clear only the first
            # non-effective selector and its dependent suffix.
            if target_key != old_task_type_key:
                from ..slot_store import (
                    invalidate_robot_cascade_dependents,
                    reset_slot_to_missing,
                )

                for selector_key in (
                    "equipment_class",
                    "equipment_family",
                    "equipment_type",
                    "equipment_unit_id",
                ):
                    selector_slot = new_slots.get(selector_key)
                    if not selector_slot or selector_slot.status not in {
                        "candidate",
                        "conflict",
                        "invalid",
                        "unresolved",
                    }:
                        continue
                    invalidate_robot_cascade_dependents(
                        new_slots,
                        [selector_key],
                    )
                    reset_slot_to_missing(
                        selector_slot,
                        source="task_type_change_invalidation",
                    )
                    if selector_key == "equipment_unit_id":
                        equipment_name_slot = new_slots.get("equipment_name")
                        if equipment_name_slot is not None:
                            reset_slot_to_missing(
                                equipment_name_slot,
                                source="task_type_change_invalidation",
                            )
                    break

            # Clean up old dynamic slots in new_slots that do not belong to BASE_SLOT_TYPES, schema_keys, or ALLOWED_INTERNAL_SLOTS
            from ..slot_store import BASE_SLOT_TYPES, ALLOWED_INTERNAL_SLOTS
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

            self._auto_collapse_robot_cascade(new_slots, allow_overwrite=True)

    def _handle_rov_description_in_transaction(self, description: str, new_slots: dict):
        all_rovs = self.kb.get_all_rovs()
        task_type_slot = new_slots.get("task_type_key")
        task_type_key = (
            task_type_slot.value
            if task_type_slot
            and task_type_slot.status == "valid"
            and task_type_slot.value is not None
            else None
        )
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


    # 别名兼容
    resolve_task_type_target = _resolve_task_type_target
    task_transition_shared_field_keys = _task_transition_shared_field_keys
    build_task_transition_state = _build_task_transition_state
    build_post_update_evaluation_context = _build_post_update_evaluation_context
    dynamic_allowed_schema_keys = _dynamic_allowed_schema_keys
    clear_non_inherited_transition_slots = _clear_non_inherited_transition_slots
    normalize_transition_discovery_candidates = _normalize_transition_discovery_candidates
    merge_task_transition_extractions = _merge_task_transition_extractions
    task_selector_updates_from_extraction = _task_selector_updates_from_extraction
    resolve_task_type_update_context = _resolve_task_type_update_context
    record_task_type_update_error = _record_task_type_update_error
    handle_task_type_update_in_transaction = _handle_task_type_update_in_transaction
    handle_rov_description_in_transaction = _handle_rov_description_in_transaction
