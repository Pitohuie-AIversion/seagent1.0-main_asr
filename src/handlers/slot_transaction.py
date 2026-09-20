"""
src/handlers/slot_transaction.py - 槽位事务应用与规范化执行管理器

职责：
1. 事务内槽位更新的原子应用（_apply_updates_in_transaction / apply_updates_in_transaction）；
2. 结构化规范化计划应用（_apply_normalized_plan_in_transaction / apply_normalized_plan_in_transaction）；
3. 单个候选槽位写入与冲突标记（_apply_slot_update_in_transaction / apply_slot_update_in_transaction）；
4. 事务内槽位规范化与 Schema 规则校验（_normalize_and_validate_in_transaction / normalize_and_validate_in_transaction）。
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

from src.slots.normalization_contract import NormalizationApplyPlan
from src.slots.slot_store import Slot

class SlotTransactionManager:
    """槽位事务应用与规范化执行管理器"""

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    def __getattr__(self, name: str) -> Any:
        if "manager" in self.__dict__ and self.manager is not None:
            return getattr(self.manager, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")


    def apply_updates_in_transaction(
        self,
        updates: dict,
        new_slots: dict,
        allow_overwrite: bool = False,
        transition_slots_already_cleared: bool = False,
    ):
        return self._apply_updates_in_transaction(
            updates=updates,
            new_slots=new_slots,
            allow_overwrite=allow_overwrite,
            transition_slots_already_cleared=transition_slots_already_cleared,
        )

    def _apply_updates_in_transaction(
        self,
        updates: dict,
        new_slots: dict,
        allow_overwrite: bool = False,
        transition_slots_already_cleared: bool = False,
    ):
        manager = self.manager
        # main extractor 会携带 raw/confidence/source；LHL 归一化器只接收值本身。
        # 在事务入口拆开二者，既保留确定性归一化，也保留槽位审计信息。
        update_meta: dict[str, dict] = {}
        plain_updates: dict = {}
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

        task_type_slot = new_slots.get("task_type_key")
        task_type_key = (
            task_type_slot.value
            if task_type_slot and task_type_slot.status == "valid"
            else None
        )
        (
            pending_task_type_key,
            normalization_task_type_key,
            _task_type_change_locked,
            task_type_preflight_error,
        ) = manager._resolve_task_type_update_context(updates, new_slots)
        if task_type_preflight_error:
            manager._record_task_type_update_error(
                new_slots,
                task_type_preflight_error,
            )
            return

        if (
            pending_task_type_key
            and task_type_key
            and pending_task_type_key != task_type_key
            and not _task_type_change_locked
            and not transition_slots_already_cleared
        ):
            manager._clear_non_inherited_transition_slots(new_slots)
            task_type_slot = new_slots.get("task_type_key")

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
            "task_type",
            "task_type_key",
            "emergency_mode",
            "rov_description",
            "oilfield_name",
            "__clear_oilfield_name",
            "__clear_pending_oilfield",
            "task_id",
            "intent_id",
            "internal_id",
        }

        # Schema fields in a task-switch turn belong to the target task.
        failures = {}
        if normalization_task_type_key:
            evaluation_slots = copy.deepcopy(new_slots)
            evaluation_task_slot = evaluation_slots.get("task_type_key")
            if evaluation_task_slot is None:
                evaluation_task_slot = Slot(
                    "task_type_key",
                    value_type="string",
                )
                evaluation_slots["task_type_key"] = evaluation_task_slot
            evaluation_task_slot.value = normalization_task_type_key
            evaluation_task_slot.status = "valid"
            evaluation_task_slot.candidate_value = None
            evaluation_task_slot.validation_error = None

            evaluation_equipment_updates = {
                key: value
                for key, value in updates.items()
                if key in equipment_keys
            }
            if evaluation_equipment_updates:
                manager._project_equipment_updates_for_evaluation(
                    evaluation_equipment_updates,
                    evaluation_slots,
                    normalization_task_type_key,
                )
            current_state = {
                key: slot.value
                for key, slot in evaluation_slots.items()
                if slot.status == "valid" and slot.value is not None
            }
            if pending_task_type_key:
                current_state["task_type_key"] = pending_task_type_key
            schema_updates = {
                k: v for k, v in updates.items()
                if k not in equipment_keys and k not in passthrough_keys
            }
            norm_res = manager.normalizer.normalize_updates_with_failures(
                schema_updates,
                manager.builder.get_schema(normalization_task_type_key, manager.mode),
                current_state,
                lambda field_def, state: manager.builder._resolve_allowed(
                    field_def,
                    normalization_task_type_key,
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
                manager._handle_task_type_update_in_transaction(key, value, new_slots)
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

        if "emergency_mode" in updates:
            em_val = updates["emergency_mode"]
            if em_val is True:
                if "emergency_mode" not in new_slots:
                    new_slots["emergency_mode"] = Slot("emergency_mode")
                new_slots["emergency_mode"].value = True
                new_slots["emergency_mode"].status = "valid"
                manager.mode = "emergency"
            elif em_val is False:
                if "emergency_mode" in new_slots:
                    new_slots["emergency_mode"].value = False
                    new_slots["emergency_mode"].status = "valid"
                manager.mode = "normal"

        manager._handle_equipment_updates_in_transaction(
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

        manager._auto_collapse_robot_cascade(new_slots, allow_overwrite)

    def apply_normalized_plan_in_transaction(
        self,
        plan: NormalizationApplyPlan,
        new_slots: dict,
        allow_overwrite: bool = False,
        transition_from_task_type_key: str | None = None,
        transition_to_task_type_key: str | None = None,
    ) -> None:
        return self._apply_normalized_plan_in_transaction(
            plan=plan,
            new_slots=new_slots,
            allow_overwrite=allow_overwrite,
            transition_from_task_type_key=transition_from_task_type_key,
            transition_to_task_type_key=transition_to_task_type_key,
        )

    def _apply_normalized_plan_in_transaction(
        self,
        plan: NormalizationApplyPlan,
        new_slots: dict,
        allow_overwrite: bool = False,
        transition_from_task_type_key: str | None = None,
        transition_to_task_type_key: str | None = None,
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

        if not (
            transition_from_task_type_key
            and transition_to_task_type_key
            and transition_from_task_type_key != transition_to_task_type_key
        ):
            self.manager._auto_collapse_robot_cascade(new_slots, allow_overwrite)

    @staticmethod
    def apply_slot_update_in_transaction(
        key: str,
        value: Any,
        new_slots: dict,
        allow_overwrite: bool,
    ) -> None:
        return SlotTransactionManager._apply_slot_update_in_transaction(
            key=key,
            value=value,
            new_slots=new_slots,
            allow_overwrite=allow_overwrite,
        )

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

    def normalize_and_validate_in_transaction(
        self,
        new_slots: dict,
        task_type_key: str | None,
        skip_schema_keys: set[str] | frozenset[str] | None = None,
    ):
        return self._normalize_and_validate_in_transaction(
            new_slots=new_slots,
            task_type_key=task_type_key,
            skip_schema_keys=skip_schema_keys,
        )

    def _normalize_and_validate_in_transaction(
        self,
        new_slots: dict,
        task_type_key: str | None,
        skip_schema_keys: set[str] | frozenset[str] | None = None,
    ):
        manager = self.manager
        if not task_type_key:
            return

        schema = manager.builder.get_schema(task_type_key, manager.mode)

        for field_def in schema:
            key = field_def["key"]
            if skip_schema_keys and key in skip_schema_keys:
                continue
            ftype = field_def["type"]
            slot = new_slots.get(key)
            if not slot or slot.status in ("conflict", "invalid") or key.startswith("equipment_"):
                continue

            target_val = slot.candidate_value if slot.candidate_value is not None else slot.value
            if target_val is None or (isinstance(target_val, list) and len(target_val) == 0):
                if isinstance(target_val, list) and len(target_val) == 0 and slot.status != "conflict":
                    slot.status = "missing"
                continue

            temp_state = {
                state_key: (
                    state_slot.candidate_value
                    if state_slot.candidate_value is not None
                    else state_slot.value
                )
                for state_key, state_slot in new_slots.items()
                if (
                    state_slot.status not in ("invalid", "missing")
                    and (
                        state_slot.value is not None
                        or state_slot.candidate_value is not None
                    )
                    and (
                        not state_key.startswith("equipment_")
                        or state_slot.status == "valid"
                    )
                )
            }

            allowed = manager.builder._resolve_allowed(field_def, task_type_key, temp_state)
            if allowed:
                raw = target_val
                if ftype == "list":
                    normalized = manager.normalizer.normalize(raw, allowed, ftype)
                else:
                    normalized = manager.normalizer.normalize(str(raw), allowed, ftype)

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
                    coord = manager.builder._validate_coord(target_val)
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
                    num = manager.builder._validate_number(target_val)
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

        manager._auto_collapse_robot_cascade(new_slots, allow_overwrite=True)
