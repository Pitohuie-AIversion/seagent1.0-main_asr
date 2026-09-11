"""
src/spatial_validator.py — 空间坐标与地理环境安全校验器

职责：
1. 空间禁入区 / 禁航区判定 (forbidden_area)；
2. DVL 声学作业高风险海域判定 (dvl_high_risk)；
3. 海底底质与机器人作业能力兼容性校验 (seabed_compatibility)。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .knowledge_retriever import (
    KnowledgeBase,
    format_seabed_type,
)

if TYPE_CHECKING:
    from .validator import Violation


SPATIAL_CHECKS = {
    "forbidden_area",
    "dvl_high_risk",
    "seabed_compatibility",
}


class SpatialValidator:
    """空间坐标与地理环境安全校验器"""

    def __init__(self, kb: KnowledgeBase, parent_validator: Any = None):
        self.kb = kb
        self.parent_validator = parent_validator

    def check_rule(
        self,
        c: dict,
        check: str,
        task_state: dict,
        rov: dict | None,
        purpose: str = "publish",
    ) -> Violation | None:
        """执行单一空间与地理环境安全校验"""
        from .validator import Violation, _CHECK_FIELDS

        rel_fields = _CHECK_FIELDS.get(check, [])

        if check == "forbidden_area":
            for field_name in ["start_point", "end_point", "oilfield_coordinates", "cable_position"]:
                coords = task_state.get(field_name)
                if coords:
                    try:
                        env_info = self.kb.get_environment_info_dict(coords)
                    except Exception as e:
                        return Violation(
                            c["id"], c["name"], f"查询环境坐标数据失败: {e}", "hard",
                            rel_fields, check_type=check, observed_value=coords
                        )
                    if not isinstance(env_info, dict):
                        return Violation(
                            c["id"], c["name"], "环境坐标查询结果非字典格式", "hard",
                            rel_fields, check_type=check, observed_value=coords
                        )
                    if env_info.get("forbidden") is True:
                        return Violation(
                            c["id"], c["name"], c["violation_message"].strip(),
                            c["severity"], rel_fields, check_type=check
                        )

        elif check == "dvl_high_risk":
            for field_name in ["start_point", "oilfield_coordinates", "cable_position"]:
                coords = task_state.get(field_name)
                if coords:
                    try:
                        env_info = self.kb.get_environment_info_dict(coords)
                    except Exception as e:
                        return Violation(
                            c["id"], c["name"], f"查询环境坐标数据失败: {e}", "hard",
                            rel_fields, check_type=check, observed_value=coords
                        )
                    if not isinstance(env_info, dict):
                        return Violation(
                            c["id"], c["name"], "环境坐标查询结果非字典格式", "hard",
                            rel_fields, check_type=check, observed_value=coords
                        )
                    if env_info.get("dvl_risk") is True:
                        return Violation(
                            c["id"], c["name"], c["violation_message"].strip(),
                            c["severity"], rel_fields, check_type=check
                        )

        elif check == "seabed_compatibility" and rov:
            for field_name in ["start_point", "oilfield_coordinates"]:
                coords = task_state.get(field_name)
                if coords:
                    try:
                        env_info = self.kb.get_environment_info_dict(coords)
                    except Exception as e:
                        return Violation(
                            c["id"], c["name"], f"查询底质环境数据失败: {e}", "hard",
                            rel_fields, check_type=check, observed_value=coords
                        )
                    if not isinstance(env_info, dict):
                        return Violation(
                            c["id"], c["name"], "环境坐标查询结果非字典格式", "hard",
                            rel_fields, check_type=check, observed_value=coords
                        )
                    seabed = env_info.get("seabed_type")
                    if seabed and seabed != "unknown":
                        supported_raw = rov.get("supported_seabed")
                        if not supported_raw:
                            continue
                        supported = [supported_raw] if isinstance(supported_raw, str) else supported_raw
                        if seabed not in supported:
                            rov_name = rov.get("full_name", str(rov))
                            msg = c["violation_message"].replace("{current_rov}", rov_name)
                            msg = msg.replace("{current_seabed}", format_seabed_type(seabed))
                            return Violation(
                                c["id"], c["name"], msg.strip(), c["severity"],
                                rel_fields, check_type=check, observed_value=seabed
                            )

        return None
