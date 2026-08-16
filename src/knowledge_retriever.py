"""
knowledge_retriever.py — 知识库加载与按需检索
不使用向量数据库，基于任务状态进行规则化知识片段选取。
知识总量在10000字以内，精准注入比全量注入更高效。
"""

import yaml
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo
from .environment_info import EnvironmentInfo
from .simulated_time import get_current_datetime
from .state_info import RobotStateInfo

CONFIG_DIR = Path(__file__).parent.parent / "config"


def _load(filename: str) -> dict | list:
    with open(CONFIG_DIR / filename, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _norm(value: object) -> str:
    return str(value or "").lower().replace(" ", "")


def _payload_match_key(value: object) -> str:
    text = str(value or "").strip().lower().replace(" ", "")
    for suffix in ("（可选）", "(可选)"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def robot_selection_result_contract_error(
    task_state: dict,
    selection: object,
    *,
    require_unit: bool = False,
) -> str | None:
    """Return the missing canonical key for an invalid static-validator result.

    Callers at publish/restore boundaries must not treat ``None`` or an empty
    mapping as successful validation when an explicit selector was supplied.
    The deepest explicit selector determines the minimum canonical result that
    the registry validator must return.
    """
    if not isinstance(task_state, dict):
        raise RobotSelectionDataError(
            "task_state must be a dictionary.",
            error_code="INVALID_TASK_STATE",
            actual_value=task_state,
        )

    selector_contract = (
        ("equipment_class", "robot_class", "INVALID_ROBOT_CLASS_SELECTOR"),
        ("equipment_family", "family_id", "INVALID_FAMILY_SELECTOR"),
        ("equipment_type", "variant_id", "INVALID_VARIANT_SELECTOR"),
        ("equipment_unit_id", "unit_id", "INVALID_UNIT_SELECTOR"),
    )
    explicit_selectors: dict[str, str] = {}
    for selector_key, canonical_key, error_code in selector_contract:
        if selector_key not in task_state or task_state[selector_key] is None:
            continue
        selector_value = task_state[selector_key]
        if not isinstance(selector_value, str) or not selector_value.strip():
            raise RobotSelectionDataError(
                f"{selector_key} must be a non-empty string when explicitly provided.",
                error_code=error_code,
                expected_field=selector_key,
                actual_value=selector_value,
            )
        explicit_selectors[selector_key] = canonical_key

    if require_unit and "equipment_unit_id" not in explicit_selectors:
        raise RobotSelectionDataError(
            "A concrete equipment_unit_id is required.",
            error_code="MISSING_UNIT_ID",
            expected_field="equipment_unit_id",
            actual_value=task_state.get("equipment_unit_id"),
        )

    expected_key = "unit_id" if require_unit else None
    if expected_key is None:
        for selector_key, canonical_key, _error_code in reversed(selector_contract):
            if selector_key in explicit_selectors:
                expected_key = explicit_selectors[selector_key]
                break
    if expected_key is None:
        return None
    if not isinstance(selection, dict):
        return expected_key
    canonical_value = selection.get(expected_key)
    if not isinstance(canonical_value, str) or not canonical_value.strip():
        return expected_key
    return None


@dataclass(frozen=True)
class RobotVariantFeasibility:
    eligible: bool
    reasons: tuple[str, ...] = ()
    requires_installation: tuple[str, ...] = ()


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
            if not isinstance(onboard_raw, list) or not isinstance(supported_raw, list):
                raise RobotSelectionDataError(
                    f"Variant '{variant_id}' payload declarations must be lists.",
                    error_code="INVALID_VARIANT_PAYLOAD_CONFIG",
                    variant_id=variant_id,
                    expected_field="onboard_payloads/supported_payloads",
                    actual_value={
                        "onboard_payloads": onboard_raw,
                        "supported_payloads": supported_raw,
                    },
                )

            onboard = {_payload_match_key(item) for item in onboard_raw}
            supported = {_payload_match_key(item) for item in supported_raw}
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
        time_window_minutes: int = 10,
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
        """Validate the deepest explicit robot selector and every parent edge.

        Missing keys and ``None`` mean that collection has not reached that
        level yet.  Any other explicit value must be a non-empty string.  A
        deeper valid selector may derive omitted parents from the registry, but
        it may never overwrite or ignore an explicitly supplied parent.
        """
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
    ) -> dict:
        """
        计算当前任务的可行机器人子图 (Feasible Robot Selection Domain)。
        四级结构：class -> family -> model_variant -> fleet_unit
        先按 class/capability 与 Variant 硬参数过滤；即时任务再按 Unit 运行状态过滤。
        """
        self._validate_model_variants_integrity()
        self._validate_fleet_units_integrity()

        template = self._validate_task_type_key(task_type_key)
        robot_classes = self.get_robot_classes()
        robot_families = self.robot_fleet.get("robot_families", {})
        all_variants = self.robot_fleet.get("model_variants", {})
        fleet_units = self.robot_fleet.get("fleet_units", [])

        if template is None:
            allowed_classes = list(robot_classes.keys())
            required_caps = set()
        else:
            allowed_classes = template.get("allowed_robot_classes", [])
            required_caps = set(template.get("required_capabilities", []))
        apply_runtime_filter = self._task_starts_within_runtime_window(task_state)
        rejected_variants: list[dict] = []
        rejected_units: list[dict] = []

        # 1. 筛选符合 capability 与 allowed_classes 的 feasible families
        feasible_families: dict[str, dict] = {}
        for family_id, family in robot_families.items():
            f_class = family.get("robot_class")
            if not f_class or f_class not in robot_classes:
                raise RobotSelectionDataError(
                    f"Family '{family_id}' references missing or invalid robot_class '{f_class}'.",
                    error_code="INVALID_ROBOT_CLASS_REFERENCE",
                    expected_field="robot_classes",
                    actual_value=f_class,
                )
            if f_class not in allowed_classes:
                continue

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

        # 2. 反向由 feasible_families 推导 feasible_classes
        feasible_class_ids = {
            f.get("robot_class") for f in feasible_families.values()
        }

        # 3. 逐层组装 4 层 domain 结构
        classes_node: list[dict] = []
        for class_id in allowed_classes:
            if class_id not in robot_classes:
                raise RobotSelectionDataError(
                    f"Task template '{task_type_key}' references non-existent robot_class '{class_id}'.",
                    error_code="INVALID_ROBOT_CLASS_REFERENCE",
                    expected_field="robot_classes",
                    actual_value=class_id,
                )
            if class_id not in feasible_class_ids:
                # Class 下任何 family 均不满足 capability，被反向过滤剔除
                continue

            class_cfg = robot_classes[class_id]
            families_node: list[dict] = []

            for family_id, family_cfg in feasible_families.items():
                if family_cfg.get("robot_class") != class_id:
                    continue

                variants_node: list[dict] = []
                for variant_id, variant_cfg in all_variants.items():
                    if variant_cfg.get("family_id") != family_id:
                        continue

                    feasibility = self.evaluate_static_robot_variant(
                        variant_id,
                        variant_cfg,
                        task_state,
                    )
                    if not feasibility.eligible:
                        rejected_variants.append({
                            "variant_id": variant_id,
                            "reasons": list(feasibility.reasons),
                        })
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

                    if apply_runtime_filter and not units_node:
                        continue

                    variants_node.append({
                        "variant_id": variant_id,
                        "full_name": variant_cfg.get("full_name", variant_id),
                        "family_id": family_id,
                        "robot_class": class_id,
                        "aliases": list(variant_cfg.get("aliases", []) or []),
                        "hard_params": dict(variant_cfg.get("hard_params", {}) or {}),
                        "requires_installation": list(feasibility.requires_installation),
                        "units": units_node,
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
                "full_name": class_cfg.get("full_name", class_id),
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
        if task_type_key is not None:
            domain = self.get_feasible_robot_selection_domain(task_type_key)
            return [
                {
                    "class_id": c["class_id"],
                    "robot_class": c["class_id"],
                    "full_name": c["full_name"],
                }
                for c in domain["classes"]
            ]
        robot_classes = self.get_robot_classes()
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
            allowed_classes = self.get_task_allowed_robot_classes(task_type_key)
            if class_id not in allowed_classes:
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
        # Reuse the authoritative Class -> Family -> task capability gate before
        # resolving a Variant or Unit.  Resolving these selectors independently
        # would allow a valid Variant -> Family pair to bypass its parent Class
        # or the task template's admissible robot domain.
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

        # Static gates accept only a canonical Variant id/full name/declared
        # alias.  Candidate-list APIs may remain fuzzy for interactive input,
        # but substring matching must never authorize a publish/restore tuple.
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
            sections.append(self._rovs_for_task(task_type, task_state.get("equipment_class")))

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

    def _rovs_for_task(self, task_type: str, class_selector: str | None = None) -> str:
        rovs = self.get_task_allowed_robot_variants(task_type, class_selector=class_selector)
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

        # A canonical unit_id is authoritative even if a poorly maintained
        # alias on another Unit happens to duplicate it.
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
        alias_index = self.get_device_alias_index()
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

    def get_environment_alias_index(self) -> dict[str, list[str]]:
        """构建油气田、禁入保护区与DVL风险区的多维度别名索引。"""
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

        for field in self.environment.get("oil_fields", []):
            field_id = field.get("id")
            if not field_id:
                continue
            target = f"oil_field:{field_id}"
            add(field_id, target)
            add(field.get("name"), target)
            for alias in field.get("aliases", []):
                add(alias, target)

        for area in self.environment.get("forbidden_areas", []):
            area_id = area.get("id")
            if not area_id:
                continue
            target = f"forbidden_area:{area_id}"
            add(area_id, target)
            add(area.get("name"), target)
            for alias in area.get("aliases", []):
                add(alias, target)

        for dvl in self.environment.get("dvl_bottom_lock_failure_areas", []):
            dvl_id = dvl.get("id")
            if not dvl_id:
                continue
            target = f"dvl_area:{dvl_id}"
            add(dvl_id, target)
            add(dvl.get("name"), target)
            for alias in dvl.get("aliases", []):
                add(alias, target)

        return {alias: sorted(targets) for alias, targets in index.items()}

    def _find_environment_entity_targets(self, user_message: str) -> tuple[str | None, list[str]]:
        message_norm = _norm(user_message)
        if not message_norm:
            return None, []
        matches = [
            (alias, targets)
            for alias, targets in self.get_environment_alias_index().items()
            if _norm(alias) and _norm(alias) in message_norm
        ]
        if not matches:
            return None, []
        matches.sort(key=lambda item: len(_norm(item[0])), reverse=True)
        return matches[0]

    def _resolve_typed_read_query(
        self,
        query_type: str,
        user_message: str,
        context: dict,
    ) -> tuple[str, str]:
        """用结构化主题和权威别名选择事实域，不重新猜测自然语言意图。"""
        if (
            context.get("relation") == "status"
            or context.get("source_policy") == "realtime_state"
        ):
            return query_type, user_message

        if query_type == "ENVIRONMENT_QUERY":
            return query_type, user_message

        if query_type != "KNOWLEDGE_QA":
            return query_type, user_message

        subject_type = context.get("subject_type")
        subject_text = context.get("subject_text")
        device_subject_types = {"device", "device_class", "device_family"}
        env_subject_types = {"environment", "oilfield", "location"}
        neutral_subject_types = {None, "unknown", "general_concept"}

        # 优先检查设备实体
        device_candidates: list[str] = []
        if subject_type in device_subject_types:
            if isinstance(subject_text, str) and subject_text.strip():
                device_candidates.append(subject_text.strip())
            device_candidates.append(user_message)
        elif subject_type in neutral_subject_types:
            device_candidates.append(user_message)

        for selector in device_candidates:
            _, entity_targets = self._find_query_entity_targets(selector)
            if not entity_targets:
                continue
            if selector == user_message:
                return "DEVICE_CAPABILITY", user_message
            return "DEVICE_CAPABILITY", f"{selector} {user_message}"

        # 检查环境/油田实体
        env_candidates: list[str] = []
        if subject_type in env_subject_types:
            if isinstance(subject_text, str) and subject_text.strip():
                env_candidates.append(subject_text.strip())
            env_candidates.append(user_message)
        elif subject_type in neutral_subject_types:
            env_candidates.append(user_message)

        env_generic_keywords = (
            "油田", "气田", "油气田", "海域", "禁入区", "保护区", "dvl", "底锁", "环境", "水深上限"
        )
        for selector in env_candidates:
            _, entity_targets = self._find_environment_entity_targets(selector)
            if entity_targets:
                if selector == user_message:
                    return "ENVIRONMENT_QUERY", user_message
                return "ENVIRONMENT_QUERY", f"{selector} {user_message}"
            if any(k in _norm(selector) for k in env_generic_keywords):
                return "ENVIRONMENT_QUERY", user_message

        return query_type, user_message

    @staticmethod
    def _match_payload_catalog(
        payload_catalog: dict,
        user_message: str,
    ) -> list[dict]:
        """Match canonical payload names before considering their aliases.

        A broad alias such as ``视觉系统`` must never override a more specific
        catalog name such as ``三维视觉系统`` that appears in the same query.
        Multiple explicit canonical names are preserved for comparison queries.
        """
        message_key = _norm(user_message)
        canonical_matches = []
        alias_matches = []

        for payload_info in payload_catalog.values():
            if not isinstance(payload_info, dict):
                continue
            name_key = _norm(payload_info.get("name"))
            if name_key and name_key in message_key:
                canonical_matches.append(payload_info)
                continue

            aliases = payload_info.get("aliases", [])
            if any(
                alias_key and alias_key in message_key
                for alias_key in (_norm(alias) for alias in aliases)
            ):
                alias_matches.append(payload_info)

        return canonical_matches or alias_matches

    def execute_typed_query(
        self,
        query_type: str,
        user_message: str,
        context: dict | None = None,
    ) -> dict:
        """执行强类型只读知识查询，并返回稳定的结构化证据。"""
        context = context if isinstance(context, dict) else {}
        requested_query_type = query_type
        query_type, retrieval_message = self._resolve_typed_read_query(
            query_type, user_message, context
        )
        response = {
            "query_type": query_type,
            "requested_query_type": requested_query_type,
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
            matched_payloads = self._match_payload_catalog(
                payload_catalog,
                user_message,
            )

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
                retrieval_message,
                context,
                response,
            )

        if query_type == "ENVIRONMENT_QUERY":
            return self._execute_environment_query(
                retrieval_message,
                context,
                response,
            )

        if query_type == "KNOWLEDGE_QA":
            response["results"] = [
                {
                    "category": "oil_fields",
                    "oil_fields": self.environment.get("oil_fields", []),
                },
                {
                    "category": "forbidden_areas",
                    "forbidden_areas": self.environment.get("forbidden_areas", []),
                },
                {
                    "category": "dvl_bottom_lock_failure_areas",
                    "dvl_areas": self.environment.get("dvl_bottom_lock_failure_areas", []),
                },
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

    def _execute_environment_query(
        self,
        user_message: str,
        context: dict,
        response: dict,
    ) -> dict:
        """执行作业海域、油气田、禁入区与DVL风险区的结构化知识检索。"""
        matched_alias, targets = self._find_environment_entity_targets(user_message)
        oil_fields = self.environment.get("oil_fields", [])
        forbidden_areas = self.environment.get("forbidden_areas", [])
        dvl_areas = self.environment.get("dvl_bottom_lock_failure_areas", [])

        results = []
        if targets:
            for target in targets:
                if target.startswith("oil_field:"):
                    field_id = target.split(":", 1)[1]
                    for of in oil_fields:
                        if of.get("id") == field_id:
                            results.append({
                                "category": "oil_field_details",
                                "oil_field": of,
                            })
                elif target.startswith("forbidden_area:"):
                    area_id = target.split(":", 1)[1]
                    for fa in forbidden_areas:
                        if fa.get("id") == area_id:
                            results.append({
                                "category": "forbidden_area_details",
                                "forbidden_area": fa,
                            })
                elif target.startswith("dvl_area:"):
                    dvl_id = target.split(":", 1)[1]
                    for da in dvl_areas:
                        if da.get("id") == dvl_id:
                            results.append({
                                "category": "dvl_area_details",
                                "dvl_area": da,
                            })

        if not results:
            # 泛查询或未命中单一实体，返回所有海域知识条目的结构化摘要
            results = [
                {
                    "category": "oil_fields_summary",
                    "oil_fields": oil_fields,
                },
                {
                    "category": "forbidden_areas_summary",
                    "forbidden_areas": forbidden_areas,
                },
                {
                    "category": "dvl_bottom_lock_failure_areas_summary",
                    "dvl_areas": dvl_areas,
                },
            ]

        response["results"] = results
        response["found"] = True
        if matched_alias:
            response["matched_alias"] = matched_alias
            response["matched_targets"] = targets
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
        condition = {
            "operator": None,
            "depth_m": None,
            "has_depth_expression": False,
            "parse_status": "absent",
        }
        patterns = (
            ("eq", r"(\d+)\s*米级"),
            ("lte", r"(?:最大(?:下潜|作业)?深度(?:为|是)?|下潜极限(?:为|是)?)\s*(\d+)\s*(?:米|m)?"),
            ("lte", r"(?:不超过|至多|最大不超过|不大于|最多)\s*(\d+)\s*(?:米|m)?"),
            ("lt", r"(?:低于|小于|不到)\s*(\d+)\s*(?:米|m)?"),
            ("gte", r"(?:不少于|不低于|至少)\s*(\d+)\s*(?:米|m)?"),
            ("gt", r"(?<!不)(?:超过|大于)\s*(\d+)\s*(?:米|m)?"),
            ("gte", r"(?:支持在?|能够下潜至|能够下潜到|能够在?|能下潜到?|可在?|在|水深(?:为|是)?)\s*(\d+)\s*(?:米|m)?"),
            ("eq", r"(\d+)\s*(?:米|m)\s*(?:水深|深)"),
            ("eq", r"(?:水深|深)\s*(\d+)\s*(?:米|m)"),
        )
        for operator, pattern in patterns:
            match = re.search(pattern, user_message, re.IGNORECASE)
            if match:
                condition.update({
                    "operator": operator,
                    "depth_m": int(match.group(1)),
                    "has_depth_expression": True,
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
