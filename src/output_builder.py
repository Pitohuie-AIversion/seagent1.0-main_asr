"""
output_builder.py — 标准 JSON 构建器 & 完整性检查器

职责：
1. 从 task_state 按照 task_schemas.yaml 中的 output_schema 构建标准 flat JSON
2. 用 Python 判断哪些字段缺失（不依赖 LLM）
3. 解析 allowed_values_ref，从 assets/robot_fleet 中动态获取合法值列表

输出 JSON 结构规则：
- 所有字段并列，无嵌套
- 唯一例外：type=coord 的字段值为 {"lat": float, "lon": float}
"""

import logging
from typing import Any

from .knowledge_retriever import KnowledgeBase, RobotSelectionDataError
from .simulated_time import get_business_date
from .coord_parser import parse_coord_value
from .id_sequence import next_daily_task_id, peek_daily_task_id, validate_task_prefix
from .exceptions import IdReservationError
from .result_paths import get_task_dir, get_history_dir
from .normalizer import FieldNormalizer

logger = logging.getLogger(__name__)


class OutputBuilder:
    def __init__(self, kb: KnowledgeBase):
        self.kb = kb
        # 缓存 allowed_values_ref 解析结果（运行期间配置不变）
        self._ref_cache: dict[str, list[str]] = {}

    # ══════════════════════════════════════════════════════════════════════════
    # 主接口
    # ══════════════════════════════════════════════════════════════════════════

    def get_required(
            self,
            task_type_key: str,
            mode: str = "normal",
            task_state: dict | None = None,
    ) -> list[dict]:
        """
        获取当前任务模板下所需字段（含 allowed_values）
        """
        schema_key = "emergency" if mode == "emergency" else "normal"
        schema = self._get_schema(task_type_key, schema_key)
        if not schema:
            return [{"key": "task_type_key", "label": "任务类型", "type": "string"}]

        required: list[dict] = []

        for field_def in schema:
            key = field_def["key"]
            label = field_def["label"]
            ftype = field_def["type"]
            if ftype not in ("auto", "fixed"):
                item = {"key": key, "label": label, "type": ftype}
                catalog = self._resolve_candidate_catalog(
                    field_def,
                    task_type_key,
                    task_state,
                )
                allowed = [item["canonical_value"] for item in catalog]
                if allowed:
                    item["allowed_values"] = allowed
                alias_mappings, ambiguous_aliases = self._build_alias_indexes(catalog)
                if alias_mappings:
                    item["alias_mappings"] = alias_mappings
                if ambiguous_aliases:
                    item["ambiguous_aliases"] = ambiguous_aliases
                if catalog:
                    item["candidate_evidence"] = catalog
                required.append(item)

        return required

    def build(
        self,
        task_state: dict,
        task_type_key: str,
        mode: str = "normal",   # "normal" | "emergency"
    ) -> tuple[dict, list[dict]]:
        """
        构建标准 flat JSON 并返回缺失字段列表。

        Returns:
            (json_dict, missing_fields)
            json_dict     — 尽可能填充的结果，缺失字段不出现在 dict 中
            missing_fields — [{"key": str, "label": str, "type": str, "allowed_values": [...]}]
        """
        schema_key = "emergency" if mode == "emergency" else "normal"
        schema = self._get_schema(task_type_key, schema_key)
        if not schema:
            return {}, [{"key": "task_type_key", "label": "任务类型", "type": "string", "allowed_values": []}]

        result: dict = {}
        missing: list[dict] = []

        for field_def in schema:
            key       = field_def["key"]
            label     = field_def["label"]
            ftype     = field_def["type"]
            allowed   = self._resolve_allowed(field_def, task_type_key, task_state)

            value = self._extract_field(key, ftype, field_def, task_state, task_type_key)

            if value is not None:
                result[key] = value
            elif ftype not in ("auto", "fixed"):
                missing.append({
                    "key":            key,
                    "label":          label,
                    "type":           ftype,
                    "allowed_values": allowed,
                })

        return result, missing

    def get_allowed_values(self, task_type_key: str, field_key: str, mode: str = "normal") -> list[str]:
        """查询某个字段的合法值列表（供 normalizer 调用）"""
        schema_key = "emergency" if mode == "emergency" else "normal"
        schema = self._get_schema(task_type_key, schema_key)
        if not schema:
            return []
        for field_def in schema:
            if field_def["key"] == field_key:
                return self._resolve_allowed(field_def, task_type_key)
        return []

    def get_schema(self, task_type_key: str, mode: str = "normal") -> list[dict]:
        """返回完整 schema 定义列表"""
        schema_key = "emergency" if mode == "emergency" else "normal"
        return self._get_schema(task_type_key, schema_key) or []

    # ══════════════════════════════════════════════════════════════════════════
    # 字段值提取
    # ══════════════════════════════════════════════════════════════════════════

    def _extract_field(
        self,
        key: str,
        ftype: str,
        field_def: dict,
        task_state: dict,
        task_type_key: str,
    ) -> Any:
        if ftype == "auto":
            return task_state.get(key)

        if ftype == "fixed":
            return field_def.get("fixed_value")

        # tasktype: allowed_values 来自本模板的 task_type_values
        if ftype == "tasktype":
            raw = task_state.get(key)
            if raw is None:
                return None
            allowed = self._get_template_task_type_values(task_type_key)
            return raw if raw in allowed else None

        raw = task_state.get(key)

        if ftype == "coord":
            return self._validate_coord(raw)

        if ftype == "number":
            return self._validate_number(raw)

        if ftype == "datetime":
            return self._validate_datetime(raw)

        if ftype in ("object", "raw"):
            return raw if raw else None

        if ftype == "string":
            if raw is None or not isinstance(raw, str):
                return None
            allowed = self._resolve_allowed(field_def, task_type_key, task_state)
            if not allowed:
                return raw
            # 必须是 allowed_values 中的值，否则视为未规范化（缺失）
            if raw in allowed:
                return raw

            # 新增逻辑：去除所有空格后匹配，返回 allowed 中的原始值
            raw_stripped = raw.replace(" ", "")  # 去掉所有空格
            for item in allowed:
                if isinstance(item, str) and item.replace(" ", "") == raw_stripped:
                    return item  # 返回 allowed 里的原始值

            return None  # 未规范化，交给 normalizer 处理

        if ftype == "list":
            if not raw:
                return None
            raw_list = [raw] if isinstance(raw, str) else (list(raw) if isinstance(raw, (list, tuple, set)) else None)
            if not raw_list:
                return None
            allowed = self._resolve_allowed(field_def, task_type_key, task_state)
            if not allowed:
                return list(raw_list)

            allowed_stripped_map = {str(item).replace(" ", ""): item for item in allowed}
            valid_list = []
            for item in raw_list:
                if item in allowed:
                    valid_list.append(item)
                elif isinstance(item, str) and item.replace(" ", "") in allowed_stripped_map:
                    matched = allowed_stripped_map[item.replace(" ", "")]
                    valid_list.append(matched)
                else:
                    # 任一元素非法 → 整个列表返回 None
                    return None

            return list(valid_list)

        return None

    # ══════════════════════════════════════════════════════════════════════════
    # task_id 显式生成入口
    # ══════════════════════════════════════════════════════════════════════════

    def reserve_task_id(self, task_type_key: str) -> str:
        """显式预留新的任务业务编号 (<PREFIX>-YYYYMMDD-NNN)。

        权威前缀仅取自 KnowledgeBase.task_schemas["task_templates"][task_type_key]["code"]。
        前缀缺失或非法时直接抛出 IdReservationError。
        此函数消耗正式编号，只应在最终确认发布时调用一次。
        """
        return self._generate_task_id(task_type_key)

    def preview_task_id(self, task_type_key: str) -> str:
        """预览下一个任务业务编号 (<PREFIX>-YYYYMMDD-NNN)，只读估算，不消耗编号。

        用于草稿阶段向用户展示预计任务编号，对用户必须说明这是预估值。
        权威编号以发布时 reserve_task_id() 的返回值为准。

        前缀缺失或非法时直接抛出 IdReservationError。
        """
        templates = self.kb.task_schemas.get("task_templates", {})
        if task_type_key not in templates:
            raise IdReservationError(f"Task type key {task_type_key!r} not found in task templates schema.")

        template = templates[task_type_key]
        code = template.get("code")
        if not code or not validate_task_prefix(code):
            raise IdReservationError(
                f"Invalid or missing code prefix {code!r} for task_type_key {task_type_key!r}."
            )

        allowed_prefixes = [t.get("code") for t in templates.values() if t.get("code")]
        today = get_business_date().strftime("%Y%m%d")
        return peek_daily_task_id(
            code,
            today,
            3,
            [(get_task_dir(create=False), "task_id"), (get_history_dir(create=False), "task_id")],
            allowed_prefixes=allowed_prefixes,
        )

    def _generate_task_id(self, task_type_key: str, task_state: dict | None = None) -> str:
        templates = self.kb.task_schemas.get("task_templates", {})
        if task_type_key not in templates:
            raise IdReservationError(f"Task type key {task_type_key!r} not found in task templates schema.")

        template = templates[task_type_key]
        code = template.get("code")
        if not code or not validate_task_prefix(code):
            raise IdReservationError(
                f"Invalid or missing code prefix {code!r} for task_type_key {task_type_key!r}."
            )

        allowed_prefixes = [t.get("code") for t in templates.values() if t.get("code")]
        today = get_business_date().strftime("%Y%m%d")
        return next_daily_task_id(
            code,
            today,
            3,
            [(get_task_dir(create=False), "task_id"), (get_history_dir(create=False), "task_id")],
            allowed_prefixes=allowed_prefixes,
        )



    def _get_template_task_type_values(self, task_type_key: str) -> list[str]:
        """返回某模板下的合法 task_type 值（供 tasktype 字段校验用）"""
        templates = self.kb.task_schemas.get("task_templates", {})
        return templates.get(task_type_key, {}).get("task_type_values", [])

    # ══════════════════════════════════════════════════════════════════════════
    # 类型校验工具
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _validate_coord(raw: Any) -> dict | None:
        return parse_coord_value(raw)

    @staticmethod
    def _validate_number(raw: Any) -> float | None:
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _validate_datetime(raw: Any) -> str | None:
        if not isinstance(raw, str):
            return None
        import re
        pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$"
        return raw if re.match(pattern, raw) else None

    # ══════════════════════════════════════════════════════════════════════════
    # allowed_values 解析
    # ══════════════════════════════════════════════════════════════════════════

    def resolve_allowed_values(
        self,
        field_def: dict,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[str]:
        """解析字段在当前任务状态下的合法候选值。"""
        return self._resolve_allowed(field_def, task_type_key, task_state)

    def _resolve_alias_mappings(
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
            return mappings

        if ref == "robot_unit_ids":
            variant_selector = (
                str(task_state.get("equipment_type") or "")
                if task_state
                else ""
            )
            # fleet_units 必须依赖已确认的 model_variant。型号尚未确定时，
            # 不向提取器暴露其他型号的单机 aliases。
            if not variant_selector:
                return {}
            robots = [
                self.kb.get_rov_for_task(variant_selector, task_type_key)
            ]
            for robot in (item for item in robots if item):
                for unit in robot.get("fleet_units", []):
                    unit_id = unit.get("unit_id")
                    if not unit_id:
                        continue
                    targets = [
                        unit_id,
                        unit.get("display_name"),
                        *unit.get("aliases", []),
                    ]
                    for alias in targets:
                        add_mapping(alias, unit_id)

            return mappings

        return mappings

    def _resolve_candidate_catalog(
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
                for value in self._resolve_allowed(field_def, task_type_key, task_state)
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
                logger.warning("Robot candidate catalog resolution failed: ref=%s task=%s error=%s", ref, task_type_key, exc)
                return []

        if ref == "robot_family_full_names":
            class_selector = str(task_state.get("equipment_class") or "") if task_state else ""
            if class_selector:
                try:
                    families = self.kb.list_robot_families(
                        class_selector,
                        task_type_key,
                    )
                    domain = self.kb.get_feasible_robot_selection_domain(
                        task_type_key,
                        task_state,
                    )
                    class_id = self.kb._resolve_class_key(class_selector)
                    class_node = next(
                        (
                            item
                            for item in domain["classes"]
                            if item["class_id"] == class_id
                        ),
                        None,
                    )
                    feasible_family_ids = {
                        item["family_id"]
                        for item in (class_node["families"] if class_node else [])
                    }
                    for family in families:
                        if family.get("family_id") not in feasible_family_ids:
                            continue
                        standard = family.get("full_name")
                        if standard:
                            catalog.append({
                                "canonical_value": standard,
                                "aliases": list(family.get("aliases", []) or []),
                                "display_name": family.get("display_name"),
                                "parent": {"field": "equipment_class", "value": class_selector},
                            })
                    return catalog
                except RobotSelectionDataError as exc:
                    logger.warning("Robot candidate catalog resolution failed: ref=%s task=%s error=%s", ref, task_type_key, exc)
                    return []
            for _, family in self.kb.get_robot_families_for_task(task_type_key):
                standard = family.get("full_name")
                if standard:
                    catalog.append(
                        {
                            "canonical_value": standard,
                            "aliases": list(family.get("aliases", []) or []),
                            "display_name": family.get("display_name"),
                            "parent": None,
                        }
                    )
            return catalog


        if ref in ("robot_full_names", "robot_variant_full_names"):
            class_selector = str(task_state.get("equipment_class") or "") if task_state else ""
            family_selector = str(task_state.get("equipment_family") or "") if task_state else ""
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
            for value in self._resolve_allowed(field_def, task_type_key, task_state)
        ]

    def _build_alias_indexes(
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

    def _resolve_allowed(
        self,
        field_def: dict,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[str]:
        # tasktype：合法值来自本模板的 task_type_values
        if field_def.get("type") == "tasktype":
            return self._get_template_task_type_values(task_type_key)

        # 内联定义优先
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
            return self._lookup_ref(ref, task_type_key, task_state)

        if ref in self._ref_cache:
            return self._ref_cache[ref]

        result = self._lookup_ref(ref, task_type_key, task_state)
        self._ref_cache[ref] = result
        return result

    def _lookup_ref(
        self,
        ref: str,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[str]:
        """
        解析 allowed_values_ref 字符串，从知识库中取对应列表。
        支持：
          robot_category_labels          → 所有 ROV 类型的 label
          robot_family_full_names        → 当前任务允许的机器人族 full_name
          robot_variant_full_names       → 当前任务及机器人族允许的型号 full_name
          robot_full_names               → robot_variant_full_names 的兼容名称
          payload_options.pipeline_inspection
          payload_options.tree_valve_operation
          vessel_ids
        """
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
                logger.warning("Robot category resolution failed: ref=%s task=%s error=%s", ref, task_type_key, exc)
                return []

        if ref == "robot_family_full_names":
            class_selector = str(task_state.get("equipment_class") or "") if task_state else ""
            if class_selector:
                try:
                    families = self.kb.list_robot_families(
                        class_selector,
                        task_type_key,
                    )
                    domain = self.kb.get_feasible_robot_selection_domain(
                        task_type_key,
                        task_state,
                    )
                    class_id = self.kb._resolve_class_key(class_selector)
                    class_node = next(
                        (
                            item
                            for item in domain["classes"]
                            if item["class_id"] == class_id
                        ),
                        None,
                    )
                    feasible_family_ids = {
                        item["family_id"]
                        for item in (class_node["families"] if class_node else [])
                    }
                    return [
                        f.get("full_name")
                        for f in families
                        if f
                        and f.get("full_name")
                        and f.get("family_id") in feasible_family_ids
                    ]
                except RobotSelectionDataError as exc:
                    logger.warning("Robot family resolution failed: ref=%s task=%s error=%s", ref, task_type_key, exc)
                    return []
            return self.kb.get_task_allowed_robot_family_names(task_type_key)

        if ref in ("robot_full_names", "robot_variant_full_names"):
            class_selector = ""
            family_selector = ""
            if task_state:
                class_selector = str(task_state.get("equipment_class") or "")
                family_selector = str(task_state.get("equipment_family") or "")
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
                    logger.warning("Robot unit resolution failed: ref=%s task=%s error=%s", ref, task_type_key, exc)
                    return []
            return self._get_robot_unit_ids(task_type_key, task_state)

        if ref == "vessel_ids":
            return [r['id'] for r in self.kb.assets.get("vessels", [])]
            # return self.kb.assets.get("vessel_ids", [])

        if ref.startswith("payload_options."):
            task_key = ref.split(".", 1)[1]
            task_commons = list(self.kb.assets.get("payload_options", {}).get(task_key, {}).get("common", []))
            eq_type = str(task_state.get("equipment_type") or "") if task_state else ""
            if eq_type:
                robot = self.kb.get_rov(eq_type)
                if robot:
                    # task_commons 是任务维度的通用推荐工具集；robot.all_payloads 是
                    # 该型号实际支持的全部载荷（onboard + supported）。
                    # 合法值 = (task_commons ∩ robot.all_payloads) ∪ robot.onboard_payloads
                    #
                    # 设计原则：
                    # 1. 交集：只保留当前任务场景下有意义、且该机器人实际支持的工具；
                    # 2. 并集 onboard_payloads：机器人自带的必选传感器（INS、DVL 等）
                    #    不在 task_commons 中，但属于合法的设备配置，必须允许填写。
                    robot_all_key = {p.strip().replace(" ", "") for p in robot.get("all_payloads", [])}
                    task_intersect = [
                        item for item in task_commons
                        if item.strip().replace(" ", "") in robot_all_key
                    ]
                    onboard = list(robot.get("onboard_payloads", []))
                    # 去重，保持 task_intersect 顺序在前，onboard 补充在后
                    seen: set[str] = {item.strip().replace(" ", "") for item in task_intersect}
                    for item in onboard:
                        k = item.strip().replace(" ", "")
                        if k not in seen:
                            task_intersect.append(item)
                            seen.add(k)
                    return task_intersect
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

    def _get_robot_unit_ids(
        self,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[str]:
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

        # 没有 equipment_type 时无法确定 model_variant，不能把当前任务下
        # 所有型号的 fleet_units 混成一个候选列表。
        return []

    # ══════════════════════════════════════════════════════════════════════════
    # Schema 获取
    # ══════════════════════════════════════════════════════════════════════════

    def _get_schema(self, task_type_key: str, schema_key: str) -> list[dict] | None:
        task_templates = self.kb.task_schemas.get("task_templates", {})
        task_cfg = task_templates.get(task_type_key, {})
        return task_cfg.get("output_schema", {}).get(schema_key)
