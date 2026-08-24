# -*- coding: utf-8 -*-
"""
TaskCapabilityAdapter Module

将任务模板（task_templates）中要求的 required_capabilities 与载荷配置域（payload_options）
进行集中管理与校验解耦，为机器人选型及工具链匹配提供能力查询与校验支持。
"""

from typing import Any, Dict, List, Set, Optional


class TaskCapabilityAdapter:
    """任务能力与载荷配置域适配器。"""

    def __init__(self, task_schemas: Optional[Dict[str, Any]] = None):
        self.task_schemas = task_schemas or {}

    def get_task_template(self, task_type_key: str) -> Dict[str, Any]:
        """获取指定 task_type_key 的任务模板定义。"""
        templates = self.task_schemas.get("task_templates", {})
        return templates.get(task_type_key, {})

    def get_required_capabilities(self, task_type_key: str) -> List[str]:
        """获取指定任务模板要求的能力列表（如 ['inspection']）。"""
        template = self.get_task_template(task_type_key)
        caps = template.get("required_capabilities", [])
        return list(caps) if isinstance(caps, (list, tuple, set)) else []

    def get_payload_options_ref(self, task_type_key: str) -> str:
        """获取任务对应的载荷配置引用标识（如 'payload_options.pipeline_inspection'）。"""
        return f"payload_options.{task_type_key}"

    def is_payload_supported_for_task(
        self,
        task_type_key: str,
        payload_name: str,
        payload_options: Optional[Dict[str, List[str]]] = None,
    ) -> bool:
        """
        校验指定的载荷名称是否在当前任务类型的可搭配工具集中。
        """
        if not payload_name or not task_type_key:
            return False

        if payload_options and task_type_key in payload_options:
            allowed = payload_options.get(task_type_key, [])
            return payload_name in allowed

        return True

    def format_payload_guidance(self, text: str, missing_fields: list) -> str:
        """格式化 payload 引导话术，告知用户机器人的 on_board 已搭载项，并提示在下面列表中做替换或增减配。"""
        if not missing_fields:
            return text
        payload_field = next((f for f in missing_fields if isinstance(f, dict) and f.get("key") == "payload"), None)
        if not payload_field:
            return text

        eq_type = payload_field.get("equipment_type") or "所选机器人"
        onboard = payload_field.get("onboard_payloads") or []
        if onboard:
            onboard_str = " / ".join(onboard)
            prefix = f"【提示】{eq_type} 已搭载：{onboard_str}。\n请在下方载荷按钮列表中进行替换、增加或减少配置。"
        else:
            prefix = f"【提示】{eq_type} 的基础配置可在下方载荷按钮列表中进行替换、增加或减少配置。"

        if text:
            return f"{prefix}\n{text}"
        return prefix
