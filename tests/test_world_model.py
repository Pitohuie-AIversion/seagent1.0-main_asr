"""Unit tests for the Subsea 4D World Model, Spatial Safety, and Digital Twin."""

import pytest
from src.world_model import (
    GeodeticPoint,
    CorridorReservation,
    GeofenceSeverity,
    GeofenceZone,
    GeofenceZoneType,
    PathPlanResult,
    Point3D,
    SubseaAsset,
    SubseaAssetType,
    SubseaPathPlanner,
    SubseaWorldModel,
)


@pytest.fixture(autouse=True)
def reset_world_model():
    SubseaWorldModel.reset_instance()
    yield
    SubseaWorldModel.reset_instance()


def test_world_model_initialization_and_assets():
    wm = SubseaWorldModel.get_instance()
    snapshot = wm.export_snapshot()

    assert snapshot["assets_count"] >= 3
    assert snapshot["geofences_count"] >= 1

    lh_assets = wm.list_assets("liuhua_11_1")
    assert len(lh_assets) >= 2
    asset_ids = {a.asset_id for a in lh_assets}
    assert "LH11-1-MANIFOLD-01" in asset_ids
    assert "LH11-1-XTREE-01" in asset_ids


def test_asset_property_update_and_versioning():
    wm = SubseaWorldModel.get_instance()
    asset = wm.get_asset("LH11-1-MANIFOLD-01")
    assert asset is not None
    v_init = asset.state_version

    new_version = wm.update_asset_properties("LH11-1-MANIFOLD-01", {"valve_a": "closed", "header_pressure_mpa": 21.0})
    assert new_version == v_init + 1
    assert asset.properties["valve_a"] == "closed"
    assert asset.properties["header_pressure_mpa"] == 21.0


def test_geodetic_safety_check_hard_forbidden():
    wm = SubseaWorldModel.get_instance()
    # Point inside Xuwen Coral Reef Reserve (lat: [20.3, 20.55], lon: [109.75, 110.0])
    res = wm.check_point_safety(lat=20.45, lon=109.85, depth_m=15.0)
    assert res.is_safe is False
    assert len(res.violations) >= 1
    assert any(v["severity"] == "hard" for v in res.violations)
    assert any("珊瑚礁" in v["name"] for v in res.violations)


def test_geodetic_safety_check_soft_warning():
    wm = SubseaWorldModel.get_instance()
    # Point in DVL blackout failure area from oilfield.yaml (e.g. lat: [20.81, 20.82], lon: [115.73, 115.74] or soft area)
    # Check with an area with soft severity
    soft_zones = [z for z in wm.list_geofences() if z.severity == GeofenceSeverity.SOFT]
    if soft_zones:
        z = soft_zones[0]
        mid_lat = (z.lat_range[0] + z.lat_range[1]) / 2.0
        mid_lon = (z.lon_range[0] + z.lon_range[1]) / 2.0
        res = wm.check_point_safety(lat=mid_lat, lon=mid_lon, depth_m=50.0)
        assert res.is_safe is True
        assert len(res.warnings) >= 1
        assert any(w["severity"] == "soft" for w in res.warnings)


def test_asset_proximity_collision_and_clearance():
    wm = SubseaWorldModel.get_instance()
    # LH11-1-MANIFOLD-01 is at geodetic lat=20.815, lon=115.735, depth=305.0, bounding_radius=12.0m

    # 1. Point directly on the asset (collision)
    coll_res = wm.check_point_safety(lat=20.815, lon=115.735, depth_m=305.0)
    assert coll_res.is_safe is False
    assert len(coll_res.violations) >= 1
    assert coll_res.violations[0]["type"] == "asset_collision"
    assert coll_res.closest_asset_id == "LH11-1-MANIFOLD-01"

    # 2. Point 20m away horizontally (approx d_lat = 0.00018 deg ~ 20m)
    # Clearance = 20m - 12m = 8m. With min_clearance=10m, this should trigger proximity warning.
    warn_res = wm.check_point_safety(lat=20.81518, lon=115.735, depth_m=305.0, min_clearance_m=10.0)
    assert warn_res.is_safe is True
    assert len(warn_res.warnings) >= 1
    assert warn_res.warnings[0]["type"] == "asset_proximity_warning"

    # 3. Point 500m away (safe, no warning)
    safe_res = wm.check_point_safety(lat=20.820, lon=115.735, depth_m=305.0, min_clearance_m=10.0)
    assert safe_res.is_safe is True
    assert len(safe_res.violations) == 0
    assert len(safe_res.warnings) == 0


def test_path_safety_check_sequence():
    wm = SubseaWorldModel.get_instance()
    # Sequence of Cartesian waypoints
    # LH11-1-MANIFOLD-01 is at (100.0, 150.0, -305.0) with radius 12.0m
    waypoints = [
        {"x": 0.0, "y": 0.0, "z": -305.0},          # Safe (~180m away)
        {"x": 100.0, "y": 150.0, "z": -305.0},      # Direct hit (Collision)
        {"x": 500.0, "y": 500.0, "z": -305.0},      # Safe
    ]

    path_res = wm.check_path_safety(waypoints, min_clearance_m=5.0)
    assert path_res.is_safe is False
    assert len(path_res.violations) == 1
    assert path_res.violations[0]["waypoint_index"] == 1
    assert path_res.violations[0]["asset_id"] == "LH11-1-MANIFOLD-01"


def test_dynamic_geofence_lifecycle():
    wm = SubseaWorldModel.get_instance()
    temp_zone = GeofenceZone(
        zone_id="TEMP-PLUME-001",
        name="临时水下混浊泥沙羽流区",
        zone_type=GeofenceZoneType.HIGH_CURRENT,
        severity=GeofenceSeverity.HARD,
        lat_range=(18.0, 18.1),
        lon_range=(112.0, 112.1),
        depth_range=(50.0, 200.0),
        description="现场打桩作业引起的泥沙扩散区",
    )

    # Initially safe
    assert wm.check_point_safety(lat=18.05, lon=112.05, depth_m=100.0).is_safe is True

    # Add geofence -> triggers violation
    wm.add_dynamic_geofence(temp_zone)
    blocked_res = wm.check_point_safety(lat=18.05, lon=112.05, depth_m=100.0)
    assert blocked_res.is_safe is False
    assert blocked_res.violations[0]["zone_id"] == "TEMP-PLUME-001"

    # Remove geofence -> restored to safe
    removed = wm.remove_dynamic_geofence("TEMP-PLUME-001")
    assert removed is True
    assert wm.check_point_safety(lat=18.05, lon=112.05, depth_m=100.0).is_safe is True


def test_dev_api_world_model_endpoints():
    from web_backend import app

    client = app.test_client()

    # 1. GET /api/dev/world-model/snapshot
    resp_snap = client.get("/api/dev/world-model/snapshot")
    assert resp_snap.status_code == 200
    data_snap = resp_snap.get_json()
    assert data_snap["ok"] is True
    assert "world_model" in data_snap
    assert data_snap["world_model"]["assets_count"] >= 3

    # 2. POST /api/dev/world-model/check-safety (Single point)
    resp_check = client.post("/api/dev/world-model/check-safety", json={
        "lat": 20.815,
        "lon": 115.735,
        "depth_m": 305.0,
        "min_clearance_m": 5.0,
    })
    assert resp_check.status_code == 200
    data_check = resp_check.get_json()
    assert data_check["ok"] is True
    assert "safety_assessment" in data_check
    assert data_check["safety_assessment"]["is_safe"] is False  # Asset collision detected

    # 3. POST /api/dev/world-model/check-safety (Missing args error)
    resp_bad = client.post("/api/dev/world-model/check-safety", json={"unknown": 123})
    assert resp_bad.status_code == 400


def test_ocean_current_profile_and_override():
    from src.world_model import OceanCurrentData

    wm = SubseaWorldModel.get_instance()
    # 1. Test South China Sea empirical profile at surface vs seabed
    surf_curr = wm.get_ocean_current(lat=20.0, lon=115.0, depth_m=10.0)
    deep_curr = wm.get_ocean_current(lat=20.0, lon=115.0, depth_m=1500.0)
    assert surf_curr.speed_mps > deep_curr.speed_mps
    assert surf_curr.source == "empirical_scs_profile"

    # 2. Test override
    mock_curr = OceanCurrentData(speed_mps=2.4, direction_deg=180.0, source="typhoon_sim")
    wm.set_environmental_current_override(mock_curr)
    curr_now = wm.get_ocean_current(lat=20.0, lon=115.0, depth_m=500.0)
    assert curr_now.speed_mps == 2.4
    assert curr_now.source == "typhoon_sim"

    wm.set_environmental_current_override(None)
    curr_cleared = wm.get_ocean_current(lat=20.0, lon=115.0, depth_m=500.0)
    assert curr_cleared.source == "empirical_scs_profile"


def test_geodetic_safety_check_high_current_limit_hard_and_soft():
    from src.world_model import OceanCurrentData

    wm = SubseaWorldModel.get_instance()
    # Simulate high current of 2.0 m/s
    wm.set_environmental_current_override(OceanCurrentData(speed_mps=2.0, direction_deg=90.0))

    # 1. Robot limit = 1.5 m/s. 2.0 m/s > 1.5 * 1.25 = 1.875 m/s -> HARD hazard
    res_hard = wm.check_point_safety(lat=18.0, lon=113.0, depth_m=200.0, current_limit_mps=1.5)
    assert res_hard.is_safe is False
    assert any(v.get("type") == "high_current_hazard" for v in res_hard.violations)
    assert any(v.get("severity") == "hard" for v in res_hard.violations)

    # 2. Robot limit = 1.8 m/s. 1.8 < 2.0 <= 1.8 * 1.25 = 2.25 m/s -> SOFT warning
    res_soft = wm.check_point_safety(lat=18.0, lon=113.0, depth_m=200.0, current_limit_mps=1.8)
    assert res_soft.is_safe is True
    assert any(w.get("type") == "high_current_warning" for w in res_soft.warnings)

    # 3. High-spec heavy work-class ROV limit = 2.5 m/s -> No current hazards or warnings
    res_safe = wm.check_point_safety(lat=18.0, lon=113.0, depth_m=200.0, current_limit_mps=2.5)
    assert res_safe.is_safe is True
    assert not any(v.get("type") == "high_current_hazard" for v in res_safe.violations)
    assert not any(w.get("type") == "high_current_warning" for w in res_safe.warnings)


def test_dynamic_obstacle_registration_and_proximity_safety():
    from src.world_model import DynamicObstacle, GeodeticPoint, Point3D

    wm = SubseaWorldModel.get_instance()
    vessel = DynamicObstacle(
        obstacle_id="TEST-SURFACE-VESSEL",
        name="测试深水工程支持驳船",
        obstacle_type="support_vessel",
        point=Point3D(x=0.0, y=0.0, z=0.0),
        geodetic=GeodeticPoint(latitude=21.000, longitude=115.000, depth_m=0.0),
        safety_radius_m=50.0,
    )
    wm.add_dynamic_obstacle(vessel)
    assert len(wm.list_dynamic_obstacles()) >= 2
    assert wm.get_dynamic_obstacle("TEST-SURFACE-VESSEL") is not None

    # Point 1: 10m below vessel (depth=10m < 25m = safety_radius * 0.5) -> HARD hazard
    res_hard = wm.check_point_safety(lat=21.000, lon=115.000, depth_m=10.0)
    assert res_hard.is_safe is False
    assert any(v.get("type") == "dynamic_obstacle_collision_hazard" for v in res_hard.violations)

    # Point 2: 35m below vessel (25m <= 35m < 50m) -> SOFT warning
    res_soft = wm.check_point_safety(lat=21.000, lon=115.000, depth_m=35.0)
    assert res_soft.is_safe is True
    assert any(w.get("type") == "dynamic_obstacle_proximity_warning" for w in res_soft.warnings)

    # Point 3: deep seabed at 300m -> completely clear of surface vessel
    res_deep = wm.check_point_safety(lat=21.000, lon=115.000, depth_m=300.0)
    assert res_deep.is_safe is True
    assert not any(v.get("type") == "dynamic_obstacle_collision_hazard" for v in res_deep.violations)
    assert not any(w.get("type") == "dynamic_obstacle_proximity_warning" for w in res_deep.warnings)


def test_api_dev_asr_normalize_endpoint():
    from web_backend import app
    client = app.test_client()

    resp = client.post("/api/dev/asr/normalize", json={
        "text": "在流花油田使用柴油树控制面板进行翼阀操作"
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["normalization_changed"] is True
    assert "采油树" in data["corrected_text"]
    assert any(r["target"] == "采油树" for r in data["replacements"])


def test_path_planner_direct_path_clear():
    wm = SubseaWorldModel.get_instance()
    plan = wm.plan_safe_path(
        start_lat=20.830,
        start_lon=115.750,
        start_depth_m=280.0,
        goal_lat=20.835,
        goal_lon=115.755,
        goal_depth_m=290.0,
        min_clearance_m=10.0,
        cruise_speed_mps=1.0,
    )
    assert plan.is_feasible is True
    assert len(plan.waypoints) == 2
    assert plan.total_distance_m > 0
    assert plan.estimated_duration_s > 0
    assert len(plan.bypassed_obstacles) == 0
    assert "直线路径无阻断" in plan.notes


def test_path_planner_obstacle_detour_avoidance():
    wm = SubseaWorldModel.get_instance()
    plan = wm.plan_safe_path(
        start_lat=20.814,
        start_lon=115.735,
        start_depth_m=305.0,
        goal_lat=20.816,
        goal_lon=115.735,
        goal_depth_m=305.0,
        min_clearance_m=10.0,
        cruise_speed_mps=1.0,
    )
    assert plan.is_feasible is True
    assert len(plan.waypoints) == 3
    assert plan.waypoints[1].get("detour") is True
    assert len(plan.bypassed_obstacles) >= 1
    assert any("MANIFOLD" in obs or "manifold" in obs.lower() or "管汇" in obs for obs in plan.bypassed_obstacles)
    assert "绕避障碍物" in plan.notes


def test_api_dev_world_model_plan_path_endpoint():
    from web_backend import app
    client = app.test_client()

    resp = client.post("/api/dev/world-model/plan-path", json={
        "start": {"lat": 20.814, "lon": 115.735, "depth_m": 305.0},
        "goal": {"lat": 20.816, "lon": 115.735, "depth_m": 305.0},
        "min_clearance_m": 10.0,
        "cruise_speed_mps": 1.2,
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "path_plan" in data
    path_plan = data["path_plan"]
    assert path_plan["is_feasible"] is True
    assert path_plan["waypoints_count"] == 3
    assert len(path_plan["bypassed_obstacles"]) >= 1


def test_path_planner_hydrodynamic_energy_calculation():
    from src.world_model import OceanCurrentData
    wm = SubseaWorldModel.get_instance()

    # 1. Calm current baseline
    wm.set_environmental_current_override(OceanCurrentData(speed_mps=0.0, direction_deg=0.0))
    plan_calm = wm.plan_safe_path(
        start_lat=20.830,
        start_lon=115.750,
        start_depth_m=280.0,
        goal_lat=20.835,
        goal_lon=115.755,
        goal_depth_m=280.0,
        cruise_speed_mps=1.0,
    )
    assert plan_calm.is_feasible is True
    assert plan_calm.estimated_energy_kwh > 0.0
    assert plan_calm.avg_drag_power_kw > 0.0

    # 2. Strong head-on current of 1.2 m/s opposing motion (Southbound current against Northbound heading)
    wm.set_environmental_current_override(OceanCurrentData(speed_mps=1.2, direction_deg=180.0))
    plan_curr = wm.plan_safe_path(
        start_lat=20.830,
        start_lon=115.750,
        start_depth_m=280.0,
        goal_lat=20.835,
        goal_lon=115.755,
        goal_depth_m=280.0,
        cruise_speed_mps=1.0,
    )
    assert plan_curr.is_feasible is True
    assert plan_curr.estimated_energy_kwh > plan_calm.estimated_energy_kwh
    assert plan_curr.avg_drag_power_kw > plan_calm.avg_drag_power_kw


def test_path_planner_multi_robot_corridor_reservation_and_deconfliction():
    wm = SubseaWorldModel.get_instance()
    wm.clear_corridor_reservations()

    # Reserve corridor for ROV-ALPHA crossing (20.825, 115.745, 250.0)
    wps_alpha = [
        {"lat": 20.820, "lon": 115.745, "depth_m": 250.0},
        {"lat": 20.825, "lon": 115.745, "depth_m": 250.0},
        {"lat": 20.830, "lon": 115.745, "depth_m": 250.0},
    ]
    res_alpha = wm.reserve_robot_corridor(
        robot_id="ROV-ALPHA",
        waypoints=wps_alpha,
        start_time_s=0.0,
        cruise_speed_mps=1.0,
        radius_m=15.0,
    )
    assert len(res_alpha) >= 3
    assert len(wm.list_corridor_reservations()) >= 3

    # ROV-BETA wants to cross the exact same spot at the same time window
    # 1. Crossing at far future time (start_time_s=5000.0) -> NO conflict
    plan_future = wm.plan_safe_path(
        start_lat=20.825,
        start_lon=115.740,
        start_depth_m=250.0,
        goal_lat=20.825,
        goal_lon=115.750,
        goal_depth_m=250.0,
        robot_id="ROV-BETA",
        start_time_s=5000.0,
    )
    assert len(plan_future.corridor_conflicts) == 0

    # 2. Crossing at concurrent time window (start_time_s=0.0) with auto_deconflict=False -> conflict detected
    plan_conflict_now = wm.plan_safe_path(
        start_lat=20.825,
        start_lon=115.740,
        start_depth_m=250.0,
        goal_lat=20.825,
        goal_lon=115.750,
        goal_depth_m=250.0,
        robot_id="ROV-BETA",
        start_time_s=0.0,
        auto_deconflict=False,
    )
    assert len(plan_conflict_now.corridor_conflicts) >= 1
    assert plan_conflict_now.corridor_conflicts[0]["conflict_robot_id"] == "ROV-ALPHA"

    # 3. With auto_deconflict=True -> automated altitude separation
    plan_resolved = wm.plan_safe_path(
        start_lat=20.825,
        start_lon=115.740,
        start_depth_m=250.0,
        goal_lat=20.825,
        goal_lon=115.750,
        goal_depth_m=250.0,
        robot_id="ROV-BETA",
        start_time_s=0.0,
        auto_deconflict=True,
    )
    assert plan_resolved.is_feasible is True
    assert len(plan_resolved.corridor_conflicts) == 0
    assert "高度层分离" in plan_resolved.deconfliction_notes or "Altitude Separation" in plan_resolved.deconfliction_notes
    assert any(wp.get("altitude_separated") is True for wp in plan_resolved.waypoints)


def test_api_dev_corridor_endpoints():
    from web_backend import app
    client = app.test_client()

    # 1. Reserve corridor
    resp = client.post("/api/dev/world-model/corridors/reserve", json={
        "robot_id": "TEST-ROV-999",
        "waypoints": [
            {"lat": 20.800, "lon": 115.700, "depth_m": 200.0},
            {"lat": 20.810, "lon": 115.700, "depth_m": 200.0},
        ],
        "start_time_s": 10.0,
        "cruise_speed_mps": 1.0,
        "radius_m": 20.0,
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["robot_id"] == "TEST-ROV-999"
    assert data["reservations_count"] >= 2

    # 2. List corridors
    list_resp = client.get("/api/dev/world-model/corridors")
    assert list_resp.status_code == 200
    list_data = list_resp.get_json()
    assert list_data["ok"] is True
    assert list_data["corridors_count"] >= 2
    assert any(c["robot_id"] == "TEST-ROV-999" for c in list_data["corridors"])

    # 3. Clear corridors
    clear_resp = client.post("/api/dev/world-model/corridors/clear")
    assert clear_resp.status_code == 200
    assert clear_resp.get_json()["ok"] is True

    # 4. Confirm empty
    list_empty = client.get("/api/dev/world-model/corridors")
    assert list_empty.get_json()["corridors_count"] == 0
