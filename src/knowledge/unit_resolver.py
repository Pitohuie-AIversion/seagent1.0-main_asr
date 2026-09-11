"""
src/knowledge/unit_resolver.py — 机器人实体机（Fleet Unit）及变体文本消歧、别名索引与描述查找器
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .models import (
    RobotSelectionDataError,
    _norm,
    normalize_payload_groups,
    normalize_supported_payloads,
)

if TYPE_CHECKING:
    from .selection_engine import RobotSelectionEngine
    from ..knowledge_retriever import KnowledgeBase


class UnitResolver:
    """机器人实体机、变体别名消歧、中英文型号模糊匹配与描述查找器。"""

    def __init__(self, engine: "RobotSelectionEngine"):
        self.engine = engine

    @property
    def kb(self) -> "KnowledgeBase":
        return self.engine.kb

    @property
    def robot_fleet(self) -> dict:
        return self.engine.robot_fleet

    @property
    def _robot_variants_cache(self) -> list[dict] | None:
        return self.engine._robot_variants_cache

    @_robot_variants_cache.setter
    def _robot_variants_cache(self, value: list[dict] | None) -> None:
        self.engine._robot_variants_cache = value

    def build_robot_variant(
        self,
        robot_classes: dict,
        family_id: str,
        family: dict,
        variant_id: str,
        variant: dict,
    ) -> dict:
        """根据配置组装单条完整的机器人变体字典，包含别名、机载工具及载荷分组"""
        robot_class = family.get("robot_class")
        robot_class_name = robot_classes.get(robot_class, {}).get("full_name", robot_class)
        hard_params = variant.get("hard_params", {}) or {}
        units = self.engine.get_fleet_units_for_variant(variant_id)

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

    def build_robot_variant_index(self) -> list[dict]:
        """构建全量机器人变体内存索引列表"""
        robot_classes = self.engine.get_robot_classes()
        robots: list[dict] = []

        for family_id, family in self.engine.get_robot_families_for_classes([]):
            for variant_id, variant in self.engine.get_model_variants_for_family(family_id):
                robots.append(
                    self.build_robot_variant(
                        robot_classes,
                        family_id,
                        family,
                        variant_id,
                        variant,
                    )
                )
        return robots

    def get_all_rovs(self) -> list[dict]:
        """获取全量 ROV 变体列表（带线程安全缓存）"""
        if self._robot_variants_cache is None:
            self._robot_variants_cache = self.build_robot_variant_index()
        return list(self._robot_variants_cache)

    def get_task_allowed_robot_variants(
        self,
        task_type_key: str | None,
        family_selector: str | None = None,
        class_selector: str | None = None,
    ) -> list[dict]:
        """获取允许执行特定任务的机器人变体列表（支持按系列或类别过滤）"""
        robots = [
            robot
            for robot in self.get_all_rovs()
            if self.engine.robot_matches_task(robot, task_type_key)
        ]
        if class_selector:
            class_key = self.engine.resolve_class_key(class_selector)
            if class_key:
                robots = [r for r in robots if r.get("robot_class") == class_key]
        if not family_selector:
            return robots
        family_id = self.engine.resolve_robot_family_id(family_selector, task_type_key)
        if not family_id:
            return []
        return [robot for robot in robots if robot.get("family_id") == family_id]

    def get_ROV2type(self) -> dict:
        """获取 ROV 全称到所属机型大类的映射字典"""
        return {r["full_name"]: r.get("robot_class_name") for r in self.get_all_rovs()}

    def resolve_robot_class_key(self, value: str) -> str:
        """将类别人类可读名解析为内部 canonical key"""
        value_norm = _norm(value)
        for key, cfg in self.engine.get_robot_classes().items():
            if value_norm in {_norm(key), _norm(cfg.get("full_name"))}:
                return key
        return value

    @staticmethod
    def match_robot_variants(
        robots: list[dict],
        selector: str,
    ) -> dict | None:
        """在给定变体列表中精确或包含匹配型号目标"""
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

    def find_rov(self, name: str) -> dict | None:
        """只在 model_variants 层解析型号，不接受系列或单机 aliases"""
        return self.match_robot_variants(self.get_all_rovs(), name)

    def resolve_robot_variant_exact(self, selector: str) -> dict | None:
        """精确解析变体选择器，存在歧义时抛出 AMBIGUOUS_VARIANT_SELECTOR 异常"""
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

    def resolve_robot_unit_exact(
        self,
        unit_selector: str,
        task_type_key: str | None = None,
    ) -> dict | None:
        """精确解析单机选择器，存在歧义时抛出 AMBIGUOUS_UNIT_SELECTOR 异常"""
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
            if robot and self.engine.robot_matches_task(robot, task_type_key):
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
        """按单机层解析 unit_id、display_name 或 aliases；存在歧义时返回 None"""
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
            if unit_variant and self.engine.robot_matches_task(unit_variant, task_type_key):
                return {**u, "robot": unit_variant}
            return None

        def matching_units(contains: bool) -> list[dict]:
            matches: list[dict] = []
            variants = {r["variant_id"]: r for r in self.get_all_rovs()}
            for unit in self.robot_fleet.get("fleet_units", []):
                if variant and unit.get("variant_id") != variant.get("variant_id"):
                    continue
                unit_variant = variants.get(unit.get("variant_id"))
                if not unit_variant or not self.engine.robot_matches_task(unit_variant, task_type_key):
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
        """从自然语言文本中提取最长匹配的唯一 fleet unit"""
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
        """根据描述查找 ROV 列表（向后兼容）"""
        return self.get_all_rovs()

    def get_rov(self, model_name: str) -> dict | None:
        """根据型号名称查找 ROV 变体"""
        return self.find_rov(model_name)

    def get_rov_for_task(
        self,
        model_name: str,
        task_type: str | None,
        family_selector: str | None = None,
    ) -> dict | None:
        """在允许执行特定任务的变体中查找型号"""
        allowed_variants = self.get_task_allowed_robot_variants(
            task_type,
            family_selector,
        )
        return self.match_robot_variants(allowed_variants, model_name)
