"""
src/knowledge/query/environment_retriever.py — 海域环境、油气田、禁入区与遥测可用性检索器
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..selection.models import _norm

if TYPE_CHECKING:
    from ...knowledge_retriever import KnowledgeBase


class EnvironmentRetriever:
    """海域环境、油气田地理信息与机器人遥测状态检索器。"""

    def __init__(self, kb: "KnowledgeBase"):
        self.kb = kb

    @property
    def environment(self) -> dict:
        return self.kb.environment

    @property
    def env_info(self) -> Any:
        return self.kb.env_info

    @property
    def state_info(self) -> Any:
        return self.kb.state_info

    def get_environment_for_coords(self, coords: dict) -> dict | None:
        """根据经纬度匹配所在油气田区域。"""
        if not isinstance(coords, dict):
            return None
        lat = coords.get("lat")
        lon = coords.get("lon")
        if lat is None or lon is None:
            return None
        for area in self.environment.get("oil_fields", []):
            lat_range = area.get("lat_range")
            lon_range = area.get("lon_range")
            if lat_range and lon_range:
                if lat_range[0] <= lat <= lat_range[1] and lon_range[0] <= lon <= lon_range[1]:
                    return area
        return None

    def get_environment_info_dict(self, coords: dict) -> dict:
        """根据坐标返回动态环境信息（随机 + 未知）。"""
        empty_info = {
            "forbidden": None,
            "seabed_type": None,
            "obstacle_density": None,
            "acoustic_signal": None,
            "dvl_risk": None,
            "mothership_support": None,
        }
        if not isinstance(coords, dict):
            return empty_info
        lat = coords.get("lat")
        lon = coords.get("lon")
        if lat is None or lon is None:
            return empty_info
        return self.env_info.get_all_info(lat, lon)

    def get_robot_state_dict(self, equipment_selector: str) -> dict:
        """获取指定设备实体的当前遥测状态。"""
        empty_state = {
            "version": None,
            "water_current_velocity": None,
            "water_turbidity": None,
            "battery_percent": None,
            "current_mode": None,
            "communication_status": None,
            "latitude": None,
            "longitude": None,
            "update_timestamp": None,
            "confidence": None,
            "obstacle_density": None,
            "mothership_support": None,
            "overall_status": None,
            "survival_status": None,
            "thruster_status": None,
            "depth_keeping_status": None,
            "sonar_status": None,
            "vision_status": None,
            "arm_status": None,
            "end_effector_status": None,
            "acoustic_comms_status": None,
            "tether_connection_status": None,
        }
        if not equipment_selector or not isinstance(equipment_selector, str):
            return empty_state

        state = self.state_info.get_all_info(equipment_selector)
        if isinstance(state, dict):
            return state
        return empty_state

    def get_unit_state_snapshot(self, unit_id: str) -> dict:
        """获取单机设备的快照状态。"""
        return self.state_info.get_unit_state_snapshot(unit_id)

    def check_runtime_availability(self, unit_id: str, *, max_age_seconds: int = 600) -> dict:
        """检查单机设备运行时遥测可用性。"""
        return self.state_info.check_runtime_availability(unit_id, max_age_seconds=max_age_seconds)

    def get_environment_alias_index(self) -> dict[str, list[str]]:
        """构建油气田、禁入保护区与DVL风险区的多维度别名索引。"""
        index: dict[str, set[str]] = {}
        display_aliases: dict[str, str] = {}

        def add(alias: Any, target: str) -> None:
            if not isinstance(alias, str):
                return
            display = alias.strip()
            normalized = _norm(display)
            if not normalized:
                return
            canonical = display_aliases.setdefault(normalized, display)
            index.setdefault(canonical, set()).add(target)

        for field in self.environment.get("oil_fields", []):
            field_id = field.get("id")
            if not field_id:
                continue
            target = f"oil_field:{field_id}"
            add(field_id, target)
            add(field.get("name"), target)
            for alias in field.get("aliases", []):
                add(alias, target)

        for area in self.environment.get("forbidden_areas", []):
            area_id = area.get("id")
            if not area_id:
                continue
            target = f"forbidden_area:{area_id}"
            add(area_id, target)
            add(area.get("name"), target)
            for alias in area.get("aliases", []):
                add(alias, target)

        for dvl in self.environment.get("dvl_bottom_lock_failure_areas", []):
            dvl_id = dvl.get("id")
            if not dvl_id:
                continue
            target = f"dvl_area:{dvl_id}"
            add(dvl_id, target)
            add(dvl.get("name"), target)
            for alias in dvl.get("aliases", []):
                add(alias, target)

        return {alias: sorted(targets) for alias, targets in index.items()}

    def find_environment_entity_targets(self, user_message: str) -> tuple[str | None, list[str]]:
        """从用户文本中匹配最佳环境实体目标。"""
        message_norm = _norm(user_message)
        if not message_norm:
            return None, []
        matches = [
            (alias, targets)
            for alias, targets in self.get_environment_alias_index().items()
            if _norm(alias) and _norm(alias) in message_norm
        ]
        if not matches:
            return None, []
        matches.sort(key=lambda item: len(_norm(item[0])), reverse=True)
        return matches[0]

    def execute_environment_query(
        self,
        user_message: str,
        context: dict,
        response: dict,
    ) -> dict:
        """执行作业海域、油气田、禁入区与DVL风险区的结构化知识检索。"""
        matched_alias, targets = self.find_environment_entity_targets(user_message)
        if not targets:
            context_of = context.get("oilfield_name")
            if context_of and any(pronoun in user_message for pronoun in ("它", "这个", "那个", "该油田", "该海域", "此海域", "此区域")):
                matched_alias, targets = self.find_environment_entity_targets(str(context_of))

        oil_fields = self.environment.get("oil_fields", [])
        forbidden_areas = self.environment.get("forbidden_areas", [])
        dvl_areas = self.environment.get("dvl_bottom_lock_failure_areas", [])

        results = []
        if targets:
            for target in targets:
                if target.startswith("oil_field:"):
                    field_id = target.split(":", 1)[1]
                    for of in oil_fields:
                        if of.get("id") == field_id:
                            results.append({
                                "category": "oil_field_details",
                                "oil_field": of,
                            })
                elif target.startswith("forbidden_area:"):
                    area_id = target.split(":", 1)[1]
                    for fa in forbidden_areas:
                        if fa.get("id") == area_id:
                            results.append({
                                "category": "forbidden_area_details",
                                "forbidden_area": fa,
                            })
                elif target.startswith("dvl_area:"):
                    dvl_id = target.split(":", 1)[1]
                    for da in dvl_areas:
                        if da.get("id") == dvl_id:
                            results.append({
                                "category": "dvl_area_details",
                                "dvl_area": da,
                            })

        if not results:
            results = [
                {
                    "category": "oil_fields_summary",
                    "oil_fields": oil_fields,
                },
                {
                    "category": "forbidden_areas_summary",
                    "forbidden_areas": forbidden_areas,
                },
                {
                    "category": "dvl_bottom_lock_failure_areas_summary",
                    "dvl_areas": dvl_areas,
                },
            ]

        response["results"] = results
        response["found"] = True
        if matched_alias:
            response["matched_alias"] = matched_alias
            response["matched_targets"] = targets
        return response
