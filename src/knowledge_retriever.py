"""
knowledge_retriever.py — 知识库加载与按需检索
不使用向量数据库，基于任务状态进行规则化知识片段选取。
知识总量在10000字以内，精准注入比全量注入更高效。
"""

import yaml
import math
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any
from .environment_info import EnvironmentInfo
from .state_info import RobotStateInfo

CONFIG_DIR = Path(__file__).parent.parent / "config"


def _load(filename: str) -> dict | list:
    with open(CONFIG_DIR / filename, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _norm(value: object) -> str:
    return str(value or "").lower().replace(" ", "")


class RobotSelectionDataError(ValueError):
    """Exception raised when robot cascade query or static validation encounters invalid data or mismatched relationships."""

    def __init__(
        self,
        message: str,
        error_code: str,
        robot_class: str | None = None,
        family_id: str | None = None,
        variant_id: str | None = None,
        expected_field: str | None = None,
        actual_value: Any = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.robot_class = robot_class
        self.family_id = family_id
        self.variant_id = variant_id
        self.expected_field = expected_field
        self.actual_value = actual_value

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "robot_class": self.robot_class,
            "family_id": self.family_id,
            "variant_id": self.variant_id,
            "expected_field": self.expected_field,
            "actual_value": self.actual_value,
            "message": str(self),
        }


class KnowledgeBase:
    def __init__(self):
        self.load_all()  # 改成调用方法

    # ✅ 新增：热重载配置
    def load_all(self):
        self.task_schemas: dict = _load("task_schemas.yaml")
        self.robot_fleet: dict = _load("robot_fleet.yaml")
        self.assets: dict = _load("assets.yaml")
        self.constraints: list = _load("constraints.yaml")["constraints"]
        self.environment: dict = _load("environment.yaml")

        self.env_info = EnvironmentInfo()
        self.state_info = RobotStateInfo()
        self._robot_variants_cache: list[dict] | None = None
        self.ROV2type = self.get_ROV2type()

    # ──────────────────────────────────────────────────────────────────────────
    # 新机器人索引：robot_classes -> robot_families -> model_variants -> fleet_units
    # ──────────────────────────────────────────────────────────────────────────

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
        return None

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

    def list_robot_classes(self, task_type_key: str | None = None) -> list[dict]:
        template = self._validate_task_type_key(task_type_key)
        robot_classes = self.get_robot_classes()
        if template is not None:
            allowed_classes = template.get("allowed_robot_classes", [])
            result = []
            for class_id in allowed_classes:
                if class_id not in robot_classes:
                    raise RobotSelectionDataError(
                        f"Task template '{task_type_key}' references non-existent robot_class '{class_id}'.",
                        error_code="INVALID_ROBOT_CLASS_REFERENCE",
                        expected_field="robot_classes",
                        actual_value=class_id,
                    )
                cfg = robot_classes[class_id]
                result.append({
                    "class_id": class_id,
                    "robot_class": class_id,
                    "full_name": cfg.get("full_name", class_id),
                })
            return result

        return [
            {
                "class_id": class_id,
                "robot_class": class_id,
                "full_name": cfg.get("full_name", class_id),
            }
            for class_id, cfg in robot_classes.items()
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
            allowed_classes = self.get_task_allowed_robot_classes(task_type_key)
            if class_id not in allowed_classes:
                raise RobotSelectionDataError(
                    f"Robot class '{class_id}' is not allowed for task '{task_type_key}'.",
                    error_code="CLASS_NOT_ALLOWED_FOR_TASK",
                    robot_class=class_id,
                )
            required_caps = set(self.get_task_required_capabilities(task_type_key))
        else:
            required_caps = set()

        result = []
        robot_classes = self.get_robot_classes()
        for family_id, family in self.robot_fleet.get("robot_families", {}).items():
            f_class = family.get("robot_class")
            if not f_class or f_class not in robot_classes:
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

            if required_caps and not required_caps.issubset(set(caps)):
                continue

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
        if not f_class or f_class not in self.get_robot_classes():
            raise RobotSelectionDataError(
                f"Family '{family_id}' references missing or invalid robot_class '{f_class}'.",
                error_code="INVALID_ROBOT_CLASS_REFERENCE",
                expected_field="robot_classes",
                actual_value=f_class,
            )

        if f_class != class_id:
            raise RobotSelectionDataError(
                f"Family '{family_id}' belongs to class '{f_class}', not '{class_id}'.",
                error_code="FAMILY_CLASS_MISMATCH",
                robot_class=class_id,
                family_id=family_id,
            )

        if task_type_key is not None:
            allowed_classes = self.get_task_allowed_robot_classes(task_type_key)
            if class_id not in allowed_classes:
                raise RobotSelectionDataError(
                    f"Robot class '{class_id}' is not allowed for task '{task_type_key}'.",
                    error_code="CLASS_NOT_ALLOWED_FOR_TASK",
                    robot_class=class_id,
                    family_id=family_id,
                )
            required_caps = set(self.get_task_required_capabilities(task_type_key))
            family_caps = family_data.get("capabilities", [])
            if not isinstance(family_caps, list):
                raise RobotSelectionDataError(
                    f"Family '{family_id}' capabilities must be a list.",
                    error_code="INVALID_FAMILY_CAPABILITIES",
                    family_id=family_id,
                    actual_value=family_caps,
                )
            if required_caps and not required_caps.issubset(set(family_caps)):
                raise RobotSelectionDataError(
                    f"Family '{family_id}' does not satisfy required capabilities {required_caps} for task '{task_type_key}'.",
                    error_code="FAMILY_CAPABILITY_MISMATCH",
                    robot_class=class_id,
                    family_id=family_id,
                )

        variant_items = self.get_model_variants_for_family(family_id)
        if not variant_items:
            raise RobotSelectionDataError(
                f"No model variants found for family '{family_id}'.",
                error_code="NO_VARIANTS_FOR_FAMILY",
                robot_class=class_id,
                family_id=family_id,
            )

        specifications = []
        for variant_id, variant in variant_items:
            spec = self._extract_and_validate_variant_spec(class_id, family_id, variant_id, variant)
            specifications.append(spec)

        return specifications

    def list_robot_units(
        self,
        robot_class: str,
        family: str,
        specification: dict | Any,
        task_type_key: str | None = None,
    ) -> list[dict]:
        self._validate_task_type_key(task_type_key)
        self._validate_fleet_units_integrity()
        specifications = self.list_robot_specifications(robot_class, family, task_type_key)
        class_id = self._resolve_class_key(robot_class)
        family_id = self._resolve_family_key(family)

        if not isinstance(specification, dict):
            raise RobotSelectionDataError(
                f"Specification must be a dictionary, got {type(specification).__name__}.",
                error_code="INVALID_SPECIFICATION_FORMAT",
                robot_class=class_id,
                family_id=family_id,
                actual_value=specification,
            )

        spec_type = specification.get("type")
        spec_value = specification.get("value")
        spec_variant_id = specification.get("variant_id")

        expected_type = "diameter_mm" if class_id == "auv" else "power_hp"
        if spec_type != expected_type:
            raise RobotSelectionDataError(
                f"Specification type '{spec_type}' does not match class '{class_id}' (expected '{expected_type}').",
                error_code="SPECIFICATION_TYPE_MISMATCH",
                robot_class=class_id,
                family_id=family_id,
                variant_id=spec_variant_id,
                expected_field=expected_type,
                actual_value=spec_type,
            )

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

        matched_spec = None
        for s in specifications:
            if s["variant_id"] == spec_variant_id:
                matched_spec = s
                break

        if not matched_spec:
            raise RobotSelectionDataError(
                f"Specification for variant '{spec_variant_id}' could not be matched.",
                error_code="SPECIFICATION_MATCH_FAILED",
                robot_class=class_id,
                family_id=family_id,
                variant_id=spec_variant_id,
            )

        if matched_spec.get("value") is None:
            if spec_value is not None:
                raise RobotSelectionDataError(
                    f"Specification value '{spec_value}' does not match variant value 'None'.",
                    error_code="SPECIFICATION_VALUE_MISMATCH",
                    robot_class=class_id,
                    family_id=family_id,
                    variant_id=spec_variant_id,
                    expected_field=expected_type,
                    actual_value=spec_value,
                )
        else:
            if spec_value is None or isinstance(spec_value, bool) or not isinstance(spec_value, (int, float)) or not math.isfinite(spec_value):
                raise RobotSelectionDataError(
                    f"Specification value '{spec_value}' is invalid.",
                    error_code="SPECIFICATION_VALUE_MISMATCH",
                    robot_class=class_id,
                    family_id=family_id,
                    variant_id=spec_variant_id,
                    expected_field=expected_type,
                    actual_value=spec_value,
                )

            if abs(float(spec_value) - float(matched_spec["value"])) > 1e-6:
                raise RobotSelectionDataError(
                    f"Specification value '{spec_value}' does not match variant value '{matched_spec['value']}'.",
                    error_code="SPECIFICATION_VALUE_MISMATCH",
                    robot_class=class_id,
                    family_id=family_id,
                    variant_id=spec_variant_id,
                    expected_field=expected_type,
                    actual_value=spec_value,
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
        units = self.list_robot_units(robot_class, family, specification, task_type_key)
        class_id = self._resolve_class_key(robot_class)
        family_id = self._resolve_family_key(family)
        spec_variant_id = specification.get("variant_id") if isinstance(specification, dict) else None

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

        specifications = self.list_robot_specifications(class_id, family_id, task_type_key)
        matched_spec = next(s for s in specifications if s["variant_id"] == spec_variant_id)

        class_cfg = self.robot_fleet.get("robot_classes", {}).get(class_id, {})
        family_cfg = self.robot_fleet.get("robot_families", {}).get(family_id, {})
        variant_cfg = self.robot_fleet.get("model_variants", {}).get(spec_variant_id, {})

        return {
            "robot_class": class_id,
            "robot_class_name": class_cfg.get("full_name", class_id),
            "family_id": family_id,
            "family_name": family_cfg.get("full_name", family_id),
            "specification": matched_spec,
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
            return list(self.get_robot_classes().keys())
        template = self.task_schemas.get("task_templates", {}).get(task_type_key, {})
        return template.get("allowed_robot_classes", [])

    def get_task_required_capabilities(self, task_type_key: str | None) -> list[str]:
        if not task_type_key:
            return []
        template = self.task_schemas.get("task_templates", {}).get(task_type_key, {})
        return template.get("required_capabilities", [])

    def robot_matches_task(self, robot: dict | None, task_type_key: str | None) -> bool:
        if not robot or not task_type_key:
            return True
        allowed_classes = set(self.get_task_allowed_robot_classes(task_type_key))
        required_caps = set(self.get_task_required_capabilities(task_type_key))
        if allowed_classes and robot.get("robot_class") not in allowed_classes:
            return False
        return required_caps.issubset(set(robot.get("capabilities", [])))

    def get_robot_families_for_classes(
        self,
        robot_class_keys: list[str],
        required_capabilities: list[str] | None = None,
    ) -> list[tuple[str, dict]]:
        required = set(required_capabilities or [])
        allowed_classes = set(robot_class_keys or [])
        result: list[tuple[str, dict]] = []
        for family_id, family in self.robot_fleet.get("robot_families", {}).items():
            if allowed_classes and family.get("robot_class") not in allowed_classes:
                continue
            if not required.issubset(set(family.get("capabilities", []))):
                continue
            result.append((family_id, family))
        return result

    def get_robot_families_for_task(self, task_type_key: str | None) -> list[tuple[str, dict]]:
        return self.get_robot_families_for_classes(
            self.get_task_allowed_robot_classes(task_type_key),
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

        # 型号索引只能包含 model_variants 自身的标识与别名。系列和单机
        # 分别由独立解析接口处理，避免同一个 alias 跨层命中。
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
        onboard_list = list(onboard) if isinstance(onboard, list) else []
        supported_list = list(supported) if isinstance(supported, list) else []
        robot["onboard_payloads"] = onboard_list
        robot["supported_payloads"] = supported_list
        robot["all_payloads"] = list(dict.fromkeys(onboard_list + supported_list))
        return robot

    def _build_robot_variant_index(self) -> list[dict]:
        robot_classes = self.get_robot_classes()
        families = self.robot_fleet.get("robot_families", {})
        robots: list[dict] = []

        # Layered traversal: class -> family -> variant -> fleet unit.
        # Do not discover variants by jumping directly into the full variant set
        # for task matching; relationship fields are the source of truth.
        for robot_class_key in robot_classes:
            family_items = self.get_robot_families_for_classes([robot_class_key])
            for family_id, family in family_items:
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
    ) -> list[dict]:
        robots = [
            robot
            for robot in self.get_all_rovs()
            if self.robot_matches_task(robot, task_type_key)
        ]
        if not family_selector:
            return robots
        family_id = self.resolve_robot_family_id(family_selector, task_type_key)
        if not family_id:
            return []
        return [robot for robot in robots if robot.get("family_id") == family_id]

    def get_ROV2type(self) -> dict:
        return {r["full_name"]: r.get("robot_class_name") for r in self.get_all_rovs()}

    # ──────────────────────────────────────────────────────────────────────────
    # 按任务状态选取相关知识片段
    # ──────────────────────────────────────────────────────────────────────────

    def get_supported_task(self) -> list:
        return self.get_all_task_type_values()

    def get_context_for_state(self, task_state: dict) -> str:
        """
        根据当前任务状态，返回最相关的专业知识文本（供注入 system prompt）。
        分段组装，只选取与当前阶段相关的内容。
        """
        task_type = task_state.get("task_type_key")  # e.g. "pipeline_inspection"
        equipment_type = task_state.get("equipment_type")
        legacy_equipment = task_state.get("equipment_name")
        coords = task_state.get("start_point") or task_state.get("oilfield_coordinates")
        sections = [self._robot_category_overview()]

        # 2. 与当前任务类型相关的 ROV 约束
        if task_type:
            sections.append(self._task_rov_constraint(task_type))

        unit_selector = task_state.get("equipment_unit_id")
        resolved_unit = (
            self.resolve_robot_unit(
                str(unit_selector),
                task_type,
                str(equipment_type) if equipment_type else None,
            )
            if unit_selector
            else None
        )
        if not resolved_unit and legacy_equipment:
            resolved_unit = self.resolve_robot_unit(
                str(legacy_equipment),
                task_type,
                str(equipment_type) if equipment_type else None,
            )
        selected_robot = (
            resolved_unit.get("robot")
            if resolved_unit
            else (
                self.get_rov_for_task(
                    str(equipment_type or legacy_equipment),
                    task_type,
                )
                if equipment_type or legacy_equipment
                else None
            )
        )
        if selected_robot:
            rov_info = self._get_rov_info(selected_robot.get("full_name"))
            if rov_info:
                sections.append(f"【当前选定设备详情】\n{rov_info}")
                state_selector = (
                    resolved_unit.get("unit_id")
                    if resolved_unit
                    else selected_robot.get("full_name")
                )
                state_dict = self.get_robot_state_dict(state_selector)
                if state_dict and isinstance(state_dict, dict):
                    state_lines = []
                    label_map = {
                        "current_velocity": "当前流速",
                        "turbidity": "浑浊度",
                        "obstacle_density": "障碍物密度",
                        "mothership_support": "母船支援",
                        "update_timestamp": "更新时间",
                        "confidence": "置信度",
                        "overall_status": "总体状态",
                        "survival_status": "生存状态",
                        "thruster_status": "推进器状态",
                        "depth_keeping_status": "定深能力",
                        "sonar_status": "声呐状态",
                        "vision_status": "视觉系统状态",
                        "arm_status": "机械臂状态",
                        "end_effector_status": "末端执行器状态",
                        "acoustic_comms_status": "水声无线通信状态",
                        "tether_connection_status": "脐带缆连接状态"
                    }
                    for k, v in state_dict.items():
                        if v is not None and not k.startswith("_"):
                            label = label_map.get(k, k)
                            if isinstance(v, float):
                                state_lines.append(f"  - {label} ({k}): {v:.2f}")
                            else:
                                state_lines.append(f"  - {label} ({k}): {v}")
                    if state_lines:
                        sections.append("【当前设备实时状态】\n" + "\n".join(state_lines))
        elif task_type:
            sections.append(self._rovs_for_task(task_type))

        # 4. 管缆类型（管缆巡检任务）
        if task_type == "pipeline_inspection":
            sections.append(self._cable_types_overview())
            sections.append(self._payload_suggestions("pipeline_inspection"))
        elif task_type == "pipeline_burial":
            sections.append(self._cable_types_overview())
            sections.append(self._payload_suggestions("pipeline_burial"))
        elif task_type == "tree_valve_operation":
            sections.append(self._payload_suggestions("tree_valve_operation"))

        # 6. 支持船信息
        sections.append(self._vessels_overview())

        # 7. 海域环境（有坐标时匹配）
        if coords:
            env_info = self._get_environment(coords)
            if env_info:
                sections.append(f"【作业区域环境状态】\n{env_info}")

        # 8. 适用约束规则摘要
        sections.append(self._relevant_constraints(task_type))

        return "\n\n".join(s for s in sections if s and s.strip())

    # ──────────────────────────────────────────────────────────────────────────
    # 内部片段构建方法
    # ──────────────────────────────────────────────────────────────────────────

    def _robot_category_overview(self) -> str:
        lines = ["【机器人四大类说明】"]
        for key, value in self.get_robot_classes().items():
            lines.append(f"- {value.get('full_name', key)}（{key}）")
        return "\n".join(lines)

    def _task_rov_constraint(self, task_type: str) -> str:
        schema = self.task_schemas["task_templates"].get(task_type, {})
        class_names = [
            self.get_robot_classes().get(key, {}).get("full_name", key)
            for key in schema.get("allowed_robot_classes", [])
        ]
        caps = "、".join(schema.get("required_capabilities", []))
        if not class_names and not caps:
            return ""
        return (
            f"【任务设备约束】{schema.get('display_name', task_type)} 任务只允许使用"
            f"{'、'.join(class_names)}，且设备能力必须覆盖：{caps or '无特殊能力'}。"
        )

    def _rovs_by_category(self, category_value: str, task_type: str | None = None) -> str:
        class_key = self._resolve_robot_class_key(category_value)
        cat_label = self.get_robot_classes().get(class_key, {}).get("full_name", category_value)
        rovs = [
            r for r in self.get_all_rovs()
            if r.get("robot_class") == class_key and self.robot_matches_task(r, task_type)
        ]
        if not rovs:
            return f"【{cat_label}】当前无符合任务条件的设备。"
        lines = [f"【{cat_label}设备列表】"]
        for r in rovs:
            lines.append(f"- {r['full_name']} | 最大水深:{r.get('max_depth_m')}m\n  {r.get('brief', '')}")
        return "\n".join(lines)

    def _rovs_for_task(self, task_type: str) -> str:
        rovs = self.get_task_allowed_robot_variants(task_type)
        if not rovs:
            return "【任务可用设备】当前无符合任务条件的设备。"
        lines = ["【任务可用设备】"]
        for r in rovs:
            lines.append(
                f"- {r['full_name']} | 类型:{r.get('robot_class_name')} | "
                f"能力:{'、'.join(r.get('capabilities', []))} | 最大水深:{r.get('max_depth_m')}m"
            )
        return "\n".join(lines)

    def _get_rov_info(self, model_or_alias: str) -> str | None:
        rov = self._find_rov(model_or_alias)
        if not rov:
            return None
        onboard_list = rov.get("onboard_payloads", [])
        supported_list = rov.get("supported_payloads", [])
        if onboard_list:
            onboard_str = "、".join(onboard_list)
            supported_str = "、".join(supported_list)
            payload_desc = f"自带载荷: {onboard_str}\n可选搭载载荷: {supported_str}"
        else:
            payloads = "、".join(supported_list)
            payload_desc = f"可搭载载荷: {payloads}"
        return (
            f"{rov['full_name']}\n"
            f"类型: {rov.get('robot_class_name')} | 能力: {'、'.join(rov.get('capabilities', []))} | "
            f"最大水深: {rov.get('max_depth_m')}m\n"
            f"{payload_desc}\n"
            f"简介: {rov.get('brief', '')}"
        )

    def _find_rov(self, name: str) -> dict | None:
        """只在 model_variants 层解析型号，不接受系列或单机 aliases。"""
        return self._match_robot_variants(self.get_all_rovs(), name)

    @staticmethod
    def _match_robot_variants(
        robots: list[dict],
        selector: str,
    ) -> dict | None:
        """在限定型号集合内匹配唯一结果，避免别名碰撞时取第一个。"""
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

        # 1. 优先尝试全库 unit_id 精确匹配
        all_units = self.robot_fleet.get("fleet_units", [])
        variants = {r["variant_id"]: r for r in self.get_all_rovs()}
        exact_unit_id_matches = [
            u for u in all_units
            if _norm(u.get("unit_id")) == needle
        ]
        if len(exact_unit_id_matches) == 1:
            u = exact_unit_id_matches[0]
            unit_variant = variants.get(u.get("variant_id"))
            if unit_variant and self.robot_matches_task(unit_variant, task_type_key):
                return {**u, "robot": unit_variant}


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

    def find_rov_by_description(self, description: str) -> list[dict]:
        return self.get_all_rovs()

    def _resolve_robot_class_key(self, value: str) -> str:
        value_norm = _norm(value)
        for key, cfg in self.get_robot_classes().items():
            if value_norm in {_norm(key), _norm(cfg.get("full_name"))}:
                return key
        return value

    def _cable_types_overview(self) -> str:
        types = self.assets["cable_types"]
        lines = ["【管缆类型（必须使用标准名称）】"]
        for t in types:
            aliases = "、".join(t["aliases"][:4])
            lines.append(f"- {t['label']}（别名：{aliases}）")
        return "\n".join(lines)

    def _payload_suggestions(self, task_type: str) -> str:
        pt = self.assets["payload_options"].get(task_type, {})
        common = "、".join(pt.get("common", []))
        desc = pt.get("description", "")
        label = self.task_schemas.get("task_templates", {}).get(task_type, {}).get("display_name", task_type)
        return f"【{label}常用携带工具建议】\n{common}\n备注: {desc}"

    def _vessels_overview(self) -> str:
        lines = ["【可用支持船只列表】"]
        for v in self.assets["vessels"]:
            status = "✓ 可用" if v["available"] else "✗ 不可用"
            lines.append(f"- {v['full_name']}（{v['type']}）[{status}] — {v['description']}")
        return "\n".join(lines)

    def _get_environment(self, coords: dict) -> str | None:
        oil_field = self.get_environment_for_coords(coords)
        if not oil_field:
            return None
        return (
            f"{oil_field['name']} \n"
            f"海底底质: {oil_field['seabed_type']}\n"
            f"备注: {oil_field['notes']}"
        )

    def _relevant_constraints(self, task_type: str | None) -> str:
        lines = ["【相关作业约束规则】"]
        for c in self.constraints:
            applies = c["applies_to"]
            if "all" not in applies and task_type and task_type not in applies:
                continue
            lines.append(f"[{c['id']}] {c['name']}: {c['violation_message'].strip()}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────────────────
    # 直接查询接口（供 validator / builder 使用）
    # ──────────────────────────────────────────────────────────────────────────

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

    def get_vessel(self, vessel_id: str) -> dict | None:
        vid_lower = vessel_id.lower().replace(" ", "")
        for v in self.assets["vessels"]:
            targets = [v["id"].lower()] + [a.lower().replace(" ", "") for a in v.get("aliases", [])]
            if any(vid_lower in t or t in vid_lower for t in targets):
                return v
        return None

    def get_task_schema(self, template_key: str) -> dict:
        """返回指定任务模板，兼容 main 的公开查询接口。"""
        return self.task_schemas.get("task_templates", {}).get(template_key, {})

    def get_task_type_map(self) -> dict[str, str]:
        """
        从 task_schemas.yaml 动态构建 {task_type_value: template_key} 反查字典。
        例如：{"管缆巡检": "pipeline_inspection",
               "采油树控制面板插入": "tree_valve_operation", ...}
        """
        mapping: dict[str, str] = {}
        for template_key, cfg in self.task_schemas["task_templates"].items():
            for value in cfg.get("task_type_values", []):
                mapping[value] = template_key
        return mapping

    def get_all_task_type_values(self) -> list[str]:
        """返回所有合法 task_type 值的平铺列表（供 LLM 提示和拒绝判断用）"""
        values: list[str] = []
        for cfg in self.task_schemas["task_templates"].values():
            values.extend(cfg.get("task_type_values", []))
        return values

    def get_environment_for_coords(self, coords: dict) -> dict | None:
        if not isinstance(coords, dict):
            return None
        lat = coords.get("lat")
        lon = coords.get("lon")
        if lat is None or lon is None:
            return None
        for area in self.environment["oil_fields"]:
            if area["lat_range"][0] <= lat <= area["lat_range"][1]:
                if area["lon_range"][0] <= lon <= area["lon_range"][1]:
                    return area
        return None

    def get_constraints(self) -> list[dict]:
        return self.constraints

    def get_environment_info_dict(self, coords: dict) -> dict:
        """根据坐标返回动态环境信息（随机 + 未知）"""
        empty_info = {
            "forbidden": None,
            "seabed_type": None,
            "obstacle_density": None,
            "acoustic_signal": None,
            "dvl_risk": None,
            "mothership_support": None,
        }
        if not isinstance(coords, dict):
            return empty_info
        lat = coords.get("lat")
        lon = coords.get("lon")
        if lat is None or lon is None:
            return empty_info
        return self.env_info.get_all_info(lat, lon)

    def get_robot_state_dict(self, equipment_selector: str) -> dict:
        empty_state = {
            "current_velocity": None,
            "turbidity": None,
            "battery_percent": None,
            "current_mode": None,
            "communication_status": None,
            "latitude": None,
            "longitude": None,
            "update_timestamp": None,
            "confidence": None,
            "obstacle_density": None,
            "mothership_support": None,
            "overall_status": None,
            "survival_status": None,
            "thruster_status": None,
            "depth_keeping_status": None,
            "sonar_status": None,
            "vision_status": None,
            "arm_status": None,
            "end_effector_status": None,
            "acoustic_comms_status": None,
            "tether_connection_status": None,
        }
        if not equipment_selector or not isinstance(equipment_selector, str):
            return empty_state

        state = self.state_info.get_all_info(equipment_selector)
        if isinstance(state, dict):
            return state
        return empty_state

    def get_unit_state_snapshot(self, unit_id: str) -> dict:
        return self.state_info.get_unit_state_snapshot(unit_id)

    def check_runtime_availability(self, unit_id: str, *, max_age_seconds: int = 300) -> dict:
        return self.state_info.check_runtime_availability(unit_id, max_age_seconds=max_age_seconds)

    # ──────────────────────────────────────────────────────────────────────────
    # 动态设备词表与强类型只读查询
    # ──────────────────────────────────────────────────────────────────────────

    def get_device_alias_index(self) -> dict[str, list[str]]:
        """按类别、系列、型号、单机分层构建设备别名索引。

        索引值带有实体层级前缀，只用于只读路由和歧义判断，避免同名 ID
        在不同层级之间被误认为同一个实体。
        """
        index: dict[str, set[str]] = {}
        display_aliases: dict[str, str] = {}

        def add(alias: Any, target: str) -> None:
            if not isinstance(alias, str):
                return
            display = alias.strip()
            normalized = _norm(display)
            if not normalized:
                return
            canonical = display_aliases.setdefault(normalized, display)
            index.setdefault(canonical, set()).add(target)

        for family_id, family in self.robot_fleet.get("robot_families", {}).items():
            target = f"family:{family_id}"
            add(family_id, target)
            add(family.get("full_name"), target)
            for alias in family.get("aliases", []):
                add(alias, target)

        for variant_id, variant in self.robot_fleet.get("model_variants", {}).items():
            target = f"variant:{variant_id}"
            add(variant_id, target)
            add(variant.get("full_name"), target)
            for alias in variant.get("aliases", []):
                add(alias, target)

        for unit in self.robot_fleet.get("fleet_units", []):
            unit_id = unit.get("unit_id")
            if not unit_id:
                continue
            target = f"unit:{unit_id}"
            for field in ("unit_id", "display_name", "serial_no", "status_ref"):
                add(unit.get(field), target)
            for alias in unit.get("aliases", []):
                add(alias, target)

        return {alias: sorted(targets) for alias, targets in index.items()}

    def get_ambiguous_device_terms(self) -> set[str]:
        """返回映射到多个分层实体的设备别名。"""
        return {
            alias
            for alias, targets in self.get_device_alias_index().items()
            if len(targets) > 1
        }

    def get_all_device_terms(self) -> set[str]:
        """返回可安全用于意图路由的非歧义设备词集合。"""
        alias_index = self.get_device_alias_index()
        ambiguous = {
            alias for alias, targets in alias_index.items() if len(targets) > 1
        }
        terms = {
            alias
            for alias in alias_index
            if alias not in ambiguous
            and len(alias.strip()) >= 2
            and not alias.strip().isdigit()
        }
        for class_id, robot_class in self.get_robot_classes().items():
            terms.add(class_id)
            full_name = robot_class.get("full_name")
            if isinstance(full_name, str) and full_name.strip():
                terms.add(full_name.strip())
        return terms

    def execute_typed_query(
        self,
        query_type: str,
        user_message: str,
        context: dict | None = None,
    ) -> dict:
        """执行强类型只读知识查询，并返回稳定的结构化证据。"""
        context = context if isinstance(context, dict) else {}
        response = {
            "query_type": query_type,
            "results": [],
            "found": False,
            "source": "knowledge_base",
            "version": "kb_1.1_hierarchical",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        if query_type == "TOOL_QUERY":
            task_type_key = context.get("task_type_key")
            robots = (
                self.get_task_allowed_robot_variants(task_type_key)
                if task_type_key
                else self.get_all_rovs()
            )
            tool_set: set[str] = set()
            equipment_mappings: list[dict] = []
            for robot in robots:
                onboard = list(robot.get("onboard_payloads", []))
                supported = list(robot.get("supported_payloads", []))
                all_p = list(robot.get("all_payloads", []))
                tool_set.update(all_p)
                equipment_mappings.append({
                    "equipment_type": robot.get("full_name"),
                    "variant_id": robot.get("variant_id"),
                    "family_id": robot.get("family_id"),
                    "robot_class": robot.get("robot_class_name"),
                    "onboard_payloads": onboard,
                    "supported_payloads": supported,
                    "all_payloads": all_p,
                })

            task_payloads = self.assets.get("payload_options", {})
            payload_catalog = self.assets.get("payload_catalog", {})

            # 匹配特定 payload 工具的独立功能描述
            matched_payloads = []
            msg_norm = user_message.lower()
            if payload_catalog:
                for p_key, p_info in payload_catalog.items():
                    p_name = p_info.get("name", "")
                    p_aliases = p_info.get("aliases", [])
                    if (p_name and p_name.lower() in msg_norm) or any(alias.lower() in msg_norm for alias in p_aliases if alias):
                        matched_payloads.append(p_info)

            current_suggestions = (
                task_payloads.get(task_type_key, {}) if task_type_key else {}
            )
            response["results"] = [
                {"category": "all_supported_tools", "tools": sorted(tool_set)},
                {
                    "category": "equipment_payload_mapping",
                    "mappings": equipment_mappings,
                },
                {
                    "category": "task_payload_suggestions",
                    "task_suggestions": task_payloads,
                    "current_task_suggestions": current_suggestions,
                },
                {
                    "category": "payload_catalog",
                    "catalog": payload_catalog,
                    "matched_payloads": matched_payloads,
                },
            ]
            response["found"] = bool(tool_set or task_payloads or payload_catalog)
            if task_type_key:
                response["used_task_type_key"] = task_type_key
            return response

        if query_type == "DEVICE_CAPABILITY":
            return self._execute_device_capability_query(
                user_message,
                context,
                response,
            )

        if query_type == "KNOWLEDGE_QA":
            response["results"] = [
                {
                    "category": "task_templates",
                    "templates": self.task_schemas.get("task_templates", {}),
                },
                {
                    "category": "constraints_rules",
                    "constraints": [
                        {
                            "id": c.get("id"),
                            "name": c.get("name"),
                            "severity": c.get("severity"),
                            "message": c.get("violation_message", "").strip(),
                            "applies_to": c.get("applies_to", []),
                        }
                        for c in self.constraints
                    ],
                },
                {
                    "category": "workflow_and_persistence_rules",
                    "rules": {
                        "soft_warning_ignore": "当系统产生软约束警告 (blocked_soft) 时，用户可通过明确回复'确认'、'忽略'、'无视'等意图将警告加入白名单并继续流程；软警告忽略不会影响任务数据一致性。",
                        "hard_constraint_blocking": "当任务触发硬约束 (blocked_hard) 时，系统必须阻断发布。硬约束无法被'确认'或'忽略'绕过，必须修改参数直至合规后方可继续。",
                        "task_persistence_location": "任务发布后首先写入 staging 暂存文件，完成跨进程锁校验与原子重命名后持久化至系统的 final 任务目录中，绝对路径按任务类型与日流水号确定。",
                        "dialogue_phases": "系统维护 collecting（收集）、blocked_hard（硬阻断）、blocked_soft（软警告）、confirming（待确认）、done（已发布）、rejected（已拒绝）显式状态机。"
                    },
                },
                {
                    "category": "payload_catalog",
                    "catalog": self.assets.get("payload_catalog", {}),
                },
                {
                    "category": "robot_classes_summary",
                    "classes": self.get_robot_classes(),
                    "families": self.robot_fleet.get("robot_families", {}),
                },
                {
                    "category": "cable_types",
                    "cable_types": self.assets.get("cable_types", []),
                },
                {
                    "category": "vessels",
                    "vessels": self.assets.get("vessels", []),
                },
            ]
            response["found"] = True
            return response

        response["reason"] = "unsupported_query_type"
        return response


    def _execute_device_capability_query(
        self,
        user_message: str,
        context: dict,
        response: dict,
    ) -> dict:
        task_type_key = context.get("task_type_key")
        depth_condition = self._parse_depth_condition(user_message)
        response["depth_condition"] = depth_condition

        matched_alias, entity_targets = self._find_query_entity_targets(user_message)
        if not entity_targets:
            context_selector = context.get("equipment_type")
            if context_selector:
                entity_targets = self._resolve_context_entity_targets(
                    str(context_selector),
                    task_type_key,
                )
                if entity_targets:
                    matched_alias = str(context_selector)

        list_keywords = ("哪些", "列表", "所有", "有哪些", "推荐", "选择", "可用", "什么型号", "查询", "查看", "列出")
        is_list_query = any(keyword in user_message for keyword in list_keywords)

        generic_terms = {"设备", "机器人", "潜水器", "rov", "auv", "hov", "单机", "型号", "工具", "支持", "具备", "配备", "搭载"}
        query_strip_words = (
            "查询", "查看", "列出", "检索", "显示", "获取", "了解", "我要", "我想", "帮我",
            "可以", "能否", "请", "列表", "清单", "可用", "所有", "有哪些", "什么", "哪些",
            "推荐", "选择", "的", "一下", "看看", "知道", "信息", "能力", "状态", "目前", "现在",
            "支持", "具备", "配备", "搭载"
        )
        cleaned_msg = _norm(user_message)
        for w in query_strip_words:
            cleaned_msg = cleaned_msg.replace(_norm(w), "")

        is_broad_device_list_query = bool(cleaned_msg) and all(
            token in generic_terms for token in re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fa5]+", cleaned_msg)
        )

        if entity_targets and len(entity_targets) > 1:
            family_ids = set()
            class_ids = set()
            for target in entity_targets:
                if target.startswith("family:"):
                    family_ids.add(target.split(":", 1)[1])
                elif target.startswith("variant:"):
                    var_id = target.split(":", 1)[1]
                    rov = self.get_rov(var_id)
                    if rov and rov.get("family_id"):
                        family_ids.add(rov["family_id"])
                elif target.startswith("class:"):
                    class_ids.add(target.split(":", 1)[1])

            if len(family_ids) == 1 or len(class_ids) == 1:
                robots = []
                for et in entity_targets:
                    _, r_list = self._robots_for_entity_target(et, task_type_key)
                    for r in r_list:
                        if r not in robots:
                            robots.append(r)
                response["matched_alias"] = matched_alias
                response["matched_entity"] = entity_targets[0]
                query_mode = "device_check"
            else:
                response["reason"] = "ambiguous_device_alias"
                response["matched_alias"] = matched_alias
                response["candidate_entities"] = entity_targets
                response["query_mode"] = "device_check"
                return response
        elif entity_targets:
            entity_target = entity_targets[0]
            entity_kind, robots = self._robots_for_entity_target(
                entity_target,
                task_type_key,
            )
            response["matched_alias"] = matched_alias
            response["matched_entity"] = entity_target
            query_mode = (
                "device_list"
                if entity_kind == "class" or (entity_kind == "family" and is_list_query)
                else "device_check"
            )
        elif is_broad_device_list_query or is_list_query:
            if not is_broad_device_list_query and cleaned_msg and not depth_condition.get("has_depth_expression"):
                response["reason"] = "device_not_resolved"
                response["query_mode"] = "device_check"
                return response
            robots = (
                self.get_task_allowed_robot_variants(task_type_key)
                if task_type_key
                else self.get_all_rovs()
            )
            query_mode = "device_list"
        else:
            response["reason"] = "device_not_resolved"
            response["query_mode"] = "device_check"
            return response

        response["query_mode"] = query_mode
        if depth_condition["has_depth_expression"] and depth_condition["parse_status"] == "invalid":
            response["reason"] = "invalid_depth_expression"
            return response

        results: list[dict] = []
        for robot in robots:
            item = dict(robot)
            matches_depth = self._matches_depth_condition(
                robot.get("max_depth_m"),
                depth_condition,
            )
            item["matches_depth_condition"] = matches_depth
            if query_mode == "device_check" or matches_depth:
                results.append(item)

        response["results"] = results
        response["found"] = bool(results)
        if not results:
            response["reason"] = "no_matching_device"
        return response

    def _find_query_entity_targets(self, user_message: str) -> tuple[str | None, list[str]]:
        message_norm = _norm(user_message)
        if not message_norm:
            return None, []
        matches = [
            (alias, targets)
            for alias, targets in self.get_device_alias_index().items()
            if _norm(alias) and _norm(alias) in message_norm
        ]
        if not matches:
            class_matches = []
            for class_id, robot_class in self.get_robot_classes().items():
                label = robot_class.get("full_name") or class_id
                if _norm(label) and _norm(label) in message_norm:
                    class_matches.append((label, [f"class:{class_id}"]))
            if not class_matches:
                return None, []
            class_matches.sort(key=lambda item: len(_norm(item[0])), reverse=True)
            return class_matches[0]
        matches.sort(key=lambda item: len(_norm(item[0])), reverse=True)
        return matches[0]

    def _resolve_context_entity_targets(
        self,
        selector: str,
        task_type_key: str | None,
    ) -> list[str]:
        unit = self.resolve_robot_unit(selector, task_type_key)
        if unit:
            return [f"unit:{unit.get('unit_id')}"]
        variant = self.get_rov_for_task(selector, task_type_key)
        if variant:
            return [f"variant:{variant.get('variant_id')}"]
        family_id = self.resolve_robot_family_id(selector, task_type_key)
        if family_id:
            return [f"family:{family_id}"]
        return []

    def _robots_for_entity_target(
        self,
        entity_target: str,
        task_type_key: str | None,
    ) -> tuple[str | None, list[dict]]:
        if ":" not in entity_target:
            return None, []
        entity_kind, entity_id = entity_target.split(":", 1)
        allowed_robots = (
            self.get_task_allowed_robot_variants(task_type_key)
            if task_type_key
            else self.get_all_rovs()
        )

        if entity_kind == "class":
            return entity_kind, [
                robot
                for robot in allowed_robots
                if robot.get("robot_class") == entity_id
            ]
        if entity_kind == "family":
            return entity_kind, [
                robot
                for robot in allowed_robots
                if robot.get("family_id") == entity_id
            ]
        if entity_kind == "variant":
            return entity_kind, [
                robot
                for robot in allowed_robots
                if robot.get("variant_id") == entity_id
            ]
        if entity_kind == "unit":
            units = [
                unit
                for unit in self.robot_fleet.get("fleet_units", [])
                if unit.get("unit_id") == entity_id
            ]
            if len(units) != 1:
                return entity_kind, []
            unit = units[0]
            robots = [
                robot
                for robot in allowed_robots
                if robot.get("variant_id") == unit.get("variant_id")
            ]
            if len(robots) != 1:
                return entity_kind, []
            robot = dict(robots[0])
            robot["selected_unit"] = dict(unit)
            return entity_kind, [robot]
        return None, []

    @staticmethod
    def _parse_depth_condition(user_message: str) -> dict:
        has_depth_expression = bool(
            re.search(r"\d+\s*(?:米|m)?", user_message, re.IGNORECASE)
            and any(
                keyword in user_message.lower()
                for keyword in ("米", "m", "深度", "下潜", "水深", "能力", "支持", "可在")
            )
        )
        condition = {
            "operator": None,
            "depth_m": None,
            "has_depth_expression": has_depth_expression,
            "parse_status": "invalid" if has_depth_expression else "absent",
        }
        patterns = (
            ("eq", r"(\d+)\s*米级"),
            ("lte", r"(?:最大(?:下潜|作业)?深度(?:为|是)?|下潜极限(?:为|是)?)\s*(\d+)\s*(?:米|m)"),
            ("lte", r"(?:不超过|至多|最大不超过|不大于|最多)\s*(\d+)\s*(?:米|m)"),
            ("lt", r"(?:低于|小于|不到)\s*(\d+)\s*(?:米|m)"),
            ("gte", r"(?:不少于|不低于|至少)\s*(\d+)\s*(?:米|m)"),
            ("gt", r"(?<!不)(?:超过|大于)\s*(\d+)\s*(?:米|m)"),
            ("gte", r"(?:支持在?|能够下潜至|能够下潜到|能够在?|能下潜到?|可在?|在)\s*(\d+)\s*(?:米|m)"),
        )
        for operator, pattern in patterns:
            match = re.search(pattern, user_message, re.IGNORECASE)
            if match:
                condition.update({
                    "operator": operator,
                    "depth_m": int(match.group(1)),
                    "parse_status": "valid",
                })
                break
        return condition

    @staticmethod
    def _matches_depth_condition(max_depth: Any, condition: dict) -> bool:
        if condition.get("parse_status") != "valid":
            return True
        if isinstance(max_depth, bool) or not isinstance(max_depth, (int, float)):
            return False
        target = condition.get("depth_m")
        operator = condition.get("operator")
        if operator == "eq":
            return max_depth == target
        if operator == "gte":
            return max_depth >= target
        if operator == "gt":
            return max_depth > target
        if operator == "lte":
            return max_depth <= target
        if operator == "lt":
            return max_depth < target
        return False
