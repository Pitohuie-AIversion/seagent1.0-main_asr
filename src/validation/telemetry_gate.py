"""
src/telemetry_gate.py — 动态遥测状态与单机健康度门禁

职责：
1. 单机遥测快照动态解析与提取 (_resolve_single_unit_snapshot)；
2. 快照内容完整性与系统时钟偏斜校验 (_validate_state_snapshot_content)；
3. 任务即时执行时间窗判定 (is_task_start_now)；
4. 动态水文气象环境规则判定（流速、浑浊度、障碍物、母船动态支持）；
5. 机器人单机运行状态门禁（离线、忙碌、故障维护）；
6. 机器人关键子系统健康度检查（推进器、生存、定深、声呐、视觉、机械臂、通信链路）。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, TYPE_CHECKING
from zoneinfo import ZoneInfo

from src.exceptions import (
    StateSelectorError,
    StateSnapshotValidationError,
)
from src.knowledge_retriever import KnowledgeBase
from src.state_info import TELEMETRY_MAX_FUTURE_SKEW_SECONDS

if TYPE_CHECKING:
    from .validator import Violation


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


def matches_numeric_thresholds(value: Any, thresholds: dict[str, Any]) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"telemetry value must be numeric, got {type(value).__name__}")
    if not math.isfinite(float(value)):
        raise ValueError("telemetry value must be finite")
    if "min_exclusive" in thresholds and value <= thresholds["min_exclusive"]:
        return False
    if "min_inclusive" in thresholds and value < thresholds["min_inclusive"]:
        return False
    if "max_exclusive" in thresholds and value >= thresholds["max_exclusive"]:
        return False
    if "max_inclusive" in thresholds and value > thresholds["max_inclusive"]:
        return False
    return True


def display_threshold(thresholds: dict[str, Any]) -> Any:
    for key in ("max_inclusive", "min_exclusive", "min_inclusive", "max_exclusive"):
        if key in thresholds:
            return thresholds[key]
    return None


class TelemetryGate:
    """动态遥测状态与单机健康度门禁"""

    def __init__(self, kb: KnowledgeBase, parent_validator: Any = None):
        self.kb = kb
        self.parent_validator = parent_validator

    def _default_is_task_start_now(self, task_state: dict, time_window_minutes: int = 60) -> bool:
        """判定任务是否为当前立即执行或进行中的默认实现"""
        from .rule_engine import RuleEngine

        start_time_raw = task_state.get("start_time")
        if not start_time_raw or (isinstance(start_time_raw, str) and start_time_raw.strip() == ""):
            return True
        st_dt, err = RuleEngine.validate_time_value(start_time_raw, "start_time")
        if err or st_dt is None:
            return True
        now = get_current_datetime().replace(microsecond=0)
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        else:
            now = now.astimezone(ZoneInfo("Asia/Shanghai"))

        end_time_raw = task_state.get("end_time")
        end_dt = None
        if end_time_raw and isinstance(end_time_raw, str) and end_time_raw.strip():
            end_dt, _ = RuleEngine.validate_time_value(end_time_raw, "end_time")

        delta_seconds = (st_dt - now).total_seconds()
        # 1. 任务计划在当前或不久的将来开始 (支持前置5分钟容差至未来窗口期)
        if -300 <= delta_seconds <= time_window_minutes * 60:
            return True
        # 2. 任务已在过去开始，但结束时间在未来 (任务正在执行中)
        if delta_seconds < -300 and end_dt:
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            else:
                end_dt = end_dt.astimezone(ZoneInfo("Asia/Shanghai"))
            if end_dt > now:
                return True
        return False

    def is_task_start_now(self, task_state: dict, time_window_minutes: int = 60) -> bool:
        """判定任务是否为当前立即执行或进行中（支持 parent_validator 的外部 Mock 穿透）"""
        if self.parent_validator is not None:
            pv_fn = getattr(self.parent_validator, "_is_task_start_now", None)
            if pv_fn is not None:
                # 若 _is_task_start_now 被外部 patch (如 unittest.mock.MagicMock)，直接代理执行
                from .validator import TaskValidator
                if getattr(pv_fn, "__func__", None) is not TaskValidator._is_task_start_now:
                    return bool(pv_fn(task_state))
        return self._default_is_task_start_now(task_state, time_window_minutes)

    def resolve_single_unit_snapshot(
        self,
        task_state: dict,
        is_now: bool,
        purpose: str = "interactive",
    ) -> tuple[dict | None, dict | None]:
        """尝试确定具体单机并提取状态快照"""
        unit_selector = task_state.get("equipment_unit_id")
        task_type = task_state.get("task_type_key")
        variant_selector = (
            task_state.get("equipment_type")
            or task_state.get("equipment_name")
            or task_state.get("equipment_family")
        )

        # 显式提供了 unit_id
        if unit_selector is not None and str(unit_selector).strip():
            clean_unit_id = str(unit_selector).strip()
            try:
                snapshot = None
                if hasattr(self.kb, "get_unit_state_snapshot"):
                    snapshot = self.kb.get_unit_state_snapshot(clean_unit_id)
                elif hasattr(self.kb, "state_info") and hasattr(self.kb.state_info, "get_unit_state_snapshot"):
                    snapshot = self.kb.state_info.get_unit_state_snapshot(clean_unit_id)

                if snapshot:
                    err = self.validate_state_snapshot_content(clean_unit_id, snapshot)
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
                # 交互收集模式下，未指定单机编号属于待填槽位，不提取遥测快照且不报硬性违规
                if purpose == "interactive":
                    return None, None
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
                        err = self.validate_state_snapshot_content(single_unit_id, snapshot)
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
    def validate_state_snapshot_content(unit_id: str, snapshot: dict) -> dict | None:
        """验证单机状态快照内容完整性与时效"""
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
        timestamp_values = [
            ("state.updated_at", state_dict.get("updated_at")),
            ("state.update_timestamp", state_dict.get("update_timestamp")),
            ("state.update_at", state_dict.get("update_at")),
            ("snapshot.updated_at", snapshot.get("updated_at")),
        ]
        now_dt = get_current_datetime()
        for field_name, timestamp_value in timestamp_values:
            if timestamp_value is None:
                continue
            if not isinstance(timestamp_value, str) or not timestamp_value.strip():
                return {
                    "code": "INVALID_STATE_DATA",
                    "message": f"单机 '{unit_id}' 的 {field_name} 必须是非空 ISO 时间字符串。",
                }
            try:
                parsed_timestamp = datetime.fromisoformat(
                    timestamp_value.strip().replace("Z", "+00:00")
                )
            except ValueError:
                return {
                    "code": "INVALID_STATE_DATA",
                    "message": f"单机 '{unit_id}' 的 {field_name} 时间格式无法解析。",
                }
            if parsed_timestamp.tzinfo is None:
                parsed_timestamp = parsed_timestamp.replace(tzinfo=now_dt.tzinfo)
            else:
                parsed_timestamp = parsed_timestamp.astimezone(now_dt.tzinfo)
            if (
                parsed_timestamp - now_dt
            ).total_seconds() > TELEMETRY_MAX_FUTURE_SKEW_SECONDS:
                return {
                    "code": "INVALID_STATE_DATA",
                    "message": (
                        f"单机 '{unit_id}' 的 {field_name} 明显晚于系统时间，"
                        "请校准设备时钟并刷新遥测。"
                    ),
                }
        return None

    def check_rule(
        self,
        c: dict,
        check: str,
        task_state: dict,
        rov: dict | None,
        state_snapshot: dict | None,
        purpose: str = "publish",
    ) -> Violation | None:
        """执行单一动态遥测与单机健康度规则核验"""
        from .validator import Violation, _CHECK_FIELDS

        # 仅当任务为未来排期任务且处于非执行窗口时，跳过当前近实时动态遥测检查（保留 C032 延后提示）。
        # 交互收集模式 (interactive) 同样不执行近实时动态遥测检查。
        if purpose == "interactive":
            return None
        if purpose != "runtime_execution" and not self.is_task_start_now(task_state):
            return None

        rel_fields = _CHECK_FIELDS.get(check, [])
        state_dict = state_snapshot.get("state") if state_snapshot else None

        if check == "mothership_support":
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
                turb_w = state_dict.get("water_turbidity")
                turb_c = state_dict.get("turbidity")
                turb = turb_c if (turb_c is not None and turb_w is not None and turb_c != turb_w) else (turb_w if turb_w is not None else turb_c)
                if turb is not None:
                    thresholds = c["thresholds"]
                    if matches_numeric_thresholds(turb, thresholds):
                        msg = c["violation_message"].replace("{turbidity}", str(turb))
                        return Violation(
                            c["id"], c["name"], msg.strip(), c["severity"],
                            rel_fields, check_type=check, observed_value=turb,
                            threshold=display_threshold(thresholds),
                        )

        elif check == "current_velocity":
            if state_dict and isinstance(state_dict, dict):
                vel_w = state_dict.get("water_current_velocity")
                vel_c = state_dict.get("current_velocity")
                vel = vel_c if (vel_c is not None and vel_w is not None and vel_c != vel_w) else (vel_w if vel_w is not None else vel_c)
                if vel is not None:
                    thresholds = c["thresholds"]
                    if matches_numeric_thresholds(vel, thresholds):
                        msg = c["violation_message"].replace("{current_velocity}", f"{vel:.2f}")
                        return Violation(
                            c["id"], c["name"], msg.strip(), c["severity"],
                            rel_fields, check_type=check, observed_value=vel,
                            threshold=display_threshold(thresholds),
                        )

        elif check == "state_confidence":
            if state_dict and isinstance(state_dict, dict):
                confidence = state_dict.get("confidence")
                thresholds = c["thresholds"]
                if confidence is not None and matches_numeric_thresholds(confidence, thresholds):
                    msg = c["violation_message"].replace("{confidence}", str(confidence))
                    return Violation(
                        c["id"], c["name"], msg.strip(), c["severity"],
                        rel_fields, check_type=check, observed_value=confidence,
                        threshold=display_threshold(thresholds),
                    )

        elif check == "state_timestamp":
            if not self.is_task_start_now(task_state):
                return None
            if state_dict and isinstance(state_dict, dict):
                timestamp_str = (
                    state_dict.get("update_timestamp")
                    or state_dict.get("updated_at")
                    or state_dict.get("update_at")
                    or (state_snapshot.get("updated_at") if state_snapshot else None)
                )
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
                        max_age_seconds = c["thresholds"]["max_age_seconds"]
                        if (now_ts - dt_ts) > max_age_seconds:
                            msg = c["violation_message"].replace("{update_timestamp}", str(timestamp_str))
                            return Violation(
                                c["id"], c["name"], msg.strip(), c["severity"],
                                rel_fields, check_type=check, observed_value=timestamp_str,
                                threshold=max_age_seconds,
                            )
                    except (TypeError, ValueError, OverflowError) as exc:
                        return Violation(
                            c["id"], c["name"],
                            f"环境信息更新时间格式非法，无法完成时效校验: {exc}",
                            "hard", rel_fields, check_type=check,
                            observed_value=timestamp_str,
                        )

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
                    elif overall in ("maintenance", "fault") or task_status in ("maintenance", "fault"):
                        msg = f"无法发布任务：机器人 {unit_disp} 当前处于故障/维护状态。"
                    elif is_busy_status:
                        msg = f"无法发布任务：机器人 {unit_disp} 当前处于忙碌状态。"
                    return Violation(
                        c["id"], c["name"], msg.strip(), c["severity"],
                        rel_fields, check_type=check, observed_value=overall or ("offline" if is_offline else ("fault" if overall in ("maintenance", "fault") else "busy"))
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
                is_auv = rov.get("robot_class") == "auv" if rov else False
                details = []
                if is_auv:
                    acoustic = state_dict.get("acoustic_comms_status")
                    if acoustic == "abnormal":
                        details.append("水声无线通信异常")
                else:
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
