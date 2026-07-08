import yaml
from pathlib import Path
from typing import Dict, Any


class EnvironmentInfo:
    def __init__(self):
        config_path = Path(__file__).parent.parent / "config" / "environment.yaml"
        # 读取YAML配置文件
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        # 提取三大区域数据
        self.oil_fields = self.config.get("oil_fields", [])
        self.forbidden_areas = self.config.get("forbidden_areas", [])
        self.dvl_failure_areas = self.config.get("dvl_bottom_lock_failure_areas", [])

    def _is_point_in_area(self, lat: float, lon: float, lat_range: list, lon_range: list) -> bool:
        """内部工具函数：判断坐标是否在指定经纬度范围内"""
        lat_min, lat_max = min(lat_range), max(lat_range)
        lon_min, lon_max = min(lon_range), max(lon_range)
        return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max

    # ===================== 几何层 =====================
    def get_geometry_forbidden(self, lat: float, lon: float) -> bool:
        """判断坐标是否在禁入区：True=禁入，False=可作业"""
        for area in self.forbidden_areas:
            if self._is_point_in_area(lat, lon, area["lat_range"], area["lon_range"]):
                return True
        return False

    def get_physical_seabed_type(self, lat: float, lon: float) -> str:
        """获取海底底质类型：hard / soft / mixed / unknown"""
        for field in self.oil_fields:
            if self._is_point_in_area(lat, lon, field["lat_range"], field["lon_range"]):
                return field["seabed_type"]
        return "unknown"  # 不在任何油田区域

    def get_semantic_dvl_risk(self, lat: float, lon: float) -> bool:
        """
        严格按你配置的 dvl_bottom_lock_failure_areas 判断
        True = 在DVL风险区内
        False = 不在DVL风险区内
        """
        for area in self.dvl_failure_areas:
            if self._is_point_in_area(lat, lon, area["lat_range"], area["lon_range"]):
                return True
        return False

    # ===================== 统一全量查询接口 =====================
    def get_all_info(self, lat: float, lon: float) -> Dict[str, Any]:
        return {
            "forbidden": self.get_geometry_forbidden(lat, lon),
            "seabed_type": self.get_physical_seabed_type(lat, lon),
            "dvl_risk": self.get_semantic_dvl_risk(lat, lon),
        }