"""
src/handlers/telemetry_status.py - 环境水文与实时遥测装备状态查询处理器

职责：
1. 识别环境水文与实时遥测查询意图；
2. 查询装备/油田现场实时状态源；
3. 构建结构化环境遥测回复；
4. 后端真理源数据硬对齐护栏（校对并替换 LLM 回复中的遥测数值、文本与单位）。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from .base import BaseDialogueHandler
from src.session.intent_router import IntentRouteResult
from ..knowledge_retriever import format_telemetry_value
from src.extraction.prompts import build_status_responder_messages
from src.extraction.model_profile import ModelRole

logger = logging.getLogger(__name__)

_ENVIRONMENT_STATUS_SUBJECT_TERMS = (
    "环境",
    "海况",
    "水文",
    "海流",
    "水流",
    "流速",
    "浑浊",
    "浊度",
    "能见度",
    "障碍物",
    "母船支援",
)

_REALTIME_STATUS_TERMS = (
    "现在",
    "当前",
    "实时",
    "最新",
    "状态",
    "情况",
    "如何",
    "怎么样",
    "怎样",
)


class TelemetryStatusHandler(BaseDialogueHandler):
    """实时装备遥测与作业环境状态查询处理器"""

    @property
    def task_state(self) -> dict:
        return getattr(self.manager, "task_state", {})

    @property
    def kb(self) -> Any:
        return getattr(self.manager, "kb", None)

    @property
    def phase(self) -> str:
        return getattr(self.manager, "phase", "idle")

    @property
    def mode(self) -> str:
        return getattr(self.manager, "mode", "normal")

    @property
    def _last_built_json(self) -> dict:
        return getattr(self.manager, "_last_built_json", {})

    @property
    def _last_missing(self) -> list:
        return getattr(self.manager, "_last_missing", [])

    @property
    def conversation_history(self) -> list:
        return getattr(self.manager, "conversation_history", [])

    def is_environment_status_query(self, user_message: str, route: IntentRouteResult) -> bool:
        """判断是否为作业现场水文或环境遥测查询。"""
        plan = route.interaction_plan
        if route.query_intent == "ENVIRONMENT_QUERY" and plan is not None:
            if plan.source_policy == "realtime_state" or plan.relation == "status":
                return True

        text = (user_message or "").strip()
        if not text:
            return False
        has_environment_subject = any(term in text for term in _ENVIRONMENT_STATUS_SUBJECT_TERMS)
        has_realtime_status_relation = any(term in text for term in _REALTIME_STATUS_TERMS)
        return has_environment_subject and has_realtime_status_relation

    _is_environment_status_query = is_environment_status_query

    def handle_status_query(
        self,
        user_message: str,
        route: IntentRouteResult,
        *,
        as_environment_status: bool = False,
    ) -> str:
        """处理任务状态、设备运行状态或现场遥测环境状态查询。"""
        state_dict = None
        if route.query_intent == "TASK_STATUS":
            status_evidence = {
                "query_type": "TASK_STATUS",
                "phase": self.phase,
                "mode": self.mode,
                "task_type": self.task_state.get("task_type", "(未确定)"),
                "collected_slots": self._last_built_json,
                "missing_slots": [m.get("label") for m in self._last_missing if isinstance(m, dict)],
                "found": True,
            }
        else:
            equipment = (
                self.task_state.get("equipment_unit_id")
                or self.task_state.get("equipment_name")
                or self.task_state.get("equipment_type")
            )
            if not equipment:
                alias_index = self.kb.get_device_alias_index()
                matched_alias = None
                for alias in sorted(alias_index.keys(), key=len, reverse=True):
                    if len(alias) >= 2 and alias in user_message:
                        matched_alias = alias
                        break
                if matched_alias:
                    equipment = matched_alias
                else:
                    rov_match = self.kb._find_rov(user_message)
                    if rov_match:
                        equipment = rov_match.get("full_name") or rov_match.get("variant_name")
                    else:
                        unit_match = self.kb.resolve_robot_unit(user_message)
                        if unit_match:
                            equipment = unit_match.get("robot", {}).get("full_name") or unit_match.get("unit_id")
            has_realtime = False
            if equipment and (
                as_environment_status
                or route.query_intent in ("DEVICE_STATUS", "DEVICE_CAPABILITY", "ENVIRONMENT_QUERY")
            ):
                state_dict = self.kb.get_robot_state_dict(equipment)
                if state_dict and any(v is not None for v in state_dict.values()):
                    has_realtime = True

            if has_realtime:
                if as_environment_status:
                    return self.build_environment_status_reply(equipment, state_dict)
                status_evidence = {
                    "query_type": route.query_intent,
                    "target": equipment,
                    "state_data": state_dict,
                    "found": True,
                }
            else:
                if as_environment_status:
                    return "环境状态由当前机器人实时遥测提供；当前会话未绑定可读取的机器人状态源，请先指定或选择具体机器人。"
                return "当前实时状态源尚未建立或暂时不可用，无法确认设备/环境的最新状态。"

        messages = build_status_responder_messages(status_evidence, self.conversation_history, user_message)
        reply = self._call_safe_llm_chat(messages, temperature=0.1, role=ModelRole.KNOWLEDGE_QA)
        if not reply or not reply.strip():
            return f"当前任务处于【{self.phase}】阶段，已收集 {len(self._last_built_json)} 个字段。"
        reply = self.align_status_reply_with_backend_facts(reply, state_dict)
        return self._call_safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)

    _handle_status_query = handle_status_query

    def build_environment_status_reply(self, equipment: str, state_dict: dict) -> str:
        """构建结构化环境水文遥测回复。"""
        fields = [
            ("current_velocity", "海流流速", " m/s"),
            ("water_current_velocity", "海流流速", " m/s"),
            ("water_turbidity", "水体浑浊度", ""),
            ("turbidity", "水体浑浊度", ""),
            ("visibility_m", "能见度", " m"),
            ("obstacle_density", "障碍物密度", ""),
            ("mothership_support", "母船支援", ""),
            ("overall_status", "机器人状态", ""),
        ]
        lines = [
            "已读取当前机器人采集的实时环境状态：",
            f"- 数据来源机器人：{equipment}",
        ]
        emitted_labels: set[str] = set()
        for key, label, unit in fields:
            value = state_dict.get(key)
            if value is None or label in emitted_labels:
                continue
            formatted_val = format_telemetry_value(value) if isinstance(value, str) else value
            lines.append(f"- {label}：{formatted_val}{unit}")
            emitted_labels.add(label)

        timestamp = state_dict.get("update_timestamp")
        if timestamp:
            lines.append(f"- 更新时间：{timestamp}")
        version = state_dict.get("version")
        if version is not None:
            lines.append(f"- 状态版本：{version}")
        if len(lines) == 2:
            lines.append("- 环境遥测：当前状态源未提供可展示的环境字段。")
        return "\n".join(lines)

    _build_environment_status_reply = build_environment_status_reply

    def align_status_reply_with_backend_facts(self, reply: str, state_dict: dict | None) -> str:
        """后端数据硬对齐护栏：强制校对并替换 LLM 回复中与后端真理源不一致的所有遥测数值、文本与单位。"""
        if not isinstance(state_dict, dict) or not reply:
            return reply

        # 1. 强制水流速度对齐 (water_current_velocity / current_velocity)
        vel = state_dict.get("water_current_velocity")
        if vel is None:
            vel = state_dict.get("current_velocity")
        if vel is not None:
            try:
                vel_val = float(vel)
                vel_str = f"{vel_val:.2f}".rstrip("0").rstrip(".") if vel_val % 1 != 0 else str(int(vel_val))
                pattern = r"(海流流速|水流速度|海流速度|流速)\s*[:：]\s*(\d+(?:\.\d+)?)\s*(?:\([^)]*\)|[a-zA-Z/米秒]*)"

                def replace_vel(match):
                    prefix = match.group(1)
                    return f"{prefix}：{vel_str} m/s"

                reply = re.sub(pattern, replace_vel, reply)
            except (ValueError, TypeError):
                logger.debug("Failed to normalize reply water velocity sync. vel=%r", vel)

        # 2. 强制水体浑浊度对齐 (water_turbidity / turbidity)
        turb = state_dict.get("water_turbidity")
        if turb is None:
            turb = state_dict.get("turbidity")
        if turb is not None:
            try:
                turb_val = float(turb)
                turb_str = f"{turb_val:.1f}".rstrip("0").rstrip(".") if turb_val % 1 != 0 else str(int(turb_val))
                pattern = r"(水体浑浊度|浑浊度)\s*[:：]\s*(\d+(?:\.\d+)?)\s*(?:\([^)]*\)|[a-zA-Z/]*)"

                def replace_turb(match):
                    prefix = match.group(1)
                    return f"{prefix}：{turb_str} NTU"

                reply = re.sub(pattern, replace_turb, reply)
            except (ValueError, TypeError):
                logger.debug("Failed to normalize reply turbidity sync. turb=%r", turb)

        # 3. 强制障碍物密度对齐 (obstacle_density)
        obs = state_dict.get("obstacle_density")
        if obs is not None:
            obs_map = {"low": "低 (low)", "medium": "中 (medium)", "high": "高 (high)"}
            obs_str = obs_map.get(str(obs).lower(), str(obs))
            pattern = r"(障碍物密度)\s*[:：]\s*[\w\u4e00-\u9fa5\(\)\s]+"
            reply = re.sub(pattern, f"\\1：{obs_str}", reply)

        # 4. 强制母船支持对齐 (mothership_support)
        ship = state_dict.get("mothership_support")
        if ship is not None:
            ship_map = {"strong": "强 (strong)", "weak": "弱 (weak)", "none": "无 (none)"}
            ship_str = ship_map.get(str(ship).lower(), str(ship))
            pattern = r"(母船支持|母船支持能力)\s*[:：]\s*[\w\u4e00-\u9fa5\(\)\s]+"
            reply = re.sub(pattern, f"\\1：{ship_str}", reply)

        # 5. 强制整体状态对齐 (overall_status / status)
        ov = state_dict.get("overall_status") or state_dict.get("status")
        if ov is not None:
            ov_map = {
                "available": "可用 (available)",
                "busy": "繁忙 (busy)",
                "maintenance": "维护中 (maintenance)",
                "offline": "离线 (offline)",
            }
            ov_str = ov_map.get(str(ov).lower(), str(ov))
            pattern = r"(整体状态|设备整体状态|当前整体状态)\s*[:：]\s*[\w\u4e00-\u9fa5\(\)\s]+"
            reply = re.sub(pattern, f"\\1：{ov_str}", reply)

        # 6. 强制生存状态对齐 (survival_status)
        surv = state_dict.get("survival_status")
        if surv is not None:
            surv_map = {"normal": "正常 (normal)", "warning": "预警 (warning)", "critical": "危急 (critical)"}
            surv_str = surv_map.get(str(surv).lower(), str(surv))
            pattern = r"(生存状态|设备生存状态)\s*[:：]\s*[\w\u4e00-\u9fa5\(\)\s]+"
            reply = re.sub(pattern, f"\\1：{surv_str}", reply)

        # 7. 强制状态版本号对齐 (version)
        ver = state_dict.get("version")
        if ver is not None:
            try:
                ver_str = str(ver)
                pattern = r"(状态版本号|版本号|version)\s*[:：]\s*\d+"
                reply = re.sub(pattern, rf"\1：{ver_str}", reply)
            except (TypeError, ValueError):
                logger.debug("Failed to align version in status reply. ver=%r", ver)

        # 8. 强制最后更新时间对齐 (updated_at / update_timestamp)
        up_time = state_dict.get("updated_at") or state_dict.get("update_timestamp")
        if up_time is not None:
            up_str = str(up_time)
            pattern = r"(最后更新时间|更新时间)\s*[:：]\s*[\d\-\:\.\+T\s]+"
            reply = re.sub(pattern, f"\\1：{up_str}", reply)

        return reply

    _align_status_reply_with_backend_facts = align_status_reply_with_backend_facts

    def _call_safe_llm_chat(self, messages: list[dict], temperature: float = 0.7, role: ModelRole | str | None = None) -> str:
        fn = getattr(self.manager, "_safe_llm_chat", None)
        if callable(fn):
            return fn(messages, temperature=temperature, role=role)
        llm = getattr(self.manager, "llm", None)
        if not llm:
            return ""
        try:
            return llm.chat(messages, temperature=temperature, role=role)
        except Exception as e:
            logger.error("[TelemetryStatusHandler] safe_llm_chat failed: %s", e)
            return ""

    def _call_safe_llm_filter_reply(self, reply: str, role: ModelRole = ModelRole.FILTER_REPLY) -> str:
        fn = getattr(self.manager, "_safe_llm_filter_reply", None)
        if callable(fn):
            return fn(reply, role=role)
        return reply

    def can_handle(self, ctx: Any) -> bool:
        return False

    def handle(self, ctx: Any) -> Any:
        raise NotImplementedError
