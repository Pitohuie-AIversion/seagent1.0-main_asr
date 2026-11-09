"""src/tools/builtin_tools.py — SEAgent 核心内置领域只读工具定义。

提供标准的只读探测函数，注册至全局 ToolRegistry，供 Extractor、Auditor 等 Agent 按需调取。
"""

from __future__ import annotations

import logging
from typing import Any
from src.tools.registry import ToolRegistry, tool

logger = logging.getLogger(__name__)


def query_robot_specs(robot_name: str) -> dict[str, Any]:
    """查询指定机器人的物理参数与能力。"""
    from src.knowledge_retriever import KnowledgeBase

    kb = KnowledgeBase()
    name_clean = robot_name.strip().lower()

    matches = []

    # 1. 检索 fleet_units
    units = kb.robot_fleet.get("fleet_units", [])
    matched_variant_ids = set()
    for u in units:
        unit_id = str(u.get("unit_id", "")).lower()
        disp_name = str(u.get("display_name", "")).lower()
        aliases = [str(a).lower() for a in u.get("aliases", [])]
        if (
            name_clean in unit_id
            or name_clean in disp_name
            or any(name_clean in a for a in aliases)
            or any(a in name_clean for a in aliases)
        ):
            v_id = u.get("variant_id")
            if v_id:
                matched_variant_ids.add(v_id)

    # 2. 检索 model_variants
    variants = kb.robot_fleet.get("model_variants", {})
    for v_id, v_info in variants.items():
        v_full = str(v_info.get("full_name", "")).lower()
        aliases = [str(a).lower() for a in v_info.get("aliases", [])]
        is_match = (
            v_id in matched_variant_ids
            or name_clean in v_id.lower()
            or name_clean in v_full
            or any(name_clean in a for a in aliases)
            or any(a in name_clean for a in aliases)
        )
        if is_match:
            hard = v_info.get("hard_params", {})
            matches.append({
                "variant_id": v_id,
                "name": v_info.get("full_name", v_id),
                "family_id": v_info.get("family_id"),
                "max_depth_m": hard.get("max_depth_m"),
                "power_hp": hard.get("power_hp"),
                "supported_payloads": hard.get("supported_payloads"),
                "max_operating_speed_kn": hard.get("max_operating_speed_kn"),
            })

    if matches:
        return {"found": True, "count": len(matches), "robots": matches[:3]}
    return {"found": False, "message": f"未在资产库中找到与 '{robot_name}' 匹配的水下设备"}


def query_oilfield_position(field_name: str) -> dict[str, Any]:
    """查询油田位置与水深基准。"""
    from src.knowledge_retriever import KnowledgeBase

    kb = KnowledgeBase()
    name_clean = field_name.strip().lower()
    oil_fields = kb.environment.get("oil_fields", [])
    if isinstance(oil_fields, dict):
        oil_fields = list(oil_fields.values())

    for of in oil_fields:
        of_id = str(of.get("id", "")).lower()
        of_name = str(of.get("name", "")).lower()
        aliases = [str(a).lower() for a in of.get("aliases", [])]
        if (
            name_clean in of_id
            or name_clean in of_name
            or any(name_clean in a for a in aliases)
            or any(a in name_clean for a in aliases)
        ):
            lat_r = of.get("lat_range", [20.0, 20.0])
            lon_r = of.get("lon_range", [115.0, 115.0])
            return {
                "found": True,
                "field_name": of.get("name"),
                "field_id": of.get("id"),
                "center_lat": round((lat_r[0] + lat_r[1]) / 2, 4),
                "center_lon": round((lon_r[0] + lon_r[1]) / 2, 4),
                "default_depth_m": of.get("water_depth", 1500),
                "seabed_type": of.get("seabed_type", "soft"),
            }
    return {"found": False, "message": f"未收录油田/设施 '{field_name}' 的地理信息"}


def query_environmental_telemetry(robot_unit_id: str = "") -> dict[str, Any]:
    """查询实时遥测海况与健康状态。"""
    from src.state_info import RobotStateInfo

    state_info = RobotStateInfo()
    if robot_unit_id:
        st = state_info.get_status(robot_unit_id)
        if st:
            return {
                "found": True,
                "unit_id": robot_unit_id,
                "current_speed_knots": st.get("current_speed"),
                "water_depth": st.get("depth"),
                "status": st.get("status"),
                "updated_at": st.get("updated_at"),
            }
    # 返回全局环境基线
    return {
        "found": True,
        "general_sea_state": "moderate",
        "surface_current_knots": 1.1,
        "bottom_current_knots": 0.8,
        "turbidity": "normal",
        "status": "safe",
    }


def register_builtin_tools(registry: ToolRegistry | None = None) -> None:
    """显式幂等注册内置工具。"""
    reg = registry or ToolRegistry.get_instance()

    reg.register(
        func=query_robot_specs,
        name="query_robot_specs",
        description="查询水下机器人的规格参数，包括所属类别、最大作业水深、推力功率(HP)与支持的载荷/工具列表",
        parameters_schema={
            "type": "object",
            "properties": {
                "robot_name": {
                    "type": "string",
                    "description": "机器人系列、型号或俗称，例如 'OBSROV-75'、'工作级ROV'、'金牛座'、'海狮号'",
                }
            },
            "required": ["robot_name"],
        },
        roles=("extractor", "auditor", "knowledge_qa"),
    )

    reg.register(
        func=query_oilfield_position,
        name="query_oilfield_position",
        description="查询目标油田、井口或油气设施的标准地理坐标(经纬度)与默认作业水深",
        parameters_schema={
            "type": "object",
            "properties": {
                "field_name": {
                    "type": "string",
                    "description": "油田名称或设施编号，例如 '陵水17-2'、'流花11-1'、'17-2'",
                }
            },
            "required": ["field_name"],
        },
        roles=("extractor", "auditor"),
    )

    reg.register(
        func=query_environmental_telemetry,
        name="query_environmental_telemetry",
        description="查询当前作业水域的实时海况遥测数据，包括水流速度(knots)、水温、海况等级与设备传感器在线状态",
        parameters_schema={
            "type": "object",
            "properties": {
                "robot_unit_id": {
                    "type": "string",
                    "description": "可选单机编号(如 'OBSROV-75-001')，若不传则返回通用海况",
                }
            },
            "required": [],
        },
        roles=("extractor", "auditor"),
    )


# 模块导入时自动执行初次注册
register_builtin_tools()
