"""
src/knowledge/prompt_grounder.py — 根据任务状态与阶段组装动态提示词上下文
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import format_seabed_type, format_telemetry_value

if TYPE_CHECKING:
    from ..knowledge_retriever import KnowledgeBase


class PromptGrounder:
    """根据任务状态和上下文组装注入给大模型的领域知识文本。"""

    def __init__(self, kb: "KnowledgeBase"):
        self.kb = kb

    @property
    def assets(self) -> dict:
        return self.kb.assets

    @property
    def task_schemas(self) -> dict:
        return self.kb.task_schemas

    @property
    def constraints(self) -> list:
        return self.kb.constraints

    def get_supported_task(self) -> list:
        return self.kb.get_all_task_type_values()

    def get_context_for_state(self, task_state: dict) -> str:
        """
        根据当前任务状态，返回最相关的专业知识文本（供注入 system prompt）。
        分段组装，只选取与当前阶段相关的内容。
        """
        task_type = task_state.get("task_type_key")  # e.g. "pipeline_inspection"
        equipment_type = task_state.get("equipment_type")
        legacy_equipment = task_state.get("equipment_name")
        coords = task_state.get("start_point") or task_state.get("oilfield_coordinates")
        sections = [self._robot_category_overview()]

        # 2. 与当前任务类型相关的 ROV 约束
        if task_type:
            sections.append(self._task_rov_constraint(task_type))

        unit_selector = task_state.get("equipment_unit_id")
        resolved_unit = (
            self.kb.resolve_robot_unit(
                str(unit_selector),
                task_type,
                str(equipment_type) if equipment_type else None,
            )
            if unit_selector
            else None
        )
        if not resolved_unit and legacy_equipment:
            resolved_unit = self.kb.resolve_robot_unit(
                str(legacy_equipment),
                task_type,
                str(equipment_type) if equipment_type else None,
            )
        selected_robot = (
            resolved_unit.get("robot")
            if resolved_unit
            else (
                self.kb.get_rov_for_task(
                    str(equipment_type or legacy_equipment),
                    task_type,
                )
                if equipment_type or legacy_equipment
                else None
            )
        )
        if selected_robot:
            rov_info = self._get_rov_info(selected_robot.get("full_name"))
            if rov_info:
                sections.append(f"【当前选定设备详情】\n{rov_info}")
                state_selector = (
                    resolved_unit.get("unit_id")
                    if resolved_unit
                    else selected_robot.get("full_name")
                )
                state_dict = self.kb.get_robot_state_dict(state_selector)
                if state_dict and isinstance(state_dict, dict):
                    state_lines = []
                    label_map = {
                        "version": "状态版本号",
                        "water_current_velocity": "环境水流速度",
                        "water_turbidity": "水体浑浊度",
                        "obstacle_density": "障碍物密度",
                        "mothership_support": "母船支援",
                        "update_timestamp": "更新时间",
                        "confidence": "置信度",
                        "overall_status": "总体状态",
                        "survival_status": "生存状态",
                        "thruster_status": "推进器状态",
                        "depth_keeping_status": "定深能力",
                        "sonar_status": "声呐状态",
                        "vision_status": "视觉系统状态",
                        "arm_status": "机械臂状态",
                        "end_effector_status": "末端执行器状态",
                        "acoustic_comms_status": "水声无线通信状态",
                        "tether_connection_status": "脐带缆连接状态"
                    }
                    robot_class = (selected_robot.get("robot_class") or "").lower()
                    is_auv = robot_class == "auv" or "auv" in (selected_robot.get("full_name") or "").lower() or "auv" in str(state_selector).lower()
                    
                    for k, v in state_dict.items():
                        if v is not None and not k.startswith("_"):
                            # 过滤不适用于当前设备类别的字段，消除 LLM 产生“无缆设备有脐带缆”的输出歧义
                            if is_auv and k == "tether_connection_status":
                                continue

                            label = label_map.get(k, k)
                            if k == "water_current_velocity":
                                if isinstance(v, (int, float)):
                                    state_lines.append(f"  - {label}: {v:.2f} m/s")
                                else:
                                    state_lines.append(f"  - {label}: {v} m/s")
                            elif isinstance(v, float):
                                state_lines.append(f"  - {label}: {v:.2f}")
                            elif isinstance(v, str):
                                state_lines.append(f"  - {label}: {format_telemetry_value(v)}")
                            else:
                                state_lines.append(f"  - {label}: {v}")
                    if state_lines:
                        sections.append("【当前设备实时状态】\n" + "\n".join(state_lines))
        elif task_type:
            sections.append(self._rovs_for_task(task_type, task_state.get("equipment_class")))

        # 4. 管缆类型（管缆巡检任务）
        if task_type == "pipeline_inspection":
            sections.append(self._cable_types_overview())
            sections.append(self._payload_suggestions("pipeline_inspection"))
        elif task_type == "pipeline_burial":
            sections.append(self._cable_types_overview())
            sections.append(self._payload_suggestions("pipeline_burial"))
        elif task_type == "tree_valve_operation":
            sections.append(self._payload_suggestions("tree_valve_operation"))

        # 6. 支持船信息
        sections.append(self._vessels_overview())

        # 7. 海域环境（有坐标时匹配）
        if coords:
            env_info = self._get_environment(coords)
            if env_info:
                sections.append(f"【作业区域环境状态】\n{env_info}")

        # 8. 适用约束规则摘要
        sections.append(self._relevant_constraints(task_type))

        return "\n\n".join(s for s in sections if s and s.strip())

    def _robot_category_overview(self) -> str:
        lines = ["【机器人四大类说明】"]
        for key, value in self.kb.get_robot_classes().items():
            lines.append(f"- {value.get('full_name', key)}")
        return "\n".join(lines)

    def _task_rov_constraint(self, task_type: str) -> str:
        schema = self.task_schemas["task_templates"].get(task_type, {})
        caps = "、".join(schema.get("required_capabilities", []))
        if not caps:
            return ""
        return (
            f"【任务设备约束】{schema.get('display_name', task_type)} 任务要求机器人系列能力覆盖："
            f"{caps or '无特殊能力'}。"
        )

    def _rovs_by_category(self, category_value: str, task_type: str | None = None) -> str:
        class_key = self.kb._resolve_robot_class_key(category_value)
        cat_label = self.kb.get_robot_classes().get(class_key, {}).get("full_name", category_value)
        rovs = [
            r for r in self.kb.get_all_rovs()
            if r.get("robot_class") == class_key and self.kb.robot_matches_task(r, task_type)
        ]
        if not rovs:
            return f"【{cat_label}】当前无符合任务条件的设备。"
        lines = [f"【{cat_label}设备列表】"]
        for r in rovs:
            lines.append(f"- {r['full_name']} | 最大水深:{r.get('max_depth_m')}m\n  {r.get('brief', '')}")
        return "\n".join(lines)

    def _rovs_for_task(self, task_type: str, class_selector: str | None = None) -> str:
        rovs = self.kb.get_task_allowed_robot_variants(task_type, class_selector=class_selector)
        if not rovs:
            return "【任务可用设备】当前无符合任务条件的设备。"
        lines = ["【任务可用设备】"]
        for r in rovs:
            lines.append(
                f"- {r['full_name']} | 类型:{r.get('robot_class_name')} | "
                f"能力:{'、'.join(r.get('capabilities', []))} | 最大水深:{r.get('max_depth_m')}m"
            )
        return "\n".join(lines)

    def _get_rov_info(self, model_or_alias: str) -> str | None:
        rov = self.kb._find_rov(model_or_alias)
        if not rov:
            return None
        onboard_list = rov.get("onboard_payloads", [])
        supported_list = rov.get("supported_payloads", [])
        if onboard_list:
            onboard_str = "、".join(onboard_list)
            supported_str = "、".join(supported_list)
            payload_desc = f"自带载荷: {onboard_str}\n可选搭载载荷: {supported_str}"
        else:
            payloads = "、".join(supported_list)
            payload_desc = f"可搭载载荷: {payloads}"
        return (
            f"{rov['full_name']}\n"
            f"类型: {rov.get('robot_class_name')} | 能力: {'、'.join(rov.get('capabilities', []))} | "
            f"最大水深: {rov.get('max_depth_m')}m\n"
            f"{payload_desc}\n"
            f"简介: {rov.get('brief', '')}"
        )

    def _cable_types_overview(self) -> str:
        types = self.assets["cable_types"]
        lines = ["【管缆类型（必须使用标准名称）】"]
        for t in types:
            aliases = "、".join(t["aliases"][:4])
            lines.append(f"- {t['label']}（别名：{aliases}）")
        return "\n".join(lines)

    def _payload_suggestions(self, task_type: str) -> str:
        pt = self.assets["payload_options"].get(task_type, {})
        common = "、".join(pt.get("common", []))
        desc = pt.get("description", "")
        label = self.task_schemas.get("task_templates", {}).get(task_type, {}).get("display_name", task_type)
        return f"【{label}常用携带工具建议】\n{common}\n备注: {desc}"

    def _vessels_overview(self) -> str:
        lines = ["【可用支持船只列表】"]
        for v in self.assets["vessels"]:
            status = "✓ 可用" if v["available"] else "✗ 不可用"
            lines.append(f"- {v['full_name']}（{v['type']}）[{status}] — {v['description']}")
        return "\n".join(lines)

    def _get_environment(self, coords: dict) -> str | None:
        oil_field = self.kb.get_environment_for_coords(coords)
        if not oil_field:
            return None
        seabed_cn = format_seabed_type(oil_field.get('seabed_type'))
        return (
            f"{oil_field['name']} \n"
            f"海底底质: {seabed_cn}\n"
            f"备注: {oil_field['notes']}"
        )

    def _relevant_constraints(self, task_type: str | None) -> str:
        lines = ["【相关作业约束规则】"]
        for c in self.constraints:
            applies = c["applies_to"]
            if "all" not in applies and task_type and task_type not in applies:
                continue
            lines.append(f"[{c['id']}] {c['name']}: {c['violation_message'].strip()}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)
