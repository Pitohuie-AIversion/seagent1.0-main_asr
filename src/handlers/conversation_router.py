"""
src/handlers/conversation_router.py - 普通对话与任务路由生命周期处理器

职责：
1. 全局离题检测（Off-Topic Gate）；
2. 独立时间/状态查询快捷处理；
3. 意图分发（普通 LLM 对话、知识库问答、环境遥测查询、澄清提问、意图降级）；
4. 专有知识目录介绍与模式切换（dialogue_mode）；
5. 非任务查询状态不变性保障。
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import BaseDialogueHandler, DialogueContext, HandlerResult
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
from ..extractor import ParameterExtractor
from ..model_profile import ModelRole, _is_unsupported_role_keyword_error

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

_OFF_TOPIC_BLACKLIST_RE = re.compile(
    r"七言|绝句|律诗|写诗|写词|诗歌|作诗|赋词|笑话|段子|讲个笑|讲个段子|讲故事|小说创作|"
    r"安装\s*Python|pip\s*install|conda\s*install|安装包|软件安装|教程怎么装|Python\s*3\.|环境配置|"
    r"菜谱|做饭|怎么煮|怎么炒|食谱|今天吃什么|菜怎么做|"
    r"今天天气|天气预报|多少度|下雨吗|晴天吗|"
    r"星座|算命|运势|塔罗|占卜|生辰八字|面相|手相|"
    r"编程作业|C\+\+作业|Python作业|写代码|帮我写|代写代码|作业题|"
    r"闲聊|陪聊|打发时间|聊天|说说话|逗我|"
    r"早安|晚安|节日祝福|生日快乐|拜年",
    re.IGNORECASE,
)

_STRONG_DOMAIN_WHITELIST_RE = re.compile(
    r"水下|油田|ROV|机器人|AUV|管缆|管线|电缆|巡检|检测|维修|阀门|采油树|井口|海底|海床|深海|浅海|"
    r"海流|水流|浑浊|清澈|浑浊度|能见度|障碍物|礁石|沉积物|"
    r"载荷|工具|传感器|声呐|机械臂|摄像机|相机|采样器|切割器|扳手|FLS|DVL|USBL|MBES|SBL|LBL|CTD|ADCP|"
    r"阴极电位|电位计|厚度测定|探伤仪|液压扳手|切断刀|空化水射流|水射流|刷洗工具|"
    r"导管架|水下生产系统|SPS|脐带缆|PLET|PLEM|Manifold|管汇|飞线|Jumper|防沉板|浮体|夹持器|软管|"
    r"支持船|母船|作业船|支援船|热带风暴|波浪|快换接头|飞线插拔|海管|海缆|"
    r"任务|准入|准入条件|任务状态|设备状态|运行状态|"
    r"设备|装备|型号能力|参数|性能|功率|"
    r"水深|作业深度|最大作业水深|经纬度|坐标|经度|纬度|起始点|结束点|定位|导航|"
    r"水下作业|海上作业|船舶|作业现场|海洋工程|深水油田|浅水油田|海工|水下工程|油气田|平台|钻井|"
    r"流花|陆丰|西江|番禺|惠州|崖城|东方|陵水|渤中|锦州|绥中|"
    r"管缆类型|管道类型|电缆类型|油气管道|电力电缆|光纤通信缆|通信缆|光缆|配载|携带|带上|"
    r"观察级|工作级|轻型|重型|履带式|作业级|通用型|专用|"
    r"天鹰座|金牛座|御夫座|奇点|双子座|凤凰座|"
    r"一号机|二号机|三号机|001号|002号|003号|"
    r"起始点坐标|结束点坐标|起点|终点|"
    r"管缆|巡检任务|作业任务|管缆巡检|阀门操作|采油树|CT任务|PI任务",
    re.IGNORECASE,
)

_OFF_TOPIC_WHITELIST_RE = re.compile(
    r"水下|油田|ROV|机器人|AUV|管缆|管线|电缆|巡检|检测|维修|阀门|采油树|井口|海底|海床|深海|浅海|"
    r"海流|水流|浑浊|清澈|浑浊度|能见度|障碍物|礁石|沉积物|"
    r"载荷|工具|传感器|声呐|机械臂|摄像机|相机|采样器|切割器|扳手|FLS|DVL|USBL|"
    r"支持船|母船|作业船|支援船|热带风暴|波浪|快换接头|飞线插拔|海管|海缆|"
    r"任务|状态|阶段|槽位|发布|准入|确认|发布管理|任务状态|设备状态|运行状态|"
    r"设备|装备|型号|编号|系列|类别|型号能力|参数|性能|功率|尺寸|"
    r"水深|作业深度|最大作业水深|经纬度|坐标|经度|纬度|起始点|结束点|位置|定位|导航|"
    r"水下作业|海上作业|船舶|作业现场|海洋工程|油气田|平台|钻井|"
    r"流花|陆丰|西江|番禺|惠州|崖城|东方|陵水|渤中|锦州|绥中|"
    r"今天|明天|后天|大后天|昨日|前日|早上|早晨|上午|中午|下午|晚上|傍晚|凌晨|深夜|"
    r"点钟|点半|点整|小时|分钟|持续时间|时长|多久|开始|结束|时间|日期|期限|计划|"
    r"本周|上周|下周|星期一|星期二|星期三|星期四|星期五|星期六|星期日|周一|周二|周三|周四|周五|周六|周日|星期|本月|下月|下个月|"
    r"修改|调整|更改|改为|换成|设置|补充|添加|删除|更新|修正|变更|指定|选择|选定|采用|使用|换成|改成|换成|保留|"
    r"一样|相同|保持|不变|同样|照旧|一致|类似|差不多|沿用|继续|"
    r"管缆类型|管道类型|电缆类型|油气管道|电力电缆|光纤通信缆|通信缆|光缆|配载|携带|带上|"
    r"观察级|工作级|轻型|重型|履带式|作业级|通用型|专用|"
    r"一号机|二号机|三号机|001号|002号|003号|"
    r"开始时间|结束时间|起始点坐标|结束点坐标|起点|终点|"
    r"管缆|巡检任务|作业任务|管缆巡检|阀门操作|采油树|CT任务|PI任务",
    re.IGNORECASE,
)

_OFF_TOPIC_OUTPUT_BLACKLIST_RE = re.compile(
    r"七言|绝句|律诗|诗歌|笑话|段子|Python\s*安装|pip\s*install|conda\s*install|菜谱|食谱|天气.*度|下雨|晴天|星座|运势",
    re.IGNORECASE,
)


def _check_off_topic_gate(user_message: str) -> Optional[str]:
    """L1 确定性离题正则门控（0 token，不进 LLM）。
    返回 None 表示放行，返回 str 表示直接返回拒绝模板。"""
    if not user_message:
        return None
    msg = user_message.strip()
    if not msg:
        return None
    black_hit = _OFF_TOPIC_BLACKLIST_RE.search(msg) is not None
    if black_hit:
        strong_white_hit = _STRONG_DOMAIN_WHITELIST_RE.search(msg) is not None
        if not strong_white_hit:
            logger.info("[OFF_TOPIC_GATE_L1] blocked blacklist_hit=%s strong_white_hit=%s msg=%r",
                        black_hit, strong_white_hit, msg[:120])
            return OFF_TOPIC_REJECT_TEMPLATE
    return None

# 领域字段与推荐标签常量
FIELD_LABELS = {
    "task_type":           "作业类型",
    "equipment_class":     "机器人类别",
    "equipment_family":    "设备系列",
    "equipment_type":      "设备型号",
    "equipment_unit_id":   "具体设备编号",
    "operating_mode":      "作业模式",
    "pipeline_type":       "管线类型",
    "target_depth":        "作业水深",
    "start_point":         "起始坐标",
    "end_point":           "结束坐标",
    "work_duration":       "作业时长",
    "payload":             "搭载工具",
    "cleaning_tool":       "清洗工具",
    "inspection_sensor":   "巡检传感器",
    "operation_tool":      "操作工具",
    "oilfield_name":       "油田海域",
    "wellhead_id":         "井口编号",
}

RECOMMENDATION_FIELD_BY_SUBJECT = {
    "device_class": "equipment_family",
    "device_family": "equipment_family",
    "device": "equipment_type",
}


class ConversationRouterHandler(BaseDialogueHandler):
    """普通对话与意图路由分发处理器（HSM 分层状态机子模块）"""

    # --- 代理 DialogueManager 上下文属性，确保下沉逻辑零摩擦访问 ---
    @property
    def slot_store(self): return self.manager.slot_store
    @property
    def task_state(self): return self.manager.task_state
    @task_state.setter
    def task_state(self, val): self.manager.task_state = val
    @property
    def llm(self): return self.manager.llm
    @property
    def kb(self): return self.manager.kb
    @property
    def builder(self): return self.manager.builder
    @property
    def extractor(self): return self.manager.extractor
    @property
    def conversation_history(self): return self.manager.conversation_history
    @property
    def mode(self): return self.manager.mode
    @property
    def phase(self): return self.manager.phase
    @property
    def _last_built_json(self): return self.manager._last_built_json
    @property
    def _last_missing(self): return self.manager._last_missing
    @property
    def _pending_rov_candidates(self): return self.manager._pending_rov_candidates
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

        if (
            any(d in user_message for d in ("机器人", "设备", "装备", "ROV", "AUV"))
            and any(q in user_message for q in ("介绍", "哪些", "什么", "支持", "包含", "列表", "清单", "所有", "推荐", "有哪些", "有什么"))
            and not any(e in user_message for e in ("金牛座", "天鹰座", "凤凰座", "LROV", "WROV", "通用工作级", "轻型工作级", "特种工作级", "001", "002"))
        ):
            reply = self._build_grounded_fleet_introduction()
        elif (
            any(t in user_message for t in ("任务", "作业类型", "活", "工作"))
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
            reply = self._build_grounded_tool_catalog_introduction()
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

    def _missing_field_definition(self, key: str) -> dict | None:
        return next(
            (
                item
                for item in self._last_missing
                if isinstance(item, dict) and item.get("key") == key
            ),
            None,
        )

    def _build_grounded_recommendation(
        self,
        route: IntentRouteResult,
        user_message: str | None = None,
    ) -> str | None:
        """把模型的推荐选择约束到当前待填字段的配置候选中。

        设计原则：
        - 仅拦截 operation=READ、relation=recommend 的询问。
        - 合法候选（allowed_values）来自项目配置，是唯一可信来源。
        - LLM 的 subject_text 可能与配置名称存在出入（幻觉、别名等），
          因此优先从 allowed_values 中选取推荐值，而不依赖 subject_text 精确匹配。
        - 若 subject_type 无对应字段或当前任务无合法候选，则不拦截，
          让后续知识库检索逻辑处理。
        """
        plan = route.interaction_plan
        if plan is None or plan.operation != "READ" or plan.relation != "recommend":
            return None

        target_key = RECOMMENDATION_FIELD_BY_SUBJECT.get(plan.subject_type or "")
        if not target_key:
            # subject_type 不在推荐字段映射中，不拦截
            return None

        field_def = self._missing_field_definition(target_key)
        if not field_def and target_key == "equipment_family":
            legacy_def = self._missing_field_definition("equipment_class")
            if legacy_def:
                field_def = legacy_def
                target_key = "equipment_class"

        allowed_values = list((field_def or {}).get("allowed_values") or [])
        label = (field_def or {}).get("label") or FIELD_LABELS.get(target_key, target_key or "该字段")

        if not allowed_values:
            # 当前任务阶段无合法候选（字段尚未解析或不在缺失列表中），不拦截
            return None

        # OutputBuilder.build() 的 missing_fields 只承担确定性完整性校验，运行时
        # 不携带候选描述。推荐属于只读语义判断：从同一 task_state 下的权威 schema
        # 补齐别名和候选证据，但保留原 missing field 的 allowed_values 作为最终边界。
        semantic_field_def = dict(field_def or {})
        task_type_key = self.task_state.get("task_type_key")
        if task_type_key:
            required = self.builder.get_required(
                task_type_key,
                self.mode,
                self.task_state,
            )
            match_keys = (target_key, "equipment_family") if target_key == "equipment_class" else (target_key,)
            authoritative = next(
                (
                    item
                    for item in required
                    if isinstance(item, dict) and item.get("key") in match_keys
                ),
                {},
            )
            allowed_set = set(allowed_values)
            for evidence_key in (
                "alias_mappings",
                "ambiguous_aliases",
                "candidate_evidence",
            ):
                value = authoritative.get(evidence_key)
                if evidence_key == "candidate_evidence" and isinstance(value, list):
                    filtered_items = []
                    for item in value:
                        if not isinstance(item, dict):
                            continue
                        canon = item.get("canonical_value")
                        aliases = item.get("aliases", [])
                        if canon in allowed_set:
                            filtered_items.append(item)
                        else:
                            matched_allowed = next((a for a in allowed_set if a == canon or a in aliases), None)
                            if matched_allowed:
                                item_copy = dict(item)
                                item_copy["canonical_value"] = matched_allowed
                                filtered_items.append(item_copy)
                    value = filtered_items
                if value:
                    semantic_field_def[evidence_key] = value
        semantic_field_def["allowed_values"] = allowed_values

        selected = plan.subject_text
        task_name = self.task_state.get("task_type") or self.task_state.get("task_type_key")
        task_prefix = f"针对当前【{task_name}】任务，" if task_name else ""

        # 兼容直接调用：没有用户原句时，模型初选若已是合法候选可直接使用。
        # 真实对话中则必须结合用户原句和候选证据复核，避免 TurnPlanner 在缺少
        # 候选说明时碰巧输出一个合法、但并不符合用户偏好的枚举值。
        if not user_message and selected and selected in allowed_values:
            chosen = selected
            return (
                f"{task_prefix}我明确推荐{label}【{chosen}】。"
                "本轮仅提供建议，尚未写入任务。若接受，请确认采用该选择。"
            )

        if len(allowed_values) == 1:
            chosen = allowed_values[0]
            return (
                f"{task_prefix}当前任务的{label}推荐选项为【{chosen}】。"
                "本轮建议采用该值，尚未写入任务。若接受，请确认采用该选择。"
            )

        # 有原始用户表达时以它为唯一偏好证据；TurnPlanner 初选可能缺少候选说明，
        # 把它再次塞给消歧模型反而会制造冲突。仅在没有原句的兼容调用中使用初选。
        semantic_input = user_message or selected or ""
        if semantic_input:
            chosen = (
                ParameterExtractor._match_allowed_value(
                    semantic_input,
                    allowed_values,
                )
                or ParameterExtractor._match_alias_value(
                    semantic_input,
                    semantic_field_def,
                )
            )
            if chosen in allowed_values:
                return (
                    f"{task_prefix}我明确推荐{label}【{chosen}】。"
                    "本轮仅提供建议，尚未写入任务。若接受，请确认采用该选择。"
                )
            chosen = self.extractor.resolve_allowed_candidate(
                semantic_input,
                target_key,
                semantic_field_def,
                current_state=self.task_state,
                conversation_history=self.conversation_history,
            )
            if chosen in allowed_values:
                return (
                    f"{task_prefix}我明确推荐{label}【{chosen}】。"
                    "本轮仅提供建议，尚未写入任务。若接受，请确认采用该选择。"
                )
            # 用户没有给出足以区分候选的偏好时，允许 TurnPlanner 在合法域内
            # 直接做一次语义选择。这不是按列表顺序默认；selected 必须是模型明确
            # 输出且可通过当前字段别名归一到 allowed_values。包含明确偏好时，
            # 上面的证据解析优先。
            if selected:
                selected_chosen = (
                    ParameterExtractor._match_allowed_value(selected, allowed_values)
                    or ParameterExtractor._match_alias_value(
                        selected,
                        semantic_field_def,
                    )
                )
                if selected_chosen in allowed_values:
                    return (
                        f"{task_prefix}我明确推荐{label}【{selected_chosen}】。"
                        "本轮仅提供建议，尚未写入任务。若接受，请确认采用该选择。"
                    )
                selected_chosen = self.extractor.resolve_allowed_candidate(
                    selected,
                    target_key,
                    semantic_field_def,
                    current_state=self.task_state,
                    conversation_history=self.conversation_history,
                )
                if selected_chosen in allowed_values:
                    return (
                        f"{task_prefix}我明确推荐{label}【{selected_chosen}】。"
                        "本轮仅提供建议，尚未写入任务。若接受，请确认采用该选择。"
                    )
            if selected in allowed_values:
                return (
                    f"{task_prefix}我明确推荐{label}【{selected}】。"
                    "本轮仅提供建议，尚未写入任务。若接受，请确认采用该选择。"
                )
            logger.info(
                "[GROUNDED_RECOMMEND] subject_text=%r 无法唯一映射到合法%s候选，"
                "allowed=%r",
                selected,
                label,
                allowed_values,
            )

        # 多候选仍无法消歧时只展示权威候选，不用列表顺序伪造推荐。
        candidates = "、".join(map(str, allowed_values))
        return (
            f"{task_prefix}当前任务允许的{label}选项有：{candidates}。\n"
            "目前信息不足以可靠推荐其中一个，请补充偏好或作业侧重点。"
            "本轮尚未写入任务。"
        )

    def _resolve_project_robot_classes(self, text: str) -> list[tuple[str, str]]:
        """仅依据 robot_fleet 配置识别文本中明确提到的机器人类别。"""
        raw_text = str(text or "")
        compact = raw_text.lower().replace(" ", "")
        matched: set[str] = set()
        classes = self.kb.get_robot_classes()

        for class_id, config in classes.items():
            names = [class_id, config.get("full_name")]
            if any(
                str(name).lower().replace(" ", "") in compact
                for name in names
                if name
            ):
                matched.add(class_id)

        # “ROV”是类别族称而不是某个固定 class。仅当当前任务的权威可行域中
        # 恰好存在一个 ROV class 时才消歧，避免在全局多类别下武断映射。
        if re.search(r"(?<![A-Za-z0-9_])ROV(?![A-Za-z0-9_])", raw_text, re.IGNORECASE):
            task_type_key = self.task_state.get("task_type_key")
            if task_type_key:
                domain = self.kb.get_feasible_robot_selection_domain(
                    task_type_key,
                    self.task_state,
                )
                rov_classes = [
                    node.get("class_id")
                    for node in domain.get("classes", [])
                    if node.get("class_id") in classes
                    and (
                        str(node.get("class_id")).endswith("_rov")
                        or "ROV" in str(classes[node.get("class_id")].get("full_name") or "")
                    )
                ]
                if len(rov_classes) == 1:
                    matched.add(rov_classes[0])

        for family in self.kb.robot_fleet.get("robot_families", {}).values():
            class_id = family.get("robot_class")
            if class_id not in classes:
                continue
            names = [family.get("full_name"), *(family.get("aliases") or [])]
            if any(
                str(name).lower().replace(" ", "") in compact
                for name in names
                if name
            ):
                matched.add(class_id)

        return [
            (class_id, config.get("full_name", class_id))
            for class_id, config in classes.items()
            if class_id in matched
        ]

    def _extract_robot_entity_from_text(self, text: str) -> str | None:
        """从文本中提取提及的水下机器人名称、系列或型号代号。"""
        if not text:
            return None
        raw = str(text).strip()
        families = self.kb.robot_fleet.get("robot_families", {})
        for fid, f_cfg in families.items():
            names = [f_cfg.get("full_name"), *(f_cfg.get("aliases") or [])]
            for n in names:
                if n and n in raw:
                    return f_cfg.get("full_name") or fid
        units = self.kb.robot_fleet.get("fleet_units", [])
        if isinstance(units, dict):
            units_list = list(units.values())
        elif isinstance(units, list):
            units_list = units
        else:
            units_list = []
        for u_cfg in units_list:
            if isinstance(u_cfg, dict):
                uid = u_cfg.get("id") or u_cfg.get("unit_id") or ""
                if uid and uid in raw:
                    return uid
        classes = self.kb.get_robot_classes()
        for cid, c_cfg in classes.items():
            name = c_cfg.get("full_name", cid)
            if name in raw or cid in raw:
                return name
        return None

    def _extract_oilfield_entity_from_text(self, text: str) -> str | None:
        """从文本中提取提及的水下油气田或作业海域名称。"""
        if not text:
            return None
        raw = str(text).strip()
        oilfields = (
            self.kb.environment.get("oil_fields")
            or self.kb.environment.get("oilfields")
            or []
            if hasattr(self.kb, "environment")
            else []
        )
        if isinstance(oilfields, dict):
            oilfields_list = list(oilfields.values())
        elif isinstance(oilfields, list):
            oilfields_list = oilfields
        else:
            oilfields_list = []
        for o_cfg in oilfields_list:
            if isinstance(o_cfg, dict):
                name = o_cfg.get("name") or o_cfg.get("oilfield_name") or ""
                aliases = o_cfg.get("aliases") or []
                if (name and name in raw) or any(a in raw for a in aliases if a):
                    return name
        return None

    def _extract_payload_entity_from_text(self, text: str) -> str | None:
        """从文本中提取提及的水下载荷或机械工器具名称。"""
        if not text:
            return None
        raw = str(text).strip()
        payload_catalog = getattr(self.kb, "onboard_payloads", {}).get("payload_catalog", {}) if hasattr(self.kb, "onboard_payloads") else {}
        for pid, p_cfg in payload_catalog.items():
            name = p_cfg.get("name") or pid
            aliases = p_cfg.get("aliases") or []
            if name in raw or any(a in raw for a in aliases if a):
                return name
        kw_map = {
            "摄像机": "高清水下摄像机",
            "声呐": "前视避障声呐",
            "激光": "水下激光标尺",
            "机械手": "七功能液压机械手",
            "腐蚀": "阴极保护腐蚀检测仪",
            "厚度": "超声波厚度传感器",
        }
        for kw, canonical in kw_map.items():
            if kw in raw:
                return canonical
        return None

    def _extract_task_type_from_text(self, text: str) -> str | None:
        """从文本中提取明确提及或匹配的水下任务类型 Key。"""
        if not text:
            return None
        raw = str(text).lower()
        if any(kw in raw for kw in ("巡检", "管缆巡检", "管道巡检", "电缆巡检", "pipeline_inspection")):
            return "pipeline_inspection"
        if any(kw in raw for kw in ("埋设", "管缆埋设", "开沟埋设", "埋缆", "pipeline_burial")):
            return "pipeline_burial"
        if any(kw in raw for kw in ("采油树", "阀门", "控制面板", "阀门操作", "tree_valve_operation")):
            return "tree_valve_operation"
        return None

    def _build_grounded_single_task_introduction(self, task_type_key: str) -> str:
        templates = self.kb.task_schemas.get("task_templates", {})
        tv = templates.get(task_type_key, {})
        name = tv.get("display_name") or tv.get("name") or task_type_key
        desc = tv.get("description") or ""
        allowed_classes = tv.get("allowed_robot_classes") or tv.get("allowed_equipment_classes") or []
        cn_classes = []
        for cid in allowed_classes:
            c_info = self.kb.get_robot_classes().get(cid, {})
            cn_classes.append(c_info.get("full_name") or cid)
        class_str = f"，适用装备：{' / '.join(cn_classes)}" if cn_classes else ""
        desc_clean = desc.rstrip("。")
        return (
            f"**{name}**：{desc_clean}{class_str}。\n\n"
            f"如需开启该任务规划，请回复“开始这个任务”或直接提供作业参数（如水深、区域或机器人）。"
        )

    def _build_grounded_fleet_introduction(self) -> str:
        """依据 robot_fleet 配置返回真实水下机器人与作业装备阵列介绍。"""
        classes = self.kb.get_robot_classes()
        families = self.kb.robot_fleet.get("robot_families", {})
        lines = ["本系统当前支持以下水下机器人与作业装备阵列："]
        visible_items = []
        for idx, (cid, cinfo) in enumerate(classes.items(), start=1):
            c_name = cinfo.get("full_name", cid)
            visible_items.append({"index": idx, "name": c_name, "type": "device_class", "key": cid})
            f_names = [f.get("full_name") for f in families.values() if f.get("robot_class") == cid and f.get("full_name")]
            aliases = []
            for f in families.values():
                if f.get("robot_class") == cid and f.get("aliases"):
                    aliases.extend([a for a in f.get("aliases") if "座" in a or "HP" in a or "马力" in a])
            alias_str = f"，涵盖系列代号：{'/'.join(list(dict.fromkeys(aliases))[:3])}" if aliases else ""
            f_str = f"包含 {', '.join(f_names)}" if f_names else ""
            lines.append(f"{idx}. **{c_name}**：{f_str}{alias_str}。".strip())

        self._last_visible_catalog_items = visible_items
        lines.append("\n您可以指定具体的机器人类别或系列代号，也可由系统根据任务水深与作业需求自动为您匹配推荐。")
        return "\n".join(lines)

    def _build_grounded_task_catalog_introduction(self) -> str:
        """依据 task_schemas 配置返回真实水下作业任务类型清单。"""
        templates = self.kb.task_schemas.get("task_templates", {})
        lines = ["本系统当前支持以下水下作业任务类型："]
        visible_items = []
        for idx, (tk, tv) in enumerate(templates.items(), start=1):
            name = tv.get("display_name") or tv.get("name") or tk
            visible_items.append({"index": idx, "name": name, "type": "task_type", "key": tk})
            desc = tv.get("description") or ""
            allowed_classes = tv.get("allowed_robot_classes") or tv.get("allowed_equipment_classes") or []
            cn_classes = []
            for cid in allowed_classes:
                c_info = self.kb.get_robot_classes().get(cid, {})
                cn_classes.append(c_info.get("full_name") or cid)
            class_str = f"，适用装备：{' / '.join(cn_classes)}" if cn_classes else ""
            desc_clean = desc.rstrip("。")
            lines.append(f"{idx}. **{name}**：{desc_clean}{class_str}。".strip())
        self._last_visible_catalog_items = visible_items
        lines.append("\n您可以输入具体任务指令（例如“帮我安排一个管缆巡检任务”），系统将引导您收集参数并完成校验发布。")
        return "\n".join(lines)

    def _build_grounded_tool_catalog_introduction(self) -> str:
        """依据 robot_fleet 配置返回真实水下载荷、工具与传感器清单。"""
        payloads = self.kb.robot_fleet.get("onboard_payloads", {})
        categories: dict[str, list[str]] = {}
        for pk, pv in payloads.items():
            cat = pv.get("category") or "通用载荷"
            name = pv.get("name") or pk
            categories.setdefault(cat, []).append(name)
        lines = ["本系统当前支持以下水下载荷、工具与传感器配置："]
        visible_items = []
        for idx, (cat, items) in enumerate(categories.items(), start=1):
            visible_items.append({"index": idx, "name": cat, "type": "tool_category", "key": cat, "items": items})
            lines.append(f"{idx}. **{cat}**：{'、'.join(items)}")
        self._last_visible_catalog_items = visible_items
        lines.append("\n创建任务时，您可以随时为机器人挂载或卸载特定载荷（例如：“机械臂带上海胆式刷洗工具”）。")
        return "\n".join(lines)

    def _build_grounded_oilfield_catalog_introduction(self) -> str:
        """依据 environment_info 配置返回收录的水下油气田清单。"""
        oil_fields = self.kb.environment.get("oil_fields", [])
        lines = ["本系统知识库目前收录的水下油气田与作业海域包括："]
        visible_items = []
        for idx, of in enumerate(oil_fields, start=1):
            name = of.get("name") or "未命名油田"
            visible_items.append({"index": idx, "name": name, "type": "environment", "key": of.get("id")})
            depth = of.get("water_depth")
            max_d = of.get("maximum_reference_water_depth")
            seabed_raw = of.get("seabed_type") or "未知"
            seabed_cn = format_seabed_type(seabed_raw)
            lines.append(f"{idx}. **{name}**：参考水深 {depth}m，校验上限 {max_d}m，{seabed_cn}。")
        self._last_visible_catalog_items = visible_items
        lines.append("\n系统会根据您选择的油气田自动校验水下机器人的额定耐压水深与履带接地比压。")
        return "\n".join(lines)

    def _build_grounded_rule_catalog_introduction(self) -> str:
        """返回系统安全准入与硬约束校验规则说明。"""
        return (
            "本系统在任务创建与发布前会自动执行以下四项严格的物理与物理安全约束校验：\n"
            "1. **耐压水深与接地比压**：校验机器人额定最大深度是否满足目标油气田水深，履带式机器人额外校验海床硬度与接地比压；\n"
            "2. **海流与水体能见度**：校验现场海流是否超过 3.0 节抗流上限，能见度是否低于 0.5 米；\n"
            "3. **地理与禁航区限制**：校验作业起始点与终点坐标是否侵入生态敏感保护区或危险禁航区；\n"
            "4. **DVL底锁与配载浮力**：校验海床泥沙高度是否引发DVL失锁风险，以及工具载荷配平与电力配额。\n\n"
            "存在任何硬约束违规时系统将阻断任务发布并引导修正。"
        )

    def _build_grounded_device_class_answer(
        self,
        user_message: str,
        route: IntentRouteResult,
    ) -> str | None:
        """用 task_schemas/robot_fleet 回答类别适用任务，禁止自由补充项目事实。"""
        plan = route.interaction_plan
        if (
            plan is None
            or plan.operation != "READ"
            or plan.subject_type != "device_class"
            or plan.relation not in {"compare", "supports", "capabilities", "describe", "list"}
        ):
            return None

        # 如果是广义全量设备列表查询（如“当前支持的所有机器人”），跳过单 class 问答限制，交给全量知识库查询
        if any(kw in user_message for kw in ("所有", "全部", "清单", "有哪些机器人", "支持的所有机器人", "当前支持")):
            return None

        mentioned = self._resolve_project_robot_classes(
            f"{plan.subject_text or ''} {user_message}"
        )
        if not mentioned:
            return None

        templates = self.kb.task_schemas.get("task_templates", {})
        task_type_key = self.task_state.get("task_type_key")

        robot_classes = self.kb.get_robot_classes()
        robot_families = self.kb.robot_fleet.get("robot_families", {})

        required = (
            self.builder.get_required(
                task_type_key,
                self.mode,
                self.task_state,
            )
            if task_type_key
            else []
        )
        class_field = next(
            (field for field in required if isinstance(field, dict) and field.get("key") == "equipment_class"),
            {},
        ) if required else {}
        evidence_by_name = {
            item.get("canonical_value"): item
            for item in class_field.get("candidate_evidence", [])
            if isinstance(item, dict) and item.get("canonical_value")
        }

        results = []
        for class_id, class_name in mentioned:
            supported_tasks: list[str] = []
            for task_key, template in templates.items():
                domain = self.kb.get_feasible_robot_selection_domain(task_key)
                if any(node.get("class_id") == class_id for node in domain.get("classes", [])):
                    supported_tasks.append(template.get("display_name", task_key))

            class_info = robot_classes.get(class_id, {})
            assoc_families = [
                {
                    "family_id": f.get("family_id"),
                    "full_name": f.get("full_name"),
                    "aliases": f.get("aliases", []),
                }
                for f in robot_families.values()
                if isinstance(f, dict) and f.get("robot_class") == class_id
            ]

            results.append({
                "class_id": class_id,
                "full_name": class_name,
                "supported_tasks": supported_tasks,
                "class_info": class_info,
                "associated_families": assoc_families,
                "candidate_evidence": evidence_by_name.get(class_name, {}),
            })

        kb_evidence = {
            "found": True,
            "query_type": "DEVICE_CAPABILITY",
            "query_mode": "device_class_compare" if plan.relation == "compare" else "device_class_describe",
            "relation": plan.relation,
            "task_type": self.task_state.get("task_type") or task_type_key,
            "results": results,
            "note": "本轮未创建或修改任务，仅为只读信息展示",
        }

        messages = build_knowledge_responder_messages(
            kb_evidence,
            self.conversation_history,
            user_message,
            task_state=self.task_state,
        )
        reply = self._safe_llm_chat(
            messages,
            temperature=0.1,
            role=ModelRole.KNOWLEDGE_QA,
        )
        if reply and reply.strip() and reply.strip() != "不应调用自由回答模型":
            return self._safe_llm_filter_reply(
                reply,
                role=ModelRole.FILTER_REPLY,
            )

        lines = ["依据项目配置："]
        for item in results:
            class_name = item["full_name"]
            supported_tasks = item["supported_tasks"]
            rendered = "、".join(supported_tasks) if supported_tasks else "暂无已配置的适用任务"
            lines.append(f"- 【{class_name}】：{rendered}。")
        lines.append("以上仅说明项目知识库中已配置的适用关系，本轮未创建或修改任务。")
        return "\n".join(lines)

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
        off_topic_reply = _check_off_topic_gate(user_message)
        if off_topic_reply is not None:
            return off_topic_reply

        if (
            any(d in user_message for d in ("机器人", "设备", "装备", "ROV", "AUV"))
            and any(q in user_message for q in ("介绍", "哪些", "什么", "支持", "包含", "列表", "清单", "所有", "推荐", "有哪些", "有什么"))
        ):
            return self._build_grounded_fleet_introduction()

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
        if _OFF_TOPIC_OUTPUT_BLACKLIST_RE.search(filtered_reply or ""):
            logger.warning("[OFF_TOPIC_GATE_L3] knowledge_query output blocked, forcing reject template. preview=%r",
                           (filtered_reply or "")[:120])
            return OFF_TOPIC_REJECT_TEMPLATE
        return filtered_reply


    def _is_environment_status_query(self, user_message: str, route: IntentRouteResult) -> bool:
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

    def _handle_status_query(
        self,
        user_message: str,
        route: IntentRouteResult,
        *,
        as_environment_status: bool = False,
    ) -> str:
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
                    return self._build_environment_status_reply(equipment, state_dict)
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
        reply = self._safe_llm_chat(messages, temperature=0.1, role=ModelRole.KNOWLEDGE_QA)
        if not reply or not reply.strip():
            return f"当前任务处于【{self.phase}】阶段，已收集 {len(self._last_built_json)} 个字段。"
        reply = self._align_status_reply_with_backend_facts(reply, state_dict)
        return self._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)

    def _build_environment_status_reply(self, equipment: str, state_dict: dict) -> str:
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

    def _align_status_reply_with_backend_facts(self, reply: str, state_dict: dict | None) -> str:
        """后端数据硬对齐护栏：强制校对并替换 LLM 回复中与后端真理源不一致的所有遥测数值、文本与单位。"""
        if not isinstance(state_dict, dict) or not reply:
            return reply

        import re

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
            except Exception:
                pass

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
            except Exception:
                pass

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
            ov_map = {"available": "可用 (available)", "busy": "繁忙 (busy)", "maintenance": "维护中 (maintenance)", "offline": "离线 (offline)"}
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
            except Exception:
                pass

        # 8. 强制最后更新时间对齐 (updated_at / update_timestamp)
        up_time = state_dict.get("updated_at") or state_dict.get("update_timestamp")
        if up_time is not None:
            up_str = str(up_time)
            pattern = r"(最后更新时间|更新时间)\s*[:：]\s*[\d\-\:\.\+T\s]+"
            reply = re.sub(pattern, f"\\1：{up_str}", reply)

        return reply

    def _handle_general_chat(self, user_message: str, route: IntentRouteResult) -> str:
        off_topic_reply = _check_off_topic_gate(user_message)
        if off_topic_reply is not None:
            return off_topic_reply
        messages = build_general_chat_messages(self.conversation_history, user_message)
        reply = self._safe_llm_chat(messages, temperature=0.7, role=ModelRole.GENERAL_REASONING)
        if not reply or not reply.strip():
            reply = "您好！我是水下多智能体任务规划与决策助手。请问有什么可以帮您的？"
        filtered_reply = self._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)
        if _OFF_TOPIC_OUTPUT_BLACKLIST_RE.search(filtered_reply or ""):
            logger.warning("[OFF_TOPIC_GATE_L3] general_chat output blocked, forcing reject template. preview=%r",
                           (filtered_reply or "")[:120])
            return OFF_TOPIC_REJECT_TEMPLATE
        return filtered_reply

    def _handle_clarification(self, user_message: str, route: IntentRouteResult) -> str:
        plan = route.interaction_plan
        # 离线协议明确表示“没有语义模型”，不把内部能力状态当成对用户的澄清问题；
        # 继续走无副作用通用回复，保留 mock/降级环境的基本可用性。
        if plan is not None and plan.reason_code == "OFFLINE_SEMANTIC_MODEL_UNAVAILABLE":
            return self._handle_general_chat(user_message, route)
        if plan is not None and plan.clarification_reason:
            return plan.clarification_reason
        return "我还不能安全判断您是想查询信息还是修改任务，请再说明一下本轮目的。"

    def _handle_unknown_intent(self, user_message: str, route: IntentRouteResult) -> str:
        return "对不起，我没有完全理解您的意思。请问您是要新建水下任务、修改任务参数，还是查询设备工具与系统功能？"

    # --------------------------------------------------------------------------
    # TASK_CONFIRM 独立控制指令处理（彻底隔离于槽位抽取流水线）
    # --------------------------------------------------------------------------


