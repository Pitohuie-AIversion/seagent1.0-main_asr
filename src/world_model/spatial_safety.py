"""Spatial safety analysis engine for geofence and infrastructure proximity checks."""
from __future__ import annotations

import logging
from typing import Sequence

from .models import (
    GeofenceSeverity,
    GeofenceZone,
    Point3D,
    SpatialSafetyCheckResult,
    SubseaAsset,
)

logger = logging.getLogger(__name__)


class SpatialSafetyEngine:
    """Evaluates spatial-temporal safety for subsea robot operations."""

    def __init__(
        self,
        geofences: Sequence[GeofenceZone] | None = None,
        assets: Sequence[SubseaAsset] | None = None,
        obstacles: Sequence[Any] | None = None,
    ):
        self._geofences: list[GeofenceZone] = list(geofences or [])
        self._assets: list[SubseaAsset] = list(assets or [])
        self._obstacles: list[Any] = list(obstacles or [])

    def set_geofences(self, geofences: Sequence[GeofenceZone]) -> None:
        self._geofences = list(geofences)

    def set_assets(self, assets: Sequence[SubseaAsset]) -> None:
        self._assets = list(assets)

    def set_obstacles(self, obstacles: Sequence[Any]) -> None:
        self._obstacles = list(obstacles)

    def evaluate_geodetic_point(
        self,
        lat: float,
        lon: float,
        depth_m: float = 0.0,
        min_clearance_m: float = 5.0,
        current_limit_mps: float | None = None,
        current_data: Any | None = None,
    ) -> SpatialSafetyCheckResult:
        """Check geodetic coordinates against spatial geofence boundaries, asset locations, and environmental currents."""
        violations: list[dict] = []
        warnings: list[dict] = []

        # 1. Check geofence intersections
        for zone in self._geofences:
            if zone.contains_geodetic(lat, lon, depth_m):
                detail = {
                    "zone_id": zone.zone_id,
                    "name": zone.name,
                    "zone_type": zone.zone_type.value,
                    "severity": zone.severity.value,
                    "description": zone.description,
                }
                if zone.severity == GeofenceSeverity.HARD:
                    violations.append(detail)
                else:
                    warnings.append(detail)

        # 2. Check asset proximity if asset has geodetic coordinates
        closest_distance: float | None = None
        closest_id: str | None = None

        for asset in self._assets:
            if asset.geodetic:
                # Approximate distance in meters using Equirectangular approximation
                d_lat = (lat - asset.geodetic.latitude) * 111320.0
                d_lon = (lon - asset.geodetic.longitude) * 111320.0 * 0.939  # cos(20 deg) ~ 0.939 for South China Sea
                d_depth = depth_m - asset.geodetic.depth_m
                dist_m = (d_lat**2 + d_lon**2 + d_depth**2) ** 0.5

                if closest_distance is None or dist_m < closest_distance:
                    closest_distance = round(dist_m, 2)
                    closest_id = asset.asset_id

                clearance = dist_m - asset.bounding_radius_m
                if clearance <= 0:
                    violations.append({
                        "type": "asset_collision",
                        "asset_id": asset.asset_id,
                        "asset_name": asset.name,
                        "distance_m": round(dist_m, 2),
                        "bounding_radius_m": asset.bounding_radius_m,
                        "message": f"坐标侵入水下设施 '{asset.name}' 保护包络内 (间距 {dist_m:.1f}m <= 包络 {asset.bounding_radius_m}m)",
                    })
                elif clearance < min_clearance_m:
                    warnings.append({
                        "type": "asset_proximity_warning",
                        "asset_id": asset.asset_id,
                        "asset_name": asset.name,
                        "distance_m": round(dist_m, 2),
                        "message": f"距离水下设施 '{asset.name}' 较近 ({dist_m:.1f}m)，低于建议安全间隙 ({min_clearance_m}m)",
                    })

        # 3. Check ocean current velocity vs robot envelope
        if current_data is not None and current_limit_mps is not None:
            spd = getattr(current_data, "speed_mps", 0.0)
            limit = float(current_limit_mps)
            if spd > limit * 1.25:
                violations.append({
                    "type": "high_current_hazard",
                    "severity": "hard",
                    "speed_mps": round(spd, 2),
                    "limit_mps": round(limit, 2),
                    "message": f"流速 {spd:.2f} m/s 严重超过机器人抗流上限 {limit:.2f} m/s (超标 25% 以上)，存在动力推进失控及脐带缆缠绕风险",
                })
            elif spd > limit:
                warnings.append({
                    "type": "high_current_warning",
                    "severity": "soft",
                    "speed_mps": round(spd, 2),
                    "limit_mps": round(limit, 2),
                    "message": f"流速 {spd:.2f} m/s 略微超过建议流速上限 {limit:.2f} m/s，建议微调下潜时机或启用高推力模式",
                })

        # 4. Check dynamic moving obstacles / support vessels
        for obs in self._obstacles:
            if getattr(obs, "geodetic", None):
                obs_geo = obs.geodetic
                d_lat = (lat - obs_geo.latitude) * 111320.0
                d_lon = (lon - obs_geo.longitude) * 111320.0 * 0.939
                d_depth = depth_m - obs_geo.depth_m
                dist_m = (d_lat**2 + d_lon**2 + d_depth**2) ** 0.5
                radius = getattr(obs, "safety_radius_m", 30.0)

                if dist_m < radius * 0.5:
                    violations.append({
                        "type": "dynamic_obstacle_collision_hazard",
                        "severity": "hard",
                        "obstacle_id": obs.obstacle_id,
                        "obstacle_name": obs.name,
                        "distance_m": round(dist_m, 2),
                        "safety_radius_m": radius,
                        "message": f"距离动态船舶/障碍物 '{obs.name}' 极近 ({dist_m:.1f}m < 紧急规避距离 {radius * 0.5:.1f}m)，触发紧急防碰撞硬约束阻断",
                    })
                elif dist_m < radius:
                    warnings.append({
                        "type": "dynamic_obstacle_proximity_warning",
                        "severity": "soft",
                        "obstacle_id": obs.obstacle_id,
                        "obstacle_name": obs.name,
                        "distance_m": round(dist_m, 2),
                        "safety_radius_m": radius,
                        "message": f"距离动态船舶/障碍物 '{obs.name}' 较近 ({dist_m:.1f}m)，位于外圈安全警戒区内 ({radius:.1f}m)",
                    })

        is_safe = len(violations) == 0
        current_dict = current_data.to_dict() if hasattr(current_data, "to_dict") else None
        return SpatialSafetyCheckResult(
            is_safe=is_safe,
            violations=violations,
            warnings=warnings,
            closest_asset_distance_m=closest_distance,
            closest_asset_id=closest_id,
            ocean_current=current_dict,
        )

    def evaluate_cartesian_point(
        self,
        point: Point3D,
        min_clearance_m: float = 5.0,
    ) -> SpatialSafetyCheckResult:
        """Check 3D Cartesian coordinates against asset bounding envelopes."""
        violations: list[dict] = []
        warnings: list[dict] = []
        closest_distance: float | None = None
        closest_id: str | None = None

        for asset in self._assets:
            dist_m = point.distance_to(asset.point)
            if closest_distance is None or dist_m < closest_distance:
                closest_distance = round(dist_m, 2)
                closest_id = asset.asset_id

            clearance = dist_m - asset.bounding_radius_m
            if clearance <= 0:
                violations.append({
                    "type": "asset_collision",
                    "asset_id": asset.asset_id,
                    "asset_name": asset.name,
                    "distance_m": round(dist_m, 2),
                    "bounding_radius_m": asset.bounding_radius_m,
                    "message": f"轨迹点侵入设施 '{asset.name}' 实体包络内 (间距 {dist_m:.1f}m)",
                })
            elif clearance < min_clearance_m:
                warnings.append({
                    "type": "asset_proximity_warning",
                    "asset_id": asset.asset_id,
                    "asset_name": asset.name,
                    "distance_m": round(dist_m, 2),
                    "message": f"接近设施 '{asset.name}' (间距 {dist_m:.1f}m，建议安全间距 {min_clearance_m}m)",
                })

        return SpatialSafetyCheckResult(
            is_safe=(len(violations) == 0),
            violations=violations,
            warnings=warnings,
            closest_asset_distance_m=closest_distance,
            closest_asset_id=closest_id,
        )
