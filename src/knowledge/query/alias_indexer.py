"""
src/knowledge/query/alias_indexer.py — 机器人分层设备别名索引、实体解析与消歧器
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..selection.models import _norm

if TYPE_CHECKING:
    from ...knowledge_retriever import KnowledgeBase


class DeviceAliasIndexer:
    """按类别、系列、型号、单机分层构建设备别名索引与解析器。"""

    def __init__(self, kb: "KnowledgeBase"):
        self.kb = kb

    @property
    def robot_fleet(self) -> dict:
        return self.kb.robot_fleet

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

        for class_id, robot_class in self.kb.get_robot_classes().items():
            target = f"class:{class_id}"
            add(class_id, target)
            add(robot_class.get("full_name"), target)
            for alias in robot_class.get("aliases", []):
                add(alias, target)

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
            raw_serial = str(unit.get("serial_no") or "").strip()
            if raw_serial:
                add(f"{raw_serial}号", target)
                add(f"{raw_serial}号机", target)
                add(f"{raw_serial}机", target)
                lstripped = raw_serial.lstrip("0")
                if lstripped.isdigit():
                    num = int(lstripped)
                    cn_map = {1: ["一"], 2: ["二", "两"], 3: ["三"], 4: ["四"], 5: ["五"], 6: ["六"], 7: ["七"], 8: ["八"], 9: ["九"]}
                    add(f"{num}号", target)
                    add(f"{num}号机", target)
                    add(f"{num}机", target)
                    for cn in cn_map.get(num, []):
                        add(f"{cn}号", target)
                        add(f"{cn}号机", target)
                        add(f"{cn}机", target)
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
        for class_id, robot_class in self.kb.get_robot_classes().items():
            terms.add(class_id)
            full_name = robot_class.get("full_name")
            if isinstance(full_name, str) and full_name.strip():
                terms.add(full_name.strip())
        return terms

    def find_query_entity_targets(self, user_message: str) -> tuple[str | None, list[str]]:
        """从用户文本中匹配最佳设备实体目标。"""
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
            for class_id, robot_class in self.kb.get_robot_classes().items():
                label = robot_class.get("full_name") or class_id
                if _norm(label) and _norm(label) in message_norm:
                    class_matches.append((label, [f"class:{class_id}"]))
            if not class_matches:
                return None, []
            class_matches.sort(key=lambda item: len(_norm(item[0])), reverse=True)
            return class_matches[0]
        matches.sort(key=lambda item: len(_norm(item[0])), reverse=True)
        return matches[0]

    def resolve_context_entity_targets(
        self,
        selector: str,
        task_type_key: str | None,
    ) -> list[str]:
        """根据上下文选择器解析实体 targets。"""
        unit = self.kb.resolve_robot_unit(selector, task_type_key)
        if unit:
            return [f"unit:{unit.get('unit_id')}"]
        variant = self.kb.get_rov_for_task(selector, task_type_key)
        if variant:
            return [f"variant:{variant.get('variant_id')}"]
        family_id = self.kb.resolve_robot_family_id(selector, task_type_key)
        if family_id:
            return [f"family:{family_id}"]
        return []

    def robots_for_entity_target(
        self,
        entity_target: str,
        task_type_key: str | None,
    ) -> tuple[str | None, list[dict]]:
        """根据实体 target 解析允许使用的具体机器人变体配置。"""
        if ":" not in entity_target:
            return None, []
        entity_kind, entity_id = entity_target.split(":", 1)
        allowed_robots = (
            self.kb.get_task_allowed_robot_variants(task_type_key)
            if task_type_key
            else self.kb.get_all_rovs()
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
