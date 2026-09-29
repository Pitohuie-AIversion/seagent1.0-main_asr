"""
src/state/fleet_selector.py - 机器人机队配置加载与选择器解析模块

职责：
1. 规范化选择器输入（normalize_selector）
2. 安全加载与校验 robot_fleet.yaml 配置
3. 精确匹配与别名消歧（unit_id, display_name, serial_no, aliases）
4. 多级级联匹配（单机 -> 型号变体 model_variants -> 机型系列 robot_families）
5. 唯一映射解析（resolve_status_ref_from_snapshot）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from src.exceptions import StateSnapshotValidationError


def normalize_selector(value: object) -> str:
    """去除首尾空白、全部转为小写并剔除所有空格，用于机队标识与别名的规范化比较。"""
    return str(value or "").strip().lower().replace(" ", "")


def unit_status_ref(unit: Dict[str, Any]) -> Optional[str]:
    """提取机器人的物理状态引用键（status_ref 优先，回退到 unit_id）。"""
    status_ref = unit.get("status_ref") or unit.get("unit_id")
    return str(status_ref) if status_ref else None


def load_fleet(fleet_file: Path | str) -> Dict[str, Any]:
    """读取并解析机队 YAML 配置文件，确保其存在且顶层为映射字典。"""
    path = Path(fleet_file)
    try:
        fleet = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise StateSnapshotValidationError(
            "Robot fleet selector configuration is unavailable"
        ) from exc
    if not isinstance(fleet, dict):
        raise StateSnapshotValidationError(
            "Robot fleet selector configuration must be a mapping"
        )
    return fleet


def matching_unit_refs(units: list, needle: str) -> set[str]:
    """在 fleet_units 列表中根据 unit_id、display_name、serial_no 或 aliases 寻找匹配项。"""
    matches: set[str] = set()
    for unit in units:
        if not isinstance(unit, dict):
            raise StateSnapshotValidationError(
                "Each fleet unit must be a mapping"
            )
        aliases = unit.get("aliases", [])
        if not isinstance(aliases, list):
            raise StateSnapshotValidationError(
                "Fleet unit aliases must be a list"
            )
        targets = [
            unit.get("unit_id"),
            unit.get("display_name"),
            unit.get("serial_no"),
            unit.get("status_ref"),
            *aliases,
        ]
        if any(
            normalize_selector(target) == needle
            for target in targets
            if target
        ):
            ref = unit_status_ref(unit)
            if ref:
                matches.add(ref)
    return matches


def matching_variant_refs(
    fleet: Dict[str, Any],
    units: list,
    needle: str,
) -> set[str]:
    """在 model_variants 字典中通过 variant_id、full_name 或 aliases 进行匹配，并映射回挂载该变体的机器人的 status_ref。"""
    variants = fleet.get("model_variants", {})
    if not isinstance(variants, dict):
        raise StateSnapshotValidationError(
            "Robot model_variants must be a mapping"
        )
    matched_variant_ids: set[str] = set()
    for variant_id, variant in variants.items():
        if not isinstance(variant, dict):
            raise StateSnapshotValidationError(
                "Each robot model variant must be a mapping"
            )
        aliases = variant.get("aliases", [])
        if not isinstance(aliases, list):
            raise StateSnapshotValidationError(
                "Robot model variant aliases must be a list"
            )
        targets = [variant_id, variant.get("full_name"), *aliases]
        if any(
            normalize_selector(target) == needle
            for target in targets
            if target
        ):
            matched_variant_ids.add(variant_id)
    return {
        status_ref
        for unit in units
        if unit.get("variant_id") in matched_variant_ids
        for status_ref in [unit_status_ref(unit)]
        if status_ref
    }


def matching_family_refs(
    fleet: Dict[str, Any],
    units: list,
    needle: str,
) -> set[str]:
    """在 robot_families 字典中通过 family_id、full_name 或 aliases 进行匹配，并映射回挂载该族系的机器人的 status_ref。"""
    families = fleet.get("robot_families", {})
    variants = fleet.get("model_variants", {})
    if not isinstance(families, dict) or not isinstance(variants, dict):
        raise StateSnapshotValidationError(
            "Robot family selector configuration must be a mapping"
        )

    matched_family_ids: set[str] = set()
    for family_id, family in families.items():
        if not isinstance(family, dict):
            raise StateSnapshotValidationError(
                "Each robot family must be a mapping"
            )
        aliases = family.get("aliases", [])
        if not isinstance(aliases, list):
            raise StateSnapshotValidationError(
                "Robot family aliases must be a list"
            )
        targets = [family_id, family.get("full_name"), *aliases]
        if any(
            normalize_selector(target) == needle
            for target in targets
            if target
        ):
            matched_family_ids.add(family_id)

    matched_variant_ids = {
        variant_id
        for variant_id, variant in variants.items()
        if isinstance(variant, dict)
        and variant.get("family_id") in matched_family_ids
    }
    return {
        status_ref
        for unit in units
        if unit.get("variant_id") in matched_variant_ids
        for status_ref in [unit_status_ref(unit)]
        if status_ref
    }


def resolve_status_ref_from_snapshot(
    equipment_selector: str,
    snapshot: Dict[str, Any],
    fleet_file: Path | str,
    fleet: Dict[str, Any] | None = None,
) -> Optional[str]:
    """核心消岐解析：将用户/输入端指定的机器人选择器，唯一解析为持久化状态中的 status_ref。

    多阶段查找规则：
    1. 单机级别精确匹配 (matching_unit_refs)
    2. 型号变体匹配 (matching_variant_refs)
    3. 机型系列匹配 (matching_family_refs)
    4. 当前持久化快照中已有 robots 键名后备匹配
    若产生二义性（多于1个匹配）或无匹配，返回 None。
    """
    needle = normalize_selector(equipment_selector)
    if not needle:
        return None
    if fleet is None:
        fleet = load_fleet(fleet_file)
    units = fleet.get("fleet_units", [])
    if not isinstance(units, list):
        raise StateSnapshotValidationError("Robot fleet_units must be a list")

    for matches in (
        matching_unit_refs(units, needle),
        matching_variant_refs(fleet, units, needle),
        matching_family_refs(fleet, units, needle),
    ):
        if len(matches) == 1:
            return next(iter(matches))
        if len(matches) > 1:
            return None

    robots = snapshot.get("robots", {}) if isinstance(snapshot, dict) else {}
    existing_refs = {
        status_ref
        for status_ref in robots
        if normalize_selector(status_ref) == needle
    }
    return next(iter(existing_refs)) if len(existing_refs) == 1 else None
