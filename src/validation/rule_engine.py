"""
src/rule_engine.py — 静态规则与选型约束校验引擎

职责：
1. 基础字段值与时间、水深格式合法性校验（Fail-Closed）；
2. 机器人四级选型关系静态校验 (validate_robot_selection_tuple)；
3. 局部机器人候选可行域预校验 (_validate_partial_robot_selection_feasibility)；
4. 具体机器人载荷能力匹配 (_validate_concrete_robot_payload_feasibility)；
5. 静态规则违规判定（时间顺序、未超期、深度限制、母船可用性、机器人作业类别匹配）。
"""

from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING
from zoneinfo import ZoneInfo

from src.knowledge_retriever import (
    KnowledgeBase,
    RobotSelectionDataError,
    robot_selection_result_contract_error,
)

if TYPE_CHECKING:
    from .validator import Violation

START_TIME_PAST_GRACE_MINUTES = 5


def get_current_datetime() -> datetime:
    """获取当前仿真时间，优先兼容外部针对 src.validation.validator.get_current_datetime 的动态 Mock"""
    import sys
    val_mod = sys.modules.get("src.validation.validator")
    if val_mod is not None:
        gcd = getattr(val_mod, "get_current_datetime", None)
        if gcd is not None and callable(gcd):
            try:
                return gcd()
            except Exception:
                pass
    from src.temporal.simulated_time import get_current_datetime as _gcd
    return _gcd()


class RuleEngine:
    """静态规则与选型约束校验引擎"""

    def __init__(self, kb: KnowledgeBase, parent_validator: Any = None):
        self.kb = kb
        self.parent_validator = parent_validator

    # ──────────────────────────────────────────────────────────────────────────
    # 静态数据校验辅助函数 (Fail-Closed)
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def validate_water_depth_value(val: Any) -> tuple[float | None, dict | None]:
        if val is None:
            return None, None
        if isinstance(val, bool):
            return None, {"code": "INVALID_WATER_DEPTH", "message": "水深 (water_depth) 不能为布尔值。"}
        if isinstance(val, (int, float, str)):
            try:
                f_val = float(val)
            except (ValueError, TypeError):
                return None, {"code": "INVALID_WATER_DEPTH", "message": f"水深 (water_depth='{val}') 格式非法，无法解析为数字。"}
            if math.isnan(f_val) or math.isinf(f_val):
                return None, {"code": "INVALID_WATER_DEPTH", "message": f"水深 (water_depth='{val}') 不能为 NaN 或 Inf。"}
            if f_val <= 0:
                return None, {"code": "INVALID_WATER_DEPTH", "message": f"水深 (water_depth={f_val}) 必须为大于 0 的正数。"}
            return f_val, None
        return None, {"code": "INVALID_WATER_DEPTH", "message": f"水深 (water_depth) 类型非法: {type(val).__name__}"}

    @staticmethod
    def validate_time_value(val: Any, field_name: str) -> tuple[datetime | None, dict | None]:
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
                dt = datetime.fromisoformat(text)
            except (ValueError, TypeError):
                for fmt in (
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M",
                    "%Y/%m/%d %H:%M:%S",
                    "%Y/%m/%d %H:%M",
                    "%Y-%m-%d",
                ):
                    try:
                        dt = datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        pass
        if dt is None:
            return None, {"code": "MALFORMED_TIME_FORMAT", "message": f"{field_name} 格式非法 ('{val}')，无法解析为标准时间戳。"}
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        else:
            dt = dt.astimezone(ZoneInfo("Asia/Shanghai"))
        return dt, None

    @staticmethod
    def validate_max_depth_m_value(max_depth_raw: Any, rov_name: str = "") -> tuple[float | None, dict | None]:
        if max_depth_raw is None:
            return None, {"code": "INVALID_ROV_MAX_DEPTH", "message": f"机器人 '{rov_name}' 缺少最大作业深度 (max_depth_m) 配置。"}
        if isinstance(max_depth_raw, bool):
            return None, {"code": "INVALID_ROV_MAX_DEPTH", "message": f"机器人 '{rov_name}' 最大作业深度 (max_depth_m) 不能为布尔值。"}
        if isinstance(max_depth_raw, (int, float, str)):
            try:
                max_depth = float(max_depth_raw)
            except (ValueError, TypeError):
                return None, {"code": "INVALID_ROV_MAX_DEPTH", "message": f"机器人 '{rov_name}' 最大作业深度 (max_depth_m='{max_depth_raw}') 格式非法，无法解析为数字。"}
            if math.isnan(max_depth) or math.isinf(max_depth):
                return None, {"code": "INVALID_ROV_MAX_DEPTH", "message": f"机器人 '{rov_name}' 最大作业深度 (max_depth_m='{max_depth_raw}') 不能为 NaN 或 Inf。"}
            if max_depth <= 0:
                return None, {"code": "INVALID_ROV_MAX_DEPTH", "message": f"机器人 '{rov_name}' 最大作业深度 (max_depth_m={max_depth}) 必须为大于 0 的正数。"}
            return max_depth, None
        return None, {"code": "INVALID_ROV_MAX_DEPTH", "message": f"机器人 '{rov_name}' 最大作业深度 (max_depth_m) 类型非法。"}

    def parse_task_datetime(self, value: Any) -> datetime | None:
        dt, err = self.validate_time_value(value, "time")
        if err:
            return None
        return dt

    # ──────────────────────────────────────────────────────────────────────────
    # 机器人选型静态关系与可行域校验
    # ──────────────────────────────────────────────────────────────────────────

    def validate_robot_selection_tuple(
        self,
        task_state: dict,
        *,
        require_unit: bool = False,
    ) -> tuple[dict | None, dict | None]:
        """Validate a complete robot tuple without reading runtime telemetry."""
        if self.kb is None:
            return None, None
        validator = getattr(self.kb, "validate_robot_selection_from_task_state", None)
        if not callable(validator):
            has_robot_fields = any(
                task_state.get(k) is not None
                for k in ("equipment_type", "equipment_name", "equipment_unit_id", "equipment_class", "robot_class")
            )
            if (require_unit or task_state.get("equipment_unit_id") is not None) and has_robot_fields:
                return None, {
                    "code": "STATIC_ROBOT_VALIDATOR_UNAVAILABLE",
                    "message": "机器人四级静态校验器不可用，无法安全继续。",
                }
            return None, None
        try:
            selection = validator(task_state, require_unit=require_unit)
            missing_key = robot_selection_result_contract_error(
                task_state,
                selection,
                require_unit=require_unit,
            )
            if missing_key is not None:
                return None, {
                    "code": "STATIC_ROBOT_VALIDATOR_FAILURE",
                    "message": (
                        "机器人四级静态校验器返回结果不完整，"
                        f"缺少规范字段 {missing_key}。"
                    ),
                }
            return selection, None
        except RobotSelectionDataError as exc:
            return None, {
                "code": exc.error_code,
                "message": f"机器人四级选择关系不一致：{exc}",
                "details": exc.to_dict(),
            }
        except Exception as exc:
            return None, {
                "code": "STATIC_ROBOT_VALIDATOR_FAILURE",
                "message": f"机器人四级静态校验失败：{exc}",
            }

    def validate_partial_robot_selection_feasibility(
        self,
        task_state: dict,
        canonical_selection: dict | None,
    ) -> dict | None:
        """Fail closed when an explicit Class/Family has no feasible child."""
        if not isinstance(task_state, dict) or not isinstance(
            canonical_selection,
            dict,
        ):
            return None
        if task_state.get("equipment_type") is not None or task_state.get(
            "equipment_unit_id"
        ) is not None:
            return None

        has_explicit_class = task_state.get("equipment_class") is not None
        has_explicit_family = task_state.get("equipment_family") is not None
        task_type_key = task_state.get("task_type_key")
        if not task_type_key or not (has_explicit_class or has_explicit_family):
            return None

        domain_builder = getattr(
            self.kb,
            "get_feasible_robot_selection_domain",
            None,
        )
        if not callable(domain_builder):
            return None

        try:
            domain = domain_builder(task_type_key, task_state)
        except RobotSelectionDataError as exc:
            return {
                "code": exc.error_code,
                "message": f"机器人候选域校验失败：{exc}",
                "details": exc.to_dict(),
            }
        except Exception as exc:
            return {
                "code": "ROBOT_FEASIBILITY_CHECK_FAILED",
                "message": f"机器人候选域计算失败：{exc}",
            }

        if not isinstance(domain, dict) or not isinstance(
            domain.get("classes"),
            list,
        ):
            return {
                "code": "ROBOT_FEASIBILITY_CHECK_FAILED",
                "message": "机器人候选域返回了非法结构，无法安全继续。",
            }

        class_id = (
            (canonical_selection.get("robot_class") if canonical_selection else None)
            or self.kb._resolve_class_key(str(task_state.get("equipment_class") or ""))
        )
        class_node = next(
            (
                item
                for item in domain.get("classes", [])
                if item.get("class_id") == class_id
            ),
            None,
        )
        family_id = (
            (canonical_selection.get("family_id") if canonical_selection else None)
            or self.kb._resolve_family_key(str(task_state.get("equipment_family") or ""))
        )

        # 若仅显式指定了 equipment_family 而未指定 equipment_class，依据 Capabilities 自动查找对应的父级 class_node
        if class_node is None and has_explicit_family and family_id:
            for c_node in domain.get("classes", []):
                if any(
                    f_node.get("family_id") == family_id
                    for f_node in c_node.get("families", [])
                ):
                    class_node = c_node
                    class_id = c_node.get("class_id")
                    break

        family_node = None
        if class_node is not None and has_explicit_family:
            family_node = next(
                (
                    item
                    for item in class_node.get("families", [])
                    if item.get("family_id") == family_id
                ),
                None,
            )

        # 若选定了 class_node 且未指定 family_id 时，只要 class_node 包含可行的 families 候选，说明处于正常等待选择阶段
        if class_node is not None and (
            (not has_explicit_family and bool(class_node.get("families")))
            or family_node is not None
        ):
            return None

        selected_level = "Family" if has_explicit_family else "Class"
        selected_value = family_id if has_explicit_family else class_id
        return {
            "code": "NO_FEASIBLE_ROBOT_CANDIDATE",
            "message": (
                f"当前任务条件下，已选机器人 {selected_level} "
                f"'{selected_value}' 不符合作业能力要求 (Capabilities) 或没有可行的下级机器人候选。"
                "请修改任务条件或更换机器人选择。"
            ),
            "details": {
                "task_type_key": task_type_key,
                "selected_level": selected_level.lower(),
                "selected_value": selected_value,
            },
        }

    def validate_concrete_robot_payload_feasibility(
        self,
        task_state: dict,
        canonical_selection: dict | None,
    ) -> dict | None:
        """Validate payload compatibility for a concrete selected Variant."""
        if not isinstance(task_state, dict) or task_state.get("payload") is None:
            return None
        if not isinstance(canonical_selection, dict):
            return None
        if task_state.get("equipment_type") is None and task_state.get(
            "equipment_unit_id"
        ) is None:
            return None

        fleet = getattr(self.kb, "robot_fleet", None)
        variants = fleet.get("model_variants") if isinstance(fleet, dict) else None
        evaluator = getattr(type(self.kb), "evaluate_static_robot_variant", None)
        authoritative_gate = isinstance(variants, dict) and callable(evaluator)
        if not authoritative_gate:
            return None

        variant_id = canonical_selection.get("variant_id")
        canonical_unit_id = canonical_selection.get("unit_id")
        explicit_unit_selector = task_state.get("equipment_unit_id")
        if explicit_unit_selector is not None:
            exact_unit_resolver = getattr(
                type(self.kb),
                "_resolve_robot_unit_exact",
                None,
            )
            if not callable(exact_unit_resolver):
                return {
                    "code": "STATIC_ROBOT_VALIDATOR_FAILURE",
                    "message": "权威机器人注册表缺少精确 Unit 解析器。",
                }
            try:
                explicit_unit = exact_unit_resolver(
                    self.kb,
                    explicit_unit_selector,
                    task_state.get("task_type_key"),
                )
            except RobotSelectionDataError as exc:
                return {
                    "code": "STATIC_ROBOT_VALIDATOR_FAILURE",
                    "message": f"显式机器人 Unit 无法安全解析：{exc}",
                    "details": exc.to_dict(),
                }
            explicit_unit_id = (
                explicit_unit.get("unit_id")
                if isinstance(explicit_unit, dict)
                else None
            )
            if not isinstance(explicit_unit_id, str) or not explicit_unit_id:
                return {
                    "code": "STATIC_ROBOT_VALIDATOR_FAILURE",
                    "message": (
                        f"显式机器人 Unit '{explicit_unit_selector}' 无法在 "
                        "fleet registry 中唯一解析。"
                    ),
                }
            if canonical_unit_id != explicit_unit_id:
                return {
                    "code": "STATIC_ROBOT_VALIDATOR_FAILURE",
                    "message": (
                        f"显式机器人 Unit '{explicit_unit_id}' 与静态校验器返回的 "
                        f"canonical Unit '{canonical_unit_id}' 不一致。"
                    ),
                }

        if isinstance(canonical_unit_id, str) and canonical_unit_id.strip():
            fleet_units = fleet.get("fleet_units")
            matching_units = [
                unit
                for unit in (fleet_units if isinstance(fleet_units, list) else [])
                if isinstance(unit, dict)
                and unit.get("unit_id") == canonical_unit_id
            ]
            if len(matching_units) != 1:
                return {
                    "code": "STATIC_ROBOT_VALIDATOR_FAILURE",
                    "message": (
                        f"canonical unit_id '{canonical_unit_id}' 无法在 fleet registry "
                        "中唯一绑定机器人型号。"
                    ),
                }
            registry_variant_id = matching_units[0].get("variant_id")
            if (
                isinstance(variant_id, str)
                and variant_id.strip()
                and variant_id != registry_variant_id
            ):
                return {
                    "code": "STATIC_ROBOT_VALIDATOR_FAILURE",
                    "message": (
                        f"canonical unit_id '{canonical_unit_id}' 属于型号 "
                        f"'{registry_variant_id}'，但静态校验器返回 '{variant_id}'。"
                    ),
                }
            variant_id = registry_variant_id

        if not isinstance(variant_id, str) or not variant_id.strip():
            return {
                "code": "STATIC_ROBOT_VALIDATOR_FAILURE",
                "message": (
                    "机器人四级静态校验器未返回 canonical variant_id，"
                    "且无法由 canonical unit_id 安全反推。"
                ),
            }

        variant_cfg = variants.get(variant_id)
        if not isinstance(variant_cfg, dict):
            return {
                "code": "ROBOT_FEASIBILITY_CHECK_FAILED",
                "message": (
                    f"无法读取已选机器人型号 '{variant_id}' 的载荷能力配置，"
                    "不能安全继续发布或执行。"
                ),
            }

        try:
            feasibility = evaluator(
                variant_id,
                variant_cfg,
                {"payload": copy.deepcopy(task_state.get("payload"))},
            )
        except RobotSelectionDataError as exc:
            return {
                "code": exc.error_code,
                "message": f"机器人载荷可行性校验失败：{exc}",
                "details": exc.to_dict(),
            }
        except Exception as exc:
            return {
                "code": "ROBOT_FEASIBILITY_CHECK_FAILED",
                "message": f"机器人载荷可行性校验失败：{exc}",
            }

        eligible = getattr(feasibility, "eligible", None)
        reasons = getattr(feasibility, "reasons", None)
        if not isinstance(eligible, bool) or not isinstance(reasons, tuple):
            return {
                "code": "ROBOT_FEASIBILITY_CHECK_FAILED",
                "message": "机器人载荷可行性校验器返回了非法结构。",
            }
        if eligible:
            return None
        return {
            "code": "ROBOT_SELECTION_NOT_FEASIBLE",
            "message": (
                f"已选机器人型号 '{variant_id}' 不支持当前载荷要求："
                + "; ".join(str(reason) for reason in reasons)
            ),
            "details": {
                "variant_id": variant_id,
                "payload": copy.deepcopy(task_state.get("payload")),
                "reasons": list(reasons),
            },
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 静态规则检查分发
    # ──────────────────────────────────────────────────────────────────────────

    def check_rule(
        self,
        c: dict,
        check: str,
        task_state: dict,
        rov: dict | None,
        water_depth: Any,
        vessel_id: str | None,
        is_task_start_now: bool = True,
        purpose: str = "publish",
    ) -> Violation | None:
        """执行单一静态规则核验"""
        from .validator import Violation, _CHECK_FIELDS

        rel_fields = _CHECK_FIELDS.get(check, [])

        if check == "robot_category" and rov:
            task_type = task_state.get("task_type_key")
            if not self.kb.robot_matches_task(rov, task_type):
                return Violation(
                    c["id"], c["name"], c["violation_message"].strip(),
                    c["severity"], rel_fields, check_type=check
                )

        elif check == "depth_vs_rov_limit" and rov:
            if water_depth is not None:
                depth_val, depth_err = self.validate_water_depth_value(water_depth)
                if depth_err:
                    return Violation(
                        c["id"], c["name"], depth_err["message"], "hard",
                        rel_fields, check_type=check, observed_value=water_depth
                    )
                max_depth, max_depth_err = self.validate_max_depth_m_value(
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
            start_time, st_err = self.validate_time_value(task_state.get("start_time"), "start_time")
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
            raw_st = task_state.get("start_time")
            raw_et = task_state.get("end_time")
            if not raw_st or not raw_et:
                return None
            start_time, st_err = self.validate_time_value(raw_st, "start_time")
            end_time, et_err = self.validate_time_value(raw_et, "end_time")
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

        elif check == "future_task_runtime_notice":
            start_time, st_err = self.validate_time_value(task_state.get("start_time"), "start_time")
            if st_err or start_time is None:
                return None
            now = get_current_datetime().replace(microsecond=0)
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            if now.tzinfo is None:
                now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            else:
                now = now.astimezone(ZoneInfo("Asia/Shanghai"))
            if (start_time - now).total_seconds() > 0 and not is_task_start_now:
                msg = (
                    c["violation_message"]
                    .replace("{start_time}", start_time.strftime("%Y-%m-%d %H:%M:%S"))
                )
                return Violation(
                    c["id"], c["name"], msg.strip(), c["severity"],
                    rel_fields, check_type=check, observed_value=start_time.isoformat()
                )

        elif check == "vessel_availability" and vessel_id:
            vessel = self.kb.get_vessel(vessel_id)
            if vessel and not vessel.get("available", True):
                msg = c["violation_message"].replace("{vessel_id}", vessel_id)
                return Violation(
                    c["id"], c["name"], msg.strip(), c["severity"],
                    rel_fields, check_type=check, observed_value=vessel_id
                )

        return None
