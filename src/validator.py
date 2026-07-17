"""
validator.py — 约束验证器
- 每条 Violation 携带 related_fields，说明该违规由哪些字段触发
- 支持按 changed_fields 精准触发检查
- validate_for_fields() 只运行与变化字段相关的约束
"""

import time
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from .knowledge_retriever import KnowledgeBase
from .simulated_time import get_current_datetime, get_current_timestamp


START_TIME_PAST_GRACE_MINUTES = 5


@dataclass
class Violation:
    constraint_id: str
    constraint_name: str
    message: str
    severity: str          # "hard" | "soft"
    related_fields: list[str] = field(default_factory=list)
    # related_fields：触发本条违规的字段名列表，用于白名单 key 和失效判断


# check_type → 该约束关注的字段集合
_EQUIPMENT_FIELDS = ["equipment_unit_id", "equipment_type", "equipment_name"]

_CHECK_FIELDS: dict[str, list[str]] = {
    "robot_category":              _EQUIPMENT_FIELDS,
    "depth_vs_rov_limit":          [*_EQUIPMENT_FIELDS, "water_depth"],
    # "sea_state":                   ["start_point", "oilfield_coordinates"],
    "vessel_availability":         ["support_vessel"],
    # "tree_type_compatibility":     ["tree_type", *_EQUIPMENT_FIELDS],
    # 环境约束关联字段
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

class TaskValidator:
    def __init__(self, kb: KnowledgeBase):
        self.kb = kb

    # ──────────────────────────────────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────────────────────────────────

    def validate(self, task_state: dict) -> list[Violation]:
        """全量约束检查，返回所有当前违规"""
        return self._run_checks(task_state, trigger_fields=None)

    def validate_for_fields(
        self, task_state: dict, changed_fields: set[str]
    ) -> list[Violation]:
        """
        只运行与 changed_fields 相关的约束。
        用于字段变化后的增量检查，避免每轮都全量扫描。
        """
        return self._run_checks(task_state, trigger_fields=changed_fields)

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

    def _run_checks(
        self, task_state: dict, trigger_fields: set[str] | None
    ) -> list[Violation]:
        violations = []
        task_type    = task_state.get("task_type_key")
        equipment    = (
            task_state.get("equipment_unit_id")
            or task_state.get("equipment_name")
            or task_state.get("equipment_type")
        )
        water_depth  = task_state.get("water_depth")
        vessel_id    = task_state.get("support_vessel")
        tree_type    = task_state.get("tree_type")
        rov          = self.kb.get_rov(equipment) if equipment else None

        for c in self.kb.get_constraints():
            check = c["check_type"]

            # 若是增量模式，跳过与 changed_fields 无关的约束（但硬约束除外）
            if trigger_fields is not None:
                # 硬约束始终检查，不跳过
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

            v = self._check_one(c, check, task_state, rov, water_depth, vessel_id, tree_type)
            if v:
                violations.append(v)

        return violations

    def _is_task_start_now(self, task_state: dict, time_window_minutes: int = 10) -> bool:
        try:
            start_time_str = task_state.get("start_time")
            if not start_time_str:
                return False

            # 使用模拟时间代替系统时间
            from .simulated_time import get_current_datetime
            now = get_current_datetime()
            now = now.replace(microsecond=0)

            start_time_str = start_time_str.replace("T", " ").replace("：", ":").strip()
            if start_time_str.endswith("Z"):
                start_time_str = start_time_str[:-1] + "+00:00"
            start_time = datetime.fromisoformat(start_time_str)
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            else:
                start_time = start_time.astimezone(ZoneInfo("Asia/Shanghai"))

            delta_seconds = (start_time - now).total_seconds()
            return delta_seconds <= time_window_minutes * 60
        except Exception:
            return False

    def _check_one(
        self, c: dict, check: str, task_state: dict,
        rov: dict | None, water_depth: Any,
        vessel_id: str | None, tree_type: str | None,
    ) -> Violation | None:
        if check in _DYNAMIC_CHECKS and not self._is_task_start_now(task_state):
            return None

        rel_fields = _CHECK_FIELDS.get(check, [])

        if check == "robot_category" and rov:
            task_type = task_state.get("task_type_key")
            if not self.kb.robot_matches_task(rov, task_type):
                return Violation(c["id"], c["name"],
                                 c["violation_message"].strip(), c["severity"],
                                 rel_fields)
        elif check == "depth_vs_rov_limit" and rov and water_depth is not None:
            try:
                depth_val = float(water_depth)
            except (TypeError, ValueError):
                return None  # 水深无效，跳过检查
            max_depth = rov.get("max_depth_m", 99999)
            if depth_val > max_depth:
                msg = c["violation_message"].replace("{rov_max_depth}", str(max_depth))
                return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        elif check == "start_time_not_in_past":
            start_time = self._parse_task_datetime(task_state.get("start_time"))
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
                return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)


        # elif check == "sea_state":
        #     coords = task_state.get("start_point") or task_state.get("oilfield_coordinates")
        #     if coords:
        #         area = self.kb.get_environment_for_coords(coords)
        #         if area:
        #             cond = area["current_conditions"]
        #             max_wave = c.get("max_wave_height_m", 999)
        #             max_wind = c.get("max_wind_speed_knots", 999)
        #             if cond["wave_height_m"] > max_wave or cond["wind_speed_knots"] > max_wind:
        #                 msg = (c["violation_message"]
        #                        .replace("{max_wave_height}", str(max_wave))
        #                        .replace("{max_wind_speed}", str(max_wind)))
        #                 return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        elif check == "vessel_availability" and vessel_id:
            vessel = self.kb.get_vessel(vessel_id)
            if vessel and not vessel.get("available", True):
                msg = c["violation_message"].replace("{vessel_id}", vessel_id)
                return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        # 采油树不再区分立式/卧式，停用类型兼容性检查。
        # elif check == "tree_type_compatibility" and tree_type == "卧式":
        #     return Violation(c["id"], c["name"],
        #                      c["violation_message"].strip(), c["severity"],
        #                      rel_fields)

        # 环境约束检查
        elif check == "forbidden_area":
            for field_name in ["start_point", "end_point", "oilfield_coordinates", "cable_position"]:
                coords = task_state.get(field_name)
                if coords:
                    env_info = self.kb.get_environment_info_dict(coords)
                    if env_info.get("forbidden") is True:
                        return Violation(c["id"], c["name"],
                                         c["violation_message"].strip(), c["severity"],
                                         rel_fields)

        elif check == "dvl_high_risk":
            for field_name in ["start_point", "oilfield_coordinates", "cable_position"]:
                coords = task_state.get(field_name)
                if coords:
                    env_info = self.kb.get_environment_info_dict(coords)
                    if env_info.get("dvl_risk") is True:
                        return Violation(c["id"], c["name"],
                                         c["violation_message"].strip(), c["severity"],
                                         rel_fields)

        elif check == "seabed_compatibility" and rov:
            for field_name in ["start_point", "oilfield_coordinates"]:
                coords = task_state.get(field_name)
                if coords:
                    env_info = self.kb.get_environment_info_dict(coords)
                    seabed = env_info.get("seabed_type")
                    if seabed and seabed != "unknown":
                        supported_raw = rov.get("supported_seabed")
                        if not supported_raw:
                            continue
                        if isinstance(supported_raw, str):
                            supported = [supported_raw]
                        else:
                            supported = supported_raw
                        if seabed not in supported:
                            rov_name = rov.get("full_name", str(rov))
                            msg = c["violation_message"].replace("{current_rov}", rov_name)
                            msg = msg.replace("{current_seabed}", str(seabed))
                            return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        # 状态约束检查
        elif check == "mothership_support":
            if rov:
                state_info = self.kb.get_robot_state_dict(rov["full_name"])
                support_cap = state_info.get("mothership_support")
                if support_cap == "weak":
                    return Violation(c["id"], c["name"],
                                     c["violation_message"].strip(), c["severity"],
                                     rel_fields)
        elif check == "obstacle_dense":
            if rov:
                state_info = self.kb.get_robot_state_dict(rov["full_name"])
                dense = state_info.get("obstacle_density")
                if dense == "high":
                    return Violation(c["id"], c["name"],
                                     c["violation_message"].strip(), c["severity"],
                                     rel_fields)
        elif check == "turbidity":
            if rov:
                state_info = self.kb.get_robot_state_dict(rov["full_name"])
                if not isinstance(state_info, dict):
                    return None
                turb = state_info.get("turbidity")
                if turb is None:
                    return None
                # 浑浊度分级
                if c["id"] == "C013":
                    if 5 < turb <= 10:
                        msg = c["violation_message"].replace("{turbidity}", str(turb))
                        return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)
                elif c["id"] == "C014":
                    if turb > 10:
                        msg = c["violation_message"].replace("{turbidity}", str(turb))
                        return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        elif check == "current_velocity":
            if rov:
                state_info = self.kb.get_robot_state_dict(rov["full_name"])
                if not isinstance(state_info, dict):
                    return None
                vel = state_info.get("current_velocity")
                if vel is None:
                    return None
                print("【当前检查约束的流速为】")
                print(vel)
                # 按规则ID分别判断
                if c["id"] == "C015":
                    if 0.5 < vel <= 0.8:
                        msg = c["violation_message"].replace("{current_velocity}", f"{vel:.2f}")
                        return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

                elif c["id"] == "C016":
                    if 0.8 < vel <= 1.2:
                        msg = c["violation_message"].replace("{current_velocity}", f"{vel:.2f}")
                        return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

                elif c["id"] == "C017":
                    if vel > 1.2:
                        msg = c["violation_message"].replace("{current_velocity}", f"{vel:.2f}")
                        return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        elif check == "state_confidence":
            if rov:
                state_info = self.kb.get_robot_state_dict(rov["full_name"])
                if not isinstance(state_info, dict):
                    return None
                confidence = state_info.get("confidence")
                if confidence is not None and confidence < 0.5:
                    msg = c["violation_message"].replace("{confidence}", str(confidence))
                    return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        elif check == "state_timestamp":
            if rov:
                state_info = self.kb.get_robot_state_dict(rov["full_name"])
                if not isinstance(state_info, dict):
                    return None
                timestamp_str = state_info.get("update_timestamp")
                if timestamp_str is not None:
                    try:
                        if isinstance(timestamp_str, str):
                            # 解析 ISO 8601 格式，兼容 +08:00 等时区后缀
                            clean = timestamp_str.replace('+00:00', '').replace('Z', '')
                            if '+' in clean:
                                clean = clean.split('+')[0]
                            if clean.endswith('Z'):
                                clean = clean[:-1]
                            dt = datetime.fromisoformat(clean)
                            timestamp = dt.timestamp()
                        else:
                            timestamp = float(timestamp_str)
                        current_ts = get_current_timestamp()  
                        if (current_ts - timestamp) > 3600:
                            msg = c["violation_message"].replace("{update_timestamp}", str(timestamp_str))
                            return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)
                    except Exception:
                        # 解析失败则忽略此约束
                        pass

        # ========== 机器人状态相关约束（基于 3002 文档） ==========
        elif check == "robot_overall_status":
            if rov:
                state_dict = self.kb.get_robot_state_dict(rov["full_name"])
                if isinstance(state_dict, dict):
                    overall = state_dict.get("overall_status")
                    if overall == "unavailable":
                        # 机器人总体状态不可用 → 硬性违规
                        msg = c["violation_message"].replace("{equipment_name}", rov["full_name"])
                        return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        elif check == "robot_survival_status":
            if rov:
                state_dict = self.kb.get_robot_state_dict(rov["full_name"])
                if isinstance(state_dict, dict):
                    survival = state_dict.get("survival_status")
                    if survival == "abnormal":
                        msg = c["violation_message"].replace("{equipment_name}", rov["full_name"])
                        return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        elif check == "robot_thruster_status":
            if rov:
                state_dict = self.kb.get_robot_state_dict(rov["full_name"])
                if isinstance(state_dict, dict):
                    thruster = state_dict.get("thruster_status")
                    if thruster == "abnormal":
                        msg = c["violation_message"].replace("{equipment_name}", rov["full_name"])
                        return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        elif check == "robot_depth_keeping_status":
            if rov:
                state_dict = self.kb.get_robot_state_dict(rov["full_name"])
                if isinstance(state_dict, dict):
                    depth_keep = state_dict.get("depth_keeping_status")
                    if depth_keep == "abnormal":
                        msg = c["violation_message"].replace("{equipment_name}", rov["full_name"])
                        return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        elif check == "robot_sonar_status":
            if rov:
                state_dict = self.kb.get_robot_state_dict(rov["full_name"])
                if isinstance(state_dict, dict):
                    sonar = state_dict.get("sonar_status")
                    if sonar == "abnormal":
                        msg = c["violation_message"].replace("{equipment_name}", rov["full_name"])
                        return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        elif check == "robot_vision_status":
            if rov:
                state_dict = self.kb.get_robot_state_dict(rov["full_name"])
                if isinstance(state_dict, dict):
                    vision = state_dict.get("vision_status")
                    if vision == "abnormal":
                        msg = c["violation_message"].replace("{equipment_name}", rov["full_name"])
                        return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        elif check == "robot_manipulator_status":
            if rov:
                state_dict = self.kb.get_robot_state_dict(rov["full_name"])
                if isinstance(state_dict, dict):
                    arm = state_dict.get("arm_status")
                    end_effector = state_dict.get("end_effector_status")
                    # 机械臂或末端执行器任一异常即触发（符合执行机构模块判断逻辑）
                    if arm == "abnormal" or end_effector == "abnormal":
                        msg = c["violation_message"].replace("{equipment_name}", rov["full_name"])
                        return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)

        elif check == "robot_communication_status":
            if rov:
                state_dict = self.kb.get_robot_state_dict(rov["full_name"])
                if not isinstance(state_dict, dict):
                    return None

                is_auv = rov.get("robot_class") == "auv"

                details = []

                if is_auv:
                    # AUV：只检查水声无线通信
                    acoustic = state_dict.get("acoustic_comms_status")
                    if acoustic == "abnormal":
                        details.append("水声无线通信异常")
                else:
                    # ROV / 海底拖拉机：只检查与母船的脐带缆连接
                    tether = state_dict.get("tether_connection_status")
                    if tether == "abnormal" or tether == "weak":
                        details.append("与母船连接异常")

                if details:
                    detail_str = "、".join(details)
                    msg = c["violation_message"].replace("{equipment_name}", rov["full_name"])
                    msg = msg.replace("{detail}", detail_str)
                    return Violation(c["id"], c["name"], msg.strip(), c["severity"], rel_fields)
        return None

    @staticmethod
    def _parse_task_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            text = value.strip().replace("：", ":")
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(text.replace("T", " "))
            except ValueError:
                return None
        else:
            return None

        if dt.tzinfo is None:
            return dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return dt.astimezone(ZoneInfo("Asia/Shanghai"))
