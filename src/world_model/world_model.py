"""Subsea 4D World Model coordinating infrastructure digital twins, spatial geofences, and safety checks."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .models import (
    DynamicObstacle,
    GeodeticPoint,
    GeofenceSeverity,
    GeofenceZone,
    GeofenceZoneType,
    OceanCurrentData,
    Point3D,
    SpatialSafetyCheckResult,
    SubseaAsset,
    SubseaAssetType,
)
from .path_planner import PathPlanResult, SubseaPathPlanner
from .spatial_safety import SpatialSafetyEngine

logger = logging.getLogger(__name__)


class SubseaWorldModel:
    """Thread-safe Subsea 4D World Model maintaining subsea physical assets, geofences, and fleet telemetry."""

    _instance: SubseaWorldModel | None = None
    _lock = threading.Lock()

    def __init__(self, config_path: str | Path | None = None):
        self._rw_lock = threading.RLock()
        self._assets: dict[str, SubseaAsset] = {}
        self._geofences: dict[str, GeofenceZone] = {}
        self._obstacles: dict[str, DynamicObstacle] = {}
        self._robot_positions: dict[str, dict[str, Any]] = {}
        self._current_override: OceanCurrentData | None = None
        self._safety_engine = SpatialSafetyEngine()
        self._path_planner = SubseaPathPlanner(self._safety_engine, world_model=self)

        self._load_config(config_path)
        self._init_default_assets()
        self._sync_safety_engine()

    @classmethod
    def get_instance(cls) -> SubseaWorldModel:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        with cls._lock:
            cls._instance = None

    def _load_config(self, config_path: str | Path | None = None) -> None:
        path = Path(config_path) if config_path else Path(__file__).parent.parent.parent / "config" / "oilfield.yaml"
        if not path.exists():
            logger.warning("[WorldModel] Configuration file %s does not exist.", path)
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            # 1. Load forbidden areas (HARD severity)
            for area in data.get("forbidden_areas", []):
                zid = area.get("id", f"forbidden_{len(self._geofences)}")
                self._geofences[zid] = GeofenceZone(
                    zone_id=zid,
                    name=area.get("name", zid),
                    zone_type=GeofenceZoneType.FORBIDDEN,
                    severity=GeofenceSeverity.HARD,
                    lat_range=tuple(area.get("lat_range", [0.0, 0.0])),
                    lon_range=tuple(area.get("lon_range", [0.0, 0.0])),
                    description=area.get("notes", "生态红线或禁止作业区域"),
                )

            # 2. Load DVL bottom-lock failure areas (SOFT severity)
            for area in data.get("dvl_bottom_lock_failure_areas", []):
                zid = area.get("id", f"dvl_blackout_{len(self._geofences)}")
                self._geofences[zid] = GeofenceZone(
                    zone_id=zid,
                    name=area.get("name", zid),
                    zone_type=GeofenceZoneType.DVL_BLACKOUT,
                    severity=GeofenceSeverity.SOFT,
                    lat_range=tuple(area.get("lat_range", [0.0, 0.0])),
                    lon_range=tuple(area.get("lon_range", [0.0, 0.0])),
                    description=area.get("notes", "声学测速底锁定失效风险区"),
                )
        except Exception as exc:
            logger.error("[WorldModel] Failed to load oilfield config: %s", exc)

    def _init_default_assets(self) -> None:
        """Pre-populate default digital twin assets for key South China Sea oilfields."""
        default_assets = [
            SubseaAsset(
                asset_id="LH11-1-MANIFOLD-01",
                asset_type=SubseaAssetType.MANIFOLD,
                name="流花11-1主水下管汇",
                oilfield_id="liuhua_11_1",
                point=Point3D(x=100.0, y=150.0, z=-305.0),
                geodetic=GeodeticPoint(latitude=20.815, longitude=115.735, depth_m=305.0),
                bounding_radius_m=12.0,
                properties={"valve_a": "open", "valve_b": "closed", "header_pressure_mpa": 19.5},
            ),
            SubseaAsset(
                asset_id="LH11-1-XTREE-01",
                asset_type=SubseaAssetType.XMAS_TREE,
                name="流花11-1 #1水下采油树",
                oilfield_id="liuhua_11_1",
                point=Point3D(x=130.0, y=170.0, z=-305.0),
                geodetic=GeodeticPoint(latitude=20.817, longitude=115.737, depth_m=305.0),
                bounding_radius_m=6.0,
                properties={"master_valve": "open", "wing_valve": "closed", "tree_status": "PRODUCING"},
            ),
            SubseaAsset(
                asset_id="LS17-2-MANIFOLD-NORTH",
                asset_type=SubseaAssetType.MANIFOLD,
                name="深海一号北区中心水下管汇",
                oilfield_id="lingshui_17_2",
                point=Point3D(x=500.0, y=420.0, z=-1500.0),
                geodetic=GeodeticPoint(latitude=17.525, longitude=110.150, depth_m=1500.0),
                bounding_radius_m=15.0,
                properties={"choke_position_pct": 45, "safety_isolation": "intact"},
            ),
        ]
        for a in default_assets:
            self._assets[a.asset_id] = a

        # Default offshore support vessel
        self._obstacles["VESSEL-HAIYANG-681"] = DynamicObstacle(
            obstacle_id="VESSEL-HAIYANG-681",
            name="海洋石油681 深水工程支持船",
            obstacle_type="support_vessel",
            point=Point3D(x=0.0, y=0.0, z=0.0),
            geodetic=GeodeticPoint(latitude=20.815, longitude=115.735, depth_m=0.0),
            safety_radius_m=60.0,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def _sync_safety_engine(self) -> None:
        self._safety_engine.set_geofences(list(self._geofences.values()))
        self._safety_engine.set_assets(list(self._assets.values()))
        self._safety_engine.set_obstacles(list(self._obstacles.values()))

    # ================= Asset Operations =================
    def register_asset(self, asset: SubseaAsset) -> None:
        with self._rw_lock:
            self._assets[asset.asset_id] = asset
            self._sync_safety_engine()

    def get_asset(self, asset_id: str) -> SubseaAsset | None:
        with self._rw_lock:
            return self._assets.get(asset_id)

    def list_assets(self, oilfield_id: str | None = None) -> list[SubseaAsset]:
        with self._rw_lock:
            if oilfield_id:
                return [a for a in self._assets.values() if a.oilfield_id == oilfield_id]
            return list(self._assets.values())

    def update_asset_properties(self, asset_id: str, updates: dict[str, Any]) -> int:
        """Update asset properties and increment state_version atomically."""
        with self._rw_lock:
            asset = self._assets.get(asset_id)
            if not asset:
                raise KeyError(f"Subsea asset '{asset_id}' not found in world model.")
            asset.properties.update(updates)
            asset.state_version += 1
            return asset.state_version

    # ================= Geofence Operations =================
    def add_dynamic_geofence(self, zone: GeofenceZone) -> None:
        with self._rw_lock:
            self._geofences[zone.zone_id] = zone
            self._sync_safety_engine()

    def remove_dynamic_geofence(self, zone_id: str) -> bool:
        with self._rw_lock:
            removed = bool(self._geofences.pop(zone_id, None))
            if removed:
                self._sync_safety_engine()
            return removed

    def list_geofences(self) -> list[GeofenceZone]:
        with self._rw_lock:
            return list(self._geofences.values())

    # ================= Dynamic Obstacle & Vessel Operations =================
    def add_dynamic_obstacle(self, obstacle: DynamicObstacle) -> None:
        with self._rw_lock:
            self._obstacles[obstacle.obstacle_id] = obstacle
            self._sync_safety_engine()

    def remove_dynamic_obstacle(self, obstacle_id: str) -> bool:
        with self._rw_lock:
            removed = bool(self._obstacles.pop(obstacle_id, None))
            if removed:
                self._sync_safety_engine()
            return removed

    def list_dynamic_obstacles(self) -> list[DynamicObstacle]:
        with self._rw_lock:
            return list(self._obstacles.values())

    def get_dynamic_obstacle(self, obstacle_id: str) -> DynamicObstacle | None:
        with self._rw_lock:
            return self._obstacles.get(obstacle_id)

    # ================= Robot Pose Tracking =================
    def update_robot_pose(
        self,
        unit_id: str,
        x: float,
        y: float,
        z: float,
        lat: float | None = None,
        lon: float | None = None,
        health: str = "HEALTHY",
    ) -> None:
        with self._rw_lock:
            self._robot_positions[unit_id] = {
                "unit_id": unit_id,
                "x": x,
                "y": y,
                "z": z,
                "depth_m": abs(z),
                "lat": lat,
                "lon": lon,
                "health": health,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

    def get_robot_pose(self, unit_id: str) -> dict[str, Any] | None:
        with self._rw_lock:
            return self._robot_positions.get(unit_id)

    # ================= Environmental & Ocean Current =================
    def set_environmental_current_override(self, current_data: OceanCurrentData | None) -> None:
        """Set or clear a simulated environmental ocean current field override."""
        with self._rw_lock:
            self._current_override = current_data

    def get_ocean_current(
        self,
        lat: float,
        lon: float,
        depth_m: float = 0.0,
        timestamp: Any | None = None,
    ) -> OceanCurrentData:
        """Query physical ocean current field at (lat, lon, depth_m).
        Uses forecast cache, registered override, or South China Sea empirical decay profile."""
        with self._rw_lock:
            if self._current_override is not None:
                return self._current_override

        # South China Sea empirical depth attenuation profile:
        import math
        depth_norm = max(0.0, float(depth_m))
        base_speed = 0.85 * math.exp(-depth_norm / 450.0) + 0.15 * math.sin(depth_norm / 100.0)
        speed = max(0.12, min(2.8, abs(base_speed)))
        direction = (lat * 12.0 + lon * 6.0 + depth_norm * 0.15) % 360.0
        shear = max(0.01, 0.10 * math.exp(-depth_norm / 300.0))

        return OceanCurrentData(
            speed_mps=round(speed, 3),
            direction_deg=round(direction, 1),
            vertical_speed_mps=0.01,
            shear_per_100m=round(shear, 3),
            source="empirical_scs_profile",
        )

    # ================= Safety Checks =================
    def check_point_safety(
        self,
        lat: float,
        lon: float,
        depth_m: float = 0.0,
        min_clearance_m: float = 5.0,
        current_limit_mps: float | None = None,
    ) -> SpatialSafetyCheckResult:
        with self._rw_lock:
            current_data = None
            if current_limit_mps is not None:
                current_data = self.get_ocean_current(lat=lat, lon=lon, depth_m=depth_m)

            return self._safety_engine.evaluate_geodetic_point(
                lat=lat,
                lon=lon,
                depth_m=depth_m,
                min_clearance_m=min_clearance_m,
                current_limit_mps=current_limit_mps,
                current_data=current_data,
            )

    def check_path_safety(
        self,
        waypoints: list[dict[str, float]],
        min_clearance_m: float = 5.0,
    ) -> SpatialSafetyCheckResult:
        """Validate a sequence of waypoints [{lat, lon, depth_m}] or [{x, y, z}]."""
        with self._rw_lock:
            all_violations: list[dict] = []
            all_warnings: list[dict] = []
            min_dist: float | None = None
            closest_asset: str | None = None

            for i, wp in enumerate(waypoints):
                if "lat" in wp and "lon" in wp:
                    res = self._safety_engine.evaluate_geodetic_point(
                        lat=float(wp["lat"]),
                        lon=float(wp["lon"]),
                        depth_m=float(wp.get("depth_m", 0.0)),
                        min_clearance_m=min_clearance_m,
                    )
                elif "x" in wp and "y" in wp and "z" in wp:
                    pt = Point3D(x=float(wp["x"]), y=float(wp["y"]), z=float(wp["z"]))
                    res = self._safety_engine.evaluate_cartesian_point(pt, min_clearance_m=min_clearance_m)
                else:
                    continue

                for v in res.violations:
                    v_tagged = dict(v, waypoint_index=i)
                    if v_tagged not in all_violations:
                        all_violations.append(v_tagged)
                for w in res.warnings:
                    w_tagged = dict(w, waypoint_index=i)
                    if w_tagged not in all_warnings:
                        all_warnings.append(w_tagged)

                if res.closest_asset_distance_m is not None:
                    if min_dist is None or res.closest_asset_distance_m < min_dist:
                        min_dist = res.closest_asset_distance_m
                        closest_asset = res.closest_asset_id

            return SpatialSafetyCheckResult(
                is_safe=(len(all_violations) == 0),
                violations=all_violations,
                warnings=all_warnings,
                closest_asset_distance_m=min_dist,
                closest_asset_id=closest_asset,
            )

    def plan_safe_path(
        self,
        start_lat: float,
        start_lon: float,
        start_depth_m: float,
        goal_lat: float,
        goal_lon: float,
        goal_depth_m: float,
        min_clearance_m: float = 10.0,
        cruise_speed_mps: float = 1.0,
        robot_id: str | None = None,
        start_time_s: float = 0.0,
        auto_deconflict: bool = True,
    ) -> PathPlanResult:
        """Compute collision-free waypoint trajectory connecting start and goal with 4D corridor checks."""
        with self._rw_lock:
            return self._path_planner.plan_path(
                start_lat=start_lat,
                start_lon=start_lon,
                start_depth_m=start_depth_m,
                goal_lat=goal_lat,
                goal_lon=goal_lon,
                goal_depth_m=goal_depth_m,
                min_clearance_m=min_clearance_m,
                cruise_speed_mps=cruise_speed_mps,
                robot_id=robot_id,
                start_time_s=start_time_s,
                auto_deconflict=auto_deconflict,
            )

    def reserve_robot_corridor(
        self,
        robot_id: str,
        waypoints: list[dict[str, Any]],
        start_time_s: float = 0.0,
        cruise_speed_mps: float = 1.0,
        radius_m: float = 15.0,
    ) -> list[dict[str, Any]]:
        """Reserve 4D space-time corridor for a robot trajectory."""
        with self._rw_lock:
            reservations = self._path_planner.reserve_corridor(
                robot_id=robot_id,
                waypoints=waypoints,
                start_time_s=start_time_s,
                cruise_speed_mps=cruise_speed_mps,
                radius_m=radius_m,
            )
            return [r.to_dict() for r in reservations]

    def list_corridor_reservations(self) -> list[dict[str, Any]]:
        """List active 4D space-time corridor reservations."""
        with self._rw_lock:
            return [r.to_dict() for r in self._path_planner.get_corridor_reservations()]

    def clear_corridor_reservations(self) -> None:
        """Clear all active 4D space-time corridor reservations."""
        with self._rw_lock:
            self._path_planner.clear_corridors()

    def export_snapshot(self) -> dict[str, Any]:
        """Export serializable snapshot of the subsea digital twin."""
        with self._rw_lock:
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "assets_count": len(self._assets),
                "geofences_count": len(self._geofences),
                "obstacles_count": len(self._obstacles),
                "assets": [a.to_dict() for a in self._assets.values()],
                "geofences": [g.to_dict() for g in self._geofences.values()],
                "dynamic_obstacles": [o.to_dict() for o in self._obstacles.values()],
                "robots": list(self._robot_positions.values()),
            }
