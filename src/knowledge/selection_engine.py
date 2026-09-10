"""
src/knowledge/selection_engine.py — 机器人选型、规格校验、可用域与全量设备检索引擎
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .models import (
    RobotSelectionDataError,
    RobotVariantFeasibility,
    _norm,
    _payload_match_key,
    normalize_payload_groups,
    normalize_supported_payloads,
)
from ..simulated_time import get_current_datetime

if TYPE_CHECKING:
    from ..knowledge_retriever import KnowledgeBase


class RobotSelectionEngine:
    """机器人 4 级拓扑选择、规格校验与设备检索引擎。"""

    def __init__(self, kb: "KnowledgeBase"):
        self.kb = kb

    @property
    def robot_fleet(self) -> dict:
        return self.kb.robot_fleet

    @property
    def task_schemas(self) -> dict:
        return self.kb.task_schemas

    @property
    def state_info(self) -> Any:
        return self.kb.state_info

    @property
    def _robot_variants_cache(self) -> list[dict] | None:
        return self.kb._robot_variants_cache

    @_robot_variants_cache.setter
    def _robot_variants_cache(self, value: list[dict] | None) -> None:
        self.kb._robot_variants_cache = value

    def _validate_task_type_key(self, task_type_key: str | None) -> dict | None:
        if task_type_key is None:
            return None
        templates = self.task_schemas.get("task_templates", {})
        if not isinstance(templates, dict) or task_type_key not in templates:
            raise RobotSelectionDataError(
                f"Task template '{task_type_key}' not found in task_schemas.",
                error_code="TASK_TEMPLATE_NOT_FOUND",
                expected_field="task_templates",
                actual_value=task_type_key,
            )
        return templates[task_type_key]

    def _validate_fleet_units_integrity(self) -> None:
        self._validate_model_variants_integrity()
        fleet_units = self.robot_fleet.get("fleet_units")
        if fleet_units is None or not isinstance(fleet_units, list):
            raise RobotSelectionDataError(
                "fleet_units in configuration must be a list.",
                error_code="INVALID_FLEET_UNITS_CONFIG",
                expected_field="fleet_units",
                actual_value=fleet_units,
            )
        seen_units = set()
        all_variants = self.robot_fleet.get("model_variants", {})
        for unit in fleet_units:
            if not isinstance(unit, dict):
                raise RobotSelectionDataError(
                    "fleet_units item must be a dictionary.",
                    error_code="INVALID_UNIT_CONFIG",
                    actual_value=unit,
                )
            uid = unit.get("unit_id")
            if not uid or not isinstance(uid, str):
                raise RobotSelectionDataError(
                    "Fleet unit is missing unit_id.",
                    error_code="MISSING_UNIT_ID",
                    actual_value=uid,
                )
            if uid in seen_units:
                raise RobotSelectionDataError(
                    f"Duplicate unit_id '{uid}' found in fleet_units.",
                    error_code="DUPLICATE_UNIT_ID",
                    actual_value=uid,
                )
            seen_units.add(uid)

            vid = unit.get("variant_id")
            if not vid or vid not in all_variants:
                raise RobotSelectionDataError(
                    f"Fleet unit '{uid}' references non-existent variant_id '{vid}'.",
                    error_code="INVALID_VARIANT_REFERENCE",
                    expected_field="model_variants",
                    actual_value=vid,
                )

    def _resolve_class_key(self, value: str | None) -> str | None:
        if not value or not isinstance(value, str):
            return None
        value_norm = _norm(value)
        for key, cfg in self.get_robot_classes().items():
            if value_norm in {_norm(key), _norm(cfg.get("full_name"))}:
                return key
        family_classes = {
            family.get("robot_class")
            for family in (self.robot_fleet.get("robot_families", {}) or {}).values()
            if isinstance(family, dict) and family.get("robot_class")
        }
        for key in family_classes:
            if value_norm == _norm(key):
                return key
        return None

    def _robot_class_display_name(self, class_id: str | None) -> str:
        if not class_id:
            return ""
        return self.get_robot_classes().get(class_id, {}).get("full_name", class_id)

    def _resolve_family_key(self, value: str | None) -> str | None:
        if not value or not isinstance(value, str):
            return None
        families = self.robot_fleet.get("robot_families", {})
        if not isinstance(families, dict):
            return None

        # Priority 1: Exact canonical family_id
        if value in families:
            return value

        # Priority 2: Full_name / alias matching
        value_norm = _norm(value)
        matches = set()
        for family_id, family in families.items():
            targets = [family.get("full_name", ""), *family.get("aliases", [])]
            if any(value_norm == _norm(target) for target in targets if target):
                matches.add(family_id)

        if len(matches) > 1:
            raise RobotSelectionDataError(
                f"Family selector '{value}' is ambiguous (matches multiple families: {sorted(matches)}).",
                error_code="AMBIGUOUS_FAMILY_SELECTOR",
                actual_value=value,
            )

        if len(matches) == 1:
            return next(iter(matches))

        return None

    def _validate_model_variants_integrity(self) -> None:
        variants = self.robot_fleet.get("model_variants")
        if variants is None or not isinstance(variants, dict):
            raise RobotSelectionDataError(
                "model_variants in configuration must be a dictionary.",
                error_code="INVALID_MODEL_VARIANTS_CONFIG",
                expected_field="model_variants",
                actual_value=variants,
            )
        families = self.robot_fleet.get("robot_families", {})
        if not isinstance(families, dict):
            raise RobotSelectionDataError(
                "robot_families in configuration must be a dictionary.",
                error_code="INVALID_ROBOT_FAMILIES_CONFIG",
                expected_field="robot_families",
                actual_value=families,
            )
        for variant_id, variant in variants.items():
            if not isinstance(variant, dict):
                raise RobotSelectionDataError(
                    f"Variant '{variant_id}' must be a dictionary.",
                    error_code="INVALID_VARIANT_CONFIG",
                    variant_id=variant_id,
                    actual_value=variant,
                )
            f_id = variant.get("family_id")
            if not f_id or not isinstance(f_id, str) or f_id not in families:
                raise RobotSelectionDataError(
                    f"Variant '{variant_id}' references non-existent or missing family_id '{f_id}'.",
                    error_code="INVALID_FAMILY_REFERENCE",
                    variant_id=variant_id,
                    expected_field="robot_families",
                    actual_value=f_id,
                )

    def _extract_and_validate_variant_spec(
        self,
        robot_class_id: str,
        family_id: str,
        variant_id: str,
        variant: dict,
    ) -> dict:
        v_family_id = variant.get("family_id")
        if not v_family_id or v_family_id not in self.robot_fleet.get("robot_families", {}):
            raise RobotSelectionDataError(
                f"Variant '{variant_id}' references non-existent or missing family_id '{v_family_id}'.",
                error_code="INVALID_FAMILY_REFERENCE",
                robot_class=robot_class_id,
                family_id=v_family_id,
                variant_id=variant_id,
                expected_field="robot_families",
                actual_value=v_family_id,
            )

        hard_params = variant.get("hard_params")
        if hard_params is None or not isinstance(hard_params, dict):
            raise RobotSelectionDataError(
                f"Variant '{variant_id}' is missing hard_params dictionary.",
                error_code="MISSING_HARD_PARAMS",
                robot_class=robot_class_id,
                family_id=family_id,
                variant_id=variant_id,
                expected_field="hard_params",
                actual_value=hard_params,
            )

        if robot_class_id == "auv":
            expected_field = "diameter_mm"
            unit_str = "mm"
            display_suffix = "CC"
            incompatible_field = "power_hp"
            incompatible_val = hard_params.get(incompatible_field)
            if incompatible_val is not None:
                raise RobotSelectionDataError(
                    f"Variant '{variant_id}' for AUV class cannot specify incompatible field '{incompatible_field}'={incompatible_val}.",
                    error_code="INCOMPATIBLE_SPECIFICATION_FIELD",
                    robot_class=robot_class_id,
                    family_id=family_id,
                    variant_id=variant_id,
                    expected_field=expected_field,
                    actual_value=incompatible_val,
                )
        else:
            expected_field = "power_hp"
            unit_str = "hp"
            display_suffix = "HP"
            incompatible_field = "diameter_mm"
            incompatible_val = hard_params.get(incompatible_field)
            if incompatible_val is not None and incompatible_val != "不适用":
                raise RobotSelectionDataError(
                    f"Variant '{variant_id}' for non-AUV class cannot specify incompatible field '{incompatible_field}'={incompatible_val}.",
                    error_code="INCOMPATIBLE_SPECIFICATION_FIELD",
                    robot_class=robot_class_id,
                    family_id=family_id,
                    variant_id=variant_id,
                    expected_field=expected_field,
                    actual_value=incompatible_val,
                )

        if expected_field not in hard_params:
            raise RobotSelectionDataError(
                f"Variant '{variant_id}' missing expected field '{expected_field}'.",
                error_code="MISSING_EXPECTED_FIELD",
                robot_class=robot_class_id,
                family_id=family_id,
                variant_id=variant_id,
                expected_field=expected_field,
                actual_value=None,
            )

        raw_val = hard_params[expected_field]

        if raw_val is None:
            if expected_field == "power_hp":
                return {
                    "type": expected_field,
                    "value": None,
                    "unit": unit_str,
                    "display_value": "未知",
                    "variant_id": variant_id,
                }
            raise RobotSelectionDataError(
                f"Variant '{variant_id}' field '{expected_field}' is None.",
                error_code="MISSING_SPECIFICATION_VALUE",
                robot_class=robot_class_id,
                family_id=family_id,
                variant_id=variant_id,
                expected_field=expected_field,
                actual_value=raw_val,
            )

        if isinstance(raw_val, bool):
            raise RobotSelectionDataError(
                f"Variant '{variant_id}' field '{expected_field}' cannot be boolean ({raw_val}).",
                error_code="INVALID_SPECIFICATION_TYPE",
                robot_class=robot_class_id,
                family_id=family_id,
                variant_id=variant_id,
                expected_field=expected_field,
                actual_value=raw_val,
            )

        if raw_val == "不适用":
            raise RobotSelectionDataError(
                f"Variant '{variant_id}' field '{expected_field}' is '不适用'.",
                error_code="SPECIFICATION_NOT_APPLICABLE",
                robot_class=robot_class_id,
                family_id=family_id,
                variant_id=variant_id,
                expected_field=expected_field,
                actual_value=raw_val,
            )

        if isinstance(raw_val, str):
            raise RobotSelectionDataError(
                f"Variant '{variant_id}' field '{expected_field}' cannot be string ('{raw_val}'). Must be YAML numeric.",
                error_code="INVALID_SPECIFICATION_TYPE",
                robot_class=robot_class_id,
                family_id=family_id,
                variant_id=variant_id,
                expected_field=expected_field,
                actual_value=raw_val,
            )

        if not isinstance(raw_val, (int, float)):
            raise RobotSelectionDataError(
                f"Variant '{variant_id}' field '{expected_field}' has invalid type {type(raw_val).__name__}.",
                error_code="INVALID_SPECIFICATION_TYPE",
                robot_class=robot_class_id,
                family_id=family_id,
                variant_id=variant_id,
                expected_field=expected_field,
                actual_value=raw_val,
            )

        if not math.isfinite(raw_val):
            raise RobotSelectionDataError(
                f"Variant '{variant_id}' field '{expected_field}' must be a finite number, got {raw_val}.",
                error_code="NON_FINITE_SPECIFICATION_VALUE",
                robot_class=robot_class_id,
                family_id=family_id,
                variant_id=variant_id,
                expected_field=expected_field,
                actual_value=raw_val,
            )

        if raw_val <= 0:
            raise RobotSelectionDataError(
                f"Variant '{variant_id}' field '{expected_field}' must be positive, got {raw_val}.",
                error_code="NON_POSITIVE_SPECIFICATION_VALUE",
                robot_class=robot_class_id,
                family_id=family_id,
                variant_id=variant_id,
                expected_field=expected_field,
                actual_value=raw_val,
            )

        num_val = float(raw_val)
        if num_val.is_integer():
            final_val = int(num_val)
        else:
            final_val = num_val

        display_value = f"{final_val}{display_suffix}"

        return {
            "type": expected_field,
            "value": final_val,
            "unit": unit_str,
            "display_value": display_value,
            "variant_id": variant_id,
        }

    @staticmethod
    def _validated_positive_number(
        value: Any,
        *,
        error_code: str,
        field_name: str,
    ) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise RobotSelectionDataError(
                f"{field_name} must be a positive finite number.",
                error_code=error_code,
                expected_field=field_name,
                actual_value=value,
            )
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise RobotSelectionDataError(
                f"{field_name} must be a positive finite number.",
                error_code=error_code,
                expected_field=field_name,
                actual_value=value,
            ) from exc
        if not math.isfinite(normalized) or normalized <= 0:
            raise RobotSelectionDataError(
                f"{field_name} must be a positive finite number.",
                error_code=error_code,
                expected_field=field_name,
                actual_value=value,
            )
        return normalized

    @classmethod
    def evaluate_static_robot_variant(
        cls,
        variant_id: str,
        variant_cfg: dict,
        task_state: dict | None,
    ) -> RobotVariantFeasibility:
        """Evaluate only task facts that have an authoritative Variant mapping."""
        state = task_state if isinstance(task_state, dict) else {}
        hard_params = variant_cfg.get("hard_params")
        if not isinstance(hard_params, dict):
            raise RobotSelectionDataError(
                f"Variant '{variant_id}' is missing hard_params dictionary.",
                error_code="MISSING_HARD_PARAMS",
                variant_id=variant_id,
                expected_field="hard_params",
                actual_value=hard_params,
            )

        reasons: list[str] = []
        water_depth = state.get("water_depth")
        if water_depth is not None:
            required_depth = cls._validated_positive_number(
                water_depth,
                error_code="INVALID_WATER_DEPTH",
                field_name="water_depth",
            )
            max_depth = cls._validated_positive_number(
                hard_params.get("max_depth_m"),
                error_code="INVALID_ROV_MAX_DEPTH",
                field_name=f"model_variants.{variant_id}.hard_params.max_depth_m",
            )
            if required_depth > max_depth:
                reasons.append(
                    f"water_depth {required_depth:g} exceeds max_depth_m {max_depth:g}"
                )

        required_payloads = state.get("payload")
        if required_payloads is not None:
            if not isinstance(required_payloads, (list, tuple)) or any(
                not isinstance(item, str) or not item.strip()
                for item in required_payloads
            ):
                raise RobotSelectionDataError(
                    "payload must be a list of non-empty canonical names.",
                    error_code="INVALID_PAYLOAD_REQUIREMENTS",
                    variant_id=variant_id,
                    expected_field="payload",
                    actual_value=required_payloads,
                )

            onboard_raw = hard_params.get("onboard_payloads")
            supported_raw = hard_params.get("supported_payloads")
            onboard_list, _ = normalize_payload_groups(onboard_raw)
            supported_list, _ = normalize_supported_payloads(supported_raw)
            if not isinstance(onboard_raw, (list, dict)) or not isinstance(supported_raw, (list, dict)):
                raise RobotSelectionDataError(
                    f"Variant '{variant_id}' payload declarations must be list/dict compatible.",
                    error_code="INVALID_VARIANT_PAYLOAD_CONFIG",
                    variant_id=variant_id,
                    expected_field="onboard_payloads/supported_payloads",
                    actual_value={
                        "onboard_payloads": onboard_raw,
                        "supported_payloads": supported_raw,
                    },
                )

            onboard = {_payload_match_key(item) for item in onboard_list}
            supported = {_payload_match_key(item) for item in supported_list}
            available = onboard | supported
            missing = [
                item
                for item in required_payloads
                if _payload_match_key(item) not in available
            ]
            if missing:
                reasons.append(f"unsupported payloads: {missing}")
            installation = tuple(
                item
                for item in required_payloads
                if _payload_match_key(item) in supported
                and _payload_match_key(item) not in onboard
            )
        else:
            installation = ()

        return RobotVariantFeasibility(
            eligible=not reasons,
            reasons=tuple(reasons),
            requires_installation=installation,
        )

    @staticmethod
    def _task_starts_within_runtime_window(
        task_state: dict | None,
        *,
        time_window_minutes: int = 60,
    ) -> bool:
        if not isinstance(task_state, dict):
            return False
        raw_start = task_state.get("start_time")
        if not isinstance(raw_start, str) or not raw_start.strip():
            return False
        try:
            clean = raw_start.strip().replace("：", ":")
            if clean.endswith("Z"):
                clean = clean[:-1] + "+00:00"
            start_time = datetime.fromisoformat(clean.replace("T", " "))
        except (TypeError, ValueError):
            return False
        business_tz = ZoneInfo("Asia/Shanghai")
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=business_tz)
        else:
            start_time = start_time.astimezone(business_tz)
        now = get_current_datetime().astimezone(business_tz).replace(microsecond=0)
        delta_seconds = (start_time - now).total_seconds()
        return 0 <= delta_seconds <= time_window_minutes * 60

    def validate_robot_selection_from_task_state(
        self,
        task_state: dict,
        *,
        require_unit: bool = False,
    ) -> dict | None:
        """Validate the deepest explicit robot selector and every parent edge."""
        if not isinstance(task_state, dict):
            raise RobotSelectionDataError(
                "task_state must be a dictionary.",
                error_code="INVALID_TASK_STATE",
                actual_value=task_state,
            )

        selector_specs = (
            ("equipment_class", "INVALID_ROBOT_CLASS_SELECTOR"),
            ("equipment_family", "INVALID_FAMILY_SELECTOR"),
            ("equipment_type", "INVALID_VARIANT_SELECTOR"),
            ("equipment_unit_id", "INVALID_UNIT_SELECTOR"),
        )
        selectors: dict[str, str] = {}
        for field_name, error_code in selector_specs:
            if field_name not in task_state or task_state[field_name] is None:
                continue
            raw_selector = task_state[field_name]
            if not isinstance(raw_selector, str) or not raw_selector.strip():
                raise RobotSelectionDataError(
                    f"{field_name} must be a non-empty string when explicitly provided.",
                    error_code=error_code,
                    expected_field=field_name,
                    actual_value=raw_selector,
                )
            selectors[field_name] = raw_selector.strip()

        unit_selector = selectors.get("equipment_unit_id")
        if require_unit and unit_selector is None:
            raise RobotSelectionDataError(
                "A concrete equipment_unit_id is required.",
                error_code="MISSING_UNIT_ID",
                expected_field="equipment_unit_id",
                actual_value=task_state.get("equipment_unit_id"),
            )
        if not selectors:
            return None

        task_type_key = task_state.get("task_type_key")
        explicit_class = selectors.get("equipment_class")
        explicit_family = selectors.get("equipment_family")
        explicit_variant = selectors.get("equipment_type")

        resolved_explicit_variant = None
        if explicit_variant is not None:
            resolved_explicit_variant = self._resolve_robot_variant_exact(
                explicit_variant,
            )
            if not resolved_explicit_variant:
                raise RobotSelectionDataError(
                    f"Variant '{explicit_variant}' does not exist.",
                    error_code="VARIANT_NOT_FOUND",
                    expected_field="equipment_type",
                    actual_value=explicit_variant,
                )

        if unit_selector is not None:
            resolved_unit = self._resolve_robot_unit_exact(
                unit_selector,
                task_type_key,
            )
            if not resolved_unit:
                raise RobotSelectionDataError(
                    f"Fleet unit '{unit_selector}' does not exist or is not allowed for this task.",
                    error_code="UNIT_NOT_FOUND",
                    expected_field="equipment_unit_id",
                    actual_value=unit_selector,
                )

            robot = resolved_unit["robot"]
            family_id = robot.get("family_id")
            family_cfg = self.robot_fleet.get("robot_families", {}).get(
                family_id,
                {},
            )
            return self.validate_static_robot_selection(
                explicit_class if explicit_class is not None else robot.get("robot_class"),
                explicit_family
                if explicit_family is not None
                else family_cfg.get("full_name") or family_id,
                explicit_variant
                if explicit_variant is not None
                else robot.get("full_name") or robot.get("variant_id"),
                resolved_unit["unit_id"],
                task_type_key,
            )

        if explicit_variant is not None:
            robot = resolved_explicit_variant
            family_id = robot.get("family_id")
            family_cfg = self.robot_fleet.get("robot_families", {}).get(
                family_id,
                {},
            )
            selected_class = (
                explicit_class
                if explicit_class is not None
                else robot.get("robot_class")
            )
            selected_family = (
                explicit_family
                if explicit_family is not None
                else family_cfg.get("full_name") or family_id
            )
            allowed_variants = self.list_robot_variants(
                selected_class,
                selected_family,
                task_type_key,
            )
            if robot.get("variant_id") not in {
                item.get("variant_id") for item in allowed_variants
            }:
                raise RobotSelectionDataError(
                    f"Variant '{robot.get('variant_id')}' does not belong to family '{selected_family}'.",
                    error_code="VARIANT_FAMILY_MISMATCH",
                    robot_class=self._resolve_class_key(selected_class),
                    family_id=self._resolve_family_key(selected_family),
                    variant_id=robot.get("variant_id"),
                )
            return {
                "robot_class": robot.get("robot_class"),
                "family_id": family_id,
                "family_name": family_cfg.get("full_name", family_id),
                "variant_id": robot.get("variant_id"),
                "equipment_type": robot.get("full_name"),
            }

        if explicit_family is not None:
            family_id = self._resolve_family_key(explicit_family)
            if not family_id:
                raise RobotSelectionDataError(
                    f"Robot family '{explicit_family}' not found.",
                    error_code="FAMILY_NOT_FOUND",
                    expected_field="equipment_family",
                    actual_value=explicit_family,
                )
            family_cfg = self.robot_fleet.get("robot_families", {}).get(
                family_id,
                {},
            )
            canonical_class = family_cfg.get("robot_class")
            selected_class = (
                explicit_class if explicit_class is not None else canonical_class
            )
            resolved_class = self._resolve_class_key(selected_class)
            if resolved_class != canonical_class:
                raise RobotSelectionDataError(
                    f"Family '{family_id}' belongs to class '{canonical_class}', not '{selected_class}'.",
                    error_code="FAMILY_CLASS_MISMATCH",
                    robot_class=resolved_class,
                    family_id=family_id,
                )
            allowed_families = self.list_robot_families(
                selected_class,
                task_type_key,
            )
            if family_id not in {item.get("family_id") for item in allowed_families}:
                raise RobotSelectionDataError(
                    f"Family '{family_id}' is not allowed for task '{task_type_key}'.",
                    error_code="FAMILY_CAPABILITY_MISMATCH",
                    robot_class=canonical_class,
                    family_id=family_id,
                )
            return {
                "robot_class": canonical_class,
                "family_id": family_id,
                "family_name": family_cfg.get("full_name", family_id),
            }

        resolved_class = self._resolve_class_key(explicit_class)
        if not resolved_class:
            raise RobotSelectionDataError(
                f"Robot class '{explicit_class}' not found.",
                error_code="ROBOT_CLASS_NOT_FOUND",
                expected_field="equipment_class",
                actual_value=explicit_class,
            )
        allowed_classes = self.list_robot_classes(task_type_key)
        if resolved_class not in {item.get("class_id") for item in allowed_classes}:
            raise RobotSelectionDataError(
                f"Robot class '{resolved_class}' is not allowed for task '{task_type_key}'.",
                error_code="CLASS_NOT_ALLOWED_FOR_TASK",
                robot_class=resolved_class,
            )
        class_cfg = self.robot_fleet.get("robot_classes", {}).get(
            resolved_class,
            {},
        )
        return {
            "robot_class": resolved_class,
            "robot_class_name": class_cfg.get("full_name", resolved_class),
        }

    def get_feasible_robot_selection_domain(
        self,
        task_type_key: str | None,
        task_state: dict | None = None,
        purpose: str = "interactive",
    ) -> dict:
        """计算当前任务的机器人选择子图。四级结构：class -> family -> model_variant -> fleet_unit。"""
        self._validate_model_variants_integrity()
        self._validate_fleet_units_integrity()

        template = self._validate_task_type_key(task_type_key)
        robot_families = self.robot_fleet.get("robot_families", {})
        all_variants = self.robot_fleet.get("model_variants", {})
        fleet_units = self.robot_fleet.get("fleet_units", [])

        if template is None:
            required_caps = set()
        else:
            required_caps = set(template.get("required_capabilities", []))
        apply_runtime_filter = (purpose != "interactive") and self._task_starts_within_runtime_window(task_state)
        rejected_variants: list[dict] = []
        rejected_units: list[dict] = []

        # 1. 筛选符合 capability 的 feasible families
        feasible_families: dict[str, dict] = {}
        for family_id, family in robot_families.items():
            f_class = family.get("robot_class")
            if not f_class or not isinstance(f_class, str):
                raise RobotSelectionDataError(
                    f"Family '{family_id}' is missing robot_class grouping metadata '{f_class}'.",
                    error_code="INVALID_ROBOT_CLASS_REFERENCE",
                    expected_field="robot_families.robot_class",
                    actual_value=f_class,
                )

            caps = family.get("capabilities", [])
            if caps is None or not isinstance(caps, list):
                raise RobotSelectionDataError(
                    f"Family '{family_id}' capabilities must be a list.",
                    error_code="INVALID_FAMILY_CAPABILITIES",
                    family_id=family_id,
                    actual_value=caps,
                )
            if required_caps and not required_caps.issubset(set(caps)):
                continue

            feasible_families[family_id] = family

        # 2. 反向由 feasible_families 推导 class 分组
        feasible_class_ids = list(dict.fromkeys(
            f.get("robot_class") for f in feasible_families.values() if f.get("robot_class")
        ))

        # 3. 逐层组装 4 层 domain 结构
        classes_node: list[dict] = []
        for class_id in feasible_class_ids:
            families_node: list[dict] = []

            for family_id, family_cfg in feasible_families.items():
                if family_cfg.get("robot_class") != class_id:
                    continue

                variants_node: list[dict] = []
                for variant_id, variant_cfg in all_variants.items():
                    if variant_cfg.get("family_id") != family_id:
                        continue

                    units_node: list[dict] = []
                    for unit_cfg in fleet_units:
                        if unit_cfg.get("variant_id") != variant_id:
                            continue
                        unit_id = unit_cfg.get("unit_id")
                        if apply_runtime_filter:
                            runtime = self.state_info.check_runtime_availability(
                                str(unit_id or "")
                            )
                            if not runtime.get("available"):
                                rejected_units.append({
                                    "unit_id": unit_id,
                                    "reason_code": runtime.get("reason_code"),
                                    "message": runtime.get("message"),
                                })
                                continue
                        units_node.append({
                            "unit_id": unit_id,
                            "variant_id": unit_cfg.get("variant_id"),
                            "serial_no": unit_cfg.get("serial_no"),
                            "display_name": unit_cfg.get("display_name"),
                            "status_ref": unit_cfg.get("status_ref"),
                            "aliases": list(unit_cfg.get("aliases", []) or []),
                        })

                    variants_node.append({
                        "variant_id": variant_id,
                        "full_name": variant_cfg.get("full_name", variant_id),
                        "family_id": family_id,
                        "robot_class": class_id,
                        "aliases": list(variant_cfg.get("aliases", []) or []),
                        "hard_params": dict(variant_cfg.get("hard_params", {}) or {}),
                        "requires_installation": [],
                        "units": units_node,
                        "has_available_units": bool(units_node),
                    })

                if not variants_node:
                    continue
                families_node.append({
                    "family_id": family_id,
                    "full_name": family_cfg.get("full_name", family_id),
                    "robot_class": class_id,
                    "aliases": list(family_cfg.get("aliases", []) or []),
                    "capabilities": list(family_cfg.get("capabilities", []) or []),
                    "brief": family_cfg.get("brief", ""),
                    "variants": variants_node,
                })

            if not families_node:
                continue
            classes_node.append({
                "class_id": class_id,
                "robot_class": class_id,
                "full_name": self._robot_class_display_name(class_id),
                "families": families_node,
            })

        return {
            "task_type_key": task_type_key,
            "required_capabilities": sorted(list(required_caps)),
            "runtime_filter_applied": apply_runtime_filter,
            "rejected_variants": rejected_variants,
            "rejected_units": rejected_units,
            "classes": classes_node,
        }

    def list_robot_classes(self, task_type_key: str | None = None) -> list[dict]:
        template = self._validate_task_type_key(task_type_key)
        robot_classes = self.get_robot_classes()
        if template is not None:
            allowed_classes = template.get("allowed_robot_classes", [])
            for class_id in allowed_classes:
                if class_id not in robot_classes:
                    raise RobotSelectionDataError(
                        f"Task template '{task_type_key}' references non-existent robot_class '{class_id}'.",
                        error_code="INVALID_ROBOT_CLASS_REFERENCE",
                        expected_field="robot_classes",
                        actual_value=class_id,
                    )
            domain = self.get_feasible_robot_selection_domain(task_type_key)
            return [
                {
                    "class_id": c["class_id"],
                    "robot_class": c["class_id"],
                    "full_name": c["full_name"],
                }
                for c in domain["classes"]
            ]
        family_class_ids = [
            family.get("robot_class")
            for family in (self.robot_fleet.get("robot_families", {}) or {}).values()
            if isinstance(family, dict) and family.get("robot_class")
        ]
        class_ids = list(robot_classes.keys()) or list(dict.fromkeys(family_class_ids))
        return [
            {
                "class_id": class_id,
                "robot_class": class_id,
                "full_name": self._robot_class_display_name(class_id),
            }
            for class_id in class_ids
        ]

    def list_robot_families(
        self,
        robot_class: str,
        task_type_key: str | None = None,
    ) -> list[dict]:
        self._validate_task_type_key(task_type_key)
        self._validate_model_variants_integrity()
        class_id = self._resolve_class_key(robot_class)
        if not class_id:
            raise RobotSelectionDataError(
                f"Robot class '{robot_class}' not found.",
                error_code="ROBOT_CLASS_NOT_FOUND",
                robot_class=robot_class,
            )

        if task_type_key is not None:
            domain = self.get_feasible_robot_selection_domain(task_type_key)
            target_cnode = next((c for c in domain["classes"] if c["class_id"] == class_id), None)
            if target_cnode is None:
                raise RobotSelectionDataError(
                    f"Robot class '{class_id}' is not allowed or has no capability-matching family for task '{task_type_key}'.",
                    error_code="CLASS_NOT_ALLOWED_FOR_TASK",
                    robot_class=class_id,
                )
            return [
                {
                    "family_id": f["family_id"],
                    "full_name": f["full_name"],
                    "robot_class": class_id,
                    "aliases": list(f.get("aliases", [])),
                    "capabilities": list(f.get("capabilities", [])),
                    "brief": f.get("brief", ""),
                }
                for f in target_cnode["families"]
            ]

        result = []
        robot_classes = self.get_robot_classes()
        robot_families = self.robot_fleet.get("robot_families", {})
        for family_id, family in robot_families.items():
            f_class = family.get("robot_class")
            if not f_class or not isinstance(f_class, str) or f_class not in robot_classes:
                raise RobotSelectionDataError(
                    f"Family '{family_id}' references missing or invalid robot_class '{f_class}'.",
                    error_code="INVALID_ROBOT_CLASS_REFERENCE",
                    expected_field="robot_classes",
                    actual_value=f_class,
                )
            if f_class != class_id:
                continue
            caps = family.get("capabilities")
            if caps is None or not isinstance(caps, list):
                raise RobotSelectionDataError(
                    f"Family '{family_id}' capabilities must be a list.",
                    error_code="INVALID_FAMILY_CAPABILITIES",
                    family_id=family_id,
                    actual_value=caps,
                )

            result.append({
                "family_id": family_id,
                "full_name": family.get("full_name", family_id),
                "robot_class": class_id,
                "aliases": list(family.get("aliases", [])),
                "capabilities": list(caps),
                "brief": family.get("brief", ""),
            })
        return result

    def list_robot_specifications(
        self,
        robot_class: str,
        family: str,
        task_type_key: str | None = None,
    ) -> list[dict]:
        """Backward-compatible alias for list_robot_variants."""
        return self.list_robot_variants(robot_class, family, task_type_key)

    def list_robot_variants(
        self,
        robot_class: str,
        family: str,
        task_type_key: str | None = None,
    ) -> list[dict]:
        self._validate_task_type_key(task_type_key)
        self._validate_model_variants_integrity()
        class_id = self._resolve_class_key(robot_class)
        if not class_id:
            raise RobotSelectionDataError(
                f"Robot class '{robot_class}' not found.",
                error_code="ROBOT_CLASS_NOT_FOUND",
                robot_class=robot_class,
            )

        family_id = self._resolve_family_key(family)
        if not family_id:
            raise RobotSelectionDataError(
                f"Robot family '{family}' not found.",
                error_code="FAMILY_NOT_FOUND",
                robot_class=class_id,
                family_id=family,
            )

        family_data = self.robot_fleet.get("robot_families", {}).get(family_id, {})
        f_class = family_data.get("robot_class")
        if f_class != class_id:
            raise RobotSelectionDataError(
                f"Family '{family_id}' belongs to class '{f_class}', not '{class_id}'.",
                error_code="FAMILY_CLASS_MISMATCH",
                robot_class=class_id,
                family_id=family_id,
            )

        if task_type_key is not None:
            template = self.task_schemas.get("task_templates", {}).get(task_type_key, {})
            allowed_classes = template.get("allowed_robot_classes")
            if allowed_classes is not None and class_id not in allowed_classes:
                raise RobotSelectionDataError(
                    f"Robot class '{class_id}' is not allowed for task '{task_type_key}'.",
                    error_code="CLASS_NOT_ALLOWED_FOR_TASK",
                    robot_class=class_id,
                    family_id=family_id,
                )
            domain = self.get_feasible_robot_selection_domain(task_type_key)
            target_cnode = next((c for c in domain["classes"] if c["class_id"] == class_id), None)
            target_fnode = next((f for f in target_cnode["families"] if f["family_id"] == family_id), None) if target_cnode else None
            if not target_fnode:
                raise RobotSelectionDataError(
                    f"Family '{family_id}' does not satisfy required capabilities for task '{task_type_key}'.",
                    error_code="FAMILY_CAPABILITY_MISMATCH",
                    robot_class=class_id,
                    family_id=family_id,
                )
            return [
                {
                    "variant_id": v["variant_id"],
                    "full_name": v["full_name"],
                    "family_id": family_id,
                    "robot_class": class_id,
                    "aliases": list(v.get("aliases", [])),
                    "hard_params": dict(v.get("hard_params", {})),
                }
                for v in target_fnode["variants"]
            ]

        variant_items = self.get_model_variants_for_family(family_id)
        if not variant_items:
            raise RobotSelectionDataError(
                f"No model variants found for family '{family_id}'.",
                error_code="NO_VARIANTS_FOR_FAMILY",
                robot_class=class_id,
                family_id=family_id,
            )

        result = []
        for variant_id, variant in variant_items:
            result.append({
                "variant_id": variant_id,
                "full_name": variant.get("full_name", variant_id),
                "family_id": family_id,
                "robot_class": class_id,
                "aliases": list(variant.get("aliases", [])),
                "hard_params": dict(variant.get("hard_params", {})),
            })
        return result

    def list_robot_units(
        self,
        robot_class: str,
        family: str,
        specification: dict | Any,
        task_type_key: str | None = None,
    ) -> list[dict]:
        self._validate_task_type_key(task_type_key)
        self._validate_fleet_units_integrity()
        self.list_robot_variants(robot_class, family, task_type_key)
        class_id = self._resolve_class_key(robot_class)
        family_id = self._resolve_family_key(family)

        spec_variant_id = None
        if isinstance(specification, dict):
            spec_variant_id = specification.get("variant_id")
        elif isinstance(specification, str):
            rov = self.get_rov_for_task(specification, task_type_key, family_id)
            if not rov:
                rov = self.get_rov(specification)
            if rov:
                spec_variant_id = rov.get("variant_id")
            else:
                spec_variant_id = specification

        all_variants = self.robot_fleet.get("model_variants", {})
        if not spec_variant_id or spec_variant_id not in all_variants:
            raise RobotSelectionDataError(
                f"Variant '{spec_variant_id}' does not exist.",
                error_code="VARIANT_NOT_FOUND",
                robot_class=class_id,
                family_id=family_id,
                variant_id=spec_variant_id,
            )

        variant_data = all_variants[spec_variant_id]
        if variant_data.get("family_id") != family_id:
            raise RobotSelectionDataError(
                f"Variant '{spec_variant_id}' belongs to family '{variant_data.get('family_id')}', not '{family_id}'.",
                error_code="VARIANT_FAMILY_MISMATCH",
                robot_class=class_id,
                family_id=family_id,
                variant_id=spec_variant_id,
            )

        units = []
        for u in self.robot_fleet.get("fleet_units", []):
            if u.get("variant_id") == spec_variant_id:
                units.append({
                    "unit_id": u.get("unit_id"),
                    "variant_id": u.get("variant_id"),
                    "serial_no": u.get("serial_no"),
                    "display_name": u.get("display_name"),
                    "status_ref": u.get("status_ref"),
                    "aliases": list(u.get("aliases", [])),
                })
        return units

    def validate_static_robot_selection(
        self,
        robot_class: str,
        family: str,
        specification: dict | Any,
        unit_id: str,
        task_type_key: str | None = None,
    ) -> dict:
        self._validate_task_type_key(task_type_key)
        self._validate_fleet_units_integrity()
        class_id = self._resolve_class_key(robot_class)
        family_id = self._resolve_family_key(family)

        spec_variant_id = None
        if isinstance(specification, dict):
            spec_variant_id = specification.get("variant_id")
        elif isinstance(specification, str):
            rov = self._resolve_robot_variant_exact(specification)
            spec_variant_id = rov.get("variant_id") if rov else None

        if not spec_variant_id:
            raise RobotSelectionDataError(
                f"Variant '{specification}' does not exist.",
                error_code="VARIANT_NOT_FOUND",
                robot_class=class_id,
                family_id=family_id,
                actual_value=specification,
            )
        canonical_specification = {"variant_id": spec_variant_id}
        units = self.list_robot_units(
            robot_class,
            family,
            canonical_specification,
            task_type_key,
        )

        matching_units = [u for u in self.robot_fleet.get("fleet_units", []) if u.get("unit_id") == unit_id]
        if len(matching_units) > 1:
            raise RobotSelectionDataError(
                f"Duplicate unit_id '{unit_id}' found in fleet configuration.",
                error_code="DUPLICATE_UNIT_ID",
                actual_value=unit_id,
            )
        if not matching_units:
            raise RobotSelectionDataError(
                f"Fleet unit '{unit_id}' does not exist.",
                error_code="UNIT_NOT_FOUND",
                robot_class=class_id,
                family_id=family_id,
                variant_id=spec_variant_id,
                actual_value=unit_id,
            )

        target_unit = matching_units[0]
        if target_unit.get("variant_id") != spec_variant_id:
            raise RobotSelectionDataError(
                f"Fleet unit '{unit_id}' belongs to variant '{target_unit.get('variant_id')}', not '{spec_variant_id}'.",
                error_code="UNIT_VARIANT_MISMATCH",
                robot_class=class_id,
                family_id=family_id,
                variant_id=spec_variant_id,
                actual_value=unit_id,
            )

        class_cfg = self.robot_fleet.get("robot_classes", {}).get(class_id, {})
        family_cfg = self.robot_fleet.get("robot_families", {}).get(family_id, {})
        variant_cfg = self.robot_fleet.get("model_variants", {}).get(spec_variant_id, {})

        return {
            "robot_class": class_id,
            "robot_class_name": class_cfg.get("full_name", class_id),
            "family_id": family_id,
            "family_name": family_cfg.get("full_name", family_id),
            "equipment_type": variant_cfg.get("full_name", spec_variant_id),
            "specification": specification if isinstance(specification, dict) else None,
            "variant_id": spec_variant_id,
            "variant_name": variant_cfg.get("full_name", spec_variant_id),
            "unit_id": target_unit.get("unit_id"),
            "unit_display_name": target_unit.get("display_name"),
            "unit": {
                "unit_id": target_unit.get("unit_id"),
                "variant_id": target_unit.get("variant_id"),
                "serial_no": target_unit.get("serial_no"),
                "display_name": target_unit.get("display_name"),
                "status_ref": target_unit.get("status_ref"),
                "aliases": list(target_unit.get("aliases", [])),
            },
        }

    def get_robot_classes(self) -> dict:
        return self.robot_fleet.get("robot_classes", {})

    def get_robot_class_labels(self) -> list[str]:
        return [v.get("full_name", k) for k, v in self.get_robot_classes().items()]

    def get_task_allowed_robot_classes(self, task_type_key: str | None) -> list[str]:
        if not task_type_key:
            return [item.get("class_id") for item in self.list_robot_classes()]
        domain = self.get_feasible_robot_selection_domain(task_type_key)
        return [item.get("class_id") for item in domain.get("classes", [])]

    def get_task_required_capabilities(self, task_type_key: str | None) -> list[str]:
        if not task_type_key:
            return []
        template = self.task_schemas.get("task_templates", {}).get(task_type_key, {})
        return template.get("required_capabilities", [])

    def robot_matches_task(self, robot: dict | None, task_type_key: str | None) -> bool:
        if not robot or not task_type_key:
            return True
        required_caps = set(self.get_task_required_capabilities(task_type_key))
        return required_caps.issubset(set(robot.get("capabilities", [])))

    def get_robot_families_for_classes(
        self,
        robot_class_keys: list[str],
        required_capabilities: list[str] | None = None,
    ) -> list[tuple[str, dict]]:
        required = set(required_capabilities or [])
        allowed_classes = set(robot_class_keys or [])
        result: list[tuple[str, dict]] = []
        robot_families = self.robot_fleet.get("robot_families", {})

        for family_id in robot_families:
            family = robot_families.get(family_id, {})
            f_class = family.get("robot_class")
            if not f_class or not isinstance(f_class, str):
                raise RobotSelectionDataError(
                    f"Family '{family_id}' is missing robot_class grouping metadata '{f_class}'.",
                    error_code="INVALID_ROBOT_CLASS_REFERENCE",
                    expected_field="robot_families.robot_class",
                    actual_value=f_class,
                )
            if allowed_classes and f_class not in allowed_classes:
                continue
            if not required.issubset(set(family.get("capabilities", []))):
                continue
            result.append((family_id, family))
        return result

    def get_robot_families_for_task(self, task_type_key: str | None) -> list[tuple[str, dict]]:
        return self.get_robot_families_for_classes(
            [],
            self.get_task_required_capabilities(task_type_key),
        )

    def get_task_allowed_robot_family_names(self, task_type_key: str | None) -> list[str]:
        """返回当前任务允许询问的机器人族标准名称。"""
        return [
            family.get("full_name", family_id)
            for family_id, family in self.get_robot_families_for_task(task_type_key)
        ]

    def resolve_robot_family_id(
        self,
        family_selector: str,
        task_type_key: str | None = None,
    ) -> str | None:
        """把机器人族 ID、标准名称或别名解析为 family_id。"""
        needle = _norm(family_selector)
        if not needle:
            return None
        families = (
            self.get_robot_families_for_task(task_type_key)
            if task_type_key
            else list(self.robot_fleet.get("robot_families", {}).items())
        )
        for family_id, family in families:
            targets = [family_id, family.get("full_name", ""), *family.get("aliases", [])]
            if any(needle == _norm(target) for target in targets if target):
                return family_id
        return None

    def resolve_robot_family(
        self,
        family_selector: str,
        task_type_key: str | None = None,
    ) -> dict | None:
        """按系列层解析 ID、标准名称或 aliases，并返回标准系列数据。"""
        family_id = self.resolve_robot_family_id(family_selector, task_type_key)
        if not family_id:
            return None
        family = self.robot_fleet.get("robot_families", {}).get(family_id)
        if not family:
            return None
        return {"family_id": family_id, **family}

    def get_model_variants_for_family(self, family_id: str) -> list[tuple[str, dict]]:
        result: list[tuple[str, dict]] = []
        for variant_id, variant in self.robot_fleet.get("model_variants", {}).items():
            if variant.get("family_id") == family_id:
                result.append((variant_id, variant))
        return result

    def get_model_variants_for_task(self, task_type_key: str | None) -> list[tuple[str, dict]]:
        """兼容 main 接口，返回当前任务允许的型号原始配置。"""
        result: list[tuple[str, dict]] = []
        for family_id, _family in self.get_robot_families_for_task(task_type_key):
            result.extend(self.get_model_variants_for_family(family_id))
        return result

    def get_fleet_units_for_variant(self, variant_id: str) -> list[dict]:
        return [
            unit
            for unit in self.robot_fleet.get("fleet_units", [])
            if unit.get("variant_id") == variant_id
        ]

    def _build_robot_variant(
        self,
        robot_classes: dict,
        family_id: str,
        family: dict,
        variant_id: str,
        variant: dict,
    ) -> dict:
        robot_class = family.get("robot_class")
        robot_class_name = robot_classes.get(robot_class, {}).get("full_name", robot_class)
        hard_params = variant.get("hard_params", {}) or {}
        units = self.get_fleet_units_for_variant(variant_id)

        aliases: list[str] = list(variant.get("aliases", []))
        lookup_targets: list[str] = [
            variant.get("full_name", ""),
            variant_id,
        ]
        lookup_targets.extend(aliases)

        deduped_aliases: list[str] = []
        seen_aliases = set()
        for alias in aliases:
            if alias and alias not in seen_aliases:
                deduped_aliases.append(alias)
                seen_aliases.add(alias)

        deduped_lookup_targets: list[str] = []
        seen_targets = set()
        for target in lookup_targets:
            if target and target not in seen_targets:
                deduped_lookup_targets.append(target)
                seen_targets.add(target)

        robot = {
            "model": variant_id,
            "variant_id": variant_id,
            "family_id": family_id,
            "full_name": variant.get("full_name"),
            "family_full_name": family.get("full_name"),
            "robot_class": robot_class,
            "robot_class_name": robot_class_name,
            # Backward-compatible keys used by existing prompts/status code.
            "category": robot_class,
            "category_name": robot_class_name,
            "capabilities": family.get("capabilities", []),
            "aliases": deduped_aliases,
            "_lookup_targets": deduped_lookup_targets,
            "brief": family.get("brief", ""),
            "hard_params": hard_params,
            "fleet_units": units,
            "unit_ids": [u.get("unit_id") for u in units if u.get("unit_id")],
        }
        robot.update(hard_params)
        onboard = hard_params.get("onboard_payloads")
        supported = hard_params.get("supported_payloads")
        onboard_list, onboard_groups = normalize_payload_groups(onboard)
        supported_list, payload_groups = normalize_supported_payloads(supported)
        onboard_set = set(onboard_list)
        clean_supported_list = [p for p in supported_list if p not in onboard_set]
        clean_payload_groups = {
            g: [p for p in items if p not in onboard_set]
            for g, items in payload_groups.items()
        }
        robot["onboard_payloads"] = onboard_list
        robot["onboard_payload_groups"] = onboard_groups
        robot["raw_supported_payloads"] = clean_supported_list
        robot["supported_payloads"] = clean_supported_list
        robot["payload_groups"] = clean_payload_groups
        robot["all_payloads"] = list(dict.fromkeys(onboard_list + clean_supported_list))
        return robot

    def _build_robot_variant_index(self) -> list[dict]:
        robot_classes = self.get_robot_classes()
        robots: list[dict] = []

        for family_id, family in self.get_robot_families_for_classes([]):
            for variant_id, variant in self.get_model_variants_for_family(family_id):
                robots.append(
                    self._build_robot_variant(
                        robot_classes,
                        family_id,
                        family,
                        variant_id,
                        variant,
                    )
                )
        return robots

    def get_all_rovs(self) -> list[dict]:
        if self._robot_variants_cache is None:
            self._robot_variants_cache = self._build_robot_variant_index()
        return list(self._robot_variants_cache)

    def get_task_allowed_robot_variants(
        self,
        task_type_key: str | None,
        family_selector: str | None = None,
        class_selector: str | None = None,
    ) -> list[dict]:
        robots = [
            robot
            for robot in self.get_all_rovs()
            if self.robot_matches_task(robot, task_type_key)
        ]
        if class_selector:
            class_key = self._resolve_class_key(class_selector)
            if class_key:
                robots = [r for r in robots if r.get("robot_class") == class_key]
        if not family_selector:
            return robots
        family_id = self.resolve_robot_family_id(family_selector, task_type_key)
        if not family_id:
            return []
        return [robot for robot in robots if robot.get("family_id") == family_id]

    def get_ROV2type(self) -> dict:
        return {r["full_name"]: r.get("robot_class_name") for r in self.get_all_rovs()}

    def _resolve_robot_class_key(self, value: str) -> str:
        value_norm = _norm(value)
        for key, cfg in self.get_robot_classes().items():
            if value_norm in {_norm(key), _norm(cfg.get("full_name"))}:
                return key
        return value

    @staticmethod
    def _match_robot_variants(
        robots: list[dict],
        selector: str,
    ) -> dict | None:
        needle = _norm(selector)
        if not needle:
            return None

        exact = [
            robot
            for robot in robots
            if any(
                needle == _norm(target)
                for target in robot.get("_lookup_targets", [])
                if target
            )
        ]
        if exact:
            return exact[0] if len(exact) == 1 else None

        partial = [
            robot
            for robot in robots
            if any(
                needle in _norm(target) or _norm(target) in needle
                for target in robot.get("_lookup_targets", [])
                if target
            )
        ]
        return partial[0] if len(partial) == 1 else None

    def _find_rov(self, name: str) -> dict | None:
        """只在 model_variants 层解析型号，不接受系列或单机 aliases。"""
        return self._match_robot_variants(self.get_all_rovs(), name)

    def _resolve_robot_variant_exact(self, selector: str) -> dict | None:
        """Resolve a static Variant selector without interactive substring matching."""
        needle = _norm(selector)
        if not needle:
            return None
        matches = [
            robot
            for robot in self.get_all_rovs()
            if any(
                needle == _norm(target)
                for target in robot.get("_lookup_targets", [])
                if target
            )
        ]
        if len(matches) > 1:
            raise RobotSelectionDataError(
                f"Variant selector '{selector}' is ambiguous.",
                error_code="AMBIGUOUS_VARIANT_SELECTOR",
                expected_field="equipment_type",
                actual_value=selector,
            )
        return matches[0] if matches else None

    def _resolve_robot_unit_exact(
        self,
        unit_selector: str,
        task_type_key: str | None = None,
    ) -> dict | None:
        """Resolve a static Unit selector by canonical ID/full name/declared alias only."""
        needle = _norm(unit_selector)
        if not needle:
            return None

        variants = {robot["variant_id"]: robot for robot in self.get_all_rovs()}
        units = self.robot_fleet.get("fleet_units", [])

        canonical_matches = [
            unit for unit in units if _norm(unit.get("unit_id")) == needle
        ]
        if len(canonical_matches) > 1:
            raise RobotSelectionDataError(
                f"Unit selector '{unit_selector}' is ambiguous.",
                error_code="AMBIGUOUS_UNIT_SELECTOR",
                expected_field="equipment_unit_id",
                actual_value=unit_selector,
            )
        candidate_units = canonical_matches
        if not candidate_units:
            candidate_units = [
                unit
                for unit in units
                if any(
                    needle == _norm(target)
                    for target in (
                        unit.get("display_name", ""),
                        *unit.get("aliases", []),
                    )
                    if target
                )
            ]

        matches = []
        for unit in candidate_units:
            robot = variants.get(unit.get("variant_id"))
            if robot and self.robot_matches_task(robot, task_type_key):
                matches.append({**unit, "robot": robot})
        if len(matches) > 1:
            raise RobotSelectionDataError(
                f"Unit selector '{unit_selector}' is ambiguous.",
                error_code="AMBIGUOUS_UNIT_SELECTOR",
                expected_field="equipment_unit_id",
                actual_value=unit_selector,
            )
        return matches[0] if matches else None

    def resolve_robot_unit(
        self,
        unit_selector: str,
        task_type_key: str | None = None,
        variant_selector: str | None = None,
    ) -> dict | None:
        """按单机层解析 unit_id、display_name 或 aliases；歧义时返回 None。"""
        needle = _norm(unit_selector)
        if not needle:
            return None

        variant = None
        if variant_selector:
            variant = self.get_rov_for_task(variant_selector, task_type_key)
            if not variant:
                return None

        needle_conv = re.sub(r'^(?:改|改成|换成|采用|使用|选择)?\s*(\d+|[零〇一二两三四五六七八九十]+)\s*(?:号\s*)?级$', r'\1号机', needle)
        if needle_conv != needle:
            alt_res = self.resolve_robot_unit(needle_conv, task_type_key, variant_selector)
            if alt_res:
                return alt_res

        all_units = self.robot_fleet.get("fleet_units", [])
        variants = {r["variant_id"]: r for r in self.get_all_rovs()}
        exact_unit_id_matches = [
            u for u in all_units
            if _norm(u.get("unit_id")) == needle
        ]
        if len(exact_unit_id_matches) == 1:
            u = exact_unit_id_matches[0]
            unit_variant = variants.get(u.get("variant_id"))
            if variant and u.get("variant_id") != variant.get("variant_id"):
                return None
            if unit_variant and self.robot_matches_task(unit_variant, task_type_key):
                return {**u, "robot": unit_variant}
            return None

        def matching_units(contains: bool) -> list[dict]:
            matches: list[dict] = []
            variants = {r["variant_id"]: r for r in self.get_all_rovs()}
            for unit in self.robot_fleet.get("fleet_units", []):
                if variant and unit.get("variant_id") != variant.get("variant_id"):
                    continue
                unit_variant = variants.get(unit.get("variant_id"))
                if not unit_variant or not self.robot_matches_task(unit_variant, task_type_key):
                    continue
                targets = [
                    unit.get("unit_id", ""),
                    unit.get("display_name", ""),
                    *unit.get("aliases", []),
                ]
                if contains:
                    matched = any(
                        needle in _norm(target) or _norm(target) in needle
                        for target in targets
                        if target
                    )
                else:
                    matched = any(
                        needle == _norm(target) for target in targets if target
                    )
                if matched:
                    matches.append({**unit, "robot": unit_variant})
            return matches

        exact = matching_units(False)
        if exact:
            return exact[0] if len(exact) == 1 else None
        partial = matching_units(True)
        return partial[0] if len(partial) == 1 else None

    def resolve_robot_unit_from_text(
        self,
        text: str,
        task_type_key: str | None = None,
    ) -> dict | None:
        """从自然语言文本中提取最长匹配的唯一 fleet unit。"""
        if not text or not isinstance(text, str):
            return None
        text_norm = _norm(text)
        alias_index = self.kb.get_device_alias_index()
        unit_matches = []
        for alias, targets in sorted(alias_index.items(), key=lambda x: len(_norm(x[0])), reverse=True):
            if len(_norm(alias)) >= 2 and _norm(alias) in text_norm:
                for target in targets:
                    if target.startswith("unit:"):
                        uid = target.split(":", 1)[1]
                        unit = self.resolve_robot_unit(uid, task_type_key)
                        if unit and not any(u.get("unit_id") == unit.get("unit_id") for u in unit_matches):
                            unit_matches.append(unit)
        if len(unit_matches) == 1:
            return unit_matches[0]
        return None

    def find_rov_by_description(self, description: str) -> list[dict]:
        return self.get_all_rovs()

    def get_rov(self, model_name: str) -> dict | None:
        return self._find_rov(model_name)

    def get_rov_for_task(
        self,
        model_name: str,
        task_type: str | None,
        family_selector: str | None = None,
    ) -> dict | None:
        allowed_variants = self.get_task_allowed_robot_variants(
            task_type,
            family_selector,
        )
        return self._match_robot_variants(allowed_variants, model_name)
