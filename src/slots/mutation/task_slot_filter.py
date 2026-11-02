# -*- coding: utf-8 -*-
"""
TaskSlotFilter Module

基于任务 Schema 限定槽位提取范围、过滤跨任务非法候选值，
并在检测到非模板槽位（如无油田槽位任务输入油田）时提供定向坐标引导。
"""

from typing import Any, Dict, List, Set, Tuple, Optional


class TaskSlotFilter:
    """按任务模板与 Schema 约束过滤提取结果并提供槽位引导。"""

    def __init__(self, task_schemas: Optional[Dict[str, Any]] = None):
        self.task_schemas = task_schemas or {}

    @staticmethod
    def supports_oilfield_slots(schema_keys: Set[str]) -> bool:
        """判断当前 Schema 是否显式支持油田相关槽位。"""
        return "oilfield_name" in schema_keys or "oilfield_coordinates" in schema_keys

    def format_non_template_oilfield_message(self, task_type_key: str, raw_value: str) -> str:
        """生成针对非油田模板任务的专属坐标引导提示。"""
        task_templates = self.task_schemas.get("task_templates", {})
        task_info = task_templates.get(task_type_key, {})
        task_display = task_info.get("display_name", task_type_key)
        return (
            f"当前任务类型‘{task_display}’未包含油田槽位，无法通过油田名称“{raw_value}”进行坐标映射。"
            f"请明确具体的坐标（如起始点经纬度、结束点经纬度），而不是通过油田名称映射。"
        )

    def filter_candidates(
        self,
        task_type_key: str,
        effective_schema_keys: Set[str],
        candidates: List[Dict[str, Any]],
        task_type_change_locked: bool = False,
        pending_task_type_key: Optional[str] = None,
        active_task_type_key: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """
        根据当前激活任务 Schema 及控制字段白名单，过滤槽位候选值。

        Returns:
            (projected_candidates, unresolved_messages)
        """
        control_candidate_keys = {
            "task_type",
            "task_type_key",
            "emergency_mode",
            "rov_description",
            "equipment_name",
        }

        if self.supports_oilfield_slots(effective_schema_keys) and not (
            task_type_change_locked
            and pending_task_type_key
            and pending_task_type_key != active_task_type_key
        ):
            control_candidate_keys.add("oilfield_name")

        projected: List[Dict[str, Any]] = []
        unresolved: List[str] = []

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue

            candidate_key = str(candidate.get("canonical_key") or "")
            if candidate_key in effective_schema_keys or candidate_key in control_candidate_keys:
                projected.append(candidate)
                continue

            raw_value = str(
                candidate.get("raw_value")
                or candidate.get("normalized_value")
                or ""
            )

            if candidate_key in ("oilfield_name", "raw_oilfield_name"):
                message = self.format_non_template_oilfield_message(task_type_key, raw_value)
            else:
                message = (
                    f"字段 {candidate_key or '未知字段'} 表达“{raw_value}”"
                    f"不属于目标任务 {task_type_key}，未写入。"
                )
            unresolved.append(message)

        return projected, unresolved

    def check_non_template_oilfield_mention(
        self,
        task_type_key: str,
        effective_schema_keys: Set[str],
        user_message: str,
        unresolved_list: List[str],
        oilfield_linker: Any = None,
    ) -> Optional[str]:
        """
        若当前任务不支持油田槽位，且用户消息中提及“油田”，补充非模板引导信息。
        """
        if self.supports_oilfield_slots(effective_schema_keys):
            return None

        already_guided = any("未包含油田槽位" in str(item) for item in unresolved_list)
        if already_guided or not user_message or "油田" not in user_message:
            return None

        of_str = "油田"
        if oilfield_linker is not None:
            match = oilfield_linker.link(user_message)
            if match and match.status == "accepted" and match.standard_name:
                of_str = match.standard_name

        return self.format_non_template_oilfield_message(task_type_key, of_str)
