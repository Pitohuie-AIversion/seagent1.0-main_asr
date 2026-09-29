"""
src/dispatch/catalog_resolver.py - 任务模板候选值与机队资产目录解析器

职责：
1. 解析 allowed_values_ref 引用，从 KnowledgeBase (assets, robot_fleet, task_schemas) 取对应列表；
2. 构建标准分层候选目录 (resolve_candidate_catalog)；
3. 构建唯一 alias 与歧义 alias 索引 (build_alias_indexes)；
4. 解析分层别名映射 (resolve_alias_mappings)；
5. 根据任务类型与当前任务状态过滤单机号、变体型号、系列及挂载载荷。
"""

from __future__ import annotations

import logging
from typing import Any

from src.knowledge_retriever import KnowledgeBase, RobotSelectionDataError
from src.extraction.normalizer import FieldNormalizer

logger = logging.getLogger(__name__)


class CatalogResolver:
    """负责任务字段 allowed_values、别名索引和资产目录解析。"""

    def __init__(
        self,
        kb: KnowledgeBase,
        ref_cache: dict[str, list[str]] | None = None,
    ):
        self.kb = kb
        # 缓存 allowed_values_ref 解析结果（运行期间配置不变）
        self._ref_cache: dict[str, list[str]] = ref_cache if ref_cache is not None else {}

    def get_template_task_type_values(self, task_type_key: str) -> list[str]:
        """返回某模板下的合法 task_type 值（供 tasktype 字段校验用）。"""
        templates = self.kb.task_schemas.get("task_templates", {})
        return templates.get(task_type_key, {}).get("task_type_values", [])

    def resolve_allowed_values(
        self,
        field_def: dict | str,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[str]:
        """解析字段在当前任务状态下的合法候选值。"""
        if isinstance(field_def, str):
            field_name = field_def
            field_def = self.kb.task_schemas.get("fields", {}).get(field_name, {})
            if not field_def:
                field_def = {"allowed_values_ref": f"payload_options.{task_type_key}"}
        return self.resolve_allowed(field_def, task_type_key, task_state)

    def resolve_alias_mappings(
        self,
        field_def: dict,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> dict[str, str]:
        """向现有提取上下文提供分层 alias -> 标准值映射。

        allowed_values 仍只包含标准候选；aliases 仅用于识别用户的模糊表达，
        且不会跨机器人系列、型号和单机层级复用。
        """
        ref = field_def.get("allowed_values_ref")
        mappings: dict[str, str] = {}
        ambiguous_aliases: set[str] = set()

        def add_mapping(alias: object, standard: object) -> None:
            if not alias or not standard:
                return
            alias_text = str(alias)
            standard_text = str(standard)
            if alias_text in ambiguous_aliases:
                return
            existing = mappings.get(alias_text)
            if existing is not None and existing != standard_text:
                mappings.pop(alias_text, None)
                ambiguous_aliases.add(alias_text)
                return
            mappings[alias_text] = standard_text

        if ref == "robot_family_full_names":
            for _, family in self.kb.get_robot_families_for_task(task_type_key):
                standard = family.get("full_name")
                if not standard:
                    continue
                for alias in family.get("aliases", []):
                    add_mapping(alias, standard)
            return mappings

        if ref in ("robot_full_names", "robot_variant_full_names"):
            class_selector = (
                str(task_state.get("equipment_class") or "")
                if task_state
                else ""
            )
            family_selector = (
                str(task_state.get("equipment_family") or "")
                if task_state
                else ""
            )
            for robot in self.kb.get_task_allowed_robot_variants(
                task_type_key,
                family_selector or None,
                class_selector or None,
            ):
                standard = robot.get("full_name")
                if not standard:
                    continue
                for alias in robot.get("aliases", []):
                    add_mapping(alias, standard)
                # 兼容：允许 Family 级别的别名（如“天鹰座”）直接映射到对应的型号
                fam_id = robot.get("family_id")
                if fam_id:
                    fam_cfg = self.kb.robot_fleet.get("robot_families", {}).get(fam_id, {})
                    for alias in fam_cfg.get("aliases", []):
                        add_mapping(alias, standard)
            return mappings

        if ref == "robot_unit_ids":
            variant_selector = (
                str(task_state.get("equipment_type") or "")
                if task_state
                else ""
            )
            if not variant_selector:
                return {}
            robots = [
                self.kb.get_rov_for_task(variant_selector, task_type_key)
            ]
            for robot in (item for item in robots if item):
                fam_id = robot.get("family_id")
                fam_aliases = []
                if fam_id:
                    fam_cfg = self.kb.robot_fleet.get("robot_families", {}).get(fam_id, {})
                    fam_aliases = fam_cfg.get("aliases", [])
                for unit in robot.get("fleet_units", []):
                    unit_id = unit.get("unit_id")
                    if not unit_id:
                        continue
                    targets = [
                        unit_id,
                        unit.get("display_name"),
                        *unit.get("aliases", []),
                        *fam_aliases,  # 允许 Family 裸别名（如“天鹰座”）在单机搜寻阶段连带匹配映射
                    ]
                    for alias in targets:
                        add_mapping(alias, unit_id)

            return mappings

        return mappings

    def resolve_candidate_catalog(
        self,
        field_def: dict,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[dict]:
        """统一构建候选目录，避免 allowed_values / aliases / evidence 三处不一致。"""
        ref = field_def.get("allowed_values_ref")
        catalog: list[dict] = []

        if field_def.get("type") == "tasktype" or "allowed_values" in field_def or not ref:
            return [
                {
                    "canonical_value": value,
                    "aliases": [],
                    "display_name": None,
                    "parent": None,
                }
                for value in self.resolve_allowed(field_def, task_type_key, task_state)
            ]

        if ref in ("robot_category_labels", "robot_class_labels", "robot_classes"):
            try:
                classes = self.kb.list_robot_classes(task_type_key)
                domain = self.kb.get_feasible_robot_selection_domain(
                    task_type_key,
                    task_state,
                )
                feasible_class_ids = {
                    item["class_id"] for item in domain["classes"]
                }
                for c in classes:
                    if c.get("class_id") not in feasible_class_ids:
                        continue
                    class_id = c.get("class_id")
                    name = c.get("full_name") or c.get("class_id")
                    if name:
                        class_node = next(
                            (
                                node
                                for node in domain["classes"]
                                if node.get("class_id") == class_id
                            ),
                            {},
                        )
                        feasible_family_ids = {
                            node.get("family_id")
                            for node in class_node.get("families", [])
                        }
                        class_aliases = [str(name), str(class_id)]
                        for a in (c.get("aliases", []) or []):
                            if a and str(a) not in class_aliases:
                                class_aliases.append(str(a))
                        descriptions: list[str] = []
                        for family_id, family in self.kb.robot_fleet.get(
                            "robot_families", {}
                        ).items():
                            if family_id not in feasible_family_ids:
                                continue
                            brief = " ".join(str(family.get("brief") or "").split())
                            if brief:
                                descriptions.append(brief[:500])
                        catalog.append({
                            "canonical_value": name,
                            "aliases": class_aliases,
                            "display_name": c.get("full_name"),
                            "parent": None,
                            "description": "\n".join(descriptions),
                        })
                return catalog
            except RobotSelectionDataError as exc:
                logger.warning(
                    "Robot candidate catalog resolution failed: ref=%s task=%s error=%s",
                    ref,
                    task_type_key,
                    exc,
                )
                return []

        if ref == "robot_family_full_names":
            for _, family in self.kb.get_robot_families_for_task(task_type_key):
                standard = family.get("full_name")
                if standard:
                    brief = " ".join(str(family.get("brief") or family.get("description") or "").split())
                    catalog.append(
                        {
                            "canonical_value": standard,
                            "aliases": list(family.get("aliases", []) or []),
                            "display_name": family.get("display_name"),
                            "parent": None,
                            "description": brief,
                        }
                    )
            return catalog

        if ref in ("robot_full_names", "robot_variant_full_names"):
            family_selector = str(task_state.get("equipment_family") or "") if task_state else ""
            class_selector = str(task_state.get("equipment_class") or "") if task_state else ""
            robots = self.kb.get_task_allowed_robot_variants(
                task_type_key,
                family_selector or None,
                class_selector or None,
            )
            domain = self.kb.get_feasible_robot_selection_domain(
                task_type_key,
                task_state,
            )
            family_id = (
                self.kb.resolve_robot_family_id(family_selector, task_type_key)
                if family_selector
                else None
            )
            class_id = (
                self.kb._resolve_class_key(class_selector)
                if class_selector
                else None
            )
            feasible_variant_ids = {
                variant["variant_id"]
                for class_node in domain["classes"]
                if not class_id or class_node["class_id"] == class_id
                for family in class_node["families"]
                if not family_id or family["family_id"] == family_id
                for variant in family["variants"]
            }
            for robot in robots:
                if robot.get("variant_id") not in feasible_variant_ids:
                    continue
                standard = robot.get("full_name")
                if standard:
                    parent = None
                    family = self.kb.robot_fleet.get("robot_families", {}).get(robot.get("family_id"))
                    if family and family.get("full_name"):
                        parent = {
                            "field": "equipment_family",
                            "value": family.get("full_name"),
                        }
                    catalog.append(
                        {
                            "canonical_value": standard,
                            "aliases": list(robot.get("aliases", []) or []),
                            "display_name": robot.get("display_name"),
                            "parent": parent,
                        }
                    )
            return catalog

        if ref == "robot_unit_ids":
            variant_selector = str(task_state.get("equipment_type") or "") if task_state else ""
            if not variant_selector:
                return []
            robot = self.kb.get_rov_for_task(variant_selector, task_type_key)
            if not robot:
                return []
            domain = self.kb.get_feasible_robot_selection_domain(
                task_type_key,
                task_state,
            )
            variant_node = next(
                (
                    variant
                    for class_node in domain["classes"]
                    for family in class_node["families"]
                    for variant in family["variants"]
                    if variant["variant_id"] == robot.get("variant_id")
                ),
                None,
            )
            if not variant_node:
                return []
            feasible_unit_ids = {
                unit["unit_id"] for unit in variant_node["units"]
            }
            for unit in robot.get("fleet_units", []):
                if unit.get("unit_id") not in feasible_unit_ids:
                    continue
                unit_id = unit.get("unit_id")
                if unit_id:
                    aliases = [
                        alias
                        for alias in [unit.get("display_name"), *(unit.get("aliases", []) or [])]
                        if alias
                    ]
                    catalog.append(
                        {
                            "canonical_value": unit_id,
                            "aliases": aliases,
                            "display_name": unit.get("display_name"),
                            "parent": {
                                "field": "equipment_type",
                                "value": robot.get("full_name"),
                            },
                        }
                    )
            return catalog

        return [
            {
                "canonical_value": value,
                "aliases": [],
                "display_name": None,
                "parent": None,
            }
            for value in self.resolve_allowed(field_def, task_type_key, task_state)
        ]

    def build_alias_indexes(
        self,
        catalog: list[dict],
    ) -> tuple[dict[str, str], dict[str, list[str]]]:
        """拆分唯一 alias 与歧义 alias；歧义项保留给 LLM 语义兜底。"""
        alias_display: dict[str, str] = {}
        alias_targets: dict[str, dict[str, str]] = {}

        for item in catalog:
            standard = item.get("canonical_value")
            if not standard:
                continue
            for alias in item.get("aliases", []) or []:
                if not alias:
                    continue
                alias_text = str(alias)
                match_key = FieldNormalizer.make_match_key(alias_text)
                if not match_key:
                    continue
                alias_display.setdefault(match_key, alias_text)
                alias_targets.setdefault(match_key, {})[str(standard)] = str(standard)

        mappings: dict[str, str] = {}
        ambiguous_aliases: dict[str, list[str]] = {}
        for match_key, targets in alias_targets.items():
            alias_text = alias_display[match_key]
            values = list(targets.values())
            if len(values) == 1:
                mappings[alias_text] = values[0]
            else:
                ambiguous_aliases[alias_text] = values

        return mappings, ambiguous_aliases

    def resolve_allowed(
        self,
        field_def: dict,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[str]:
        """解析合法值列表（内联优先，其次 dynamic 或 cache 查找）。"""
        if field_def.get("type") == "tasktype":
            return self.get_template_task_type_values(task_type_key)

        if "allowed_values" in field_def:
            return field_def["allowed_values"]

        ref = field_def.get("allowed_values_ref")
        if not ref:
            return []

        dynamic_robot_refs = {
            "robot_category_labels",
            "robot_class_labels",
            "robot_classes",
            "robot_family_full_names",
            "robot_specifications",
            "equipment_specification",
            "robot_full_names",
            "robot_variant_full_names",
            "robot_unit_ids",
            "supported_payloads",
            "onboard_payloads",
            "all_payloads",
        }

        if ref in dynamic_robot_refs or ref.startswith("payload_options."):
            return self.lookup_ref(ref, task_type_key, task_state)

        if ref in self._ref_cache:
            return self._ref_cache[ref]

        result = self.lookup_ref(ref, task_type_key, task_state)
        self._ref_cache[ref] = result
        return result

    def lookup_ref(
        self,
        ref: str,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[str]:
        """解析 allowed_values_ref 字符串，从知识库中取对应列表。"""
        if ref in ("robot_category_labels", "robot_class_labels", "robot_classes"):
            try:
                classes = self.kb.list_robot_classes(task_type_key)
                domain = self.kb.get_feasible_robot_selection_domain(
                    task_type_key,
                    task_state,
                )
                feasible_class_ids = {
                    item["class_id"] for item in domain["classes"]
                }
                return [
                    c.get("full_name") or c.get("class_id")
                    for c in classes
                    if c and c.get("class_id") in feasible_class_ids
                ]
            except RobotSelectionDataError as exc:
                logger.warning(
                    "Robot category resolution failed: ref=%s task=%s error=%s",
                    ref,
                    task_type_key,
                    exc,
                )
                return []

        if ref == "robot_family_full_names":
            return self.kb.get_task_allowed_robot_family_names(task_type_key)

        if ref in ("robot_full_names", "robot_variant_full_names"):
            family_selector = ""
            class_selector = ""
            if task_state:
                family_selector = str(task_state.get("equipment_family") or "")
                class_selector = str(task_state.get("equipment_class") or "")
            robots = self.kb.get_task_allowed_robot_variants(
                task_type_key,
                family_selector or None,
                class_selector or None,
            )
            family_id = (
                self.kb.resolve_robot_family_id(family_selector, task_type_key)
                if family_selector
                else None
            )
            class_id = (
                self.kb._resolve_class_key(class_selector)
                if class_selector
                else None
            )
            feasible_variant_ids = {
                variant["variant_id"]
                for class_node in self.kb.get_feasible_robot_selection_domain(
                    task_type_key,
                    task_state,
                )["classes"]
                if not class_id or class_node["class_id"] == class_id
                for family in class_node["families"]
                if not family_id or family["family_id"] == family_id
                for variant in family["variants"]
            }
            return [
                robot["full_name"]
                for robot in robots
                if robot.get("variant_id") in feasible_variant_ids
            ]

        if ref == "robot_unit_ids":
            class_selector = str(task_state.get("equipment_class") or "") if task_state else ""
            family_selector = str(task_state.get("equipment_family") or "") if task_state else ""
            type_selector = task_state.get("equipment_type") if task_state else None
            if class_selector and family_selector and type_selector:
                try:
                    robot = self.kb.get_rov_for_task(str(type_selector), task_type_key)
                    units = self.kb.list_robot_units(
                        class_selector,
                        family_selector,
                        type_selector,
                        task_type_key,
                    )
                    domain = self.kb.get_feasible_robot_selection_domain(
                        task_type_key,
                        task_state,
                    )
                    variant_node = next(
                        (
                            variant
                            for class_node in domain["classes"]
                            for family in class_node["families"]
                            for variant in family["variants"]
                            if robot and variant["variant_id"] == robot.get("variant_id")
                        ),
                        None,
                    )
                    feasible_unit_ids = {
                        unit["unit_id"]
                        for unit in (variant_node["units"] if variant_node else [])
                    }
                    units = [
                        unit
                        for unit in units
                        if unit.get("unit_id") in feasible_unit_ids
                    ]
                    if units:
                        return [u.get("unit_id") for u in units if u and u.get("unit_id")]
                except RobotSelectionDataError as exc:
                    logger.warning(
                        "Robot unit resolution failed: ref=%s task=%s error=%s",
                        ref,
                        task_type_key,
                        exc,
                    )
                    return []
            return self.get_robot_unit_ids(task_type_key, task_state)

        if ref == "vessel_ids":
            return [r['id'] for r in self.kb.assets.get("vessels", [])]

        if ref.startswith("payload_options."):
            task_key = ref.split(".", 1)[1]
            task_commons = list(self.kb.assets.get("payload_options", {}).get(task_key, {}).get("common", []))
            eq_type = str(task_state.get("equipment_type") or "") if task_state else ""
            if eq_type:
                robot = self.kb.get_rov(eq_type)
                if robot:
                    # 当选定机器人后，推荐与合法携带工具只包含该机器支持的扩展载荷 supported_payloads
                    robot_supported = list(robot.get("raw_supported_payloads", robot.get("supported_payloads", [])))
                    robot_supported_map = {p.strip().replace(" ", ""): p for p in robot_supported}
                    res = []
                    seen = set()
                    for item in task_commons:
                        k = item.strip().replace(" ", "")
                        if k in robot_supported_map and k not in seen:
                            res.append(robot_supported_map[k])
                            seen.add(k)
                    for item in robot_supported:
                        k = item.strip().replace(" ", "")
                        if k not in seen:
                            res.append(item)
                            seen.add(k)
                    return res
            return task_commons

        if ref in ("supported_payloads", "onboard_payloads", "all_payloads"):
            eq_type = str(task_state.get("equipment_type") or "") if task_state else ""
            if eq_type:
                robot = self.kb.get_rov(eq_type)
                if robot:
                    if ref == "onboard_payloads":
                        return list(robot.get("onboard_payloads", []))
                    elif ref == "supported_payloads":
                        return list(robot.get("raw_supported_payloads", robot.get("supported_payloads", [])))
                    else:
                        return list(robot.get("supported_payloads", []))
            robots = self.kb.get_task_allowed_robot_variants(task_type_key) if task_type_key else self.kb.get_all_rovs()
            res: set[str] = set()
            for r in robots:
                if ref == "onboard_payloads":
                    res.update(r.get("onboard_payloads", []))
                elif ref == "supported_payloads":
                    res.update(r.get("raw_supported_payloads", r.get("supported_payloads", [])))
                else:
                    res.update(r.get("supported_payloads", []))
            return sorted(res)

        return []

    def get_robot_unit_ids(
        self,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[str]:
        """从匹配的型号变体中提取可用的单机 unit_id 列表。"""
        selector = ""
        if task_state:
            selector = str(task_state.get("equipment_type") or "")

        if selector:
            robot = self.kb.get_rov(selector)
            if not robot or not self.kb.robot_matches_task(robot, task_type_key):
                return []
            domain = self.kb.get_feasible_robot_selection_domain(
                task_type_key,
                task_state,
            )
            variant_node = next(
                (
                    variant
                    for class_node in domain["classes"]
                    for family in class_node["families"]
                    for variant in family["variants"]
                    if variant["variant_id"] == robot.get("variant_id")
                ),
                None,
            )
            feasible_unit_ids = {
                unit["unit_id"]
                for unit in (variant_node["units"] if variant_node else [])
            }
            return [
                unit_id
                for unit_id in robot.get("unit_ids", [])
                if unit_id in feasible_unit_ids
            ]

        return []
