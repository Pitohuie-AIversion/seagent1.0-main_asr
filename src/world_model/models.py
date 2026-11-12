"""Data models for Subsea 4D World Model and Spatial-Temporal Digital Twin."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SubseaAssetType(str, Enum):
    WELLHEAD = "wellhead"
    XMAS_TREE = "xmas_tree"
    MANIFOLD = "manifold"
    PIPELINE = "pipeline"
    RISER = "riser"
    BUOY = "buoy"
    TEMPLATE = "template"


class GeofenceSeverity(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class GeofenceZoneType(str, Enum):
    FORBIDDEN = "forbidden"
    DVL_BLACKOUT = "dvl_blackout"
    HIGH_CURRENT = "high_current"
    ANCHORING_HAZARD = "anchoring_hazard"
    ECOLOGICAL_RESERVE = "ecological_reserve"


@dataclass
class Point3D:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0  # z < 0 indicates depth below water surface in meters

    @property
    def depth_m(self) -> float:
        return abs(self.z)

    def distance_to(self, other: Point3D) -> float:
        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2 + (self.z - other.z) ** 2) ** 0.5


@dataclass
class GeodeticPoint:
    latitude: float
    longitude: float
    depth_m: float = 0.0


@dataclass
class SubseaAsset:
    asset_id: str
    asset_type: SubseaAssetType
    name: str
    oilfield_id: str
    point: Point3D
    geodetic: GeodeticPoint | None = None
    bounding_radius_m: float = 10.0
    state_version: int = 1
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "asset_type": self.asset_type.value,
            "name": self.name,
            "oilfield_id": self.oilfield_id,
            "point": {"x": self.point.x, "y": self.point.y, "z": self.point.z, "depth_m": self.point.depth_m},
            "geodetic": (
                {"lat": self.geodetic.latitude, "lon": self.geodetic.longitude, "depth_m": self.geodetic.depth_m}
                if self.geodetic
                else None
            ),
            "bounding_radius_m": self.bounding_radius_m,
            "state_version": self.state_version,
            "properties": self.properties,
        }


@dataclass
class GeofenceZone:
    zone_id: str
    name: str
    zone_type: GeofenceZoneType
    severity: GeofenceSeverity
    lat_range: tuple[float, float]
    lon_range: tuple[float, float]
    depth_range: tuple[float, float] = (0.0, 4000.0)
    description: str = ""

    def contains_geodetic(self, lat: float, lon: float, depth_m: float = 0.0) -> bool:
        lat_min, lat_max = min(self.lat_range), max(self.lat_range)
        lon_min, lon_max = min(self.lon_range), max(self.lon_range)
        depth_min, depth_max = min(self.depth_range), max(self.depth_range)
        return (lat_min <= lat <= lat_max) and (lon_min <= lon <= lon_max) and (depth_min <= depth_m <= depth_max)

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "name": self.name,
            "zone_type": self.zone_type.value,
            "severity": self.severity.value,
            "lat_range": list(self.lat_range),
            "lon_range": list(self.lon_range),
            "depth_range": list(self.depth_range),
            "description": self.description,
        }


@dataclass
class SpatialSafetyCheckResult:
    is_safe: bool
    violations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    closest_asset_distance_m: float | None = None
    closest_asset_id: str | None = None
    ocean_current: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_safe": self.is_safe,
            "violations": self.violations,
            "warnings": self.warnings,
            "closest_asset_distance_m": self.closest_asset_distance_m,
            "closest_asset_id": self.closest_asset_id,
            "ocean_current": self.ocean_current,
        }


@dataclass
class OceanCurrentData:
    speed_mps: float
    direction_deg: float = 0.0
    vertical_speed_mps: float = 0.0
    shear_per_100m: float = 0.0
    timestamp_epoch: int | None = None
    source: str = "empirical_scs_profile"

    def to_dict(self) -> dict[str, Any]:
        return {
            "speed_mps": round(self.speed_mps, 3),
            "direction_deg": round(self.direction_deg, 1),
            "vertical_speed_mps": round(self.vertical_speed_mps, 3),
            "shear_per_100m": round(self.shear_per_100m, 3),
            "source": self.source,
        }
