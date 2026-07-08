"""
knowledge_retriever.py — 知识库加载与按需检索
不使用向量数据库，基于任务状态进行规则化知识片段选取。
知识总量在10000字以内，精准注入比全量注入更高效。
"""

import yaml
from pathlib import Path
from typing import Any
from .environment_info import EnvironmentInfo
from .state_info import RobotStateInfo

CONFIG_DIR = Path(__file__).parent.parent / "config"


def _load(filename: str) -> dict | list:
    with open(CONFIG_DIR / filename, encoding="utf-8") as f:
        return yaml.safe_load(f)


class KnowledgeBase:
    def __init__(self):
        self.load_all()  # 改成调用方法

    # ✅ 新增：热重载配置
    def load_all(self):
        self.task_schemas: dict = _load("task_schemas.yaml")
        self.robot_fleet: dict = _load("robot_fleet.yaml")
        self.assets: dict = _load("assets.yaml")
        self.constraints: list = _load("constraints.yaml")["constraints"]
        self.environment: dict = _load("environment.yaml")

        self.ROV2type = self.get_ROV2type()
        self.env_info = EnvironmentInfo()
        self.state_info = RobotStateInfo()

    # ──────────────────────────────────────────────────────────────────────────
    # 按任务状态选取相关知识片段
    # ──────────────────────────────────────────────────────────────────────────

    def get_supported_task(self) -> list:
        res = self.task_schemas.get("task_templates", [])
        return [res[t]["task_type_values"] for t in res]

    def get_ROV2type(self) -> dict:
        result = {}
        rov = self.robot_fleet.get("robot_fleet")
        for r in rov:
            result[r["full_name"]] = r["category_name"]
        return result

    def get_context_for_state(self, task_state: dict) -> str:
        """
        根据当前任务状态，返回最相关的专业知识文本（供注入 system prompt）。
        分段组装，只选取与当前阶段相关的内容。
        """
        task_type = task_state.get("task_type_key")  # e.g. "pipeline_inspection"
        equipment = task_state.get("equipment_name")
        equipment_type = task_state.get("equipment_type")
        coords = task_state.get("start_point") or task_state.get("oilfield_coordinates")
        depth = task_state.get("water_depth")

        sections = []

        # 1. ROV 类型总览（始终包含）
        sections.append(self._robot_category_overview())

        # 2. 与当前任务类型相关的 ROV 约束
        if task_type:
            sections.append(self._task_rov_constraint(task_type))

        # 3. 当前已选/候选 ROV 详情
        if equipment:
            rov_info = self._get_rov_info(equipment)
            if rov_info:
                sections.append(f"【当前选定设备详情】\n{rov_info}")
        elif equipment_type:
            sections.append(self._rovs_by_category(equipment_type))

        # 4. 管缆类型（管缆巡检任务）
        if task_type == "pipeline_inspection":
            sections.append(self._cable_types_overview())
            sections.append(self._payload_suggestions("pipeline_inspection"))

        # 5. 采油树插拔工具建议
        if task_type == "tree_valve_operation":
            sections.append(self._payload_suggestions("tree_valve_operation"))

        # 6. 支持船信息
        sections.append(self._vessels_overview())

        # 7. 海域环境（有坐标时匹配）
        if coords:
            env_info = self._get_environment(coords)
            if env_info:
                sections.append(f"【作业区域环境状态】\n{env_info}")
        
        # 8. 适用约束规则摘要
        sections.append(self._relevant_constraints(task_type, equipment_type, depth))

        return "\n\n".join(s for s in sections if s.strip())

    # ──────────────────────────────────────────────────────────────────────────
    # 内部片段构建方法
    # ──────────────────────────────────────────────────────────────────────────

    def _robot_category_overview(self) -> str:
        cats = self.robot_fleet["robot_categories"]
        lines = ["【ROV设备类型说明】"]
        for k, v in cats.items():
            lines.append(f"- {v['label']}（{k}）: {v['description']}")
        return "\n".join(lines)

    def _task_rov_constraint(self, task_type: str) -> str:
        schema = self.task_schemas["task_templates"].get(task_type, {})
        required_cat = schema.get("robot_category_required")
        if not required_cat:
            return ""
        cats = self.robot_fleet["robot_categories"]
        cat_label = cats.get(required_cat, {}).get("label", required_cat)
        return f"【任务设备约束】{schema.get('display_name', task_type)} 任务强制要求使用 {cat_label}，不可使用其他类型ROV。"

    def _rovs_by_category(self, category_key: str) -> str:
        cats = self.robot_fleet["robot_categories"]
        cat_label = cats.get(category_key, {}).get("label", category_key)
        rovs = [r for r in self.robot_fleet["robot_fleet"] if r["category"] == category_key]
        if not rovs:
            return f"【{cat_label}】当前无可用设备。"
        lines = [f"【{cat_label}设备列表】"]
        for r in rovs:
        #     avail = r["available_count"]
            lines.append(
                f"- {r['full_name']} | 最大水深:{r['max_depth_m']}m \n  {r['brief']}"
            )
        return "\n".join(lines)

    def _get_rov_info(self, model_or_alias: str) -> str | None:
        rov = self._find_rov(model_or_alias)
        if not rov:
            return None
        # avail = rov["available_count"]
        payloads = "、".join(rov.get("typical_payload", []))
        return (
            f"{rov['full_name']}\n"
            f"类型: {rov['category']} | 最大水深: {rov['max_depth_m']}m \n"
            f"常用载荷: {payloads}\n"
            f"简介: {rov['brief']}"
        )

    def _find_rov(self, name: str) -> dict | None:
        name_lower = name.lower().replace(" ", "")
        for r in self.robot_fleet["robot_fleet"]:
            targets = [r["model"].lower()] + [a.lower().replace(" ", "") for a in r.get("aliases", [])]
            if any(name_lower in t or t in name_lower for t in targets):
                return r
        return None

    def find_rov_by_description(self, description: str) -> list[dict]:
        """返回所有 ROV 信息，供 LLM 推理匹配（模糊描述时使用）"""
        return self.robot_fleet["robot_fleet"]

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
        label = "管缆巡检" if task_type == "pipeline_inspection" else "阀门插拔"
        return f"【{label}常用携带工具建议】\n{common}\n备注: {desc}"

    def _vessels_overview(self) -> str:
        lines = ["【可用支持船只列表】"]
        for v in self.assets["vessels"]:
            status = "✓ 可用" if v["available"] else "✗ 不可用"
            lines.append(f"- {v['full_name']}（{v['type']}）[{status}] — {v['description']}")
        return "\n".join(lines)

    def _get_environment(self, coords: dict) -> str | None:
        if not isinstance(coords, dict):
            return None
        lat = coords.get("lat")
        lon = coords.get("lon")
        if lat is None or lon is None:
            return None
        oil_fields = self.environment["oil_fields"]
        for oil_field in oil_fields:
            lat_ok = oil_field["lat_range"][0] <= lat <= oil_field["lat_range"][1]
            lon_ok = oil_field["lon_range"][0] <= lon <= oil_field["lon_range"][1]
            if lat_ok and lon_ok:
                return (
                    f"{oil_field['name']} \n"
                    f"海底底质: {oil_field['seabed_type']}\n"
                    f"备注: {oil_field['notes']}"
                )
        return None

    def _relevant_constraints(
        self, task_type: str | None, equipment_type: str | None, depth: float | None
    ) -> str:
        lines = ["【相关作业约束规则】"]
        for c in self.constraints:
            applies = c["applies_to"]
            if "all" not in applies and task_type and task_type not in applies:
                continue
            lines.append(f"[{c['id']}] {c['name']}: {c['violation_message'].strip()}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────────────────
    # 直接查询接口（供 validator 使用）
    # ──────────────────────────────────────────────────────────────────────────

    def get_rov(self, model_name: str) -> dict | None:
        return self._find_rov(model_name)

    def get_vessel(self, vessel_id: str) -> dict | None:
        vid_lower = vessel_id.lower().replace(" ", "")
        for v in self.assets["vessels"]:
            targets = [v["id"].lower()] + [a.lower().replace(" ", "") for a in v.get("aliases", [])]
            if any(vid_lower in t or t in vid_lower for t in targets):
                return v
        return None

    def get_task_schema(self, template_key: str) -> dict:
        """返回指定模板的完整配置（以 task_templates key 查找）"""
        return self.task_schemas["task_templates"].get(template_key, {})

    def get_task_type_map(self) -> dict[str, str]:
        """
        从 task_schemas.yaml 动态构建 {task_type_value: template_key} 反查字典。
        例如：{"管缆巡检": "pipeline_inspection",
               "采油树控制面板插入": "tree_valve_operation", ...}
        """
        mapping: dict[str, str] = {}
        for template_key, cfg in self.task_schemas["task_templates"].items():
            for value in cfg.get("task_type_values", []):
                mapping[value] = template_key
        return mapping

    def get_all_task_type_values(self) -> list[str]:
        """返回所有合法 task_type 值的平铺列表（供 LLM 提示和拒绝判断用）"""
        values: list[str] = []
        for cfg in self.task_schemas["task_templates"].values():
            values.extend(cfg.get("task_type_values", []))
        return values

    def get_environment_for_coords(self, coords: dict) -> dict | None:
        if not isinstance(coords, dict):
            return None
        lat = coords.get("lat")
        lon = coords.get("lon")
        if lat is None or lon is None:
            return None
        for area in self.environment["oil_fields"]:
            if area["lat_range"][0] <= lat <= area["lat_range"][1]:
                if area["lon_range"][0] <= lon <= area["lon_range"][1]:
                    return area
        return None

    def get_all_rovs(self) -> list[dict]:
        return self.robot_fleet["robot_fleet"]

    def get_constraints(self) -> list[dict]:
        return self.constraints

    def get_environment_info_dict(self, coords: dict) -> dict:
        """根据坐标返回动态环境信息（随机 + 未知）"""
        if not coords or not isinstance(coords, dict):
            return {
                "forbidden": None,
                "seabed_type": None,
                "obstacle_density": None,
                "acoustic_signal": None,
                "dvl_risk": None,
                "mothership_support": None
            }
        lat = coords.get("lat")
        lon = coords.get("lon")
        if lat is None or lon is None:
            return {
                "forbidden": None,
                "seabed_type": None,
                "obstacle_density": None,
                "acoustic_signal": None,
                "dvl_risk": None,
                "mothership_support": None
            }
        return self.env_info.get_all_info(lat, lon)

    def get_robot_state_dict(self, equipment_name: str) -> dict:
        """根据机器人名称返回实时状态信息（纯读取）"""
        if not equipment_name or not isinstance(equipment_name, str):
            return {
                "current_velocity": None,
                "turbidity": None,
                "battery_percent": None,
                "current_mode": None,
                "communication_status": None,
                "latitude": None,
                "longitude": None,
                "update_timestamp": None,
                "confidence": None
            }

        return self.state_info.get_all_info(equipment_name)