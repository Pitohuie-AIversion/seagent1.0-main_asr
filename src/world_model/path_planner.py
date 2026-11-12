"""Subsea 4D collision-free trajectory planner navigating around geofences and physical assets."""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Sequence

from .models import GeodeticPoint, Point3D, SpatialSafetyCheckResult
from .spatial_safety import SpatialSafetyEngine

logger = logging.getLogger(__name__)


@dataclass
class CorridorReservation:
    """4D space-time bounding corridor reservation for a subsea robot."""
    robot_id: str
    t_start_s: float
    t_end_s: float
    lat: float
    lon: float
    depth_m: float
    radius_m: float = 15.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "robot_id": self.robot_id,
            "t_start_s": round(self.t_start_s, 1),
            "t_end_s": round(self.t_end_s, 1),
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "depth_m": round(self.depth_m, 1),
            "radius_m": self.radius_m,
        }


@dataclass
class PathPlanResult:
    """Outcome of a 3D/4D subsea path planning query."""
    is_feasible: bool
    total_distance_m: float
    estimated_duration_s: float
    waypoints: list[dict[str, Any]] = field(default_factory=list)
    bypassed_obstacles: list[str] = field(default_factory=list)
    max_current_mps: float = 0.0
    estimated_energy_kwh: float = 0.0
    avg_drag_power_kw: float = 0.0
    corridor_conflicts: list[dict[str, Any]] = field(default_factory=list)
    deconfliction_notes: str = ""
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
            "estimated_energy_kwh": round(self.estimated_energy_kwh, 4),
            "avg_drag_power_kw": round(self.avg_drag_power_kw, 3),
            "corridor_conflicts_count": len(self.corridor_conflicts),
            "corridor_conflicts": self.corridor_conflicts,
            "deconfliction_notes": self.deconfliction_notes,
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


def _compute_hydrodynamic_energy(
    waypoints: list[dict[str, Any]],
    cruise_speed_mps: float,
    world_model: Any | None,
) -> tuple[float, float, float]:
    """Compute hydrodynamic drag, power, and energy consumption along waypoints."""
    if len(waypoints) < 2:
        return 0.0, 0.0, 0.0

    rho = 1025.0       # seawater density kg/m3
    cd = 0.8           # hydrodynamic drag coefficient
    area = 1.2         # frontal cross-section area m2
    eta = 0.55         # electromechanical propulsion efficiency
    p_hotel = 450.0    # baseline electronics/sensors power in Watts

    total_energy_joules = 0.0
    total_time_s = 0.0
    max_current = 0.0

    for i in range(len(waypoints) - 1):
        wp1 = waypoints[i]
        wp2 = waypoints[i + 1]

        lat1, lon1, d1 = wp1["lat"], wp1["lon"], wp1["depth_m"]
        lat2, lon2, d2 = wp2["lat"], wp2["lon"], wp2["depth_m"]
        dist = _calc_geodetic_dist(lat1, lon1, d1, lat2, lon2, d2)
        if dist <= 0.001:
            continue

        seg_speed = max(0.1, cruise_speed_mps)
        seg_duration = dist / seg_speed
        total_time_s += seg_duration

        mid_lat = (lat1 + lat2) / 2.0
        mid_lon = (lon1 + lon2) / 2.0
        mid_depth = (d1 + d2) / 2.0

        v_curr_x = 0.0
        v_curr_y = 0.0
        if world_model and hasattr(world_model, "get_ocean_current"):
            curr_data = world_model.get_ocean_current(mid_lat, mid_lon, mid_depth)
            c_speed = curr_data.speed_mps
            c_dir = curr_data.direction_deg
            max_current = max(max_current, c_speed)
            rad = math.radians(c_dir)
            v_curr_x = c_speed * math.sin(rad)
            v_curr_y = c_speed * math.cos(rad)

        d_east = (lon2 - lon1) * 111320.0 * math.cos(math.radians(mid_lat))
        d_north = (lat2 - lat1) * 111320.0
        d_z = d2 - d1
        u_east = d_east / dist
        u_north = d_north / dist
        u_z = d_z / dist

        v_ground_x = seg_speed * u_east
        v_ground_y = seg_speed * u_north
        v_ground_z = seg_speed * u_z

        v_rel_x = v_ground_x - v_curr_x
        v_rel_y = v_ground_y - v_curr_y
        v_rel_z = v_ground_z
        v_rel = math.sqrt(v_rel_x**2 + v_rel_y**2 + v_rel_z**2)

        f_drag = 0.5 * rho * cd * area * (v_rel**2)
        p_thrust = (f_drag * v_rel) / eta
        p_total = p_thrust + p_hotel

        total_energy_joules += p_total * seg_duration

    total_kwh = total_energy_joules / 3.6e6
    avg_power_kw = (total_energy_joules / max(0.1, total_time_s)) / 1000.0 if total_time_s > 0 else 0.0

    return total_kwh, avg_power_kw, max_current


class SubseaPathPlanner:
    """Generates collision-free 3D waypoints steering clear of subsea assets,
    dynamic surface vessels, and restricted geofences, with 4D multi-robot corridor deconfliction."""

    def __init__(self, safety_engine: SpatialSafetyEngine, world_model: Any | None = None):
        self.safety_engine = safety_engine
        self.world_model = world_model
        self._corridors: list[CorridorReservation] = []
        self._lock = threading.RLock()

    def reserve_corridor(
        self,
        robot_id: str,
        waypoints: list[dict[str, Any]],
        start_time_s: float = 0.0,
        cruise_speed_mps: float = 1.0,
        radius_m: float = 15.0,
    ) -> list[CorridorReservation]:
        """Reserve 4D space-time corridor for a robot along a sequence of waypoints."""
        with self._lock:
            self._corridors = [c for c in self._corridors if c.robot_id != robot_id]

            new_reservations: list[CorridorReservation] = []
            curr_t = max(0.0, start_time_s)
            speed = max(0.1, cruise_speed_mps)

            for i, wp in enumerate(waypoints):
                lat = float(wp["lat"])
                lon = float(wp["lon"])
                depth = float(wp["depth_m"])

                if i > 0:
                    prev = waypoints[i - 1]
                    dist = _calc_geodetic_dist(prev["lat"], prev["lon"], prev["depth_m"], lat, lon, depth)
                    dt = dist / speed
                    if dist > 30.0:
                        mid_lat = (prev["lat"] + lat) / 2.0
                        mid_lon = (prev["lon"] + lon) / 2.0
                        mid_depth = (prev["depth_m"] + depth) / 2.0
                        mid_t = curr_t + dt / 2.0
                        new_reservations.append(CorridorReservation(
                            robot_id=robot_id,
                            t_start_s=mid_t - 30.0,
                            t_end_s=mid_t + 30.0,
                            lat=mid_lat,
                            lon=mid_lon,
                            depth_m=mid_depth,
                            radius_m=radius_m,
                        ))
                    curr_t += dt

                res = CorridorReservation(
                    robot_id=robot_id,
                    t_start_s=curr_t - 30.0,
                    t_end_s=curr_t + 30.0,
                    lat=lat,
                    lon=lon,
                    depth_m=depth,
                    radius_m=radius_m,
                )
                new_reservations.append(res)

            self._corridors.extend(new_reservations)
            return list(new_reservations)

    def get_corridor_reservations(self) -> list[CorridorReservation]:
        """Return all active 4D corridor reservations."""
        with self._lock:
            return list(self._corridors)

    def clear_corridors(self) -> None:
        """Clear all registered 4D space-time corridor reservations."""
        with self._lock:
            self._corridors.clear()

    def check_corridor_conflicts(
        self,
        waypoints: list[dict[str, Any]],
        start_time_s: float = 0.0,
        cruise_speed_mps: float = 1.0,
        exclude_robot_id: str | None = None,
        min_separation_m: float = 20.0,
    ) -> list[dict[str, Any]]:
        """Detect any temporal-spatial overlaps with active corridors reserved by other robots."""
        with self._lock:
            conflicts: list[dict[str, Any]] = []
            curr_t = max(0.0, start_time_s)
            speed = max(0.1, cruise_speed_mps)

            # Generate interpolated trajectory points with timestamps for collision detection
            trajectory: list[tuple[float, float, float, float, int]] = []
            if waypoints:
                t = curr_t
                trajectory.append((float(waypoints[0]["lat"]), float(waypoints[0]["lon"]), float(waypoints[0]["depth_m"]), t, 0))
                for i in range(1, len(waypoints)):
                    prev = waypoints[i - 1]
                    curr = waypoints[i]
                    p_lat, p_lon, p_dep = float(prev["lat"]), float(prev["lon"]), float(prev["depth_m"])
                    c_lat, c_lon, c_dep = float(curr["lat"]), float(curr["lon"]), float(curr["depth_m"])
                    dist = _calc_geodetic_dist(p_lat, p_lon, p_dep, c_lat, c_lon, c_dep)
                    dur = dist / speed
                    samples = max(2, int(dist / 15.0))  # sample every ~15m
                    for s in range(1, samples + 1):
                        frac = s / float(samples)
                        s_lat = p_lat + (c_lat - p_lat) * frac
                        s_lon = p_lon + (c_lon - p_lon) * frac
                        s_dep = p_dep + (c_dep - p_dep) * frac
                        s_t = t + dur * frac
                        trajectory.append((s_lat, s_lon, s_dep, s_t, i))
                    t += dur

            for lat, lon, depth, sample_t, wp_idx in trajectory:
                for c in self._corridors:
                    if exclude_robot_id and c.robot_id == exclude_robot_id:
                        continue

                    if not (c.t_start_s <= sample_t <= c.t_end_s):
                        continue

                    d_spatial = _calc_geodetic_dist(lat, lon, depth, c.lat, c.lon, c.depth_m)
                    if d_spatial < min_separation_m:
                        conflicts.append({
                            "waypoint_index": wp_idx,
                            "conflict_robot_id": c.robot_id,
                            "time_s": round(sample_t, 1),
                            "distance_m": round(d_spatial, 2),
                            "corridor_center": {"lat": c.lat, "lon": c.lon, "depth_m": c.depth_m},
                            "min_separation_m": min_separation_m,
                        })
                        break

            return conflicts

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
        robot_id: str | None = None,
        start_time_s: float = 0.0,
        auto_deconflict: bool = True,
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

        chosen_path: list[dict[str, Any]] | None = None
        bypassed: list[str] = []
        notes = ""

        if direct_safe:
            chosen_path = [
                {"lat": start_lat, "lon": start_lon, "depth_m": start_depth_m, "step": 0, "segment_distance_m": 0.0},
                {"lat": goal_lat, "lon": goal_lon, "depth_m": goal_depth_m, "step": 1, "segment_distance_m": round(total_dist_direct, 2)},
            ]
            notes = "直线路径无阻断，直接通行"
        else:
            # 2. Direct path blocked: compute tangent detour waypoints around obstacle
            bypassed = [blocked_obstacle] if blocked_obstacle else ["subsea_structure"]
            mid_lat = (start_lat + goal_lat) / 2.0
            mid_lon = (start_lon + goal_lon) / 2.0
            mid_depth = (start_depth_m + goal_depth_m) / 2.0

            deg_step = 0.0008  # ~88m
            candidate_detours = [
                (mid_lat, mid_lon + deg_step, mid_depth),
                (mid_lat, mid_lon - deg_step, mid_depth),
                (mid_lat + deg_step, mid_lon, mid_depth),
                (mid_lat - deg_step, mid_lon, mid_depth),
                (mid_lat, mid_lon, max(10.0, mid_depth - 35.0)),
            ]

            best_dist = float("inf")
            for d_lat, d_lon, d_depth in candidate_detours:
                s1_safe, _ = self._check_segment(start_lat, start_lon, start_depth_m, d_lat, d_lon, d_depth, min_clearance_m=min_clearance_m)
                if not s1_safe:
                    continue

                s2_safe, _ = self._check_segment(d_lat, d_lon, d_depth, goal_lat, goal_lon, goal_depth_m, min_clearance_m=min_clearance_m)
                if not s2_safe:
                    continue

                dist1 = _calc_geodetic_dist(start_lat, start_lon, start_depth_m, d_lat, d_lon, d_depth)
                dist2 = _calc_geodetic_dist(d_lat, d_lon, d_depth, goal_lat, goal_lon, goal_depth_m)
                detour_total = dist1 + dist2

                if detour_total < best_dist:
                    best_dist = detour_total
                    chosen_path = [
                        {"lat": start_lat, "lon": start_lon, "depth_m": start_depth_m, "step": 0, "segment_distance_m": 0.0},
                        {"lat": round(d_lat, 6), "lon": round(d_lon, 6), "depth_m": round(d_depth, 1), "step": 1, "segment_distance_m": round(dist1, 2), "detour": True},
                        {"lat": goal_lat, "lon": goal_lon, "depth_m": goal_depth_m, "step": 2, "segment_distance_m": round(dist2, 2)},
                    ]

            if chosen_path is not None:
                notes = f"成功绕避障碍物 [{', '.join(bypassed)}]，已生成 3 阶段无碰撞避障航线"

        if chosen_path is None:
            return PathPlanResult(
                is_feasible=False,
                total_distance_m=total_dist_direct,
                estimated_duration_s=total_dist_direct / max(0.1, cruise_speed_mps),
                waypoints=[],
                bypassed_obstacles=bypassed,
                max_current_mps=max_curr,
                notes=f"直线航线穿入硬约束区域 [{', '.join(bypassed)}]，且未能在偏差半径 {max_deviation_m}m 内搜索到安全旁通路径",
            )

        # 3. 4D Multi-robot space-time corridor deconfliction
        corridor_conflicts = []
        deconfliction_notes = ""
        if robot_id or self._corridors:
            corridor_conflicts = self.check_corridor_conflicts(
                waypoints=chosen_path,
                start_time_s=start_time_s,
                cruise_speed_mps=cruise_speed_mps,
                exclude_robot_id=robot_id,
                min_separation_m=min_clearance_m * 2.0,
            )

            if corridor_conflicts and auto_deconflict:
                alt_offset = -25.0 if start_depth_m > 40.0 else 25.0
                shifted_wps = []
                for wp in chosen_path:
                    wp_copy = dict(wp)
                    wp_copy["depth_m"] = max(10.0, wp_copy["depth_m"] + alt_offset)
                    wp_copy["altitude_separated"] = True
                    shifted_wps.append(wp_copy)

                shift_safe = True
                for j in range(len(shifted_wps) - 1):
                    p1 = shifted_wps[j]
                    p2 = shifted_wps[j + 1]
                    s_safe, _ = self._check_segment(
                        p1["lat"], p1["lon"], p1["depth_m"],
                        p2["lat"], p2["lon"], p2["depth_m"],
                        min_clearance_m=min_clearance_m,
                    )
                    if not s_safe:
                        shift_safe = False
                        break

                if shift_safe:
                    remaining = self.check_corridor_conflicts(
                        waypoints=shifted_wps,
                        start_time_s=start_time_s,
                        cruise_speed_mps=cruise_speed_mps,
                        exclude_robot_id=robot_id,
                        min_separation_m=min_clearance_m * 2.0,
                    )
                    if not remaining:
                        c_id = corridor_conflicts[0]["conflict_robot_id"]
                        deconfliction_notes = f"检测到与 [{c_id}] 存在 4D 时空走廊冲突，已成功执行垂直高度层分离 (Altitude Separation {alt_offset:+.1f}m)"
                        chosen_path = shifted_wps
                        corridor_conflicts = []

        # 4. Calculate total distance, duration, and hydrodynamic energy
        total_dist = 0.0
        for i in range(len(chosen_path) - 1):
            p1 = chosen_path[i]
            p2 = chosen_path[i + 1]
            total_dist += _calc_geodetic_dist(p1["lat"], p1["lon"], p1["depth_m"], p2["lat"], p2["lon"], p2["depth_m"])

        duration = total_dist / max(0.1, cruise_speed_mps)
        energy_kwh, avg_kw, max_curr_calc = _compute_hydrodynamic_energy(
            waypoints=chosen_path,
            cruise_speed_mps=cruise_speed_mps,
            world_model=self.world_model,
        )
        max_curr = max(max_curr, max_curr_calc)

        return PathPlanResult(
            is_feasible=True,
            total_distance_m=total_dist,
            estimated_duration_s=duration,
            waypoints=chosen_path,
            bypassed_obstacles=bypassed,
            max_current_mps=max_curr,
            estimated_energy_kwh=energy_kwh,
            avg_drag_power_kw=avg_kw,
            corridor_conflicts=corridor_conflicts,
            deconfliction_notes=deconfliction_notes,
            notes=notes,
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
