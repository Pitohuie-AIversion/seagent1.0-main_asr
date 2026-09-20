"""
src/knowledge/variant_evaluator.py — 机器人变体规格合法性校验与任务可行性评估引擎
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .models import (
    RobotSelectionDataError,
    RobotVariantFeasibility,
    _payload_match_key,
    normalize_payload_groups,
    normalize_supported_payloads,
)
from ..temporal.simulated_time import get_current_datetime

if TYPE_CHECKING:
    from .selection_engine import RobotSelectionEngine
    from ..knowledge_retriever import KnowledgeBase


class VariantEvaluator:
    """机器人变体规格合法性校验、物理参数约束与可行性评估器。"""

    def __init__(self, engine: "RobotSelectionEngine"):
        self.engine = engine

    @property
    def kb(self) -> "KnowledgeBase":
        return self.engine.kb

    @property
    def robot_fleet(self) -> dict:
        return self.engine.robot_fleet

    @property
    def task_schemas(self) -> dict:
        return self.engine.task_schemas

    @property
    def state_info(self) -> Any:
        return self.engine.state_info

    def validate_task_type_key(self, task_type_key: str | None) -> dict | None:
        """校验任务类型合法性，未配置则抛出 TASK_TEMPLATE_NOT_FOUND 异常"""
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

    def validate_fleet_units_integrity(self) -> None:
        """校验全量实体机配置的完整性、唯一性及对变体的有效引用"""
        self.validate_model_variants_integrity()
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

    def validate_model_variants_integrity(self) -> None:
        """校验机型变体配置的完整性及对系列的有效引用"""
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

    def extract_and_validate_variant_spec(
        self,
        robot_class_id: str,
        family_id: str,
        variant_id: str,
        variant: dict,
    ) -> dict:
        """提取并强类型校验变体的规格参数（AUV径向尺寸/工作级功率）"""
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
    def validated_positive_number(
        value: Any,
        *,
        error_code: str,
        field_name: str,
    ) -> float:
        """校验并转换有限正浮点数值"""
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
        """评估特定变体与任务物理需求（水深、机载与支持载荷）的可行性匹配"""
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
            required_depth = cls.validated_positive_number(
                water_depth,
                error_code="INVALID_WATER_DEPTH",
                field_name="water_depth",
            )
            max_depth = cls.validated_positive_number(
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
    def task_starts_within_runtime_window(
        task_state: dict | None,
        *,
        time_window_minutes: int = 60,
    ) -> bool:
        """检查任务开始时间是否处于实时遥测有效时间窗口（默认 60 分钟）之内"""
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
        """从任务状态中校验最深层的显式机器人选择器及其所有的父级边"""
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
            resolved_explicit_variant = self.engine.resolve_robot_variant_exact(
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
            resolved_unit = self.engine.resolve_robot_unit_exact(
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
            allowed_variants = self.engine.list_robot_variants(
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
                    robot_class=self.engine.resolve_class_key(selected_class),
                    family_id=self.engine.resolve_family_key(selected_family),
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
            family_id = self.engine.resolve_family_key(explicit_family)
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
            resolved_class = self.engine.resolve_class_key(selected_class)
            if resolved_class != canonical_class:
                raise RobotSelectionDataError(
                    f"Family '{family_id}' belongs to class '{canonical_class}', not '{selected_class}'.",
                    error_code="FAMILY_CLASS_MISMATCH",
                    robot_class=resolved_class,
                    family_id=family_id,
                )
            allowed_families = self.engine.list_robot_families(
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

        resolved_class = self.engine.resolve_class_key(explicit_class)
        if not resolved_class:
            raise RobotSelectionDataError(
                f"Robot class '{explicit_class}' not found.",
                error_code="ROBOT_CLASS_NOT_FOUND",
                expected_field="equipment_class",
                actual_value=explicit_class,
            )
        allowed_classes = self.engine.list_robot_classes(task_type_key)
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

    def validate_static_robot_selection(
        self,
        robot_class: str,
        family: str,
        specification: dict | Any,
        unit_id: str,
        task_type_key: str | None = None,
    ) -> dict:
        """从 4 级全量静态维度校验具体 unit_id 的归属、变体匹配及任务支持度"""
        self.validate_task_type_key(task_type_key)
        self.validate_fleet_units_integrity()
        class_id = self.engine.resolve_class_key(robot_class)
        family_id = self.engine.resolve_family_key(family)

        spec_variant_id = None
        if isinstance(specification, dict):
            spec_variant_id = specification.get("variant_id")
        elif isinstance(specification, str):
            rov = self.engine.resolve_robot_variant_exact(specification)
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
        self.engine.list_robot_units(
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
