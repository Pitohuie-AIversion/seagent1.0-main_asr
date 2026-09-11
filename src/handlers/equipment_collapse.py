"""
src/handlers/equipment_collapse.py - 四级设备级联自动收敛与推导处理器

职责：
1. 四级设备级联自动收敛推导 (_auto_collapse_robot_cascade / auto_collapse_robot_cascade)；
2. 静态准入域 (admission domain) 与运行可行域 (feasible domain) 计算；
3. 逐级唯一候选自动绑定 (status='valid', source='auto') 与多候选截断等待用户决策。
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from ..slot_store import (
    Slot,
    reset_slot_to_missing,
    BASE_SLOT_TYPES,
    invalidate_robot_cascade_dependents,
)
from ..knowledge_retriever import RobotSelectionDataError
from ..constants import ROBOT_CASCADE_FIELDS


logger = logging.getLogger("src.dialogue_manager")


class EquipmentCollapseHandler:
    """四级设备级联自动收敛与推导处理器"""

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    def __getattr__(self, name: str) -> Any:
        if "manager" in self.__dict__ and self.manager is not None:
            return getattr(self.manager, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

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
        cascade_keys = (
            "equipment_class",
            "equipment_family",
            "equipment_type",
            "equipment_unit_id",
        )
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

    # 别名兼容
    auto_collapse_robot_cascade = _auto_collapse_robot_cascade
