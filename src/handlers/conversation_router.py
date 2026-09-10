"""
src/handlers/conversation_router.py - 普通对话与任务路由生命周期处理器

职责：
1. 全局离题检测（Off-Topic Gate，通过 off_topic_gate 子模块）；
2. 独立时间/状态查询快捷处理；
3. 意图分发（普通 LLM 对话、知识库问答、环境遥测查询、澄清提问、意图降级）；
4. 专有知识目录介绍与模式切换（通过 grounded_catalog 和 telemetry_status 子模块）；
5. 非任务查询状态不变性保障。
"""

from __future__ import annotations

import copy
import dataclasses
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import BaseDialogueHandler, DialogueContext, HandlerResult
from .off_topic_gate import (
    check_off_topic_gate,
    is_off_topic_output,
    _check_off_topic_gate,
    _OFF_TOPIC_BLACKLIST_RE,
    _STRONG_DOMAIN_WHITELIST_RE,
    _OFF_TOPIC_WHITELIST_RE,
    _OFF_TOPIC_OUTPUT_BLACKLIST_RE,
)
from .grounded_catalog import GroundedCatalogHandler, FIELD_LABELS
from .telemetry_status import (
    TelemetryStatusHandler,
    _ENVIRONMENT_STATUS_SUBJECT_TERMS,
    _REALTIME_STATUS_TERMS,
)
from ..intent_router import IntentRouteResult
from ..time_context import is_standalone_time_query, get_time_context
from ..visible_selection_provenance import (
    build_candidate_terms,
    parse_ordinal_reference,
    visible_ordinal_matches_candidate,
)
from ..knowledge_retriever import (
    KnowledgeBase,
    RobotSelectionDataError,
    format_seabed_type,
    format_telemetry_value,
)
from ..prompts import (
    OFF_TOPIC_REJECT_TEMPLATE,
    PUBLIC_IDENTITY_REPLY,
    build_responder_messages,
    build_general_chat_messages,
    build_knowledge_responder_messages,
    build_status_responder_messages,
)
from ..model_profile import ModelRole, _is_unsupported_role_keyword_error
from ..constants import (
    FIELD_LABELS as CORE_FIELD_LABELS,
    RECOMMENDATION_FIELD_BY_SUBJECT,
)

logger = logging.getLogger(__name__)


class ConversationRouterHandler(BaseDialogueHandler):
    """普通对话与意图路由分发处理器（HSM 分层状态机主入口）"""

    def __init__(self, manager: Any):
        super().__init__(manager)
        self.grounded_catalog = GroundedCatalogHandler(manager)
        self.telemetry_status = TelemetryStatusHandler(manager)

    def __getattr__(self, name: str) -> Any:
        for sub in ("grounded_catalog", "telemetry_status"):
            if sub in self.__dict__ and hasattr(self.__dict__[sub], name):
                return getattr(self.__dict__[sub], name)
        return super().__getattr__(name)

    # --- 代理 DialogueManager 上下文属性，确保下沉逻辑零摩擦访问 ---
    @property
    def slot_store(self): return getattr(self.manager, "slot_store", None)
    @property
    def task_state(self): return getattr(self.manager, "task_state", {})
    @task_state.setter
    def task_state(self, val): self.manager.task_state = val
    @property
    def llm(self): return getattr(self.manager, "llm", None)
    @property
    def kb(self): return getattr(self.manager, "kb", None)
    @property
    def builder(self): return getattr(self.manager, "builder", None)
    @property
    def extractor(self): return getattr(self.manager, "extractor", None)
    @property
    def conversation_history(self): return getattr(self.manager, "conversation_history", [])
    @property
    def mode(self): return getattr(self.manager, "mode", "normal")
    @property
    def phase(self): return getattr(self.manager, "phase", "idle")
    @property
    def _last_built_json(self): return getattr(self.manager, "_last_built_json", {})
    @property
    def _last_missing(self): return getattr(self.manager, "_last_missing", [])
    @property
    def _pending_rov_candidates(self): return getattr(self.manager, "_pending_rov_candidates", None)
    @property
    def _last_visible_catalog_items(self): return self.manager._last_visible_catalog_items
    @_last_visible_catalog_items.setter
    def _last_visible_catalog_items(self, val): self.manager._last_visible_catalog_items = val
    @property
    def _last_discussed_task_type(self): return self.manager._last_discussed_task_type
    @_last_discussed_task_type.setter
    def _last_discussed_task_type(self, val): self.manager._last_discussed_task_type = val
    @property
    def _last_discussed_robot(self): return self.manager._last_discussed_robot
    @_last_discussed_robot.setter
    def _last_discussed_robot(self, val): self.manager._last_discussed_robot = val
    @property
    def _last_discussed_oilfield(self): return self.manager._last_discussed_oilfield
    @_last_discussed_oilfield.setter
    def _last_discussed_oilfield(self, val): self.manager._last_discussed_oilfield = val
    @property
    def _last_discussed_payload(self): return self.manager._last_discussed_payload
    @_last_discussed_payload.setter
    def _last_discussed_payload(self, val): self.manager._last_discussed_payload = val
    @property
    def intent_router(self): return self.manager.intent_router
    @property
    def capability_adapter(self): return self.manager.capability_adapter
    @property
    def slot_filter(self): return self.manager.slot_filter

    def _switch_dialogue_mode(self, *args, **kwargs):
        return self.manager._switch_dialogue_mode(*args, **kwargs)

    def _set_execution_control_state(self, *args, **kwargs):
        return self.manager._set_execution_control_state(*args, **kwargs)

    def _transition_phase(self, *args, **kwargs):
        return self.manager._transition_phase(*args, **kwargs)

    def _ensure_payload_guidance(self, *args, **kwargs):
        return self.manager._ensure_payload_guidance(*args, **kwargs)

    # --- 生命周期门禁与处理入口 ---
    def can_handle(self, ctx: DialogueContext) -> bool:
        user_message = ctx.user_message.strip()
        if not user_message:
            return True

        if hasattr(self.manager, "_check_off_topic_gate"):
            off_topic_reply = self.manager._check_off_topic_gate(user_message)
            if off_topic_reply is not None:
                ctx.metadata["off_topic_reply"] = off_topic_reply
                return True

        if is_standalone_time_query(user_message):
            ctx.metadata["is_time_query"] = True
            return True

        return False

    def handle(self, ctx: DialogueContext) -> HandlerResult:
        user_message = ctx.user_message.strip()

        if "off_topic_reply" in ctx.metadata:
            reply = ctx.metadata["off_topic_reply"]
            self.manager.conversation_history.append({"role": "user", "content": user_message})
            self.manager.conversation_history.append({"role": "assistant", "content": reply})
            return HandlerResult.success(reply=reply)

        if ctx.metadata.get("is_time_query"):
            self.manager._switch_dialogue_mode("knowledge_qa", source="fast_path", reason="系统时间/环境状态查询")
            reply = get_time_context().user_reply
            self.manager.conversation_history.append({"role": "user", "content": user_message})
            self.manager.conversation_history.append({"role": "assistant", "content": reply})
            return HandlerResult.success(reply=reply)

        return HandlerResult.not_handled()

    # --- 完整的非任务处理集群（下沉实现） ---
    def _handle_non_task_route(self, user_message: str, route: IntentRouteResult, request_id: str) -> str:
        # 1. 记录前置快照镜像（用于严格的只读状态不变性断言）
        initial_version = self.slot_store.version
        initial_snapshot = copy.deepcopy(self.slot_store.export_snapshot())
        initial_unresolved = list(self.slot_store.unresolved)
        initial_task_state = copy.deepcopy(self.task_state)
        initial_built_json = copy.deepcopy(self._last_built_json)
        initial_missing = copy.deepcopy(self._last_missing)
        initial_phase = self.phase
        initial_mode = self.mode
        initial_rov_candidates = copy.deepcopy(self._pending_rov_candidates)

        query_intent = route.query_intent

        # 0. 优先检测基于上一轮可见选项列表的序号提问指代 (如: "介绍下第一种", "第2个水深多少")
        ordinal_ref = parse_ordinal_reference(user_message)
        if ordinal_ref and self._last_visible_catalog_items:
            idx = ordinal_ref.position
            target_item = None
            if 1 <= idx <= len(self._last_visible_catalog_items):
                target_item = self._last_visible_catalog_items[idx - 1]
            elif idx < 0 and abs(idx) <= len(self._last_visible_catalog_items):
                target_item = self._last_visible_catalog_items[idx]

            if target_item:
                item_name = target_item.get("name")
                item_type = target_item.get("type")
                item_key = target_item.get("key")

                if item_type == "task_type":
                    self._last_discussed_task_type = item_key
                    reply = self._build_grounded_single_task_introduction(item_key)
                elif item_type == "device_class":
                    self._last_discussed_robot = item_name
                    new_msg = f"介绍一下{item_name}"
                    raw_route = self.intent_router.route(new_msg, self.conversation_history, self.task_state, expected_slots=[])
                    new_plan = dataclasses.replace(raw_route.interaction_plan, query_intent="DEVICE_CAPABILITY", subject_text=item_name) if raw_route.interaction_plan else None
                    new_route = dataclasses.replace(raw_route, query_intent="DEVICE_CAPABILITY", interaction_plan=new_plan)
                    class_ans = self._build_grounded_device_class_answer(new_msg, new_route)
                    if class_ans:
                        reply = class_ans
                    else:
                        reply = self._handle_knowledge_query(new_msg, new_route, request_id)
                elif item_type == "environment":
                    self._last_discussed_oilfield = item_name
                    new_msg = f"介绍一下{item_name}"
                    raw_route = self.intent_router.route(new_msg, self.conversation_history, self.task_state, expected_slots=[])
                    new_plan = dataclasses.replace(raw_route.interaction_plan, query_intent="ENVIRONMENT_QUERY", subject_text=item_name) if raw_route.interaction_plan else None
                    new_route = dataclasses.replace(raw_route, query_intent="ENVIRONMENT_QUERY", interaction_plan=new_plan)
                    reply = self._handle_knowledge_query(new_msg, new_route, request_id)
                else:
                    new_msg = f"介绍一下{item_name}"
                    raw_route = self.intent_router.route(new_msg, self.conversation_history, self.task_state, expected_slots=[])
                    new_intent = "TOOL_QUERY" if item_type == "tool_category" else "KNOWLEDGE_QA"
                    new_plan = dataclasses.replace(raw_route.interaction_plan, query_intent=new_intent, subject_text=item_name) if raw_route.interaction_plan else None
                    new_route = dataclasses.replace(raw_route, query_intent=new_intent, interaction_plan=new_plan)
                    reply = self._handle_knowledge_query(new_msg, new_route, request_id)

                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply

        plan = route.interaction_plan if route else None
        is_targeted_device_query = bool(
            plan
            and (
                plan.relation in ("recommend", "compare")
                or plan.subject_type in ("device_class", "device_model", "equipment_family")
            )
        )

        if (
            not is_targeted_device_query
            and any(d in user_message for d in ("机器人", "设备", "装备", "ROV", "AUV"))
            and any(q in user_message for q in ("介绍", "哪些", "支持", "包含", "列表", "清单", "所有", "有哪些", "有什么"))
            and not any(e in user_message for e in ("金牛座", "天鹰座", "凤凰座", "LROV", "WROV", "通用工作级", "轻型工作级", "特种工作级", "001", "002"))
        ):
            reply = self._build_grounded_fleet_introduction()
        elif (
            not is_targeted_device_query
            and any(t in user_message for t in ("任务", "作业类型", "活", "工作"))
            and any(q in user_message for q in ("介绍", "哪些", "什么", "支持", "包含", "列表", "清单", "能做", "干什么", "能干", "有什么"))
        ):
            spec_task = self._extract_task_type_from_text(user_message)
            if spec_task:
                self._last_discussed_task_type = spec_task
                reply = self._build_grounded_single_task_introduction(spec_task)
            else:
                reply = self._build_grounded_task_catalog_introduction()
        elif (
            any(p in user_message for p in ("载荷", "工具", "传感器", "机械臂", "摄像机", "声呐", "水射流", "切断刀", "扳手", "刷洗"))
            and any(q in user_message for q in ("哪些", "什么", "支持", "包含", "列表", "清单", "有哪些", "有什么"))
            and any(q in user_message for q in ("所有", "概览", "汇总", "清单", "有哪些", "有什么"))
        ):
            reply = self._build_tool_catalog_introduction()
        elif (
            any(o in user_message for o in ("油田", "油气田", "海域", "水域", "区域"))
            and any(q in user_message for q in ("介绍", "哪些", "什么", "支持", "包含", "列表", "收录", "有什么", "在哪些"))
            and not any(of in user_message for of in ("流花", "陆丰", "文昌", "陵水", "11-1", "14-8", "16-2", "17-2"))
        ):
            reply = self._build_grounded_oilfield_catalog_introduction()
        elif (
            any(r in user_message for r in ("约束", "规则", "准入", "限制", "硬约束", "安全条件", "风控"))
            and any(q in user_message for q in ("介绍", "哪些", "什么", "说明", "要求", "有什么", "机制"))
        ):
            reply = self._build_grounded_rule_catalog_introduction()
        elif self._is_environment_status_query(user_message, route):
            reply = self._handle_status_query(user_message, route, as_environment_status=True)
        elif query_intent in ("TOOL_QUERY", "DEVICE_CAPABILITY", "KNOWLEDGE_QA"):
            reply = self._handle_knowledge_query(user_message, route, request_id)
        elif query_intent == "ENVIRONMENT_QUERY":
            plan = route.interaction_plan
            is_realtime = (
                plan is not None
                and (plan.source_policy == "realtime_state" or plan.relation == "status")
            )
            if is_realtime:
                reply = self._handle_status_query(user_message, route, as_environment_status=True)
            else:
                reply = self._handle_knowledge_query(user_message, route, request_id)
        elif query_intent in ("TASK_STATUS", "DEVICE_STATUS"):
            reply = self._handle_status_query(user_message, route)
        elif query_intent == "GENERAL_CHAT":
            reply = self._handle_general_chat(user_message, route)
        elif query_intent == "CLARIFICATION":
            reply = self._handle_clarification(user_message, route)
        elif query_intent == "UNKNOWN":
            reply = self._handle_unknown_intent(user_message, route)
        else:
            reply = self._handle_unknown_intent(user_message, route)

        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})

        # 2. 状态不变性断言与校验
        v_ok = (self.slot_store.version == initial_version)
        s_ok = (self.slot_store.export_snapshot() == initial_snapshot)
        u_ok = (self.slot_store.unresolved == initial_unresolved)
        t_ok = (self.task_state == initial_task_state)
        b_ok = (self._last_built_json == initial_built_json)
        m_ok = (self._last_missing == initial_missing)
        p_ok = (self.phase == initial_phase)
        mo_ok = (self.mode == initial_mode)
        r_ok = (self._pending_rov_candidates == initial_rov_candidates)

        if not (v_ok and s_ok and u_ok and t_ok and b_ok and m_ok and p_ok and mo_ok and r_ok):
            logger.critical(
                f"[CRITICAL] State invariance violation in non-task route '{route.query_intent}'! "
                f"ver_ok={v_ok}, snap_ok={s_ok}, unres_ok={u_ok}, state_ok={t_ok}, built_ok={b_ok}, miss_ok={m_ok}"
            )
            raise RuntimeError(f"State invariance violation in non-task route {route.query_intent}")

        return reply

    def _build_knowledge_fallback(self, kb_evidence: dict) -> str:
        query_type = kb_evidence.get("query_type")
        query_mode = kb_evidence.get("query_mode")
        if query_type == "TOOL_QUERY" or query_mode == "tool_list":
            matched_payloads = []
            tools = []
            for item in kb_evidence.get("results", []):
                if isinstance(item, dict):
                    if item.get("category") == "payload_catalog" and item.get("matched_payloads"):
                        matched_payloads = item.get("matched_payloads")
                    elif item.get("category") == "all_supported_tools":
                        tools = item.get("tools", [])

            if matched_payloads:
                p_desc = "；".join(f"【{p.get('name')}】：{p.get('description')}" for p in matched_payloads if p.get("name"))
                if p_desc:
                    return f"已查询到相关载荷说明：{p_desc}"

            if tools:
                return "当前机器人可搭载的工具与负载包括：" + "、".join(map(str, tools)) + "。"

        if (
            query_type == "DEVICE_CAPABILITY"
            and query_mode == "device_list"
        ):
            results = kb_evidence.get("results", [])
            names = [
                item.get("full_name") or item.get("display_name") or item.get("robot_class_name")
                for item in results
                if isinstance(item, dict) and (item.get("full_name") or item.get("display_name") or item.get("robot_class_name"))
            ]
            unique_names = list(dict.fromkeys(names))
            if unique_names:
                return "当前可查询的设备包括：" + "、".join(unique_names) + "。"

        if query_type == "ENVIRONMENT_QUERY":
            results = kb_evidence.get("results", [])
            for item in results:
                if isinstance(item, dict):
                    if item.get("category") == "oil_field_details":
                        of = item.get("oil_field", {})
                        seabed_cn = format_seabed_type(of.get("seabed_type"))
                        return f"【{of.get('name')}】参考水深约 {of.get('water_depth')} 米，校验水深上限 {of.get('maximum_reference_water_depth')} 米，海床类型为{seabed_cn}。说明：{of.get('notes')}"
                    elif item.get("category") == "forbidden_area_details":
                        fa = item.get("forbidden_area", {})
                        return f"【{fa.get('name')}】为生态敏感禁入保护区，坐标范围纬度 {fa.get('lat_range')}，经度 {fa.get('lon_range')}。说明：{fa.get('notes')}"
                    elif item.get("category") == "dvl_area_details":
                        da = item.get("dvl_area", {})
                        return f"【{da.get('name')}】为DVL底锁风险区，坐标范围纬度 {da.get('lat_range')}，经度 {da.get('lon_range')}。说明：{da.get('notes')}"
                    elif item.get("category") == "oil_fields_summary":
                        names = [f.get("name") for f in item.get("oil_fields", []) if f.get("name")]
                        if names:
                            return f"当前知识库收录的油气田包括：{'、'.join(names)}。"

        return "当前知识库已检索到相关信息，但暂时无法生成完整回答。"

    def _missing_field_definition(self, target_key: str) -> dict | None:
        for item in self._last_missing or []:
            if isinstance(item, dict) and item.get("key") == target_key:
                return item
        return None

    def _safe_llm_chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1500,
        role: ModelRole | str | None = None,
    ) -> str:
        try:
            return self.llm.chat(messages, temperature=temperature, max_tokens=max_tokens, role=role)
        except TypeError as exc:
            if not _is_unsupported_role_keyword_error(exc):
                raise
            return self.llm.chat(messages, temperature=temperature, max_tokens=max_tokens)

    def _safe_llm_filter_reply(
        self,
        reply: Any,
        role: ModelRole | str | None = None,
    ) -> str:
        if not hasattr(self.llm, "filter_reply"):
            return str(reply or "")
        try:
            return self.llm.filter_reply(reply, role=role)
        except TypeError as exc:
            if not _is_unsupported_role_keyword_error(exc):
                raise
            return self.llm.filter_reply(reply)
        except AttributeError:
            return str(reply or "")

    def _handle_knowledge_query(
        self,
        user_message: str,
        route: IntentRouteResult,
        request_id: str = "req_default",
    ) -> str:
        off_topic_reply = check_off_topic_gate(user_message)
        if off_topic_reply is not None:
            return off_topic_reply

        grounded_recommendation = self._build_grounded_recommendation(
            route,
            user_message=user_message,
        )
        if grounded_recommendation is not None:
            return grounded_recommendation

        grounded_class_answer = self._build_grounded_device_class_answer(
            user_message,
            route,
        )
        if grounded_class_answer is not None:
            return grounded_class_answer

        if (
            any(d in user_message for d in ("机器人", "设备", "装备", "ROV", "AUV"))
            and any(q in user_message for q in ("介绍", "哪些", "支持", "包含", "列表", "清单", "所有", "有哪些", "有什么"))
        ):
            return self._build_grounded_fleet_introduction()

        plan = route.interaction_plan
        if (
            plan is not None
            and plan.source_policy == "general_domain"
            and plan.subject_type in {"general_concept", "unknown"}
            and plan.relation != "status"
        ):
            return self._handle_general_chat(user_message, route)

        context = {
            "task_type_key": self.task_state.get("task_type_key") or self._last_discussed_task_type,
            "equipment_type": (
                self.task_state.get("equipment_type")
                or self.task_state.get("equipment_name")
                or self._last_discussed_robot
            ),
            "oilfield_name": (
                self.task_state.get("oilfield_name")
                or self._last_discussed_oilfield
            ),
            "payload_name": self._last_discussed_payload,
            "phase": self.phase,
            "mode": self.mode,
            "user_requirements": self.slot_store.get_built_json(),
            "missing_slots": [m.get("label") for m in self._last_missing if isinstance(m, dict)],
        }
        if plan is not None:
            context.update({
                "subject_type": plan.subject_type,
                "subject_text": plan.subject_text,
                "relation": plan.relation,
                "source_policy": plan.source_policy,
            })
        effective_query_type = (
            route.query_intent
            or (route.interaction_plan.query_intent if route.interaction_plan else None)
            or "KNOWLEDGE_QA"
        )
        kb_evidence = self.kb.execute_typed_query(effective_query_type, user_message, context=context)
        logger.info(
            "[KNOWLEDGE_QUERY] request_id=%s requested=%s effective=%s "
            "subject_type=%s subject_text=%r matched_entity=%s found=%s reason=%s raw_ev=%s",
            request_id,
            route.query_intent,
            kb_evidence.get("query_type"),
            context.get("subject_type"),
            context.get("subject_text"),
            kb_evidence.get("matched_entity"),
            kb_evidence.get("found"),
            kb_evidence.get("reason"),
            kb_evidence,
        )

        # 遇到确切实体检索结果时，更新上一轮讨论的设备/油田实体，便于下一轮代词指代消解
        if kb_evidence.get("found"):
            matched_alias = kb_evidence.get("matched_alias")
            if matched_alias:
                qtype = kb_evidence.get("query_type")
                if qtype == "ENVIRONMENT_QUERY":
                    self._last_discussed_oilfield = str(matched_alias)
                elif qtype in ("DEVICE_CAPABILITY", "TOOL_QUERY"):
                    self._last_discussed_robot = str(matched_alias)

        if not kb_evidence.get("found"):
            reason = kb_evidence.get("reason")
            is_system_query = (
                reason == "system_identity"
                or (context.get("subject_type") in {"system_rule"} and not any(d in user_message for d in ("机器人", "ROV", "AUV", "油田", "水深", "能力", "工具")))
                or any(kw in user_message for kw in ("你叫什么", "你是什么系统", "自我介绍", "系统功能介绍", "系统能力介绍", "你有什么能力"))
            )
            is_fleet_query = (
                any(d in user_message for d in ("机器人", "设备", "装备", "ROV", "AUV"))
                and any(q in user_message for q in ("介绍", "哪些", "什么", "支持", "包含", "列表", "清单", "所有", "推荐", "有哪些", "有什么"))
            )
            if is_fleet_query:
                return self._build_grounded_fleet_introduction()

            if is_system_query:
                return PUBLIC_IDENTITY_REPLY

            if any(kw in user_message for kw in ("机器人", "所有", "支持", "哪些", "型号", "系列")):
                class_ans = self._build_grounded_device_class_answer(user_message, route)
                if class_ans:
                    return class_ans

            if reason == "device_not_resolved":
                return "项目知识库中未找到该设备信息，请说明具体的机器人型号或名称；您也可以查询当前支持的所有机器人。"
            elif reason == "ambiguous_device_alias":
                alias = kb_evidence.get("matched_alias", "该设备")
                cands = kb_evidence.get("candidate_entities", [])
                return f"设备别名【{alias}】对应多个候选设备，请明确说明具体型号系列。"
            elif reason in ("no_matching_device", "unsupported_relation"):
                return "当前知识库暂未查到该维度的信息。您可以查询机器人的最大作业水深、支持载荷、适用任务或收录的油气田信息。"
            else:
                return "当前知识库未提供该信息。"

        if kb_evidence.get("reason") == "system_identity" or kb_evidence.get("query_mode") == "system_identity":
            return PUBLIC_IDENTITY_REPLY

        if route.query_intent == "DEVICE_CAPABILITY" and kb_evidence.get("query_mode") == "device_check":
            results = kb_evidence.get("results", [])
            depth_cond = kb_evidence.get("depth_condition", {})
            target_depth = depth_cond.get("depth_m")
            unmet_devices = [r for r in results if r.get("matches_depth_condition") is False]
            all_devices_unmet = bool(results) and len(unmet_devices) == len(results)
            if all_devices_unmet and target_depth:
                dev = unmet_devices[0]
                dev_name = dev.get("robot_class_name") or dev.get("full_name") or "目标设备"
                max_d = dev.get("max_depth_m")
                return f"已识别设备【{dev_name}】，其最大作业水深为 {max_d}米，无法满足您询问的 {target_depth}米 作业要求。"

        messages = build_knowledge_responder_messages(
            kb_evidence,
            self.conversation_history,
            user_message,
            task_state=self.task_state,
        )
        reply = self._safe_llm_chat(messages, temperature=0.1, role=ModelRole.KNOWLEDGE_QA)
        result_items = kb_evidence.get("results", [])
        all_devices_unmet = bool(result_items) and all(
            item.get("matches_depth_condition") is False
            for item in result_items
        )
        if not reply or not reply.strip() or ("符合条件" in reply and all_devices_unmet) or ("已返回相关信息" in reply):
            if route.query_intent == "DEVICE_CAPABILITY" and kb_evidence.get("query_mode") == "device_check":
                if all_devices_unmet:
                    dev = result_items[0]
                    dev_name = dev.get("robot_class_name") or dev.get("full_name") or "目标设备"
                    max_d = dev.get("max_depth_m")
                    target_d = kb_evidence.get("depth_condition", {}).get("depth_m")
                    return f"已识别设备【{dev_name}】，其最大作业水深为 {max_d}米，无法满足您询问的 {target_d}米 作业要求。"
            if kb_evidence.get("found"):
                return self._build_knowledge_fallback(kb_evidence)
            return "当前知识库未提供该信息。"
        filtered_reply = self._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)
        if is_off_topic_output(filtered_reply or ""):
            logger.warning("[OFF_TOPIC_GATE_L3] knowledge_query output blocked, forcing reject template. preview=%r",
                           (filtered_reply or "")[:120])
            return OFF_TOPIC_REJECT_TEMPLATE
        return filtered_reply

    def _handle_general_chat(self, user_message: str, route: IntentRouteResult) -> str:
        off_topic_reply = check_off_topic_gate(user_message)
        if off_topic_reply is not None:
            return off_topic_reply
        messages = build_general_chat_messages(self.conversation_history, user_message)
        reply = self._safe_llm_chat(messages, temperature=0.7, role=ModelRole.GENERAL_REASONING)
        if not reply or not reply.strip():
            reply = "您好！我是水下多智能体任务规划与决策助手。请问有什么可以帮您的？"
        filtered_reply = self._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)
        if is_off_topic_output(filtered_reply or ""):
            logger.warning("[OFF_TOPIC_GATE_L3] general_chat output blocked, forcing reject template. preview=%r",
                           (filtered_reply or "")[:120])
            return OFF_TOPIC_REJECT_TEMPLATE
        return filtered_reply

    def _handle_clarification(self, user_message: str, route: IntentRouteResult) -> str:
        plan = route.interaction_plan
        if plan is not None and plan.reason_code == "OFFLINE_SEMANTIC_MODEL_UNAVAILABLE":
            return self._handle_general_chat(user_message, route)
        if plan is not None and plan.clarification_reason:
            return plan.clarification_reason
        return "我还不能安全判断您是想查询信息还是修改任务，请再说明一下本轮目的。"

    def _handle_unknown_intent(self, user_message: str, route: IntentRouteResult) -> str:
        return "对不起，我没有完全理解您的意思。请问您是要新建水下任务、修改任务参数，还是查询设备工具与系统功能？"

    # --- 向后兼容委托：所有下沉至领域子处理器的方法保持同名直调 ---
    def _build_grounded_recommendation(self, route: IntentRouteResult, user_message: str | None = None) -> str | None:
        return self.grounded_catalog.build_grounded_recommendation(route, user_message=user_message)

    def _resolve_project_robot_classes(self, text: str) -> list[tuple[str, str]]:
        return self.grounded_catalog.resolve_project_robot_classes(text)

    def _extract_robot_entity_from_text(self, text: str) -> str | None:
        return self.grounded_catalog.extract_robot_entity_from_text(text)

    def _extract_oilfield_entity_from_text(self, text: str) -> str | None:
        return self.grounded_catalog.extract_oilfield_entity_from_text(text)

    def _extract_payload_entity_from_text(self, text: str) -> str | None:
        return self.grounded_catalog.extract_payload_entity_from_text(text)

    def _extract_task_type_from_text(self, text: str) -> str | None:
        return self.grounded_catalog.extract_task_type_from_text(text)

    def _build_grounded_single_task_introduction(self, task_type_key: str) -> str:
        return self.grounded_catalog.build_grounded_single_task_introduction(task_type_key)

    def _build_grounded_fleet_introduction(self) -> str:
        return self.grounded_catalog.build_grounded_fleet_introduction()

    def _build_grounded_task_catalog_introduction(self) -> str:
        return self.grounded_catalog.build_grounded_task_catalog_introduction()

    def _build_grounded_tool_catalog_introduction(self) -> str:
        return self.grounded_catalog.build_grounded_tool_catalog_introduction()

    _build_tool_catalog_introduction = _build_grounded_tool_catalog_introduction

    def _build_grounded_oilfield_catalog_introduction(self) -> str:
        return self.grounded_catalog.build_grounded_oilfield_catalog_introduction()

    def _build_grounded_rule_catalog_introduction(self) -> str:
        return self.grounded_catalog.build_grounded_rule_catalog_introduction()

    def _build_grounded_device_class_answer(self, user_message: str, route: IntentRouteResult) -> str | None:
        return self.grounded_catalog.build_grounded_device_class_answer(user_message, route)

    def _is_environment_status_query(self, user_message: str, route: IntentRouteResult) -> bool:
        return self.telemetry_status.is_environment_status_query(user_message, route)

    def _handle_status_query(self, user_message: str, route: IntentRouteResult, *, as_environment_status: bool = False) -> str:
        return self.telemetry_status.handle_status_query(user_message, route, as_environment_status=as_environment_status)

    def _build_environment_status_reply(self, equipment: str, state_dict: dict) -> str:
        return self.telemetry_status.build_environment_status_reply(equipment, state_dict)

    def _align_status_reply_with_backend_facts(self, reply: str, state_dict: dict | None) -> str:
        return self.telemetry_status.align_status_reply_with_backend_facts(reply, state_dict)
