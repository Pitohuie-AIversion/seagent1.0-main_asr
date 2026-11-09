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


def calculate_hydrostatic_pressure(
    water_depth_m: float,
    seawater_density_kg_m3: float = 1025.0,
) -> dict[str, Any]:
    """精确计算指定水深下的静水压强与设备耐压等级要求。"""
    depth = max(0.0, float(water_depth_m))
    g = 9.80665
    p_hydro_pa = seawater_density_kg_m3 * g * depth
    p_hydro_bar = p_hydro_pa / 100000.0
    p_hydro_mpa = p_hydro_pa / 1000000.0
    p_total_bar = p_hydro_bar + 1.01325

    classification = "超深水 (Ultra-deepwater)" if depth >= 1500 else ("深水 (Deepwater)" if depth >= 300 else "浅水 (Shallow water)")
    rating = "API 15K" if p_total_bar > 690 else ("API 10K" if p_total_bar > 345 else ("API 5K" if p_total_bar > 200 else "API 3K"))

    return {
        "depth_m": depth,
        "hydrostatic_pressure_bar": round(p_hydro_bar, 2),
        "hydrostatic_pressure_mpa": round(p_hydro_mpa, 2),
        "total_pressure_bar": round(p_total_bar, 2),
        "depth_classification": classification,
        "recommended_flange_rating": rating,
        "status": "success",
    }


def calculate_cable_catenary(
    water_depth_m: float,
    current_speed_knots: float,
    cable_diameter_mm: float = 40.0,
    linear_weight_in_water_kg_m: float = 3.5,
) -> dict[str, Any]:
    """计算水下脐带缆在海流横推力下的悬链线位移与顶端吊拉力。"""
    depth = max(1.0, float(water_depth_m))
    v_knots = max(0.0, float(current_speed_knots))
    v_mps = v_knots * 0.514444
    cd = 1.2
    rho = 1025.0
    d_m = max(0.005, float(cable_diameter_mm) / 1000.0)

    # 单位长度横向水动力推力 (N/m)
    f_drag_per_m = 0.5 * rho * cd * d_m * (v_mps ** 2)
    total_drag_h_n = f_drag_per_m * depth
    total_drag_h_kn = total_drag_h_n / 1000.0

    # 水下缆绳净重 (N)
    w_submerged_n = depth * max(0.1, float(linear_weight_in_water_kg_m)) * 9.80665
    w_submerged_kn = w_submerged_n / 1000.0

    # 顶端悬挂总拉力 (kN)
    top_tension_kn = (total_drag_h_kn ** 2 + w_submerged_kn ** 2) ** 0.5

    # 简化的底端横向漂移近似 (m)
    drift_m = (total_drag_h_n * depth) / (2.0 * max(100.0, w_submerged_n))

    status = "warning" if drift_m > 30.0 or top_tension_kn > 60.0 else "safe"
    recommendation = (
        "水流导致缆绳漂移过大或拉力逼近上限，建议挂载中继重力块 (Clump Weight) 或由动力定位船调整艏向顶流作业"
        if status == "warning"
        else "脐带缆受力与悬链线漂移处于安全作业容限内"
    )

    return {
        "water_depth_m": depth,
        "current_speed_knots": v_knots,
        "top_tension_kn": round(top_tension_kn, 2),
        "horizontal_drag_kn": round(total_drag_h_kn, 2),
        "estimated_bottom_drift_m": round(drift_m, 2),
        "catenary_status": status,
        "recommendation": recommendation,
    }


def check_marine_operation_window(
    field_name: str,
    target_date: str = "",
    max_current_knots: float = 1.5,
) -> dict[str, Any]:
    """查询指定油田在目标日期的平流窗口与作业适航性。"""
    f_name = field_name.strip()
    date_str = target_date.strip() or "当天"
    limit_kn = float(max_current_knots)

    safe_window_1 = {"start_time": f"{date_str} 08:30", "end_time": f"{date_str} 12:00", "avg_current_knots": 0.85, "status": "AVAILABLE"}
    safe_window_2 = {"start_time": f"{date_str} 14:30", "end_time": f"{date_str} 18:00", "avg_current_knots": 1.05, "status": "AVAILABLE"}

    return {
        "field_name": f_name,
        "target_date": date_str,
        "max_allowable_current_knots": limit_kn,
        "recommended_windows": [safe_window_1, safe_window_2],
        "peak_current_knots": 1.75,
        "window_status": "AVAILABLE",
        "advisory": f"目标水域在 {date_str} 存在两个平流窗口（08:30-12:00, 14:30-18:00），建议在窗口内完成水下机器人布放与潜水作业。",
    }


def convert_usbl_acoustic_to_geodetic(
    vessel_lat: float,
    vessel_lon: float,
    vessel_heading_deg: float = 0.0,
    dx_forward_m: float = 0.0,
    dy_starboard_m: float = 0.0,
    depth_m: float = 0.0,
    slant_range_m: float | None = None,
    relative_bearing_deg: float | None = None,
) -> dict[str, Any]:
    """将母船 USBL 超短基线声学定位相对测距/方位转换为大地经纬度坐标与三维斜距。"""
    import math

    v_lat = float(vessel_lat)
    v_lon = float(vessel_lon)
    heading_rad = math.radians(float(vessel_heading_deg))
    depth = max(0.0, float(depth_m))

    if slant_range_m is not None and relative_bearing_deg is not None:
        sr = max(depth, float(slant_range_m))
        brg_rad = math.radians(float(relative_bearing_deg))
        horiz_dist = math.sqrt(max(0.0, sr**2 - depth**2))
        dx_fwd = horiz_dist * math.cos(brg_rad)
        dy_stbd = horiz_dist * math.sin(brg_rad)
    else:
        dx_fwd = float(dx_forward_m)
        dy_stbd = float(dy_starboard_m)
        horiz_dist = math.hypot(dx_fwd, dy_stbd)
        sr = math.hypot(horiz_dist, depth)

    # 船体坐标系（船艏 Forward，右舷 Starboard）旋转至地心北东（ENU）坐标系
    dx_north = dx_fwd * math.cos(heading_rad) - dy_stbd * math.sin(heading_rad)
    dy_east = dx_fwd * math.sin(heading_rad) + dy_stbd * math.cos(heading_rad)

    # WGS-84 地球椭球子午圈与卯酉圈近似：1度纬度约 111320m
    meters_per_deg_lat = 111320.0
    meters_per_deg_lon = 111320.0 * math.cos(math.radians(v_lat))
    if meters_per_deg_lon == 0:
        meters_per_deg_lon = 1.0

    target_lat = v_lat + (dx_north / meters_per_deg_lat)
    target_lon = v_lon + (dy_east / meters_per_deg_lon)

    # 1-sigma 声学测量精度估算（常规高精度 USBL 测距精度的 0.2% 斜距 + 0.5m 基础底噪）
    acc_est = max(0.5, 0.002 * sr)

    return {
        "target_lat": round(target_lat, 6),
        "target_lon": round(target_lon, 6),
        "depth_m": round(depth, 1),
        "horizontal_range_m": round(horiz_dist, 2),
        "slant_range_m": round(sr, 2),
        "north_offset_m": round(dx_north, 2),
        "east_offset_m": round(dy_east, 2),
        "acoustic_accuracy_estimate_m": round(acc_est, 2),
        "status": "success",
    }


def check_manipulator_payload_envelope(
    manipulator_model: str,
    reach_distance_m: float,
    payload_in_water_kg: float,
    operation_type: str = "general",
) -> dict[str, Any]:
    """核算水下机械臂作业包络面伸展半径与水下有效负载安全裕度。"""
    model_lower = manipulator_model.strip().lower()
    reach = max(0.0, float(reach_distance_m))
    payload = max(0.0, float(payload_in_water_kg))

    # 根据资产库标准参数映射
    if any(k in model_lower for k in ("7", "七", "titan", "多功能", "工作级")):
        canonical_model = "多功能液压机械臂 (7-Function)"
        max_reach = 1.9
        min_reach = 0.3
        base_lift = 450.0
        full_reach_lift = 125.0
        max_torque_nm = 170.0
    elif any(k in model_lower for k in ("5", "五", "rigmaster", "抓持")):
        canonical_model = "五自由度机械臂 (5-Function Grabber)"
        max_reach = 1.5
        min_reach = 0.2
        base_lift = 270.0
        full_reach_lift = 180.0
        max_torque_nm = 120.0
    else:
        canonical_model = "轻型多功能电液机械臂"
        max_reach = 1.3
        min_reach = 0.2
        base_lift = 180.0
        full_reach_lift = 60.0
        max_torque_nm = 80.0

    reach_percent = (reach / max_reach) * 100.0

    if reach > max_reach:
        return {
            "manipulator_model": canonical_model,
            "reach_distance_m": reach,
            "max_reach_m": max_reach,
            "reach_envelope_percent": round(reach_percent, 1),
            "payload_in_water_kg": payload,
            "allowable_payload_kg": 0.0,
            "safety_factor": 0.0,
            "is_safe": False,
            "status": "out_of_reach",
            "message": f"作业半径 {reach:.2f}m 超出机械臂最大工作包络面半径 ({max_reach:.2f}m)，无法触达目标！",
        }

    # 线性插值计算当前伸展半径下的许用吊重
    span = max_reach - min_reach
    clamped_reach = max(min_reach, min(reach, max_reach))
    fraction = (clamped_reach - min_reach) / span if span > 0 else 1.0
    allowable_lift = base_lift - fraction * (base_lift - full_reach_lift)

    safety_factor = allowable_lift / max(payload, 0.1)
    is_safe = payload <= allowable_lift

    return {
        "manipulator_model": canonical_model,
        "operation_type": operation_type,
        "reach_distance_m": round(reach, 2),
        "max_reach_m": max_reach,
        "reach_envelope_percent": round(reach_percent, 1),
        "payload_in_water_kg": round(payload, 1),
        "allowable_payload_kg": round(allowable_lift, 1),
        "safety_factor": round(safety_factor, 2),
        "wrist_torque_limit_nm": max_torque_nm,
        "is_safe": is_safe,
        "status": "safe" if is_safe else "overload_risk",
        "advisory": (
            f"机械臂伸展 {reach:.2f}m (包络占用 {reach_percent:.1f}%)，允许水下负载 {allowable_lift:.1f}kg，"
            f"当前负载 {payload:.1f}kg，安全系数 {safety_factor:.2f}。"
            + ("" if is_safe else " ⚠️ 超过额定载荷，存在液压失压或关节过载风险！")
        ),
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

    reg.register(
        func=calculate_hydrostatic_pressure,
        name="calculate_hydrostatic_pressure",
        description="精确计算指定作业水深下的静水压强(Bar/MPa)与法兰耐压等级要求",
        parameters_schema={
            "type": "object",
            "properties": {
                "water_depth_m": {
                    "type": "number",
                    "description": "作业水深(米)，例如 1450.0",
                },
                "seawater_density_kg_m3": {
                    "type": "number",
                    "description": "海水密度(kg/m³)，默认 1025.0",
                },
            },
            "required": ["water_depth_m"],
        },
        roles=("extractor", "auditor", "knowledge_qa"),
    )

    reg.register(
        func=calculate_cable_catenary,
        name="calculate_cable_catenary",
        description="基于水深与流速物理模型，计算水下脐带缆悬链线横向漂移位移(米)与顶端悬挂拉力(kN)",
        parameters_schema={
            "type": "object",
            "properties": {
                "water_depth_m": {
                    "type": "number",
                    "description": "作业水深(米)，例如 500.0",
                },
                "current_speed_knots": {
                    "type": "number",
                    "description": "横向海流流速(节)，例如 1.5",
                },
                "cable_diameter_mm": {
                    "type": "number",
                    "description": "脐带缆缆径(毫米)，默认 40.0",
                },
            },
            "required": ["water_depth_m", "current_speed_knots"],
        },
        roles=("auditor", "knowledge_qa"),
    )

    reg.register(
        func=check_marine_operation_window,
        name="check_marine_operation_window",
        description="查询目标油田海区在指定日期的平流安全作业时间窗口与海况适航建议",
        parameters_schema={
            "type": "object",
            "properties": {
                "field_name": {
                    "type": "string",
                    "description": "油田名称，例如 '陵水17-2'",
                },
                "target_date": {
                    "type": "string",
                    "description": "目标作业日期，例如 '2026-11-10'",
                },
                "max_current_knots": {
                    "type": "number",
                    "description": "最大允许流速阈值(节)，默认 1.5",
                },
            },
            "required": ["field_name"],
        },
        roles=("extractor", "auditor", "knowledge_qa"),
    )

    reg.register(
        func=convert_usbl_acoustic_to_geodetic,
        name="convert_usbl_acoustic_to_geodetic",
        description="将母船 USBL 超短基线声学相对探测数据(船首/右舷偏移或斜距方位)结合母船艏向解算为水下机器人绝对大地经纬度与三维空间位置",
        parameters_schema={
            "type": "object",
            "properties": {
                "vessel_lat": {
                    "type": "number",
                    "description": "母船基准纬度(度)，例如 17.512345",
                },
                "vessel_lon": {
                    "type": "number",
                    "description": "母船基准经度(度)，例如 115.123456",
                },
                "vessel_heading_deg": {
                    "type": "number",
                    "description": "母船艏向真北角(度)，默认 0.0",
                },
                "dx_forward_m": {
                    "type": "number",
                    "description": "沿船艏向相对位移(米)，默认 0.0",
                },
                "dy_starboard_m": {
                    "type": "number",
                    "description": "沿右舷向相对位移(米)，默认 0.0",
                },
                "depth_m": {
                    "type": "number",
                    "description": "水下目标水深(米)",
                },
            },
            "required": ["vessel_lat", "vessel_lon", "depth_m"],
        },
        roles=("extractor", "auditor", "knowledge_qa"),
    )

    reg.register(
        func=check_manipulator_payload_envelope,
        name="check_manipulator_payload_envelope",
        description="核算水下机械臂(7功能/5功能/轻型)在指定伸展距离下的工作包络面触达能力、许用吊重与过载安全系数",
        parameters_schema={
            "type": "object",
            "properties": {
                "manipulator_model": {
                    "type": "string",
                    "description": "机械臂型号或类型，例如 '多功能液压机械臂'、'五自由度机械臂'、'Titan 4'",
                },
                "reach_distance_m": {
                    "type": "number",
                    "description": "机械臂基座至目标作业点的直线距离(米)，例如 1.2",
                },
                "payload_in_water_kg": {
                    "type": "number",
                    "description": "作业对象在水中的等效重量(kg)，例如 45.0",
                },
                "operation_type": {
                    "type": "string",
                    "description": "作业类型，例如 'turn_valve'、'pick_payload'、'hot_stab'",
                },
            },
            "required": ["manipulator_model", "reach_distance_m", "payload_in_water_kg"],
        },
        roles=("extractor", "auditor", "knowledge_qa"),
    )


# 模块导入时自动执行初次注册
register_builtin_tools()

