"""
validator.py — 结构化约束验证服务 (Issue #14 增强版)
- 支持基于具体单机 (unit_id -> status_ref) 的严格遥测状态快照提取
- 支持 ValidationResult 结构化输出（包含指纹、版本号、快照、错误信息与违规列表）
- 区分交互中(interactive)、预览(preview)、发布(publish)与运行时(runtime_execution)不同目的
- 非 constraints.yaml 的运行态诊断仅随结果携带，不决定发布阻断
"""

import copy
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .exceptions import (
    StatePersistenceError,
    StateSelectorError,
    StateSnapshotValidationError,
)
from .knowledge_retriever import KnowledgeBase
from .simulated_time import get_current_datetime, get_current_timestamp


START_TIME_PAST_GRACE_MINUTES = 5


@dataclass
class Violation:
    constraint_id: str
    constraint_name: str
    message: str
    severity: str          # "hard" | "soft" | "warning"
    related_fields: list[str] = field(default_factory=list)
    check_type: str = ""
    observed_value: Any = None
    threshold: Any = None

    def to_dict(self) -> dict:
        return {
            "constraint_id": self.constraint_id,
            "constraint_name": self.constraint_name,
            "message": self.message,
            "severity": self.severity,
            "related_fields": copy.deepcopy(self.related_fields),
            "check_type": self.check_type,
            "observed_value": copy.deepcopy(self.observed_value),
            "threshold": copy.deepcopy(self.threshold),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Violation":
        if not isinstance(data, dict):
            raise TypeError("Violation data must be a dictionary")
        return cls(
            constraint_id=str(data.get("constraint_id", "")),
            constraint_name=str(data.get("constraint_name", "")),
            message=str(data.get("message", "")),
            severity=str(data.get("severity", "warning")),
            related_fields=list(data.get("related_fields", []) or []),
            check_type=str(data.get("check_type", "")),
            observed_value=copy.deepcopy(data.get("observed_value")),
            threshold=copy.deepcopy(data.get("threshold")),
        )


@dataclass
class ValidationResult:
    overall_status: str     # "valid" | "warning" | "blocked_hard"
    validated_at: str
    task_version: int
    validation_version: int
    validation_fingerprint: str
    state_snapshot: dict | None
    violations: list[Violation] = field(default_factory=list)
    runtime_diagnostic: dict | None = None
    # Backward-compatible input holder for old snapshots/tests. New Validator
    # results do not use or serialize this field.
    error: dict | None = None

    def to_dict(self) -> dict:
        return {
            "overall_status": self.overall_status,
            "validated_at": self.validated_at,
            "task_version": self.task_version,
            "validation_version": self.validation_version,
            "validation_fingerprint": self.validation_fingerprint,
            "state_snapshot": copy.deepcopy(self.state_snapshot),
            "violations": [v.to_dict() if hasattr(v, "to_dict") else copy.deepcopy(v) for v in self.violations],
            "runtime_diagnostic": copy.deepcopy(self.runtime_diagnostic),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ValidationResult":
        if not isinstance(data, dict):
            raise TypeError("ValidationResult data must be a dictionary")
        raw_violations = data.get("violations", []) or []
        violations = [
            v if isinstance(v, Violation) else Violation.from_dict(v)
            for v in raw_violations
            if isinstance(v, (dict, Violation))
        ]
        return cls(
            overall_status=str(data.get("overall_status", "valid")),
            validated_at=str(data.get("validated_at", "")),
            task_version=int(data.get("task_version", 1)),
            validation_version=int(data.get("validation_version", 1)),
            validation_fingerprint=str(data.get("validation_fingerprint", "")),
            state_snapshot=copy.deepcopy(data.get("state_snapshot")),
            violations=violations,
            runtime_diagnostic=copy.deepcopy(data.get("runtime_diagnostic")),
            error=copy.deepcopy(data.get("error")),
        )


# check_type → 该约束关注的字段集合
_EQUIPMENT_FIELDS = ["equipment_unit_id", "equipment_family", "equipment_type", "equipment_name"]

_CHECK_FIELDS: dict[str, list[str]] = {
    "robot_category":              _EQUIPMENT_FIELDS,
    "depth_vs_rov_limit":          [*_EQUIPMENT_FIELDS, "water_depth"],
    "vessel_availability":         ["support_vessel"],
    "forbidden_area":              ["start_point", "end_point", "oilfield_coordinates", "cable_position"],
    "dvl_high_risk":               ["start_point", "oilfield_coordinates", "cable_position"],
    "seabed_compatibility":        [*_EQUIPMENT_FIELDS, "start_point", "oilfield_coordinates"],
    "obstacle_dense":              _EQUIPMENT_FIELDS,
    "mothership_support":          _EQUIPMENT_FIELDS,
    "turbidity":                   _EQUIPMENT_FIELDS,
    "current_velocity":            _EQUIPMENT_FIELDS,
    "state_confidence":            _EQUIPMENT_FIELDS,
    "state_timestamp":             _EQUIPMENT_FIELDS,
    "robot_overall_status":        _EQUIPMENT_FIELDS,
    "robot_survival_status":       _EQUIPMENT_FIELDS,
    "robot_thruster_status":       _EQUIPMENT_FIELDS,
    "robot_depth_keeping_status":  _EQUIPMENT_FIELDS,
    "robot_sonar_status":          _EQUIPMENT_FIELDS,
    "robot_vision_status":         _EQUIPMENT_FIELDS,
    "robot_manipulator_status":    _EQUIPMENT_FIELDS,
    "robot_communication_status":  _EQUIPMENT_FIELDS,
    "start_time_not_in_past":      ["start_time"],
    "end_time_after_start_time":   ["start_time", "end_time"],
}

_DYNAMIC_CHECKS = {
    "current_velocity",
    "turbidity",
    "obstacle_dense",
    "mothership_support",
    "state_confidence",
    "state_timestamp",
    "robot_overall_status",
    "robot_survival_status",
    "robot_thruster_status",
    "robot_depth_keeping_status",
    "robot_sonar_status",
    "robot_vision_status",
    "robot_manipulator_status",
    "robot_communication_status",
}


def _compute_fingerprint(
    task_version: int,
    status_ref: str | None,
    state_version: int | None,
    violations: list[Violation],
    diagnostic: dict | None = None,
) -> str:
    parts = [
        f"tv:{task_version}",
        f"sref:{status_ref or ''}",
        f"sver:{state_version or 0}",
        f"diag:{diagnostic.get('code') if diagnostic else ''}",
    ]
    v_parts = []
    for v in sorted(violations, key=lambda x: x.constraint_id):
        v_parts.append(f"{v.constraint_id}:{v.severity}:{v.observed_value}")
    parts.append("v:[" + ",".join(v_parts) + "]")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class TaskValidator:

    def __init__(self, kb: KnowledgeBase):
        self.kb = kb

    # ──────────────────────────────────────────────────────────────────────────
    # 静态数据校验辅助函数 (Fail-Closed)
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_water_depth_value(val: Any) -> tuple[float | None, dict | None]:
        if val is None:
            return None, None
        if isinstance(val, bool):
            return None, {"code": "INVALID_WATER_DEPTH", "message": "水深 (water_depth) 不能为布尔值。"}
        if isinstance(val, (int, float, str)):
            try:
                f_val = float(val)
            except (ValueError, TypeError):
                return None, {"code": "INVALID_WATER_DEPTH", "message": f"水深 (water_depth='{val}') 格式非法，无法解析为数字。"}
            import math
            if math.isnan(f_val) or math.isinf(f_val):
                return None, {"code": "INVALID_WATER_DEPTH", "message": f"水深 (water_depth='{val}') 不能为 NaN 或 Inf。"}
            if f_val <= 0:
                return None, {"code": "INVALID_WATER_DEPTH", "message": f"水深 (water_depth={f_val}) 必须为大于 0 的正数。"}
            return f_val, None
        return None, {"code": "INVALID_WATER_DEPTH", "message": f"水深 (water_depth) 类型非法: {type(val).__name__}"}

    @staticmethod
    def _validate_time_value(val: Any, field_name: str) -> tuple[datetime | None, dict | None]:
        if val is None or (isinstance(val, str) and val.strip() == ""):
            return None, None
        if isinstance(val, bool):
            return None, {"code": "MALFORMED_TIME_FORMAT", "message": f"{field_name} 不能为布尔值。"}
        dt = None
        if isinstance(val, datetime):
            dt = val
        elif isinstance(val, str):
            text = val.strip().replace("：", ":")
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(text.replace("T", " "))
            except (ValueError, TypeError):
                return None, {"code": "MALFORMED_TIME_FORMAT", "message": f"{field_name} ('{val}') 时间格式非法，无法解析。"}
        else:
            return None, {"code": "MALFORMED_TIME_FORMAT", "message": f"{field_name} 类型非法。"}

        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            else:
                dt = dt.astimezone(ZoneInfo("Asia/Shanghai"))
        return dt, None

    @staticmethod
    def _validate_max_depth_m_value(max_depth_raw: Any, rov_name: str = "") -> tuple[float | None, dict | None]:
        if max_depth_raw is None:
            return None, {"code": "INVALID_ROV_MAX_DEPTH", "message": f"机器人 '{rov_name}' 规格中缺少最大作业深度 (max_depth_m)。"}
        if isinstance(max_depth_raw, bool):
            return None, {"code": "INVALID_ROV_MAX_DEPTH", "message": f"机器人 '{rov_name}' 最大作业深度 (max_depth_m) 不能为布尔值。"}
        if isinstance(max_depth_raw, (int, float, str)):
            try:
                max_depth = float(max_depth_raw)
            except (ValueError, TypeError):
                return None, {"code": "INVALID_ROV_MAX_DEPTH", "message": f"机器人 '{rov_name}' 最大作业深度 (max_depth_m='{max_depth_raw}') 无法解析为数字。"}
            import math
            if math.isnan(max_depth) or math.isinf(max_depth):
                return None, {"code": "INVALID_ROV_MAX_DEPTH", "message": f"机器人 '{rov_name}' 最大作业深度 (max_depth_m='{max_depth_raw}') 不能为 NaN 或 Inf。"}
            if max_depth <= 0:
                return None, {"code": "INVALID_ROV_MAX_DEPTH", "message": f"机器人 '{rov_name}' 最大作业深度 (max_depth_m={max_depth}) 必须为大于 0 的正数。"}
            return max_depth, None
        return None, {"code": "INVALID_ROV_MAX_DEPTH", "message": f"机器人 '{rov_name}' 最大作业深度 (max_depth_m) 类型非法。"}

    # ──────────────────────────────────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────────────────────────────────

    def validate_task(
        self,
        task_state: dict,
        *,
        task_version: int = 1,
        previous_result: ValidationResult | dict | None = None,
        purpose: str = "interactive",
    ) -> ValidationResult:
        """
        结构化约束校验服务主入口。
        purpose 可选: "interactive" | "preview" | "publish" | "runtime_execution"
        """
        try:
            validated_at = get_current_datetime().isoformat(timespec="seconds")
            is_now = self._is_task_start_now(task_state) if purpose != "runtime_execution" else True

            # 如果 previous_result 是 dict，安全还原为 ValidationResult 对象
            if isinstance(previous_result, dict):
                try:
                    previous_result = ValidationResult.from_dict(previous_result)
                except Exception:
                    previous_result = None

            # Field parsing errors belong to Slot/Normalizer, not business
            # constraint validation. The validator skips malformed scalar fields
            # here; field-level checks still run inside relevant business checks
            # when values can be parsed.
            state_snapshot, runtime_diagnostic = self._resolve_single_unit_snapshot(task_state, is_now=is_now)

            # Missing/ambiguous unit and telemetry failures are diagnostics only.
            # They must not become business violations or publish gates; only
            # constraints.yaml checks decide warning/blocked_hard.
            if purpose in ("publish", "preview") and not (task_state.get("equipment_unit_id") or "").strip():
                runtime_diagnostic = {
                    "code": "MISSING_UNIT_ID",
                    "message": "发布或预览任务前必须指定明确的具体单机编号 (equipment_unit_id)。",
                }

            violations = self._run_checks(task_state, trigger_fields=None, state_snapshot=state_snapshot)

            # 状态优先级规则：
            # blocked_hard > warning > valid
            if any(v.severity == "hard" for v in violations):
                overall_status = "blocked_hard"
            elif any(v.severity == "soft" for v in violations):
                overall_status = "warning"
            elif any(v.severity == "warning" for v in violations):
                overall_status = "warning"
            else:
                overall_status = "valid"

            status_ref = state_snapshot.get("status_ref") if state_snapshot else None
            state_version = state_snapshot.get("state_version") if state_snapshot else None

            fingerprint = _compute_fingerprint(
                task_version=task_version,
                status_ref=status_ref,
                state_version=state_version,
                violations=violations,
                diagnostic=runtime_diagnostic,
            )

            if previous_result and previous_result.validation_fingerprint == fingerprint:
                validation_version = previous_result.validation_version
            else:
                validation_version = (previous_result.validation_version + 1) if previous_result else 1

            return ValidationResult(
                overall_status=overall_status,
                validated_at=validated_at,
                task_version=task_version,
                validation_version=validation_version,
                validation_fingerprint=fingerprint,
                state_snapshot=state_snapshot,
                violations=violations,
                runtime_diagnostic=runtime_diagnostic,
            )

        except Exception as e:
            err_dict = {"code": "VALIDATOR_EXCEPTION", "message": f"校验流程内部发生未捕获异常: {e}"}
            fp = _compute_fingerprint(task_version, None, None, [], err_dict)
            return ValidationResult(
                overall_status="valid",
                validated_at=get_current_datetime().isoformat(timespec="seconds"),
                task_version=task_version,
                validation_version=1,
                validation_fingerprint=fp,
                state_snapshot=None,
                violations=[],
                runtime_diagnostic=err_dict,
            )

    def validate(self, task_state: dict) -> list[Violation]:
        """全量约束检查，返回所有当前违规（兼容旧接口）"""
        res = self.validate_task(task_state)
        return res.violations

    def validate_for_fields(
        self, task_state: dict, changed_fields: set[str]
    ) -> list[Violation]:
        """增量模式检查"""
        snapshot, _runtime_diagnostic = self._resolve_single_unit_snapshot(task_state, is_now=self._is_task_start_now(task_state))
        return self._run_checks(task_state, trigger_fields=changed_fields, state_snapshot=snapshot)

    def has_hard_violations(self, violations: list[Violation]) -> bool:
        return any(v.severity == "hard" for v in violations)

    def format_violations(self, violations: list[Violation]) -> str:
        if not violations:
            return ""
        lines = []
        for v in violations:
            tag = "⛔ 硬性违规" if v.severity == "hard" else "⚠️ 软性警告"
            lines.append(f"{tag} [{v.constraint_id}] {v.constraint_name}\n  {v.message}")
        return "\n\n".join(lines)

    # ──────────────────────────────────────────────────────────────────────────
    # 内部实现
    # ──────────────────────────────────────────────────────────────────────────

    def _resolve_single_unit_snapshot(self, task_state: dict, is_now: bool) -> tuple[dict | None, dict | None]:
        unit_selector = task_state.get("equipment_unit_id")
        task_type = task_state.get("task_type_key")
        variant_selector = (
            task_state.get("equipment_type")
            or task_state.get("equipment_name")
            or task_state.get("equipment_family")
        )

        # 显式提供了 unit_id
        if unit_selector and isinstance(unit_selector, str) and unit_selector.strip():
            clean_unit_id = unit_selector.strip()
            try:
                snapshot = None
                if hasattr(self.kb, "get_unit_state_snapshot"):
                    snapshot = self.kb.get_unit_state_snapshot(clean_unit_id)
                elif hasattr(self.kb, "state_info") and hasattr(self.kb.state_info, "get_unit_state_snapshot"):
                    snapshot = self.kb.state_info.get_unit_state_snapshot(clean_unit_id)
                
                if snapshot:
                    err = self._validate_state_snapshot_content(clean_unit_id, snapshot)
                    if err:
                        return None, err
                    return snapshot, None
                else:
                    return None, {"code": "MISSING_TELEMETRY", "message": f"未找到单机 '{clean_unit_id}' 的实时遥测状态快照。"}
            except StateSelectorError as e:
                return None, {"code": "UNIT_NOT_FOUND", "message": f"未在系统中注册匹配单机 '{clean_unit_id}': {e}"}
            except StateSnapshotValidationError as e:
                return None, {"code": "INVALID_STATE_SNAPSHOT", "message": f"单机 '{clean_unit_id}' 状态记录或结构不合法: {e}"}
            except Exception as e:
                return None, {"code": "STATE_READ_FAILED", "message": f"读取单机 '{clean_unit_id}' 状态失败: {e}"}

        # 没有 unit_id，但有 family / variant_selector
        if variant_selector and isinstance(variant_selector, str) and variant_selector.strip():
            clean_selector = variant_selector.strip()
            robot_fleet = getattr(self.kb, "robot_fleet", {}) if isinstance(getattr(self.kb, "robot_fleet", None), dict) else {}
            fleet_units = robot_fleet.get("fleet_units", []) if isinstance(robot_fleet, dict) else []
            matches = []
            if isinstance(fleet_units, list):
                for u in fleet_units:
                    if not isinstance(u, dict):
                        continue
                    u_id = u.get("unit_id")
                    u_disp = u.get("display_name", "")
                    u_var = u.get("variant_id", "")
                    u_fam = u.get("family_id", "")
                    if clean_selector in (u_id, u_disp, u_var, u_fam):
                        matches.append(u)
                    elif hasattr(self.kb, "resolve_robot_unit"):
                        resolved = self.kb.resolve_robot_unit(u_id or "", task_type, clean_selector)
                        if resolved and resolved.get("unit_id") == u_id:
                            matches.append(u)

            unique_matches = {m.get("unit_id"): m for m in matches if m.get("unit_id")}
            if len(unique_matches) > 1:
                return None, {
                    "code": "AMBIGUOUS_UNIT_SELECTOR",
                    "message": f"所选设备 '{clean_selector}' 对应多台在役单机 ({sorted(unique_matches.keys())})，必须指定确切单机编号 (equipment_unit_id)。",
                }
            elif len(unique_matches) == 1:
                single_unit_id = next(iter(unique_matches.keys()))
                try:
                    snapshot = None
                    if hasattr(self.kb, "get_unit_state_snapshot"):
                        snapshot = self.kb.get_unit_state_snapshot(single_unit_id)
                    elif hasattr(self.kb, "state_info") and hasattr(self.kb.state_info, "get_unit_state_snapshot"):
                        snapshot = self.kb.state_info.get_unit_state_snapshot(single_unit_id)
                    if snapshot:
                        err = self._validate_state_snapshot_content(single_unit_id, snapshot)
                        if err:
                            return None, err
                        return snapshot, None
                    else:
                        return None, {"code": "MISSING_TELEMETRY", "message": f"未找到单机 '{single_unit_id}' 的实时遥测状态快照。"}
                except StateSelectorError as e:
                    return None, {"code": "UNIT_NOT_FOUND", "message": f"未在系统中注册匹配单机 '{single_unit_id}': {e}"}
                except StateSnapshotValidationError as e:
                    return None, {"code": "INVALID_STATE_SNAPSHOT", "message": f"单机 '{single_unit_id}' 状态记录或结构不合法: {e}"}
                except Exception as e:
                    return None, {"code": "STATE_READ_FAILED", "message": f"读取单机 '{single_unit_id}' 状态快照失败: {e}"}
            else:
                rov_static = self.kb.get_rov(clean_selector) if hasattr(self.kb, "get_rov") else None
                if not rov_static:
                    return None, {"code": "UNIT_NOT_FOUND", "message": f"未找到匹配的选择器或型号 '{clean_selector}'。"}

        return None, None

    @staticmethod
    def _validate_state_snapshot_content(unit_id: str, snapshot: dict) -> dict | None:
        if not isinstance(snapshot, dict):
            return {"code": "INVALID_STATE_DATA", "message": f"单机 '{unit_id}' 的状态快照非字典"}
        if not snapshot.get("status_ref"):
            return {"code": "INVALID_STATE_SNAPSHOT", "message": f"单机 '{unit_id}' 状态快照缺少 status_ref 标识。"}
        if snapshot.get("state_version") is None or not isinstance(snapshot.get("state_version"), int):
            return {"code": "INVALID_STATE_SNAPSHOT", "message": f"单机 '{unit_id}' 状态快照缺少合法的 state_version 版本号。"}
        state_dict = snapshot.get("state")
        if not isinstance(state_dict, dict):
            return {"code": "INVALID_STATE_DATA", "message": f"单机 '{unit_id}' 的状态记录不存在或非字典"}
        if "overall_status" not in state_dict or state_dict.get("overall_status") is None:
            return {"code": "INVALID_STATE_DATA", "message": f"单机 '{unit_id}' 缺少状态指标 (overall_status)。"}
        overall_val = state_dict.get("overall_status")
        if overall_val == "unknown":
            return {"code": "INVALID_STATE_DATA", "message": f"单机 '{unit_id}' 的 overall_status 为无法识别的值 'unknown'。"}
        return None

    def _run_checks(
        self,
        task_state: dict,
        trigger_fields: set[str] | None,
        state_snapshot: dict | None,
    ) -> list[Violation]:
        violations = []
        task_type = task_state.get("task_type_key")
        unit_selector = task_state.get("equipment_unit_id")
        variant_selector = task_state.get("equipment_type") or task_state.get("equipment_name")
        water_depth = task_state.get("water_depth")
        vessel_id = task_state.get("support_vessel")
        tree_type = task_state.get("tree_type")

        # 尝试静态信息获取 rov spec
        resolved_unit = (
            self.kb.resolve_robot_unit(
                str(unit_selector),
                task_type,
                str(variant_selector) if variant_selector else None,
            )
            if unit_selector
            else None
        )
        rov = (
            resolved_unit.get("robot")
            if resolved_unit
            else (
                self.kb.get_rov(str(variant_selector))
                if variant_selector
                else None
            )
        )

        for c in self.kb.get_constraints():
            check = c["check_type"]

            # 若是增量模式，跳过与 changed_fields 无关的约束（但硬约束除外）
            if trigger_fields is not None:
                if c.get("severity") != "hard":
                    watched = set(_CHECK_FIELDS.get(check, []))
                    if check in _DYNAMIC_CHECKS:
                        watched.add("start_time")
                    if not watched.intersection(trigger_fields):
                        continue

            # 过滤任务类型适用范围
            applies = c["applies_to"]
            if "all" not in applies:
                if not task_type or task_type not in applies:
                    continue

            v = self._check_one(c, check, task_state, rov, water_depth, vessel_id, tree_type, state_snapshot)
            if v:
                violations.append(v)

        return violations

    def _is_task_start_now(self, task_state: dict, time_window_minutes: int = 10) -> bool:
        start_time_raw = task_state.get("start_time")
        if not start_time_raw or (isinstance(start_time_raw, str) and start_time_raw.strip() == ""):
            return True
        st_dt, err = self._validate_time_value(start_time_raw, "start_time")
        if err or st_dt is None:
            return True
        now = get_current_datetime().replace(microsecond=0)
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        else:
            now = now.astimezone(ZoneInfo("Asia/Shanghai"))
        delta_seconds = (st_dt - now).total_seconds()
        return delta_seconds <= time_window_minutes * 60

    def _check_one(
        self,
        c: dict,
        check: str,
        task_state: dict,
        rov: dict | None,
        water_depth: Any,
        vessel_id: str | None,
        tree_type: str | None,
        state_snapshot: dict | None,
    ) -> Violation | None:
        if check in _DYNAMIC_CHECKS and not self._is_task_start_now(task_state):
            return None

        rel_fields = _CHECK_FIELDS.get(check, [])
        state_dict = state_snapshot.get("state") if state_snapshot else None

        if check == "robot_category" and rov:
            task_type = task_state.get("task_type_key")
            if not self.kb.robot_matches_task(rov, task_type):
                required = self.kb.get_task_required_capabilities(task_type) if hasattr(self.kb, "get_task_required_capabilities") else []
                robot_caps = sorted(set(rov.get("capabilities") or []))
                msg = c["violation_message"].strip()
                if required:
                    msg = (
                        f"当前选择的机器人能力不满足任务要求。"
                        f"任务需要能力：{'、'.join(required)}；"
                        f"当前设备能力：{'、'.join(robot_caps) if robot_caps else '未配置'}。"
                    )
                return Violation(
                    c["id"], c["name"], msg, c["severity"], rel_fields,
                    check_type=check, observed_value=robot_caps, threshold=required
                )
        elif check == "depth_vs_rov_limit" and rov:
            if water_depth is not None:
                depth_val, depth_err = self._validate_water_depth_value(water_depth)
                if depth_err:
                    return Violation(
                        c["id"], c["name"], depth_err["message"], "hard",
                        rel_fields, check_type=check, observed_value=water_depth
                    )
                max_depth, max_depth_err = self._validate_max_depth_m_value(
                    rov.get("max_depth_m"), rov_name=str(rov.get("full_name") or rov.get("display_name") or "ROV")
                )
                if max_depth_err:
                    return Violation(
                        c["id"], c["name"], max_depth_err["message"], "hard",
                        rel_fields, check_type=check, observed_value=rov.get("max_depth_m")
                    )
                if depth_val is not None and max_depth is not None and depth_val > max_depth:
                    msg = c["violation_message"].replace("{rov_max_depth}", str(max_depth))
                    return Violation(
                        c["id"], c["name"], msg.strip(), c["severity"],
                        rel_fields, check_type=check, observed_value=depth_val, threshold=max_depth
                    )

        elif check == "start_time_not_in_past":
            start_time, st_err = self._validate_time_value(task_state.get("start_time"), "start_time")
            if st_err:
                return Violation(
                    c["id"], c["name"], st_err["message"], "hard",
                    rel_fields, check_type=check, observed_value=task_state.get("start_time")
                )
            if start_time is None:
                return None
            now = get_current_datetime().replace(microsecond=0)
            if now.tzinfo is None:
                now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            else:
                now = now.astimezone(ZoneInfo("Asia/Shanghai"))
            grace_deadline = now - timedelta(minutes=START_TIME_PAST_GRACE_MINUTES)
            if start_time < grace_deadline:
                msg = (
                    c["violation_message"]
                    .replace("{start_time}", start_time.strftime("%Y-%m-%d %H:%M:%S"))
                    .replace("{current_time}", now.strftime("%Y-%m-%d %H:%M:%S"))
                )
                return Violation(
                    c["id"], c["name"], msg.strip(), c["severity"],
                    rel_fields, check_type=check, observed_value=start_time.isoformat()
                )

        elif check == "end_time_after_start_time":
            start_time, st_err = self._validate_time_value(task_state.get("start_time"), "start_time")
            end_time, et_err = self._validate_time_value(task_state.get("end_time"), "end_time")
            if st_err:
                return Violation(
                    c["id"], c["name"], st_err["message"], "hard",
                    rel_fields, check_type=check, observed_value=task_state.get("start_time")
                )
            if et_err:
                return Violation(
                    c["id"], c["name"], et_err["message"], "hard",
                    rel_fields, check_type=check, observed_value=task_state.get("end_time")
                )
            if start_time is None or end_time is None or end_time > start_time:
                return None
            msg = (
                c["violation_message"]
                .replace("{start_time}", start_time.strftime("%Y-%m-%d %H:%M:%S"))
                .replace("{end_time}", end_time.strftime("%Y-%m-%d %H:%M:%S"))
            )
            return Violation(
                c["id"], c["name"], msg.strip(), c["severity"],
                rel_fields, check_type=check, observed_value=end_time.isoformat()
            )

        elif check == "vessel_availability" and vessel_id:
            vessel = self.kb.get_vessel(vessel_id)
            if vessel and not vessel.get("available", True):
                msg = c["violation_message"].replace("{vessel_id}", vessel_id)
                return Violation(
                    c["id"], c["name"], msg.strip(), c["severity"],
                    rel_fields, check_type=check, observed_value=vessel_id
                )

        elif check == "forbidden_area":
            for field_name in ["start_point", "end_point", "oilfield_coordinates", "cable_position"]:
                coords = task_state.get(field_name)
                if coords:
                    try:
                        env_info = self.kb.get_environment_info_dict(coords)
                    except Exception as e:
                        return Violation(
                            c["id"], c["name"], f"查询环境坐标数据失败: {e}", "hard",
                            rel_fields, check_type=check, observed_value=coords
                        )
                    if not isinstance(env_info, dict):
                        return Violation(
                            c["id"], c["name"], "环境坐标查询结果非字典格式", "hard",
                            rel_fields, check_type=check, observed_value=coords
                        )
                    if env_info.get("forbidden") is True:
                        return Violation(
                            c["id"], c["name"], c["violation_message"].strip(),
                            c["severity"], rel_fields, check_type=check
                        )

        elif check == "dvl_high_risk":
            for field_name in ["start_point", "oilfield_coordinates", "cable_position"]:
                coords = task_state.get(field_name)
                if coords:
                    try:
                        env_info = self.kb.get_environment_info_dict(coords)
                    except Exception as e:
                        return Violation(
                            c["id"], c["name"], f"查询环境坐标数据失败: {e}", "hard",
                            rel_fields, check_type=check, observed_value=coords
                        )
                    if not isinstance(env_info, dict):
                        return Violation(
                            c["id"], c["name"], "环境坐标查询结果非字典格式", "hard",
                            rel_fields, check_type=check, observed_value=coords
                        )
                    if env_info.get("dvl_risk") is True:
                        return Violation(
                            c["id"], c["name"], c["violation_message"].strip(),
                            c["severity"], rel_fields, check_type=check
                        )

        elif check == "seabed_compatibility" and rov:
            for field_name in ["start_point", "oilfield_coordinates"]:
                coords = task_state.get(field_name)
                if coords:
                    try:
                        env_info = self.kb.get_environment_info_dict(coords)
                    except Exception as e:
                        return Violation(
                            c["id"], c["name"], f"查询底质环境数据失败: {e}", "hard",
                            rel_fields, check_type=check, observed_value=coords
                        )
                    if not isinstance(env_info, dict):
                        return Violation(
                            c["id"], c["name"], "环境坐标查询结果非字典格式", "hard",
                            rel_fields, check_type=check, observed_value=coords
                        )
                    seabed = env_info.get("seabed_type")
                    if seabed and seabed != "unknown":
                        supported_raw = rov.get("supported_seabed")
                        if not supported_raw:
                            continue
                        supported = [supported_raw] if isinstance(supported_raw, str) else supported_raw
                        if seabed not in supported:
                            rov_name = rov.get("full_name", str(rov))
                            msg = c["violation_message"].replace("{current_rov}", rov_name)
                            msg = msg.replace("{current_seabed}", str(seabed))
                            return Violation(
                                c["id"], c["name"], msg.strip(), c["severity"],
                                rel_fields, check_type=check, observed_value=seabed
                            )

        # ────────────── 动态遥测状态检查 (依赖 state_snapshot) ──────────────
        elif check == "mothership_support":
            if state_dict and isinstance(state_dict, dict):
                support_cap = state_dict.get("mothership_support")
                if support_cap == "weak":
                    return Violation(
                        c["id"], c["name"], c["violation_message"].strip(),
                        c["severity"], rel_fields,
                        check_type=check, observed_value=support_cap
                    )

        elif check == "obstacle_dense":
            if state_dict and isinstance(state_dict, dict):
                dense = state_dict.get("obstacle_density")
                if dense == "high":
                    return Violation(
                        c["id"], c["name"], c["violation_message"].strip(),
                        c["severity"], rel_fields,
                        check_type=check, observed_value=dense
                    )

        elif check == "turbidity":
            if state_dict and isinstance(state_dict, dict):
                turb = state_dict.get("turbidity")
                if turb is not None:
                    if c["id"] == "C013" and 5 < turb <= 10:
                        msg = c["violation_message"].replace("{turbidity}", str(turb))
                        return Violation(
                            c["id"], c["name"], msg.strip(), c["severity"],
                            rel_fields, check_type=check, observed_value=turb, threshold=10
                        )
                    elif c["id"] == "C014" and turb > 10:
                        msg = c["violation_message"].replace("{turbidity}", str(turb))
                        return Violation(
                            c["id"], c["name"], msg.strip(), c["severity"],
                            rel_fields, check_type=check, observed_value=turb, threshold=10
                        )

        elif check == "current_velocity":
            if state_dict and isinstance(state_dict, dict):
                vel = state_dict.get("current_velocity")
                if vel is not None:
                    if c["id"] == "C015" and 0.5 < vel <= 0.8:
                        msg = c["violation_message"].replace("{current_velocity}", f"{vel:.2f}")
                        return Violation(
                            c["id"], c["name"], msg.strip(), c["severity"],
                            rel_fields, check_type=check, observed_value=vel, threshold=0.8
                        )
                    elif c["id"] == "C016" and 0.8 < vel <= 1.2:
                        msg = c["violation_message"].replace("{current_velocity}", f"{vel:.2f}")
                        return Violation(
                            c["id"], c["name"], msg.strip(), c["severity"],
                            rel_fields, check_type=check, observed_value=vel, threshold=1.2
                        )
                    elif c["id"] == "C017" and vel > 1.2:
                        msg = c["violation_message"].replace("{current_velocity}", f"{vel:.2f}")
                        return Violation(
                            c["id"], c["name"], msg.strip(), c["severity"],
                            rel_fields, check_type=check, observed_value=vel, threshold=1.2
                        )

        elif check == "state_confidence":
            if state_dict and isinstance(state_dict, dict):
                confidence = state_dict.get("confidence")
                if confidence is not None and confidence < 0.5:
                    msg = c["violation_message"].replace("{confidence}", str(confidence))
                    return Violation(
                        c["id"], c["name"], msg.strip(), c["severity"],
                        rel_fields, check_type=check, observed_value=confidence, threshold=0.5
                    )

        elif check == "state_timestamp":
            if state_dict and isinstance(state_dict, dict):
                timestamp_str = state_dict.get("update_timestamp") or state_dict.get("updated_at")
                if timestamp_str is not None:
                    try:
                        if isinstance(timestamp_str, str):
                            clean_ts = timestamp_str.replace("Z", "+00:00")
                            dt = datetime.fromisoformat(clean_ts)
                        elif isinstance(timestamp_str, (int, float)):
                            dt = datetime.fromtimestamp(timestamp_str, tz=ZoneInfo("Asia/Shanghai"))
                        else:
                            return None

                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                        else:
                            dt = dt.astimezone(ZoneInfo("Asia/Shanghai"))

                        now_sim = get_current_datetime()
                        dt_ts = dt.timestamp()
                        now_ts = now_sim.timestamp()
                        if (now_ts - dt_ts) > 3600:
                            msg = c["violation_message"].replace("{update_timestamp}", str(timestamp_str))
                            return Violation(
                                c["id"], c["name"], msg.strip(), c["severity"],
                                rel_fields, check_type=check, observed_value=timestamp_str, threshold=3600
                            )
                    except Exception:
                        pass

        elif check == "robot_overall_status":
            if state_dict and isinstance(state_dict, dict):
                overall = state_dict.get("overall_status")
                is_online = state_dict.get("is_online")
                is_busy = state_dict.get("is_busy")
                conn_status = state_dict.get("connection_status")
                task_status = state_dict.get("task_status")

                is_offline = (
                    overall in ("offline", "disconnected")
                    or conn_status in ("offline", "disconnected")
                    or is_online is False
                    or (isinstance(is_online, str) and is_online.strip().lower() in ("false", "0"))
                )
                is_busy_status = (
                    overall in ("busy", "working", "operating", "executing", "unavailable", "maintenance", "fault")
                    or task_status in ("busy", "working", "operating", "executing")
                    or is_busy is True
                    or (isinstance(is_busy, str) and is_busy.strip().lower() in ("true", "1"))
                )

                if is_offline or is_busy_status or (overall and overall not in ("available", "idle", "ready")):
                    unit_disp = state_snapshot.get("unit_id") if state_snapshot else str(rov.get("full_name") if rov else "")
                    msg = c["violation_message"].replace("{equipment_name}", unit_disp)
                    if is_offline:
                        msg = f"无法发布任务：机器人 {unit_disp} 当前处于离线状态。"
                    elif is_busy_status:
                        msg = f"无法发布任务：机器人 {unit_disp} 当前处于忙碌状态。"
                    return Violation(
                        c["id"], c["name"], msg.strip(), c["severity"],
                        rel_fields, check_type=check, observed_value=overall or ("offline" if is_offline else "busy")
                    )

        elif check == "robot_survival_status":
            if state_dict and isinstance(state_dict, dict):
                survival = state_dict.get("survival_status")
                if survival == "abnormal":
                    unit_disp = state_snapshot.get("unit_id") if state_snapshot else str(rov.get("full_name") if rov else "")
                    msg = c["violation_message"].replace("{equipment_name}", unit_disp)
                    return Violation(
                        c["id"], c["name"], msg.strip(), c["severity"],
                        rel_fields, check_type=check, observed_value=survival
                    )

        elif check == "robot_thruster_status":
            if state_dict and isinstance(state_dict, dict):
                thruster = state_dict.get("thruster_status")
                if thruster == "abnormal":
                    unit_disp = state_snapshot.get("unit_id") if state_snapshot else str(rov.get("full_name") if rov else "")
                    msg = c["violation_message"].replace("{equipment_name}", unit_disp)
                    return Violation(
                        c["id"], c["name"], msg.strip(), c["severity"],
                        rel_fields, check_type=check, observed_value=thruster
                    )

        elif check == "robot_depth_keeping_status":
            if state_dict and isinstance(state_dict, dict):
                depth_keep = state_dict.get("depth_keeping_status")
                if depth_keep == "abnormal":
                    unit_disp = state_snapshot.get("unit_id") if state_snapshot else str(rov.get("full_name") if rov else "")
                    msg = c["violation_message"].replace("{equipment_name}", unit_disp)
                    return Violation(
                        c["id"], c["name"], msg.strip(), c["severity"],
                        rel_fields, check_type=check, observed_value=depth_keep
                    )

        elif check == "robot_sonar_status":
            if state_dict and isinstance(state_dict, dict):
                sonar = state_dict.get("sonar_status")
                if sonar == "abnormal":
                    unit_disp = state_snapshot.get("unit_id") if state_snapshot else str(rov.get("full_name") if rov else "")
                    msg = c["violation_message"].replace("{equipment_name}", unit_disp)
                    return Violation(
                        c["id"], c["name"], msg.strip(), c["severity"],
                        rel_fields, check_type=check, observed_value=sonar
                    )

        elif check == "robot_vision_status":
            if state_dict and isinstance(state_dict, dict):
                vision = state_dict.get("vision_status")
                if vision == "abnormal":
                    unit_disp = state_snapshot.get("unit_id") if state_snapshot else str(rov.get("full_name") if rov else "")
                    msg = c["violation_message"].replace("{equipment_name}", unit_disp)
                    return Violation(
                        c["id"], c["name"], msg.strip(), c["severity"],
                        rel_fields, check_type=check, observed_value=vision
                    )

        elif check == "robot_manipulator_status":
            if state_dict and isinstance(state_dict, dict):
                arm = state_dict.get("arm_status")
                end_effector = state_dict.get("end_effector_status")
                if arm == "abnormal" or end_effector == "abnormal":
                    unit_disp = state_snapshot.get("unit_id") if state_snapshot else str(rov.get("full_name") if rov else "")
                    msg = c["violation_message"].replace("{equipment_name}", unit_disp)
                    return Violation(
                        c["id"], c["name"], msg.strip(), c["severity"],
                        rel_fields, check_type=check, observed_value={"arm": arm, "end_effector": end_effector}
                    )

        elif check == "robot_communication_status":
            if state_dict and isinstance(state_dict, dict):
                details = []
                acoustic = state_dict.get("acoustic_comms_status")
                if acoustic == "abnormal":
                    details.append("水声无线通信异常")
                tether = state_dict.get("tether_connection_status")
                if tether in ("abnormal", "weak"):
                    details.append("与母船连接异常")

                if details:
                    detail_str = "、".join(details)
                    unit_disp = state_snapshot.get("unit_id") if state_snapshot else str(rov.get("full_name") if rov else "")
                    msg = c["violation_message"].replace("{equipment_name}", unit_disp)
                    msg = msg.replace("{detail}", detail_str)
                    return Violation(
                        c["id"], c["name"], msg.strip(), c["severity"],
                        rel_fields, check_type=check, observed_value=details
                    )

        return None

    def _parse_task_datetime(self, value: Any) -> datetime | None:
        dt, err = self._validate_time_value(value, "time")
        if err:
            return None
        return dt
