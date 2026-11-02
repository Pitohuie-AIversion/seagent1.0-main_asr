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

from src.knowledge_retriever import KnowledgeBase, RobotSelectionDataError
from src.temporal.simulated_time import get_business_date
from src.extraction.coord_parser import parse_coord_value
from .id_sequence import next_daily_task_id, peek_daily_task_id, validate_task_prefix
from src.exceptions import IdReservationError
from .result_paths import get_task_dir, get_history_dir
from src.extraction.normalizer import FieldNormalizer
from .catalog_resolver import CatalogResolver

logger = logging.getLogger(__name__)


class OutputBuilder:
    def __init__(self, kb: KnowledgeBase):
        self.kb = kb
        # 缓存 allowed_values_ref 解析结果（运行期间配置不变）
        self._ref_cache: dict[str, list[str]] = {}
        self.catalog_resolver = CatalogResolver(self.kb, self._ref_cache)

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
                missing_item = {
                    "key":            key,
                    "label":          label,
                    "type":           ftype,
                    "allowed_values": allowed,
                }
                if key == "payload":
                    missing_item.update(
                        self._selected_robot_payload_context(
                            task_type_key,
                            task_state,
                        )
                    )
                missing.append(missing_item)

        return result, missing

    def _selected_robot_payload_context(
        self,
        task_type_key: str,
        task_state: dict | None,
    ) -> dict:
        if not isinstance(task_state, dict):
            return {}
        eq_selectors = [
            task_state.get("equipment_type"),
            task_state.get("equipment_name"),
            task_state.get("equipment_family"),
            task_state.get("equipment_class"),
            task_state.get("equipment_unit_id"),
        ]
        robot = None
        for sel in eq_selectors:
            if not sel or not str(sel).strip():
                continue
            sel_str = str(sel).strip()
            robot = self.kb.get_rov_for_task(sel_str, task_type_key) or self.kb.get_rov(sel_str)
            if not robot and hasattr(self.kb, "resolve_robot_unit"):
                unit_res = self.kb.resolve_robot_unit(sel_str, task_type_key)
                if unit_res and unit_res.get("robot"):
                    robot = unit_res.get("robot")
            if robot:
                break

        if not robot and hasattr(self.kb, "resolve_robot_selection_from_task_state"):
            try:
                selection = self.kb.resolve_robot_selection_from_task_state(task_state, task_type=task_type_key)
                if selection and selection.get("variant"):
                    robot = selection.get("variant")
            except Exception as exc:
                logger.warning("Failed to resolve robot selection from task state: %s", exc)

        if not robot:
            return {}
        onboard = [
            str(item)
            for item in robot.get("onboard_payloads", [])
            if item is not None and str(item).strip()
        ]
        return {
            "equipment_type": robot.get("full_name") or task_state.get("equipment_type") or "选定机器人",
            "onboard_payloads": onboard,
        }

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
    # allowed_values & candidate catalog 解析 (委托给 CatalogResolver)
    # ══════════════════════════════════════════════════════════════════════════

    def resolve_allowed_values(
        self,
        field_def: dict | str,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[str]:
        """解析字段在当前任务状态下的合法候选值。"""
        return self.catalog_resolver.resolve_allowed_values(
            field_def, task_type_key, task_state
        )

    def _resolve_alias_mappings(
        self,
        field_def: dict,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> dict[str, str]:
        return self.catalog_resolver.resolve_alias_mappings(
            field_def, task_type_key, task_state
        )

    def _resolve_candidate_catalog(
        self,
        field_def: dict,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[dict]:
        return self.catalog_resolver.resolve_candidate_catalog(
            field_def, task_type_key, task_state
        )

    def _build_alias_indexes(
        self,
        catalog: list[dict],
    ) -> tuple[dict[str, str], dict[str, list[str]]]:
        return self.catalog_resolver.build_alias_indexes(catalog)

    def _resolve_allowed(
        self,
        field_def: dict,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[str]:
        return self.catalog_resolver.resolve_allowed(
            field_def, task_type_key, task_state
        )

    def _lookup_ref(
        self,
        ref: str,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[str]:
        return self.catalog_resolver.lookup_ref(
            ref, task_type_key, task_state
        )

    def _get_robot_unit_ids(
        self,
        task_type_key: str = "",
        task_state: dict | None = None,
    ) -> list[str]:
        return self.catalog_resolver.get_robot_unit_ids(
            task_type_key, task_state
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Schema 获取
    # ══════════════════════════════════════════════════════════════════════════

    def _get_schema(self, task_type_key: str, schema_key: str) -> list[dict] | None:
        task_templates = self.kb.task_schemas.get("task_templates", {})
        task_cfg = task_templates.get(task_type_key, {})
        return task_cfg.get("output_schema", {}).get(schema_key)
