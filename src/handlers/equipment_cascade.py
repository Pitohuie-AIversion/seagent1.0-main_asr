"""
src/handlers/equipment_cascade.py - 设备选型、级联折叠与消歧协调器

职责：
1. 聚合设备范围限定 (EquipmentScopingHandler) 与自动级联收敛 (EquipmentCollapseHandler)；
2. 机器人选型、类别/系列/型号/单机四级事务更新 (_handle_equipment_updates_in_transaction)；
3. 评估态设备更新沙箱投影 (_project_equipment_updates_for_evaluation)。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from .equipment_scoping import EquipmentScopingHandler
from .equipment_collapse import EquipmentCollapseHandler
from ..slot_store import (
    Slot,
    reset_slot_to_missing,
    BASE_SLOT_TYPES,
    invalidate_robot_cascade_dependents,
)
from ..knowledge_retriever import RobotSelectionDataError
from ..constants import ROBOT_CASCADE_FIELDS

logger = logging.getLogger("src.dialogue_manager")


class EquipmentCascadeResolver:
    """设备选型、级联折叠与消歧协调器"""

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager
        self.scoping = EquipmentScopingHandler(manager)
        self.collapse = EquipmentCollapseHandler(manager)

    def __getattr__(self, name: str) -> Any:
        if "_manager_resolving" in self.__dict__:
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
        for sub in ("scoping", "collapse"):
            if sub in self.__dict__ and hasattr(self.__dict__[sub], name):
                return getattr(self.__dict__[sub], name)
        if "manager" in self.__dict__ and self.manager is not None:
            try:
                self.__dict__["_manager_resolving"] = True
                return getattr(self.manager, name)
            finally:
                self.__dict__.pop("_manager_resolving", None)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")


    # 委托及别名兼容
    @staticmethod
    def _source_for_resolution_method(resolution_method: str | None) -> str:
        return EquipmentScopingHandler._source_for_resolution_method(resolution_method)

    source_for_resolution_method = _source_for_resolution_method


    def _scope_confirmed_recommendation(self, *args, **kwargs):
        return self.scoping._scope_confirmed_recommendation(*args, **kwargs)

    scope_confirmed_recommendation = _scope_confirmed_recommendation

    def _scope_visible_ordinal_selections(self, *args, **kwargs):
        return self.scoping._scope_visible_ordinal_selections(*args, **kwargs)

    scope_visible_ordinal_selections = _scope_visible_ordinal_selections

    def _auto_collapse_robot_cascade(self, *args, **kwargs):
        return self.collapse._auto_collapse_robot_cascade(*args, **kwargs)

    auto_collapse_robot_cascade = _auto_collapse_robot_cascade

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
