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

from typing import Any

from .knowledge_retriever import KnowledgeBase
from .simulated_time import get_current_date
from .coord_parser import parse_coord_value
from .id_sequence import next_daily_id
from .result_paths import get_task_dir, get_history_dir


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
                allowed = self.resolve_allowed_values(
                    field_def,
                    task_type_key,
                    task_state,
                )
                if allowed:
                    item["allowed_values"] = allowed
                alias_mappings = self._resolve_alias_mappings(
                    field_def,
                    task_type_key,
                    task_state,
                )
                if alias_mappings:
                    item["alias_mappings"] = alias_mappings
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
            return self._generate_task_id(task_type_key, task_state)

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

        if ftype == "raw":
            return raw if raw else None

        if ftype == "string":
            if raw is None:
                return None
            allowed = self._resolve_allowed(field_def, task_type_key, task_state)
            if not allowed:
                return str(raw)
            # 必须是 allowed_values 中的值，否则视为未规范化（缺失）
            if isinstance(raw, str) and raw in allowed:
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
            allowed = self._resolve_allowed(field_def, task_type_key, task_state)
            if not allowed:
                return raw if isinstance(raw, list) else None
            # 过滤出合法值
            if isinstance(raw, list):
                valid = [v for v in raw if v in allowed]
                return valid if valid else None
            return None

        return None

    # ══════════════════════════════════════════════════════════════════════════
    # task_id 自动生成
    # ══════════════════════════════════════════════════════════════════════════

    def _generate_task_id(self, task_type_key: str, task_state: dict) -> str:
        existing = task_state.get("task_id")
        if existing:
            return existing
        templates = self.kb.task_schemas.get("task_templates", {})
        code = templates.get(task_type_key, {}).get("code", "XX")
        # 使用模拟日期
        today = get_current_date().strftime("%Y%m%d")
        return next_daily_id(
            code,
            today,
            2,
            [(get_task_dir(create=False), "task_id"), (get_history_dir(create=False), "task_id")],
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
            family_selector = (
                str(task_state.get("equipment_family") or "")
                if task_state
                else ""
            )
            for robot in self.kb.get_task_allowed_robot_variants(
                task_type_key,
                family_selector or None,
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
                        unit.get("display_name"),
                        *unit.get("aliases", []),
                    ]
                    for alias in targets:
                        add_mapping(alias, unit_id)
            return mappings

        return mappings

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

        selector = ""
        if task_state and ref in ("robot_full_names", "robot_variant_full_names"):
            selector = str(task_state.get("equipment_family") or "")
        elif ref == "robot_unit_ids" and task_state:
            selector = str(task_state.get("equipment_type") or "")
        cache_key = (
            f"{ref}:{task_type_key}:{selector}"
            if ref in (
                "robot_family_full_names",
                "robot_full_names",
                "robot_variant_full_names",
                "robot_unit_ids",
            )
            else ref
        )
        if cache_key in self._ref_cache:
            return self._ref_cache[cache_key]

        result = self._lookup_ref(ref, task_type_key, task_state)
        self._ref_cache[cache_key] = result
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
        if ref == "robot_category_labels":
            return self.kb.get_robot_class_labels()

        if ref == "robot_family_full_names":
            return self.kb.get_task_allowed_robot_family_names(task_type_key)

        if ref in ("robot_full_names", "robot_variant_full_names"):
            family_selector = ""
            if task_state:
                family_selector = str(task_state.get("equipment_family") or "")
            return [
                r["full_name"]
                for r in self.kb.get_task_allowed_robot_variants(
                    task_type_key,
                    family_selector or None,
                )
            ]

        if ref == "robot_unit_ids":
            return self._get_robot_unit_ids(task_type_key, task_state)

        if ref == "vessel_ids":
            return [r['id'] for r in self.kb.assets.get("vessels", [])]
            # return self.kb.assets.get("vessel_ids", [])

        if ref.startswith("payload_options."):
            task_key = ref.split(".", 1)[1]
            return self.kb.assets.get("payload_options", {}).get(task_key, []).get("common", [])

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
            return list(robot.get("unit_ids", []))

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
