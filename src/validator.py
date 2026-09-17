"""
validator.py — 结构化约束验证服务主入口与门面调度器 (Issue #14 增强版)

职责：
1. 结构化约束检验公共数据契约 (Violation, ValidationResult)；
2. 约束字段映射与动态检查类型元数据 (_CHECK_FIELDS, _DYNAMIC_CHECKS)；
3. 校验状态优先级判定、指纹计算 (_compute_fingerprint) 与版本管理；
4. 调度子模块执行：
   - RuleEngine (src/rule_engine.py)：静态字段合法性、时限与机器人选型可行域校验；
   - SpatialValidator (src/spatial_validator.py)：空间坐标、禁入区与底质安全检查；
   - TelemetryGate (src/telemetry_gate.py)：单机遥测快照解析与运行健康度门禁。
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .knowledge_retriever import KnowledgeBase
from .rule_engine import (
    RuleEngine,
    START_TIME_PAST_GRACE_MINUTES,
)
from .simulated_time import get_current_datetime
from .spatial_validator import (
    SpatialValidator,
    SPATIAL_CHECKS,
)
from .telemetry_gate import (
    TelemetryGate,
    display_threshold as _display_threshold,
    matches_numeric_thresholds as _matches_numeric_thresholds,
)


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
    overall_status: str     # "valid" | "pending_runtime_validation" | "warning" | "blocked_soft" | "blocked_hard" | "validation_error"
    validated_at: str
    task_version: int
    validation_version: int
    validation_fingerprint: str
    state_snapshot: dict | None
    violations: list[Violation] = field(default_factory=list)
    error: dict | None = None
    purpose: str = "preview"

    def to_dict(self) -> dict:
        return {
            "overall_status": self.overall_status,
            "validated_at": self.validated_at,
            "task_version": self.task_version,
            "validation_version": self.validation_version,
            "validation_fingerprint": self.validation_fingerprint,
            "state_snapshot": copy.deepcopy(self.state_snapshot),
            "violations": [v.to_dict() if hasattr(v, "to_dict") else copy.deepcopy(v) for v in self.violations],
            "error": copy.deepcopy(self.error),
            "purpose": self.purpose,
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
            error=copy.deepcopy(data.get("error")),
            purpose=str(data.get("purpose") or "preview"),
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
    "obstacle_dense":              [*_EQUIPMENT_FIELDS, "start_time"],
    "mothership_support":          [*_EQUIPMENT_FIELDS, "start_time"],
    "turbidity":                   [*_EQUIPMENT_FIELDS, "start_time"],
    "current_velocity":            [*_EQUIPMENT_FIELDS, "start_time"],
    "state_confidence":            [*_EQUIPMENT_FIELDS, "start_time"],
    "state_timestamp":             [*_EQUIPMENT_FIELDS, "start_time"],
    "robot_overall_status":        [*_EQUIPMENT_FIELDS, "start_time"],
    "robot_survival_status":       [*_EQUIPMENT_FIELDS, "start_time"],
    "robot_thruster_status":       [*_EQUIPMENT_FIELDS, "start_time"],
    "robot_depth_keeping_status":  [*_EQUIPMENT_FIELDS, "start_time"],
    "robot_sonar_status":          [*_EQUIPMENT_FIELDS, "start_time"],
    "robot_vision_status":         [*_EQUIPMENT_FIELDS, "start_time"],
    "robot_manipulator_status":    [*_EQUIPMENT_FIELDS, "start_time"],
    "robot_communication_status":  [*_EQUIPMENT_FIELDS, "start_time"],
    "start_time_not_in_past":      ["start_time"],
    "end_time_after_start_time":   ["start_time", "end_time"],
    "future_task_runtime_notice":  ["start_time"],
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
    error: dict | None,
) -> str:
    parts = [
        f"tv:{task_version}",
        f"sref:{status_ref or ''}",
        f"sver:{state_version or 0}",
        f"err:{error.get('code') if error else ''}",
    ]
    v_parts = []
    for v in sorted(violations, key=lambda x: x.constraint_id):
        v_parts.append(f"{v.constraint_id}:{v.severity}:{v.observed_value}")
    parts.append("v:[" + ",".join(v_parts) + "]")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class TaskValidator:
    """结构化任务约束验证门面调度器"""

    def __init__(self, kb: KnowledgeBase):
        self.kb = kb
        self.rule_engine = RuleEngine(kb, parent_validator=self)
        self.spatial_validator = SpatialValidator(kb, parent_validator=self)
        self.telemetry_gate = TelemetryGate(kb, parent_validator=self)

    # ──────────────────────────────────────────────────────────────────────────
    # 向后兼容辅助方法代理
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_water_depth_value(val: Any) -> tuple[float | None, dict | None]:
        return RuleEngine.validate_water_depth_value(val)

    @staticmethod
    def _validate_time_value(val: Any, field_name: str) -> tuple[datetime | None, dict | None]:
        return RuleEngine.validate_time_value(val, field_name)

    @staticmethod
    def _validate_max_depth_m_value(max_depth_raw: Any, rov_name: str = "") -> tuple[float | None, dict | None]:
        return RuleEngine.validate_max_depth_m_value(max_depth_raw, rov_name)

    def _parse_task_datetime(self, value: Any) -> datetime | None:
        return self.rule_engine.parse_task_datetime(value)

    def validate_robot_selection_tuple(
        self,
        task_state: dict,
        *,
        require_unit: bool = False,
    ) -> tuple[dict | None, dict | None]:
        return self.rule_engine.validate_robot_selection_tuple(task_state, require_unit=require_unit)

    def _validate_partial_robot_selection_feasibility(
        self,
        task_state: dict,
        canonical_selection: dict | None,
    ) -> dict | None:
        return self.rule_engine.validate_partial_robot_selection_feasibility(task_state, canonical_selection)

    def _validate_concrete_robot_payload_feasibility(
        self,
        task_state: dict,
        canonical_selection: dict | None,
    ) -> dict | None:
        return self.rule_engine.validate_concrete_robot_payload_feasibility(task_state, canonical_selection)

    def _resolve_single_unit_snapshot(
        self,
        task_state: dict,
        is_now: bool,
        purpose: str = "interactive",
    ) -> tuple[dict | None, dict | None]:
        return self.telemetry_gate.resolve_single_unit_snapshot(task_state, is_now, purpose=purpose)

    @staticmethod
    def _validate_state_snapshot_content(unit_id: str, snapshot: dict) -> dict | None:
        return TelemetryGate.validate_state_snapshot_content(unit_id, snapshot)

    def _is_task_start_now(self, task_state: dict, time_window_minutes: int = 60) -> bool:
        return self.telemetry_gate.is_task_start_now(task_state, time_window_minutes=time_window_minutes)

    # ──────────────────────────────────────────────────────────────────────────
    # 公开验证接口
    # ──────────────────────────────────────────────────────────────────────────

    def validate_task(
        self,
        task_state: dict,
        *,
        task_version: int = 1,
        previous_result: ValidationResult | dict | None = None,
        purpose: str = "preview",
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

            # 校验原始输入字段的基础合法性 (water_depth, start_time, end_time)
            error_dict = None
            if error_dict is None and task_state.get("water_depth") is not None:
                _, w_err = self._validate_water_depth_value(task_state.get("water_depth"))
                if w_err:
                    error_dict = w_err

            if error_dict is None and task_state.get("start_time") is not None:
                _, st_err = self._validate_time_value(task_state.get("start_time"), "开始时间 (start_time)")
                if st_err:
                    error_dict = st_err

            if error_dict is None and task_state.get("end_time") is not None:
                _, et_err = self._validate_time_value(task_state.get("end_time"), "结束时间 (end_time)")
                if et_err:
                    error_dict = et_err

            canonical_selection = None
            if error_dict is None:
                canonical_selection, error_dict = self.validate_robot_selection_tuple(
                    task_state,
                    require_unit=purpose in ("publish", "runtime_execution"),
                )

            if (
                purpose in ("interactive", "preview")
                and error_dict is not None
                and error_dict.get("code")
                in {
                    "CLASS_NOT_ALLOWED_FOR_TASK",
                    "FAMILY_CAPABILITY_MISMATCH",
                }
                and (
                    task_state.get("equipment_type") is not None
                    or task_state.get("equipment_unit_id") is not None
                )
            ):
                error_dict = None

            if error_dict is None:
                error_dict = self._validate_partial_robot_selection_feasibility(
                    task_state,
                    canonical_selection,
                )

            if (
                error_dict is None
                and purpose in ("publish", "preview", "runtime_execution")
            ):
                error_dict = self._validate_concrete_robot_payload_feasibility(
                    task_state,
                    canonical_selection,
                )

            # 尝试确定具体单机并提取状态快照
            state_snapshot = None
            if error_dict is None:
                state_snapshot, error_dict = self._resolve_single_unit_snapshot(
                    task_state,
                    is_now=is_now,
                    purpose=purpose,
                )

            violations: list[Violation] = []
            is_future_pending_telemetry = (
                (not is_now or purpose == "interactive")
                and purpose != "runtime_execution"
                and error_dict is not None
                and error_dict.get("code") in ("INVALID_STATE_SNAPSHOT", "MISSING_TELEMETRY", "EXPIRED_TELEMETRY", "INVALID_STATE_DATA", "STATE_READ_FAILED")
            )

            if error_dict is None:
                violations = self._run_checks(task_state, trigger_fields=None, state_snapshot=state_snapshot, purpose=purpose)
            elif is_future_pending_telemetry:
                # 未来任务且已注册单机遥测缺失/过期：不阻断为 validation_error，按 pending_runtime_validation 处理
                violations = self._run_checks(task_state, trigger_fields=None, state_snapshot=None, purpose=purpose)
                error_dict = None
            else:
                # 存在 validation_error 时，不得返回空违规列表
                code = error_dict.get("code") if (error_dict and error_dict.get("code")) else "VAL_ERR"
                payload_feasibility_error = code in {
                    "ROBOT_SELECTION_NOT_FEASIBLE",
                    "INVALID_PAYLOAD_REQUIREMENTS",
                    "INVALID_VARIANT_PAYLOAD_CONFIG",
                }
                feasibility_error = payload_feasibility_error or code in {
                    "NO_FEASIBLE_ROBOT_CANDIDATE",
                    "ROBOT_FEASIBILITY_CHECK_FAILED",
                }
                err_violation = Violation(
                    constraint_id=code,
                    constraint_name=(
                        "机器人载荷可行性校验失败"
                        if payload_feasibility_error
                        else (
                            "机器人候选可行性校验失败"
                            if feasibility_error
                            else "单机状态校验失败"
                        )
                    ),
                    message=error_dict.get("message", "机器人选择校验错误"),
                    severity="hard",
                    related_fields=(
                        ["payload", "equipment_type", "equipment_unit_id"]
                        if payload_feasibility_error
                        else (
                            [
                                "equipment_class",
                                "equipment_family",
                                "water_depth",
                                "payload",
                                "start_time",
                            ]
                            if feasibility_error
                            else ["equipment_unit_id"]
                        )
                    ),
                    check_type="validation_error",
                )
                violations.append(err_violation)

            # 状态优先级规则：
            # validation_error > blocked_hard > blocked_soft > warning > pending_runtime_validation > valid
            blocking_soft_violations = [
                v for v in violations
                if v.severity == "soft" and v.check_type != "future_task_runtime_notice"
            ]
            if error_dict is not None:
                overall_status = "validation_error"
            elif any(v.severity == "hard" for v in violations):
                overall_status = "blocked_hard"
            elif blocking_soft_violations:
                overall_status = "blocked_soft"
            elif any(v.severity == "warning" for v in violations):
                overall_status = "warning"
            elif not is_now:
                overall_status = "pending_runtime_validation"
            else:
                overall_status = "valid"

            status_ref = state_snapshot.get("status_ref") if state_snapshot else None
            state_version = state_snapshot.get("state_version") if state_snapshot else None

            fingerprint = _compute_fingerprint(
                task_version=task_version,
                status_ref=status_ref,
                state_version=state_version,
                violations=violations,
                error=error_dict,
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
                error=error_dict,
                purpose=purpose,
            )

        except Exception as e:
            err_dict = {"code": "VALIDATOR_EXCEPTION", "message": f"校验流程内部发生未捕获异常: {e}"}
            err_v = Violation(
                constraint_id="VALIDATOR_EXCEPTION",
                constraint_name="校验服务异常",
                message=err_dict["message"],
                severity="hard",
                check_type="validation_error",
            )
            fp = _compute_fingerprint(task_version, None, None, [err_v], err_dict)
            return ValidationResult(
                overall_status="validation_error",
                validated_at=get_current_datetime().isoformat(timespec="seconds"),
                task_version=task_version,
                validation_version=1,
                validation_fingerprint=fp,
                state_snapshot=None,
                violations=[err_v],
                error=err_dict,
                purpose=purpose,
            )

    def validate(self, task_state: dict) -> list[Violation]:
        """全量约束检查，返回所有当前违规（兼容旧接口）"""
        res = self.validate_task(task_state, purpose="preview")
        return res.violations

    def preflight_check_candidates(
        self,
        current_task_state: dict,
        new_candidates: dict,
    ) -> list[Violation]:
        """准即时 Pre-flight 冲突预检：在候选提交前模拟合并，提前捕获硬性物理/环境约束违规。"""
        if not new_candidates:
            return []
        draft_state = copy.deepcopy(current_task_state or {})
        draft_state.update(new_candidates)
        violations = self.validate(draft_state)
        return [v for v in violations if getattr(v, "severity", "hard") == "hard"]

    def validate_for_fields(
        self,
        task_state: dict,
        changed_fields: set[str],
        purpose: str = "publish",
    ) -> list[Violation]:
        """增量模式检查"""
        canonical_selection, error_dict = self.validate_robot_selection_tuple(
            task_state
        )
        if (
            error_dict is not None
            and error_dict.get("code")
            in {
                "CLASS_NOT_ALLOWED_FOR_TASK",
                "FAMILY_CAPABILITY_MISMATCH",
            }
            and (
                task_state.get("equipment_type") is not None
                or task_state.get("equipment_unit_id") is not None
            )
        ):
            error_dict = None
        if error_dict is None:
            error_dict = self._validate_partial_robot_selection_feasibility(
                task_state,
                canonical_selection,
            )
        snapshot = None
        if error_dict is None:
            snapshot, error_dict = self._resolve_single_unit_snapshot(
                task_state,
                is_now=self._is_task_start_now(task_state),
                purpose=purpose,
            )
        if error_dict is not None:
            code = error_dict.get("code") if (error_dict and error_dict.get("code")) else "VAL_ERR"
            err_v = Violation(
                constraint_id=code,
                constraint_name="单机状态校验失败",
                message=error_dict.get("message", "单机校验错误"),
                severity="hard",
                related_fields=["equipment_unit_id"],
                check_type="validation_error",
            )
            return [err_v]
        return self._run_checks(task_state, trigger_fields=changed_fields, state_snapshot=snapshot, purpose=purpose)

    def has_hard_violations(self, violations: list[Violation]) -> bool:
        return any(v.severity == "hard" for v in violations)

    def format_violations(self, violations: list[Violation]) -> str:
        if not violations:
            return ""
        lines = []
        for v in violations:
            if v.check_type == "validation_error":
                tag = "⛔ 系统校验错误" if v.constraint_id == "VALIDATOR_EXCEPTION" else "⛔ 数据校验错误"
            else:
                tag = "⛔ 硬性违规" if v.severity == "hard" else "⚠️ 软性警告"
            lines.append(f"{tag} [{v.constraint_id}] {v.constraint_name}\n  {v.message}")
        return "\n\n".join(lines)

    # ──────────────────────────────────────────────────────────────────────────
    # 内部调度实现
    # ──────────────────────────────────────────────────────────────────────────

    def _run_checks(
        self,
        task_state: dict,
        trigger_fields: set[str] | None,
        state_snapshot: dict | None,
        purpose: str = "interactive",
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

            v = self._check_one(
                c, check, task_state, rov, water_depth, vessel_id, tree_type,
                state_snapshot, purpose=purpose
            )
            if v:
                violations.append(v)

        return violations

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
        purpose: str = "publish",
    ) -> Violation | None:
        if check in _DYNAMIC_CHECKS:
            return self.telemetry_gate.check_rule(
                c, check, task_state, rov, state_snapshot, purpose=purpose
            )
        if check in SPATIAL_CHECKS:
            return self.spatial_validator.check_rule(
                c, check, task_state, rov, purpose=purpose
            )
        is_now = self._is_task_start_now(task_state)
        return self.rule_engine.check_rule(
            c, check, task_state, rov, water_depth, vessel_id,
            is_task_start_now=is_now, purpose=purpose
        )
