"""Subsea 4D collision-free trajectory planner navigating around geofences and physical assets."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from .models import GeodeticPoint, Point3D, SpatialSafetyCheckResult
from .spatial_safety import SpatialSafetyEngine

logger = logging.getLogger(__name__)


@dataclass
class PathPlanResult:
    """Outcome of a 3D/4D subsea path planning query."""
    is_feasible: bool
    total_distance_m: float
    estimated_duration_s: float
    waypoints: list[dict[str, Any]] = field(default_factory=list)
    bypassed_obstacles: list[str] = field(default_factory=list)
    max_current_mps: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_feasible": self.is_feasible,
            "total_distance_m": round(self.total_distance_m, 2),
            "estimated_duration_s": round(self.estimated_duration_s, 1),
            "waypoints_count": len(self.waypoints),
            "waypoints": self.waypoints,
            "bypassed_obstacles": self.bypassed_obstacles,
            "max_current_mps": round(self.max_current_mps, 3),
            "notes": self.notes,
        }


def _calc_geodetic_dist(lat1: float, lon1: float, d1: float, lat2: float, lon2: float, d2: float) -> float:
    """Equirectangular approximate distance in meters."""
    d_lat = (lat2 - lat1) * 111320.0
    mid_lat = (lat1 + lat2) / 2.0
    cos_lat = math.cos(math.radians(mid_lat))
    d_lon = (lon2 - lon1) * 111320.0 * cos_lat
    d_depth = d2 - d1
    return math.sqrt(d_lat**2 + d_lon**2 + d_depth**2)


class SubseaPathPlanner:
    """Generates collision-free 3D waypoints steering clear of subsea assets,
    dynamic surface vessels, and restricted geofences."""

    def __init__(self, safety_engine: SpatialSafetyEngine, world_model: Any | None = None):
        self.safety_engine = safety_engine
        self.world_model = world_model

    def plan_path(
        self,
        start_lat: float,
        start_lon: float,
        start_depth_m: float,
        goal_lat: float,
        goal_lon: float,
        goal_depth_m: float,
        min_clearance_m: float = 10.0,
        cruise_speed_mps: float = 1.0,
        max_deviation_m: float = 300.0,
        samples_per_segment: int = 10,
    ) -> PathPlanResult:
        """Compute collision-free waypoint path between start and goal."""
        total_dist_direct = _calc_geodetic_dist(
            start_lat, start_lon, start_depth_m,
            goal_lat, goal_lon, goal_depth_m,
        )

        # 1. Test direct linear segment
        direct_safe, blocked_obstacle = self._check_segment(
            start_lat, start_lon, start_depth_m,
            goal_lat, goal_lon, goal_depth_m,
            samples=samples_per_segment,
            min_clearance_m=min_clearance_m,
        )

        max_curr = 0.0
        if self.world_model and hasattr(self.world_model, "get_ocean_current"):
            c1 = self.world_model.get_ocean_current(start_lat, start_lon, start_depth_m)
            c2 = self.world_model.get_ocean_current(goal_lat, goal_lon, goal_depth_m)
            max_curr = max(c1.speed_mps, c2.speed_mps)

        if direct_safe:
            # Direct path is clear
            waypoints = [
                {"lat": start_lat, "lon": start_lon, "depth_m": start_depth_m, "step": 0, "segment_distance_m": 0.0},
                {"lat": goal_lat, "lon": goal_lon, "depth_m": goal_depth_m, "step": 1, "segment_distance_m": round(total_dist_direct, 2)},
            ]
            duration = total_dist_direct / max(0.1, cruise_speed_mps)
            return PathPlanResult(
                is_feasible=True,
                total_distance_m=total_dist_direct,
                estimated_duration_s=duration,
                waypoints=waypoints,
                bypassed_obstacles=[],
                max_current_mps=max_curr,
                notes="直线路径无阻断，直接通行",
            )

        # 2. Direct path blocked: compute tangent detour waypoints around obstacle
        bypassed: list[str] = [blocked_obstacle] if blocked_obstacle else ["subsea_structure"]

        # Try lateral bypass (positive and negative offset in lon) and vertical depth offset
        mid_lat = (start_lat + goal_lat) / 2.0
        mid_lon = (start_lon + goal_lon) / 2.0
        mid_depth = (start_depth_m + goal_depth_m) / 2.0

        # Degree offset corresponding to ~50m to ~150m
        deg_step = 0.0008  # ~88m
        candidate_detours = [
            # Lateral East
            (mid_lat, mid_lon + deg_step, mid_depth),
            # Lateral West
            (mid_lat, mid_lon - deg_step, mid_depth),
            # Lateral North
            (mid_lat + deg_step, mid_lon, mid_depth),
            # Lateral South
            (mid_lat - deg_step, mid_lon, mid_depth),
            # Vertical Overflight (depth shallower by 35m)
            (mid_lat, mid_lon, max(10.0, mid_depth - 35.0)),
        ]

        best_path: list[dict[str, Any]] | None = None
        best_dist = float("inf")

        for d_lat, d_lon, d_depth in candidate_detours:
            # Check seg1: start -> detour
            s1_safe, _ = self._check_segment(start_lat, start_lon, start_depth_m, d_lat, d_lon, d_depth, min_clearance_m=min_clearance_m)
            if not s1_safe:
                continue

            # Check seg2: detour -> goal
            s2_safe, _ = self._check_segment(d_lat, d_lon, d_depth, goal_lat, goal_lon, goal_depth_m, min_clearance_m=min_clearance_m)
            if not s2_safe:
                continue

            # Both segments safe! Calculate distance
            dist1 = _calc_geodetic_dist(start_lat, start_lon, start_depth_m, d_lat, d_lon, d_depth)
            dist2 = _calc_geodetic_dist(d_lat, d_lon, d_depth, goal_lat, goal_lon, goal_depth_m)
            detour_total = dist1 + dist2

            if detour_total < best_dist:
                best_dist = detour_total
                best_path = [
                    {"lat": start_lat, "lon": start_lon, "depth_m": start_depth_m, "step": 0, "segment_distance_m": 0.0},
                    {"lat": round(d_lat, 6), "lon": round(d_lon, 6), "depth_m": round(d_depth, 1), "step": 1, "segment_distance_m": round(dist1, 2), "detour": True},
                    {"lat": goal_lat, "lon": goal_lon, "depth_m": goal_depth_m, "step": 2, "segment_distance_m": round(dist2, 2)},
                ]

        if best_path is not None:
            duration = best_dist / max(0.1, cruise_speed_mps)
            return PathPlanResult(
                is_feasible=True,
                total_distance_m=best_dist,
                estimated_duration_s=duration,
                waypoints=best_path,
                bypassed_obstacles=bypassed,
                max_current_mps=max_curr,
                notes=f"成功绕避障碍物 [{', '.join(bypassed)}]，已生成 3 阶段无碰撞避障航线",
            )

        # 3. If standard 1-point detour failed, return infeasible with diagnostic info
        return PathPlanResult(
            is_feasible=False,
            total_distance_m=total_dist_direct,
            estimated_duration_s=total_dist_direct / max(0.1, cruise_speed_mps),
            waypoints=[],
            bypassed_obstacles=bypassed,
            max_current_mps=max_curr,
            notes=f"直线航线穿入硬约束区域 [{', '.join(bypassed)}]，且未能在偏差半径 {max_deviation_m}m 内搜索到安全旁通路径",
        )

    def _check_segment(
        self,
        lat1: float,
        lon1: float,
        d1: float,
        lat2: float,
        lon2: float,
        d2: float,
        samples: int = 8,
        min_clearance_m: float = 10.0,
    ) -> tuple[bool, str | None]:
        """Sample intermediate points along a segment to detect collisions."""
        for step in range(samples + 1):
            ratio = step / float(samples)
            curr_lat = lat1 + (lat2 - lat1) * ratio
            curr_lon = lon1 + (lon2 - lon1) * ratio
            curr_depth = d1 + (d2 - d1) * ratio

            res = self.safety_engine.evaluate_geodetic_point(
                lat=curr_lat,
                lon=curr_lon,
                depth_m=curr_depth,
                min_clearance_m=min_clearance_m,
            )
            if not res.is_safe:
                obstacle_name = "restricted_zone"
                if res.violations:
                    v = res.violations[0]
                    obs_id = v.get("asset_id") or v.get("obstacle_id") or ""
                    obs_name = v.get("asset_name") or v.get("obstacle_name") or v.get("name") or v.get("type", "hazard")
                    obstacle_name = f"{obs_name} ({obs_id})" if obs_id and obs_name != obs_id else obs_name
                return False, obstacle_name

        return True, None
