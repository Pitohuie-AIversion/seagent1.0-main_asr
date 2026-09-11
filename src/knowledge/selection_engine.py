"""
src/knowledge/selection_engine.py — 机器人选型、规格校验、可用域与全量设备检索引擎主门面
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import (
    RobotSelectionDataError,
    RobotVariantFeasibility,
    _norm,
)
from .variant_evaluator import VariantEvaluator
from .unit_resolver import UnitResolver

if TYPE_CHECKING:
    from ..knowledge_retriever import KnowledgeBase


class RobotSelectionEngine:
    """机器人 4 级拓扑选择、规格校验与设备检索引擎主门面。"""

    def __init__(self, kb: "KnowledgeBase"):
        self.kb = kb
        self.evaluator = VariantEvaluator(self)
        self.resolver = UnitResolver(self)

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

    # ──────────────────────────────────────────────────────────────────────────
    # 静态规则与变体可行性评估委托 (VariantEvaluator 代理)
    # ──────────────────────────────────────────────────────────────────────────

    def _validate_task_type_key(self, task_type_key: str | None) -> dict | None:
        return self.evaluator.validate_task_type_key(task_type_key)

    def _validate_fleet_units_integrity(self) -> None:
        self.evaluator.validate_fleet_units_integrity()

    def _validate_model_variants_integrity(self) -> None:
        self.evaluator.validate_model_variants_integrity()

    def _extract_and_validate_variant_spec(
        self,
        robot_class_id: str,
        family_id: str,
        variant_id: str,
        variant: dict,
    ) -> dict:
        return self.evaluator.extract_and_validate_variant_spec(
            robot_class_id,
            family_id,
            variant_id,
            variant,
        )

    @staticmethod
    def _validated_positive_number(
        value: Any,
        *,
        error_code: str,
        field_name: str,
    ) -> float:
        return VariantEvaluator.validated_positive_number(
            value,
            error_code=error_code,
            field_name=field_name,
        )

    @classmethod
    def evaluate_static_robot_variant(
        cls,
        variant_id: str,
        variant_cfg: dict,
        task_state: dict | None,
    ) -> RobotVariantFeasibility:
        return VariantEvaluator.evaluate_static_robot_variant(
            variant_id,
            variant_cfg,
            task_state,
        )

    @staticmethod
    def _task_starts_within_runtime_window(
        task_state: dict | None,
        *,
        time_window_minutes: int = 60,
    ) -> bool:
        return VariantEvaluator.task_starts_within_runtime_window(
            task_state,
            time_window_minutes=time_window_minutes,
        )

    def validate_robot_selection_from_task_state(
        self,
        task_state: dict,
        *,
        require_unit: bool = False,
    ) -> dict | None:
        return self.evaluator.validate_robot_selection_from_task_state(
            task_state,
            require_unit=require_unit,
        )

    def validate_static_robot_selection(
        self,
        robot_class: str,
        family: str,
        specification: dict | Any,
        unit_id: str,
        task_type_key: str | None = None,
    ) -> dict:
        return self.evaluator.validate_static_robot_selection(
            robot_class,
            family,
            specification,
            unit_id,
            task_type_key,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 机体实体与变体别名消歧委托 (UnitResolver 代理)
    # ──────────────────────────────────────────────────────────────────────────

    def _build_robot_variant(
        self,
        robot_classes: dict,
        family_id: str,
        family: dict,
        variant_id: str,
        variant: dict,
    ) -> dict:
        return self.resolver.build_robot_variant(
            robot_classes,
            family_id,
            family,
            variant_id,
            variant,
        )

    def _build_robot_variant_index(self) -> list[dict]:
        return self.resolver.build_robot_variant_index()

    def get_all_rovs(self) -> list[dict]:
        return self.resolver.get_all_rovs()

    def get_task_allowed_robot_variants(
        self,
        task_type_key: str | None,
        family_selector: str | None = None,
        class_selector: str | None = None,
    ) -> list[dict]:
        return self.resolver.get_task_allowed_robot_variants(
            task_type_key,
            family_selector,
            class_selector,
        )

    def get_ROV2type(self) -> dict:
        return self.resolver.get_ROV2type()

    def _resolve_robot_class_key(self, value: str) -> str:
        return self.resolver.resolve_robot_class_key(value)

    @staticmethod
    def _match_robot_variants(
        robots: list[dict],
        selector: str,
    ) -> dict | None:
        return UnitResolver.match_robot_variants(robots, selector)

    def _find_rov(self, name: str) -> dict | None:
        return self.resolver.find_rov(name)

    def _resolve_robot_variant_exact(self, selector: str) -> dict | None:
        return self.resolver.resolve_robot_variant_exact(selector)

    def resolve_robot_variant_exact(self, selector: str) -> dict | None:
        return self.resolver.resolve_robot_variant_exact(selector)

    def _resolve_robot_unit_exact(
        self,
        unit_selector: str,
        task_type_key: str | None = None,
    ) -> dict | None:
        return self.resolver.resolve_robot_unit_exact(unit_selector, task_type_key)

    def resolve_robot_unit_exact(
        self,
        unit_selector: str,
        task_type_key: str | None = None,
    ) -> dict | None:
        return self.resolver.resolve_robot_unit_exact(unit_selector, task_type_key)

    def resolve_robot_unit(
        self,
        unit_selector: str,
        task_type_key: str | None = None,
        variant_selector: str | None = None,
    ) -> dict | None:
        return self.resolver.resolve_robot_unit(
            unit_selector,
            task_type_key,
            variant_selector,
        )

    def resolve_robot_unit_from_text(
        self,
        text: str,
        task_type_key: str | None = None,
    ) -> dict | None:
        return self.resolver.resolve_robot_unit_from_text(text, task_type_key)

    def find_rov_by_description(self, description: str) -> list[dict]:
        return self.resolver.find_rov_by_description(description)

    def get_rov(self, model_name: str) -> dict | None:
        return self.resolver.get_rov(model_name)

    def get_rov_for_task(
        self,
        model_name: str,
        task_type: str | None,
        family_selector: str | None = None,
    ) -> dict | None:
        return self.resolver.get_rov_for_task(
            model_name,
            task_type,
            family_selector,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 拓扑层级分类与名称解析辅助方法
    # ──────────────────────────────────────────────────────────────────────────

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

    def resolve_class_key(self, value: str | None) -> str | None:
        return self._resolve_class_key(value)

    def _robot_class_display_name(self, class_id: str | None) -> str:
        if not class_id:
            return ""
        return self.get_robot_classes().get(class_id, {}).get("full_name", class_id)

    def robot_class_display_name(self, class_id: str | None) -> str:
        return self._robot_class_display_name(class_id)

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

    def resolve_family_key(self, value: str | None) -> str | None:
        return self._resolve_family_key(value)

    # ──────────────────────────────────────────────────────────────────────────
    # 4 级拓扑选择子图与列表探测
    # ──────────────────────────────────────────────────────────────────────────

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
