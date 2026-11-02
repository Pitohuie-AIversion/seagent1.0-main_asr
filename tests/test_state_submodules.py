"""
tests/test_state_submodules.py - 机器人状态子模块（机队选择器与可用性检查器）单元测试
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest
import yaml

from src.state import (
    load_fleet,
    matching_family_refs,
    matching_unit_refs,
    matching_variant_refs,
    normalize_selector,
    resolve_status_ref_from_snapshot,
    unit_status_ref,
    inspect_robot_availability,
    parse_bool,
    ROBOT_STATE_MAX_AGE_SECONDS,
    TELEMETRY_MAX_FUTURE_SKEW_SECONDS,
)
from src.state_info import RobotStateInfo
from src.exceptions import StateSnapshotValidationError


class TestFleetSelectorSubmodule:
    """验证 fleet_selector 独立模块的核心逻辑"""

    def test_normalize_selector(self):
        assert normalize_selector(" WROV-250-001 ") == "wrov-250-001"
        assert normalize_selector("工作级 ROV") == "工作级rov"
        assert normalize_selector(None) == ""

    def test_unit_status_ref(self):
        assert unit_status_ref({"unit_id": "U1", "status_ref": "REF1"}) == "REF1"
        assert unit_status_ref({"unit_id": "U1"}) == "U1"
        assert unit_status_ref({}) is None

    def test_matching_unit_refs(self):
        units = [
            {
                "unit_id": "WROV-250-001",
                "display_name": "海马01号",
                "serial_no": "SN-2023-01",
                "status_ref": "WROV-250-001",
                "aliases": ["海马一号", "HM-1"],
            },
            {
                "unit_id": "LROV-150-001",
                "display_name": "轻型01号",
                "status_ref": "LROV-150-001",
            },
        ]
        assert matching_unit_refs(units, "wrov-250-001") == {"WROV-250-001"}
        assert matching_unit_refs(units, "海马01号") == {"WROV-250-001"}
        assert matching_unit_refs(units, "hm-1") == {"WROV-250-001"}
        assert matching_unit_refs(units, "sn-2023-01") == {"WROV-250-001"}
        assert matching_unit_refs(units, "nonexistent") == set()

    def test_load_fleet_validation_error(self, tmp_path):
        bad_file = tmp_path / "bad_fleet.yaml"
        bad_file.write_text("not_a_mapping", encoding="utf-8")
        with pytest.raises(StateSnapshotValidationError):
            load_fleet(bad_file)


class TestRuntimeCheckerSubmodule:
    """验证 runtime_checker 独立模块的严格指标校验与 Fail-Closed 决策"""

    def test_parse_bool(self):
        assert parse_bool(True) is True
        assert parse_bool(False) is False
        assert parse_bool(1) is True
        assert parse_bool(0) is False
        assert parse_bool("true") is True
        assert parse_bool("False") is False
        assert parse_bool("ONLINE") is True
        assert parse_bool("offline") is False
        assert parse_bool("unknown") is None

    def test_inspect_robot_availability_unit_not_found(self):
        res = inspect_robot_availability(
            "",
            matched_unit=None,
            state=None,
        )
        assert res["available"] is False
        assert res["reason_code"] == "UNIT_NOT_FOUND"

    def test_inspect_robot_availability_unregistered(self):
        res = inspect_robot_availability(
            "WROV-999-999",
            matched_unit=None,
            state=None,
        )
        assert res["available"] is False
        assert res["reason_code"] == "UNIT_NOT_FOUND"
        assert "未在系统中注册" in res["message"]

    def test_inspect_robot_availability_state_not_found(self):
        matched = {"unit_id": "WROV-250-001", "status_ref": "WROV-250-001"}
        res = inspect_robot_availability(
            "WROV-250-001",
            matched_unit=matched,
            state=None,
        )
        assert res["available"] is False
        assert res["reason_code"] == "STATE_NOT_FOUND"

    def test_inspect_robot_availability_offline(self):
        now = datetime.now(timezone.utc)
        matched = {"unit_id": "WROV-250-001", "status_ref": "WROV-250-001"}
        state = {
            "is_online": False,
            "overall_status": "offline",
            "updated_at": now.isoformat(),
        }
        res = inspect_robot_availability(
            "WROV-250-001",
            matched_unit=matched,
            state=state,
            now_dt=now,
        )
        assert res["available"] is False
        assert res["reason_code"] == "OFFLINE"

    def test_inspect_robot_availability_busy(self):
        now = datetime.now(timezone.utc)
        matched = {"unit_id": "WROV-250-001", "status_ref": "WROV-250-001"}
        state = {
            "is_online": True,
            "is_busy": True,
            "overall_status": "busy",
            "updated_at": now.isoformat(),
        }
        res = inspect_robot_availability(
            "WROV-250-001",
            matched_unit=matched,
            state=state,
            now_dt=now,
        )
        assert res["available"] is False
        assert res["reason_code"] == "BUSY"

    def test_inspect_robot_availability_expired_ttl(self):
        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(seconds=ROBOT_STATE_MAX_AGE_SECONDS + 10)
        matched = {"unit_id": "WROV-250-001", "status_ref": "WROV-250-001"}
        state = {
            "is_online": True,
            "is_busy": False,
            "overall_status": "available",
            "updated_at": stale_time.isoformat(),
        }
        res = inspect_robot_availability(
            "WROV-250-001",
            matched_unit=matched,
            state=state,
            now_dt=now,
        )
        assert res["available"] is False
        assert res["reason_code"] == "STATE_EXPIRED"

    def test_inspect_robot_availability_future_skew(self):
        now = datetime.now(timezone.utc)
        future_time = now + timedelta(seconds=TELEMETRY_MAX_FUTURE_SKEW_SECONDS + 10)
        matched = {"unit_id": "WROV-250-001", "status_ref": "WROV-250-001"}
        state = {
            "is_online": True,
            "is_busy": False,
            "overall_status": "available",
            "updated_at": future_time.isoformat(),
        }
        res = inspect_robot_availability(
            "WROV-250-001",
            matched_unit=matched,
            state=state,
            now_dt=now,
        )
        assert res["available"] is False
        assert res["reason_code"] == "INVALID_STATE_DATA"
        assert "晚于系统时间" in res["message"]

    def test_inspect_robot_availability_success(self):
        now = datetime.now(timezone.utc)
        matched = {"unit_id": "WROV-250-001", "status_ref": "WROV-250-001"}
        state = {
            "is_online": True,
            "is_busy": False,
            "overall_status": "available",
            "updated_at": now.isoformat(),
        }
        res = inspect_robot_availability(
            "WROV-250-001",
            matched_unit=matched,
            state=state,
            now_dt=now,
        )
        assert res["available"] is True
        assert res["reason_code"] == "AVAILABLE"
        assert "空闲可用" in res["message"]


class TestRobotStateInfoDelegation:
    """验证 RobotStateInfo 对解耦子模块的方法委托完整性与向后兼容性"""

    def test_delegation_methods_exist(self):
        rsi = RobotStateInfo()
        assert hasattr(rsi, "check_runtime_availability")
        assert hasattr(rsi, "resolve_status_ref")
        assert hasattr(rsi, "_load_fleet")
        assert hasattr(rsi, "_matching_unit_refs")
        assert hasattr(rsi, "_matching_variant_refs")
        assert hasattr(rsi, "_matching_family_refs")
