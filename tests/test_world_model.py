"""Unit tests for the Subsea 4D World Model, Spatial Safety, and Digital Twin."""

import pytest
from src.world_model import (
    GeodeticPoint,
    GeofenceSeverity,
    GeofenceZone,
    GeofenceZoneType,
    Point3D,
    SubseaAsset,
    SubseaAssetType,
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
