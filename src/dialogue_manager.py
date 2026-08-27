"""
dialogue_manager.py - 对话主控制器

协调提取、验证、知识检索、响应生成的完整流程。

阶段状态机:
  collecting
    → blocked_hard   (硬违规阻塞)
    → blocked_soft   (软违规阻塞)
    → confirming     (字段齐全无阻塞，等待确认)
    → done           (确认，输出最终JSON)
    → rejected       (拒绝)

约束检查策略:
  - 字段变化后增量检查
  - Hard违规阻塞，连续失败达上限则拒绝
  - Soft违规询问一次，用户可忽略并加入白名单
  - 白名单key: (field, str(value), constraint_id)，字段值变化时失效
"""

import copy
import json
import logging
import math
import re
import threading
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from .llm_client import LLMClient
from .model_profile import (
    ModelRole,
    _is_unsupported_role_keyword_error,
    is_normalization_contract_v2_enabled,
    is_session_state_v2_enabled,
    is_task_patch_v2_enabled,
)
from .session_state_shadow import (
    compare_session_state_shadow,
    record_shadow_metric,
    should_run_session_state_shadow,
)
from .session_state import (
    ConversationState,
    ExecutionControlState,
    SessionState,
    StateContractError,
    TaskLifecycleState,
    VALID_DIALOGUE_MODES,
    VALID_PHASES,
    VALID_TASK_MODES,
    session_state_from_legacy_snapshot,
    session_state_to_legacy_fields,
    validate_task_phase_transition,
)
from .normalization_contract import (
    NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS,
    NormalizationApplyPlan,
    normalize_task_patch,
    normalized_task_patch_to_apply_plan,
    validate_normalization_runtime_flags,
)
from .task_patch import build_task_patch, task_patch_to_legacy_updates
from .knowledge_retriever import KnowledgeBase, RobotSelectionDataError
from .extractor import ParameterExtractor
from .task_slot_filter import TaskSlotFilter
from .task_capability_adapter import TaskCapabilityAdapter

_USER_FACING_EXCLUDED_KEYS = {
    "raw_oilfield_name",
    "oilfield_match_status",
    "oilfield_match_confidence",
    "oilfield_match_evidence",
    "oilfield_match_candidates",
    "pending_oilfield_name",
    "pending_oilfield_candidates",
    "_rov_candidates",
}


def sanitize_user_facing_json(data: dict) -> dict:
    if not isinstance(data, dict):
        return data
    return {
        k: v for k, v in data.items()
        if not str(k).startswith("_") and str(k) not in _USER_FACING_EXCLUDED_KEYS
    }

from .normalizer import FieldNormalizer
from .output_builder import OutputBuilder
from .validator import TaskValidator, Violation, ValidationResult
from .prompts import (
    OFF_TOPIC_REJECT_TEMPLATE,
    build_responder_messages,
    build_general_chat_messages,
    build_knowledge_responder_messages,
    build_status_responder_messages,
)
from .task_intent_builder import TaskIntentBuilder
from .simulated_time import get_current_datetime
from .time_context import get_time_context, is_standalone_time_query
from .coord_parser import parse_coordinate_updates
from . import coord_parser
from .oilfield_linker import OilfieldEntityLinker, _UNSET
from . import task_intent_builder as _ti_builder_module
from .id_sequence import validate_intent_id, validate_task_id, validate_task_id_for_task_type, next_daily_id
from .slot_store import (
    BASE_SLOT_TYPES,
    Slot,
    SlotStore,
    SnapshotValidationError,
    ValidationAcknowledgement,
    normalize_slot_value_type,
    reset_slot_to_missing,
)

from .exceptions import TaskPersistenceError, IntentIdConflict, IdReservationError, TaskRollbackError
from .intent_router import IntentRouter, IntentRouteResult
from .task_request_guard import analyze_task_request
from .result_paths import get_task_dir
from .visible_selection_provenance import (
    build_candidate_terms,
    parse_ordinal_reference,
    visible_ordinal_matches_candidate,
)


HARD_REFUSAL_LIMIT = 4   # 连续拒绝上限

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

_OFF_TOPIC_WHITELIST_RE = re.compile(
    r"水下|油田|ROV|机器人|AUV|管缆|管线|电缆|巡检|检测|维修|阀门|采油树|井口|海底|海床|深海|浅海|"
    r"海流|水流|浑浊|清澈|浑浊度|能见度|障碍物|礁石|沉积物|"
    r"载荷|工具|传感器|声呐|机械臂|摄像机|相机|采样器|切割器|扳手|FLS|DVL|USBL|"
    r"支持船|母船|作业船|支援船|"
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
    white_hit = _OFF_TOPIC_WHITELIST_RE.search(msg) is not None
    if black_hit and not white_hit:
        logger.info("[OFF_TOPIC_GATE_L1] blocked blacklist_hit=%s whitelist_hit=%s msg=%r",
                    black_hit, white_hit, msg[:120])
        return OFF_TOPIC_REJECT_TEMPLATE
    return None


FIELD_LABELS = {
    "task_id":             "任务编号",
    "task_type":           "任务类型",
    "start_time":          "开始时间",
    "end_time":            "结束时间",
    "cable_position":      "管缆位置",
    "cable_type":          "管缆类型",
    "start_point":         "起始点经纬度",
    "end_point":           "结束点经纬度",
    "water_depth":         "水深（米）",
    "equipment_class":     "机器人类别",
    "equipment_family":    "机器人系列",
    "equipment_type":      "设备型号",
    "equipment_name":      "设备全称",
    "equipment_unit_id":   "具体机器人编号",
    "payload":             "携带工具",
    "support_vessel":      "支持船编号",
    "oilfield_name":       "油田名称",
    "oilfield_coordinates":"油田经纬度",
    "wellhead_id":         "井口编号",
    # 采油树不再区分立式/卧式，停用该状态标签。
    # "tree_type":           "采油树类型",
}

RECOMMENDATION_FIELD_BY_SUBJECT = {
    "device_class": "equipment_family",
    "device_family": "equipment_family",
    "device": "equipment_type",
}
ROBOT_CASCADE_FIELDS = {
    "equipment_class",
    "equipment_family",
    "equipment_type",
    "equipment_unit_id",
}
OILFIELD_CONTEXT_FIELDS = {
    "oilfield_name",
    "oilfield_coordinates",
    "raw_oilfield_name",
    "oilfield_match_status",
    "oilfield_match_confidence",
    "oilfield_match_evidence",
    "oilfield_match_candidates",
    "oilfield_entity_id",
    "pending_oilfield_name",
    "pending_oilfield_candidates",
}
TASK_TRANSITION_NON_INHERITED_FIELDS = {
    "task_type",
    "payload",
    "equipment_name",
    *ROBOT_CASCADE_FIELDS,
    *OILFIELD_CONTEXT_FIELDS,
}

# 软约束忽略关键词
SOFT_IGNORE_KEYWORDS = {"忽略", "继续", "确认", "无视", "不管", "没关系", "ok", "好的", "是"}


class DialogueManager:
    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        kb: Optional[KnowledgeBase] = None,
        session_id: str | None = None,
    ):
        if kb is None:
            kb = KnowledgeBase()
        if llm is None:
            llm = LLMClient(None, None)
        self.llm = llm
        self.kb = kb
        self.session_id = session_id
        self.extractor = ParameterExtractor(llm)
        # LHL 归一化器采用确定性规则，不依赖 LLM 猜测合法字段值。
        self.normalizer = FieldNormalizer()
        self.builder = OutputBuilder(kb)
        self.validator = TaskValidator(kb)
        self.oilfield_linker = OilfieldEntityLinker(kb.environment, getattr(kb, "constraints", None))
        self.intent_router = IntentRouter(llm)
        self.slot_filter = TaskSlotFilter(kb.task_schemas)
        self.capability_adapter = TaskCapabilityAdapter(kb.task_schemas)

        # 对话核心状态
        self.conversation_history: list[dict] = []
        self.slot_store = SlotStore(kb)
        self.task_state: dict = self.slot_store.get_task_state()
        self.mode: str = "normal"
        self.phase: str = "collecting"
        self.final_result: dict | None = None
        self.awaiting_final_confirm = False
        self.task_start_now = False

        # 约束管理状态
        self._blocking_violations: list[Violation] = []
        self.ignored_soft_warning_ids: set[str] = set()
        self.pending_warning_violations: list[Violation] = []
        self._soft_whitelist: set[tuple[str, str, str]] = set()
        self._hard_refusal_counts: dict[str, int] = {}

        # ROV候选暂存
        self._pending_rov_candidates: list[dict] = []

        # 缓存构建结果
        self._last_built_json: dict = {}
        self._last_missing: list[dict] = []

        # 会话锁（按 session 隔离并发控制）
        self._session_lock = threading.RLock()

        # 内存控制状态（Issue #10 运行期控制请求记录）
        self.control_state: str = "idle"
        self.last_control_request: dict | None = None

        # 会话模式状态（Issue #10 会话模式管理）
        self.dialogue_mode: str = "task_collection"
        self.last_mode_transition: dict | None = None
        self.mode_transition_history: list[dict] = []

    def _ensure_payload_guidance(self, text: str, missing_fields: list) -> str:
        return self.capability_adapter.format_payload_guidance(text, missing_fields)

    @property
    def task_id_preview(self) -> str | None:
        """只读预览属性：草稿阶段预估的下一个任务业务编号（仅供 UI/API 展示）。"""
        if self.slot_store:
            slot = self.slot_store.slots.get("task_id")
            if slot and slot.status == "candidate" and slot.candidate_value is not None:
                return slot.candidate_value
        return None

    def _switch_dialogue_mode(
        self,
        new_mode: str,
        *,
        source: str = "rule",
        confidence: float = 1.0,
        reason: str = "",
        restore_transition_state: tuple[dict | None, list[dict]] | None = None,
    ) -> None:
        """Issue #10 统一模式切换方法：记录切换元数据与历史轨迹。"""
        old_mode = getattr(self, "dialogue_mode", "task_collection")
        if is_session_state_v2_enabled():
            if old_mode not in VALID_DIALOGUE_MODES:
                raise StateContractError(f"Invalid old dialogue_mode in runtime: {old_mode!r}")

        changed_at = datetime.now(timezone.utc).isoformat()
        transition = {
            "from": old_mode,
            "to": new_mode,
            "source": source,
            "confidence": confidence,
            "reason": reason,
            "changed_at": changed_at,
        }

        cand_mode = new_mode
        if restore_transition_state is not None:
            restored_last, restored_history = restore_transition_state
            cand_last_transition = copy.deepcopy(restored_last)
            cand_history = copy.deepcopy(restored_history)
        elif old_mode != new_mode:
            cand_last_transition = transition
            hist = list(getattr(self, "mode_transition_history", []) or [])
            hist.append(transition)
            if len(hist) > 50:
                hist.pop(0)
            cand_history = hist
        else:
            cand_last_transition = getattr(self, "last_mode_transition", None)
            cand_history = list(getattr(self, "mode_transition_history", []) or [])

        if is_session_state_v2_enabled():
            _cand_conv = ConversationState(
                dialogue_mode=cand_mode,
                last_mode_transition=cand_last_transition,
                mode_transition_history=cand_history,
            )

        self.dialogue_mode = cand_mode
        if old_mode != new_mode or restore_transition_state is not None:
            self.last_mode_transition = cand_last_transition
            self.mode_transition_history = cand_history

    def _set_execution_control_state(
        self,
        control_state: str,
        last_control_request: dict | None,
        *,
        reason: str = "",
        source: str = "runtime",
    ) -> None:
        """Issue #10 / G3.3-B 统一 Execution Control 修改入口。

        Runtime 修改 control_state 与 last_control_request 两个字段的唯一入口。
        """
        if is_session_state_v2_enabled():
            if control_state != "idle" and self.phase != "done":
                raise StateContractError(
                    f"Cannot set non-idle execution control state '{control_state}' when task phase is '{self.phase}' (must be 'done')"
                )
            _cand_exec = ExecutionControlState(
                control_state=control_state,
                last_control_request=last_control_request,
            )

        self.control_state = control_state
        self.last_control_request = copy.deepcopy(last_control_request) if last_control_request is not None else None

    def _transition_phase(
        self,
        new_phase: str,
        *,
        reason: str = "",
        source: str = "runtime",
    ) -> None:
        """Issue #10 / G3.3-A & G3.4-A 统一 Task Phase 修改入口。

        只负责 phase 状态迁移。
        在 session_state_v2=true 时增加 old_phase, new_phase 及 transition edge 的合法性校验。
        """
        if is_session_state_v2_enabled():
            old_phase = getattr(self, "phase", "collecting")
            validate_task_phase_transition(old_phase, new_phase)

        self.phase = new_phase

    def _build_session_state_contract(self) -> SessionState:
        """从 DialogueManager 当前 Runtime 内存字段构造 SessionState 合约。无副作用，纯只读校验。"""
        conv = ConversationState(
            dialogue_mode=getattr(self, "dialogue_mode", "task_collection"),
            last_mode_transition=getattr(self, "last_mode_transition", None),
            mode_transition_history=getattr(self, "mode_transition_history", []),
        )
        task = TaskLifecycleState(
            phase=getattr(self, "phase", "collecting"),
            mode=getattr(self, "mode", "normal"),
            awaiting_final_confirm=getattr(self, "awaiting_final_confirm", False),
        )
        exec_ctrl = ExecutionControlState(
            control_state=getattr(self, "control_state", "idle"),
            last_control_request=getattr(self, "last_control_request", None),
        )
        return SessionState(
            schema_version=2,
            conversation=conv,
            task=task,
            execution=exec_ctrl,
        )

    def _apply_session_state_contract(self, state: SessionState) -> None:
        """将已校验通过的 SessionState 合约对象字段写入 DialogueManager 内存状态。"""
        fields = session_state_to_legacy_fields(state)
        self.phase = fields["phase"]
        self.mode = fields["mode"]
        self.awaiting_final_confirm = fields["awaiting_final_confirm"]
        self.dialogue_mode = fields["dialogue_mode"]
        self.last_mode_transition = copy.deepcopy(fields["last_mode_transition"])
        self.mode_transition_history = [copy.deepcopy(t) for t in fields["mode_transition_history"]]
        self.control_state = fields["control_state"]
        self.last_control_request = copy.deepcopy(fields["last_control_request"])




    # --------------------------------------------------------------------------
    # 主入口
    # --------------------------------------------------------------------------

    def process(self, user_message: str, request_id: str = "req_default") -> str:
        with self._session_lock:
            if getattr(self.slot_store, "validation_result", None) is not None or self.task_state.get("task_type_key"):
                if self._is_state_snapshot_stale():
                    try:
                        self.refresh_external_state_constraints()
                    except Exception as exc:
                        logger.warning("[DialogueManager] 自动刷新外部状态失败: %s", exc)
            reply = self._process_internal(user_message, request_id)
            self._run_session_state_shadow_check(checkpoint="process", request_id=request_id)
            return reply

    def _run_session_state_shadow_check(
        self,
        checkpoint: str,
        request_id: str | None = None,
    ) -> None:
        """在稳定边界运行 SessionState V2 Shadow 旁路比较（只读、不干预、异常隔离、日志脱敏）。"""
        if not should_run_session_state_shadow(self.session_id):
            return

        try:
            snapshot = self.export_snapshot()
            result = compare_session_state_shadow(snapshot, checkpoint=checkpoint, request_id=request_id)
            if result.classification == "PARITY":
                record_shadow_metric("parity")
                logger.info(
                    "[SESSION_STATE_SHADOW_PARITY] checkpoint=%s request_id=%s",
                    checkpoint,
                    request_id,
                )
            elif result.classification == "STRICT_REJECTED":
                record_shadow_metric("strict_rejected")
                logger.warning(
                    "[SESSION_STATE_SHADOW_STRICT_REJECTED] checkpoint=%s request_id=%s exc_type=%s",
                    checkpoint,
                    request_id,
                    result.exception_type,
                )
            elif result.classification == "MISMATCH":
                record_shadow_metric("mismatch")
                logger.warning(
                    "[SESSION_STATE_SHADOW_MISMATCH] checkpoint=%s request_id=%s diff_fields=%s",
                    checkpoint,
                    request_id,
                    result.diff_fields,
                )
        except Exception as exc:
            record_shadow_metric("error")
            logger.warning(
                "[SESSION_STATE_SHADOW_ERROR] checkpoint=%s request_id=%s exc_type=%s",
                checkpoint,
                request_id,
                type(exc).__name__,
            )

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

        if query_intent in ("TOOL_QUERY", "DEVICE_CAPABILITY", "KNOWLEDGE_QA"):
            reply = self._handle_knowledge_query(user_message, route, request_id)
        elif query_intent == "ENVIRONMENT_QUERY":
            plan = route.interaction_plan
            is_realtime = (
                plan is not None
                and (plan.source_policy == "realtime_state" or plan.relation == "status")
            )
            if is_realtime:
                reply = self._handle_status_query(user_message, route)
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
                        return f"【{of.get('name')}】参考水深约 {of.get('water_depth')} 米，校验上限 {of.get('maximum_reference_water_depth')} 米，海床类型为 {of.get('seabed_type')}。说明：{of.get('notes')}"
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
        allowed_values = list((field_def or {}).get("allowed_values") or [])
        label = FIELD_LABELS.get(target_key, target_key or "该字段")

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
            authoritative = next(
                (
                    item
                    for item in required
                    if isinstance(item, dict) and item.get("key") == target_key
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
                    value = [
                        item
                        for item in value
                        if isinstance(item, dict)
                        and item.get("canonical_value") in allowed_set
                    ]
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
            "task_type_key": self.task_state.get("task_type_key"),
            "equipment_type": (
                self.task_state.get("equipment_type")
                or self.task_state.get("equipment_name")
            ),
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
        if not kb_evidence.get("found"):
            reason = kb_evidence.get("reason")
            is_system_query = (
                reason == "system_identity"
                or context.get("subject_type") in {"system_rule", "device_family"}
                or any(kw in user_message for kw in ("你具备", "你能干", "你会", "你的能力", "你能做", "干什么", "会什么", "自我介绍", "系统功能", "系统能力", "哪些能力", "能做什么"))
            )
            if is_system_query:
                from .prompts import PUBLIC_IDENTITY_REPLY
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
                if any(kw in user_message for kw in ("能力", "系统", "功能", "机器人", "支持", "你能")):
                    from .prompts import PUBLIC_IDENTITY_REPLY
                    return PUBLIC_IDENTITY_REPLY
                return "当前暂不支持该维度的查询，您可以查询机器人的能力、载荷、所属系列或适合作业水深。"
            else:
                if any(kw in user_message for kw in ("能力", "系统", "功能", "机器人", "支持", "你能")):
                    from .prompts import PUBLIC_IDENTITY_REPLY
                    return PUBLIC_IDENTITY_REPLY
                return "当前知识库未提供该信息。"

        if kb_evidence.get("reason") == "system_identity" or kb_evidence.get("query_mode") == "system_identity":
            from .prompts import PUBLIC_IDENTITY_REPLY
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
        if not reply or not reply.strip() or ("符合条件" in reply and all_devices_unmet):
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


    def _handle_status_query(self, user_message: str, route: IntentRouteResult) -> str:
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
            equipment = self.task_state.get("equipment_name") or self.task_state.get("equipment_type")
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
            state_dict = None
            if equipment and route.query_intent in ("DEVICE_STATUS", "DEVICE_CAPABILITY", "ENVIRONMENT_QUERY"):
                state_dict = self.kb.get_robot_state_dict(equipment)
                if state_dict and any(v is not None for v in state_dict.values()):
                    has_realtime = True

            if has_realtime:
                status_evidence = {
                    "query_type": route.query_intent,
                    "target": equipment,
                    "state_data": state_dict,
                    "found": True,
                }
            else:
                return "当前实时状态源尚未建立或暂时不可用，无法确认设备/环境的最新状态。"

        messages = build_status_responder_messages(status_evidence, self.conversation_history, user_message)
        reply = self._safe_llm_chat(messages, temperature=0.1, role=ModelRole.KNOWLEDGE_QA)
        if not reply or not reply.strip():
            return f"当前任务处于【{self.phase}】阶段，已收集 {len(self._last_built_json)} 个字段。"
        reply = self._align_status_reply_with_backend_facts(reply, state_dict)
        return self._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)

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

    def _handle_task_confirm(self, user_message: str, request_id: str = "req_default") -> str:
        """处理 TASK_CONFIRM 控制指令。

        不得调用 extractor.extract_updates / slot normalization /
        _apply_updates_in_transaction / slot_store.commit_transaction。
        只修改控制状态（phase / _soft_whitelist / _blocking_violations）。
        """
        if self.phase == "blocked_soft":
            return self._handle_soft_warning_confirmation(user_message, request_id)
        elif self.phase == "confirming":
            return self._handle_final_publish_confirmation(user_message, request_id)
        else:
            # 非 confirming/blocked_soft 阶段出现确认指令 → 澄清
            reply = "当前没有待确认的任务。请先创建或补充任务参数。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

    def _refresh_validation(
        self,
        purpose: str = "interactive",
        changed_fields: Optional[set[str]] = None,
    ) -> Any:
        """DialogueManager 的单一权威校验刷新入口。"""
        task_ver = getattr(self.slot_store, "version", 1)
        prev_res = getattr(self.slot_store, "validation_result", None)
        res = self.validator.validate_task(
            self.task_state,
            task_version=task_ver,
            previous_result=prev_res,
            purpose=purpose,
        )
        self.slot_store.validation_result = res
        return res

    def _task_uses_status_ref(self, status_ref: str | None) -> bool:
        """Return whether the current task is tied to a specific robot state ref."""
        if not status_ref:
            return True

        selectors = [
            self.task_state.get("equipment_unit_id"),
            self._last_built_json.get("equipment_unit_id"),
        ]
        unit_slot = self.slot_store.slots.get("equipment_unit_id")
        if unit_slot and unit_slot.status == "valid" and unit_slot.value is not None:
            selectors.append(unit_slot.value)

        for selector in selectors:
            if selector is None or selector == "":
                continue
            try:
                resolved_ref = self.kb.state_info.resolve_status_ref(str(selector))
            except Exception:
                resolved_ref = None
            if resolved_ref == status_ref or str(selector) == status_ref:
                return True
        return False

    def refresh_external_state_constraints(self, status_ref: str | None = None) -> dict:
        """Refresh validation after external robot telemetry/state changes.

        This does not publish or edit task slots; it only synchronizes phase,
        blockers, validation_result, and missing fields with current evidence.
        """
        if self.phase in ("done", "rejected"):
            return {"refreshed": False, "reason": "terminal_phase"}
        if not self._task_uses_status_ref(status_ref):
            return {"refreshed": False, "reason": "unrelated_status_ref"}

        task_type_key = self.task_state.get("task_type_key")
        missing: list[dict] = []
        if task_type_key:
            schema = self.builder.get_schema(task_type_key, self.mode)
            user_req_schema = [
                field for field in schema
                if field.get("type") not in ("auto", "fixed")
            ]
            missing = self.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: self.builder.resolve_allowed_values(
                    field,
                    task_type_key,
                    self.task_state,
                ),
            )
            self._last_missing = missing
        else:
            self._last_missing = [{
                "key": "task_type",
                "label": "任务类型",
                "type": "string",
                "allowed_values": self.kb.get_all_task_type_values(),
            }]
            missing = self._last_missing

        purpose = "preview" if task_type_key and not missing else "interactive"
        val_res = self._refresh_validation(purpose=purpose)
        violations = self._merge_oilfield_context_violations(val_res.violations)
        hard = [v for v in violations if v.severity == "hard"]
        soft = [
            v for v in violations
            if v.severity == "soft" and not self._is_whitelisted(v)
        ]

        if hard or val_res.overall_status == "validation_error":
            self._transition_phase("blocked_hard", reason="external_state_hard_detected")
            self._blocking_violations = hard
        elif soft:
            self._transition_phase("blocked_soft", reason="external_state_soft_detected")
            self._blocking_violations = soft
        else:
            self._blocking_violations = []
            self._hard_refusal_counts.clear()
            if task_type_key and not missing:
                self._transition_phase("confirming", reason="external_state_constraints_resolved")
            else:
                self._transition_phase("collecting", reason="external_state_constraints_resolved")

        return {
            "refreshed": True,
            "phase": self.phase,
            "overall_status": val_res.overall_status,
            "hard_violations": len(hard),
            "soft_violations": len(soft),
            "missing": [m.get("key") for m in missing if isinstance(m, dict)],
        }

    def _get_valid_acknowledgements(
        self,
        validation_result: ValidationResult | None,
    ) -> list[ValidationAcknowledgement]:
        """
        过滤并返回与当前 validation_result 完全匹配的有效确认。
        至少匹配：
        - constraint_id
        - task_version
        - validation_version
        - validation_fingerprint
        - status_ref
        - state_version
        - observed_value (or field/value)
        """
        if not validation_result or not self.slot_store.validation_acknowledgements:
            return []

        status_ref = (
            validation_result.state_snapshot.get("status_ref", "")
            if validation_result.state_snapshot
            else ""
        )
        state_version = (
            validation_result.state_snapshot.get("state_version", 0)
            if validation_result.state_snapshot
            else 0
        )

        violation_map = {
            v.constraint_id: v for v in (validation_result.violations or [])
        }

        valid_acks = []
        for ack in self.slot_store.validation_acknowledgements:
            if not isinstance(ack, ValidationAcknowledgement):
                continue
            # ack 的创建版本不能晚于当前 validation_result
            if ack.task_version > validation_result.task_version:
                continue
            if ack.status_ref != status_ref:
                continue
            if ack.state_version != state_version:
                continue
            # 必须对应当前实际存在的软警告 Violation
            if ack.constraint_id not in violation_map:
                continue
            v = violation_map[ack.constraint_id]
            if ack.value != getattr(v, "observed_value", None) and ack.field not in getattr(v, "related_fields", []):
                continue
            valid_acks.append(ack)
        return valid_acks

    def _handle_soft_warning_confirmation(self, user_message: str, request_id: str) -> str:
        """blocked_soft 阶段的确认/忽略处理。

        将已确认忽略的软警告录入 SlotStore.validation_acknowledgements 绑定快照版本，
        清除 _blocking_violations，然后根据缺失槽位决定进入 collecting 或 confirming。
        """
        task_type_key = self.task_state.get("task_type_key")
        missing = []
        if task_type_key:
            req_schema = self.builder.get_schema(task_type_key, self.mode)
            user_req_schema = [f for f in req_schema if f.get("type") not in ("auto", "fixed")]
            missing = self.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: self.builder.resolve_allowed_values(
                    field,
                    task_type_key,
                    self.task_state,
                ),
            )
        if not task_type_key:
            target_purpose = "interactive"
        else:
            target_purpose = "preview" if not missing else "interactive"

        res = self._refresh_validation(purpose=target_purpose)
        status_ref = res.state_snapshot.get("status_ref") if res.state_snapshot else None
        state_ver = res.state_snapshot.get("state_version") if res.state_snapshot else None

        if self._blocking_violations:
            for v in self._blocking_violations:
                if getattr(v, "severity", "soft") == "soft":
                    ack = ValidationAcknowledgement(
                        constraint_id=v.constraint_id,
                        acknowledged_at=get_current_datetime().isoformat(timespec="seconds"),
                        task_version=res.task_version,
                        validation_version=res.validation_version,
                        validation_fingerprint=res.validation_fingerprint,
                        status_ref=status_ref or "",
                        state_version=state_ver or 0,
                        field=getattr(v, "related_fields", [""])[0] if getattr(v, "related_fields", None) else "",
                        value=getattr(v, "observed_value", None),
                    )
                    if ack not in self.slot_store.validation_acknowledgements:
                        self.slot_store.validation_acknowledgements.append(ack)
                for f in v.related_fields:
                    val = self.task_state.get(f)
                    if val is not None:
                        self._soft_whitelist.add((f, str(val), v.constraint_id))
            self._blocking_violations = []

        # 重新检查约束（使用白名单过滤后的结果）
        res = self._refresh_validation(purpose=target_purpose)
        all_violations = res.violations
        remaining_soft = [v for v in all_violations
                          if v.severity == "soft" and not self._is_whitelisted(v)]
        remaining_hard = [v for v in all_violations if v.severity == "hard"]

        if remaining_hard:
            self._transition_phase("blocked_hard", reason="hard_constraint_detected")
            self._blocking_violations = remaining_hard
        elif remaining_soft:
            self._transition_phase("blocked_soft", reason="soft_warning_detected")
            self._blocking_violations = remaining_soft
        else:
            if task_type_key:
                if not missing:
                    self._transition_phase("confirming", reason="required_slots_complete")
                else:
                    self._transition_phase("collecting", reason="required_slots_missing")
            else:
                self._transition_phase("collecting", reason="task_type_missing")

        if remaining_hard:
            reply = (
                "未能继续：重新校验发现硬约束，软警告确认不能绕过硬约束。"
                "任务尚未发布，请先修改相关参数。"
            )
        elif remaining_soft:
            reply = (
                "已记录可忽略软警告的确认，但仍有未确认的软警告。"
                "任务保持阻断且尚未发布。"
            )
        elif self.phase == "confirming":
            reply = (
                "已记录您对当前软警告的确认。任务尚未发布；"
                "所有必填字段已完整，如确认无误，请回复“确认发布”。"
            )
        else:
            labels = [
                item.get("label") or item.get("key")
                for item in self._last_missing
                if isinstance(item, dict) and (item.get("label") or item.get("key"))
            ]
            missing_text = "、".join(labels)
            suffix = (
                f"请继续补充：{missing_text}。"
                if missing_text
                else "请继续补充任务信息。"
            )
            reply = f"已记录您对当前软警告的确认。任务尚未发布；{suffix}"

        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    def _handle_final_publish_confirmation(self, user_message: str, request_id: str) -> str:
        """confirming 阶段的唯一正式确认发布处理。

        使用已有 SlotStore 内的 valid intent_id 关联并发布文件。
        不重新调用 extractor，不修改 SlotStore。
        """
        if self.phase != "confirming":
            reply = "当前没有处于等待确认状态的任务。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        prev_phase = self.phase
        prev_snap = self.slot_store.export_snapshot()
        prev_whitelist = copy.deepcopy(self._soft_whitelist)
        prev_missing = copy.deepcopy(self._last_missing)
        prev_pending_rov = copy.deepcopy(self._pending_rov_candidates)
        prev_blocking_violations = copy.deepcopy(self._blocking_violations)
        prev_hist = list(self.conversation_history)
        prev_task_start_now = self.task_start_now

        task_type_key = self.task_state.get("task_type_key")
        cand_state = copy.deepcopy(self.task_state)
        cand_built = copy.deepcopy(self._last_built_json)
        is_task_now = self.is_start_time_near_now()
        unit_id = cand_state.get("equipment_unit_id") or cand_built.get("equipment_unit_id")
        if not unit_id and self.slot_store.slots.get("equipment_unit_id"):
            unit_slot = self.slot_store.slots.get("equipment_unit_id")
            if unit_slot and unit_slot.status == "valid":
                unit_id = unit_slot.value

        # 最终约束全量检查（包含 C020 设备总体状态与 C019 遥测新鲜度核验）
        val_res = self._refresh_validation(purpose="publish")
        all_violations = val_res.violations
        has_hard = self.validator.has_hard_violations(all_violations) or val_res.overall_status == "validation_error"
        unwhitelisted_soft = [v for v in all_violations if v.severity == "soft" and not self._is_whitelisted(v)]

        # 检查缺失（排除 auto 和 fixed 等由系统自动管理的字段，如 task_id）
        if task_type_key:
            req_schema = self.builder.get_schema(task_type_key, self.mode)
            user_req_schema = [f for f in req_schema if f.get("type") not in ("auto", "fixed")]
            missing = self.slot_store.get_missing_slots(user_req_schema)
        else:
            missing = [{"key": "task_type", "label": "任务类型"}]

        if missing or has_hard or unwhitelisted_soft:
            if has_hard:
                self._transition_phase("blocked_hard", reason="publish_hard_constraint_detected")
                hard_violations = [v for v in all_violations if v.severity == "hard"]
                self._blocking_violations = hard_violations
                if hard_violations:
                    reply = "\n".join(v.message for v in hard_violations)
                else:
                    reply = val_res.error.get("message") if (val_res and val_res.error) else "当前任务参数包含硬性约束冲突，无法发布。"
            elif unwhitelisted_soft:
                self._transition_phase("blocked_soft", reason="publish_soft_warning_detected")
                self._blocking_violations = unwhitelisted_soft
                reply = "\n".join(v.message for v in unwhitelisted_soft)
            else:
                self._transition_phase("collecting", reason="publish_slots_missing")
                reply = "当前任务参数不满足发布条件，请补充或修正参数。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # 检查 intent_id 是否在 SlotStore/built_json 中有效存在 (Fail Closed)
        intent_id = cand_built.get("intent_id") or cand_state.get("intent_id")
        intent_slot = self.slot_store.slots.get("intent_id")
        if not intent_id or not intent_slot or intent_slot.status != "valid" or not validate_intent_id(intent_slot.value):
            reply = "当前任务缺少唯一任务标识(intent_id)，无法完成确认发布。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # 准备发布：正式且唯一预约任务业务编号（在跨进程锁内原子递增）。
        # reserve_task_id() 的返回值是唯一权威正式编号，必须覆盖任何草稿阶段的 preview。
        # 整个发布流程（reserve -> commit -> prepare -> staging -> publish）包含在单一 try...except 回滚保护块内。
        try:
            official_task_id = self.builder.reserve_task_id(task_type_key)

            # 正式编号通过 SlotStore 单一事务写入工作副本，维持 SSOT 与乐观锁 (store_version / version)
            publish_slots = copy.deepcopy(self.slot_store.slots)
            if "task_id" not in publish_slots:
                publish_slots["task_id"] = Slot("task_id")
            publish_slots["task_id"].value = official_task_id
            publish_slots["task_id"].status = "valid"
            publish_slots["task_id"].source = "auto_reserved"
            publish_slots["task_id"].candidate_value = None
            publish_slots["task_id"].raw_value = None
            publish_slots["task_id"].value_type = "string"
            publish_slots["task_id"].validation_error = None

            expected_version = self.slot_store.version
            self.slot_store.commit_transaction(
                publish_slots,
                list(self.slot_store.unresolved),
                request_id=request_id,
                expected_version=expected_version,
            )

            # 提交后从 SlotStore 统一重新派生权威状态，绝对不手工篡改各缓存副本
            self.task_state = self.slot_store.get_task_state()
            self._last_built_json = self.slot_store.get_built_json()

            cand_state = dict(self.task_state)
            cand_built = dict(self._last_built_json)

            # TOCTOU 防线：在最终写盘发布前核对 state_version (仅即时任务需要防线)
            if unit_id and is_task_now and val_res and getattr(val_res, "state_snapshot", None):
                try:
                    current_state_snap = self.kb.get_unit_state_snapshot(str(unit_id))
                    if current_state_snap.get("state_version") != val_res.state_snapshot.get("state_version"):
                        re_val_res = self._refresh_validation(purpose="publish")
                        # 重新校验后执行全量门禁检查：
                        # validation_error / blocked_hard -> 阻断并回滚
                        if re_val_res.overall_status in ("blocked_hard", "validation_error"):
                            raise TaskPersistenceError(f"单机 {unit_id} 的状态遥测在确认发布过程中发生变更，触发阻断告警。")
                        elif re_val_res.overall_status == "blocked_soft":
                            valid_acks = self._get_valid_acknowledgements(re_val_res)
                            unacked = [
                                v for v in (re_val_res.violations or [])
                                if getattr(v, "severity", "") == "soft" and not any(a.constraint_id == v.constraint_id for a in valid_acks)
                            ]
                            if unacked:
                                self._transition_phase("blocked_soft", reason="telemetry_soft_warning_detected")
                                self._blocking_violations = unacked
                                raise TaskPersistenceError(f"单机 {unit_id} 的状态遥测在确认发布过程中发生变更，触发未确认的软性告警。")
                        elif re_val_res.overall_status == "pending_runtime_validation" and self.task_start_now:
                            raise TaskPersistenceError(f"单机 {unit_id} 的状态遥测在确认发布过程中发生变更，实时任务无法在缺乏遥测时发布。")
                        val_res = re_val_res
                except Exception as check_exc:
                    if isinstance(check_exc, TaskPersistenceError):
                        raise check_exc
                    raise TaskPersistenceError(f"发布前单机状态复核失败 (fail closed): {check_exc}") from check_exc

            # 准备发布：仅传入匹配当前 validation_result 的有效确认
            valid_acknowledgements = self._get_valid_acknowledgements(val_res)
            ti_builder = TaskIntentBuilder(self.kb)
            ti_json_artifact = ti_builder.prepare(
                task_state=cand_state,
                built_json=cand_built,
                mode=self.mode,
                task_type_key=task_type_key,
                intent_id=intent_id,
                validation_result=val_res,
                validation_acknowledgements=valid_acknowledgements,
            )
            staging_file = ti_builder.create_staging(ti_json_artifact)

            expected_state_ver = val_res.state_snapshot.get("state_version") if (val_res and val_res.state_snapshot) else None
            if unit_id and expected_state_ver is not None and hasattr(self.kb, "state_info") and hasattr(self.kb.state_info, "guard_unit_state_version"):
                with self.kb.state_info.guard_unit_state_version(str(unit_id), expected_state_ver):
                    ti_builder.publish_staging(staging_file, ti_json_artifact)
            else:
                ti_builder.publish_staging(staging_file, ti_json_artifact)
        except Exception as exc:
            # 回滚：包含 reserve, commit_transaction, prepare, create_staging, publish_staging 在内的全流程失败保护
            target_phase = self.phase if self.phase == "blocked_soft" else prev_phase
            target_blocking = copy.deepcopy(self._blocking_violations) if self.phase == "blocked_soft" else prev_blocking_violations
            self._transition_phase(target_phase, reason="publish_rollback")
            self.final_result = None

            rollback_failed = False
            rollback_err = None
            if prev_snap:
                try:
                    self.slot_store.restore_snapshot(prev_snap)
                except Exception as rb_e:
                    rollback_failed = True
                    rollback_err = rb_e

            self.task_state = self.slot_store.get_task_state()
            self._last_built_json = self.slot_store.get_built_json()
            self._soft_whitelist = prev_whitelist
            self._pending_rov_candidates = prev_pending_rov
            self._blocking_violations = target_blocking
            self.conversation_history = prev_hist
            self.task_start_now = prev_task_start_now

            self._last_missing = prev_missing

            logger.error(
                "TaskIntent publish failed: request_id=%s, task_id=%s, intent_id=%s, err_type=%s, err=%s, rollback_failed=%s",
                request_id,
                cand_built.get("task_id", "unknown"),
                intent_id,
                type(exc).__name__,
                exc,
                rollback_failed,
                exc_info=True,
            )

            if rollback_failed:
                raise TaskRollbackError(f"TaskIntent publish failed ({exc}) and rollback error occurred: {rollback_err}") from exc
            if isinstance(exc, (TaskPersistenceError, IntentIdConflict, IdReservationError)):
                raise exc
            else:
                raise TaskPersistenceError(f"TaskIntent publish failed: {exc}") from exc

        # 发布成功
        self._transition_phase("done", reason="publish_success")
        self.task_state = self.slot_store.get_task_state()
        self._last_built_json = self.slot_store.get_built_json()
        self.final_result = self._last_built_json
        self.task_start_now = self.is_start_time_near_now()
        
        user_facing_built = sanitize_user_facing_json(cand_built)
        if self.task_start_now:
            reply = (f"✅ 信息收集完成，当前为【立即执行任务】，任务已生成并下发。\n"
                     f"{json.dumps(user_facing_built, ensure_ascii=False, indent=2)}")
        else:
            reply = (f"✅ 信息收集完成，当前为【未来规划任务】，已加入计划池。\n"
                     f"{json.dumps(user_facing_built, ensure_ascii=False, indent=2)}")
        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    def _clear_task_draft_preserving_dialogue_audit(self) -> None:
        """清空未发布任务草稿与约束，但保留会话历史与模式流转审计。"""
        task_type_key = self.task_state.get("task_type_key")
        self.slot_store = SlotStore(self.kb)
        if task_type_key:
            schema = self.builder.get_schema(task_type_key, self.mode)
            self.slot_store.init_task_slots(schema)

        self.task_state = self.slot_store.get_task_state()
        self.final_result = None
        self.awaiting_final_confirm = False
        self.task_start_now = False
        self._blocking_violations = []
        self._soft_whitelist = set()
        self._hard_refusal_counts = {}
        self._pending_rov_candidates = []
        self._last_built_json = {}
        self._last_missing = []
        self._set_execution_control_state("idle", None, reason="clear_task_draft")

    def _handle_emergency_intervention(
        self,
        user_message: str,
        route: IntentRouteResult,
        request_id: str = "req_default",
    ) -> str:
        action = route.emergency_action
        valid_actions = {"stop", "pause", "abort", "cancel"}
        if not action or action not in valid_actions:
            return self._handle_non_task_route(user_message, route, request_id)

        action_cn_map = {
            "stop": "停止",
            "pause": "暂停",
            "abort": "终止",
            "cancel": "取消",
        }
        action_cn = action_cn_map.get(action, action)

        if self.phase == "done":
            target_intent_id = self.task_state.get("intent_id") or (self._last_built_json.get("intent_id") if isinstance(self._last_built_json, dict) else None)
            target_task_id = self.task_state.get("task_id") or (self._last_built_json.get("task_id") if isinstance(self._last_built_json, dict) else None)
            target_internal_id = self.task_state.get("internal_id") or (self._last_built_json.get("internal_id") if isinstance(self._last_built_json, dict) else None)

            if is_session_state_v2_enabled():
                if not target_intent_id or not validate_intent_id(target_intent_id):
                    raise StateContractError(
                        f"Cannot create execution control request: invalid or missing target_intent_id ({target_intent_id!r}) in phase 'done'"
                    )

            req_dict = {
                "action": action,
                "status": "requested",
                "target_intent_id": target_intent_id,
                "target_task_id": target_task_id,
                "target_internal_id": target_internal_id,
                "source": route.source,
                "confidence": route.confidence,
                "reason": route.reason,
            }
            self._set_execution_control_state(
                f"{action}_requested",
                req_dict,
                reason="emergency_intervention_requested",
                source=route.source,
            )
            reply = f"已识别针对已发布任务的控制指令【{action_cn}】。该控制请求已记录，等待机器人控制适配器对接执行。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        has_active_draft = bool(self.task_state.get("task_type_key")) or any(
            s.status == "valid" and s.value is not None
            for s in self.slot_store.slots.values()
        ) or bool(self._last_built_json)

        if has_active_draft:
            if action == "cancel":
                self._clear_task_draft_preserving_dialogue_audit()
                self._transition_phase("rejected", reason="user_cancelled_draft")
                self.final_result = None
                reply = "任务已取消。如需重新规划，请重新开始。"
            else:
                reply = (
                    f"当前任务尚未发布，无正在运行的机器人实例可执行【{action_cn}】操作。"
                    f"任务草稿已保留；如需放弃草稿，请明确指示“取消当前任务”。"
                )
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply
        else:
            reply = "当前没有活动任务或可取消的未发布任务。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

    def _process_internal(self, user_message: str, request_id: str = "req_default") -> str:
        old_phase = self.phase

        off_topic_reply = _check_off_topic_gate(user_message)
        if off_topic_reply is not None:
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": off_topic_reply})
            return off_topic_reply

        if is_standalone_time_query(user_message):
            self._switch_dialogue_mode("knowledge_qa", source="fast_path", reason="系统时间/环境状态查询")
            reply = get_time_context().user_reply
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        if self.phase == "done" and (
            self._is_confirmation_only(user_message)
            or self._is_final_publish_confirmation(user_message)
        ):
            self._switch_dialogue_mode("task_collection", source="user_confirmation", reason="已发布任务重复确认")
            intent_id = self.task_state.get("intent_id") or self._last_built_json.get("intent_id")
            intent_detail = f"（intent_id: {intent_id}）" if intent_id else ""
            reply = f"任务已发布成功{intent_detail}，无需重复发布。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply


        if self.phase == "done" and self._user_requested_modification(user_message):
            self._switch_dialogue_mode("task_collection", source="user_modification", reason="已发布任务原地修改拒绝")
            intent_id = self.task_state.get("intent_id") or (self._last_built_json.get("intent_id") if isinstance(self._last_built_json, dict) else None)
            intent_detail = f"（任务ID: {intent_id}）" if intent_id else ""
            reply = f"当前任务已正式确认发布{intent_detail}并归档，无法就地修改参数。如需调整，请点击“重新开始”创建新任务，或提交工单变更申请。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        if self.phase == "blocked_hard" and (
            self._is_confirmation_only(user_message)
            or self._is_final_publish_confirmation(user_message)
            or self._is_ignore_warning(user_message)
        ):
            prev_val = getattr(self.slot_store, "validation_result", None)
            prev_has_hard = bool((prev_val and getattr(prev_val, "violations", None) and any(getattr(v, "severity", "") == "hard" for v in prev_val.violations)) or (self._blocking_violations and any(getattr(v, "severity", "") == "hard" for v in self._blocking_violations)))

            val_res = self._refresh_validation(purpose="interactive")
            current_hard = [v for v in val_res.violations if v.severity == "hard" and (getattr(self, "mode", "") != "interactive" or getattr(v, "constraint_id", "") not in ("CLASS_NOT_ALLOWED_FOR_TASK", "FAMILY_CLASS_MISMATCH"))]
            if current_hard:
                self._blocking_violations = current_hard
                return self._reject_hard_constraint_bypass(user_message)

            is_real_validation = getattr(val_res, "validation_version", 0) > 0
            if prev_has_hard and is_real_validation and getattr(val_res, "overall_status", "") in ("valid", "none"):
                self._blocking_violations = []
                self._hard_refusal_counts.clear()
                self._transition_phase("collecting", reason="external_state_constraint_resolved")
            else:
                return self._reject_hard_constraint_bypass(user_message)

        if self.phase == "blocked_soft" and self._is_ignore_warning(user_message):
            return self._handle_soft_warning_confirmation(user_message, request_id)

        if self.phase == "blocked_soft" and self._is_final_publish_confirmation(user_message):
            reply = "当前仍存在软警告。请先修改相关参数，或明确接受当前软警告后继续。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        if self.phase == "confirming":
            if self._is_final_publish_confirmation(user_message):
                return self._handle_task_confirm(user_message, request_id)
            elif self._is_confirmation_only(user_message):
                reply = "当前任务尚未发布。如确认无误，请回复‘确认发布’；如需调整，可直接说明要修改的参数。"
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply

        # ── 独立意图路由分流阶段 ──
        expected_slots = [m["key"] for m in self._last_missing if isinstance(m, dict) and "key" in m]
        expected_slot_options = [
            {
                "key": item.get("key"),
                "label": item.get("label"),
                "allowed_values": copy.deepcopy(item.get("allowed_values") or []),
                "alias_mappings": copy.deepcopy(item.get("alias_mappings") or {}),
            }
            for item in self._last_missing
            if isinstance(item, dict) and item.get("key")
        ]
        # ════════════════════════════════════════════════════════════════
        # 修复3的关键：路由切换 dialogue_mode 之前快照保存原始 mode
        #   否则 L1207-1212 _switch_dialogue_mode(route.dialogue_mode)
        #   会把原本的 task_collection 先覆盖成 knowledge_qa，
        #   后续 _already_in_task 检测（用 self.dialogue_mode）永远 False。
        route = self.intent_router.route(
            user_message=user_message,
            conversation_history=self.conversation_history,
            task_state=self.task_state,
            phase=self.phase,
            expected_slots=expected_slots,
            expected_slot_options=expected_slot_options,
        )

        self._switch_dialogue_mode(
            route.dialogue_mode,
            source=route.source,
            confidence=route.confidence,
            reason=route.reason,
        )

        plan = route.interaction_plan
        is_ignore_warning_cmd = self._is_ignore_warning(user_message)
        has_acknowledge_action = bool(
            (plan and plan.warning_action == "acknowledge")
            or (self.phase == "blocked_soft" and is_ignore_warning_cmd)
        )

        if has_acknowledge_action:
            if self.phase == "blocked_hard":
                return self._reject_hard_constraint_bypass(user_message)
            # warning_action 是 WRITE 中的次级副作用，不能在字段抽取前抢占整轮。
            # 真实模型可能把“补充参数后继续”同时误标成 acknowledge；执行器必须
            # 先尝试提取并校验字段，只有没有任何任务候选时才执行警告确认。

        pending_reply = self._resolve_pending_oilfield_confirmation(
            user_message,
            request_id=request_id,
            pending_action=plan.pending_action if plan else None,
            subject_text=plan.subject_text if plan else None,
        )
        if pending_reply is not None:
            self._switch_dialogue_mode(
                "task_collection",
                source="interaction_plan",
                reason="结构化待确认油田消解",
            )
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": pending_reply})
            return pending_reply

        if route.dialogue_mode == "emergency_intervention":
            return self._handle_emergency_intervention(user_message, route, request_id)

        if route.interaction_type == "QUERY":
            return self._handle_non_task_route(user_message, route, request_id)

        if self.phase == "done":
            is_new_task = any(kw in user_message for kw in ["重新", "新任务", "创建", "新建", "重置"]) or any(user_message.startswith(kw) for kw in ["安排", "派", "我想做", "开始做"])
            if not is_new_task:
                self._switch_dialogue_mode("task_collection", source="user_input", reason="已发布任务尝试修改")
                intent_id = self.task_state.get("intent_id") or (self._last_built_json.get("intent_id") if isinstance(self._last_built_json, dict) else None)
                intent_detail = f"（任务ID: {intent_id}）" if intent_id else ""
                reply = f"当前任务已正式确认发布{intent_detail}并归档，无法就地修改参数。如需调整，请点击“重新开始”创建新任务，或提交工单变更申请。"
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply

        compound_request = analyze_task_request(
            user_message,
            self.kb.get_all_task_type_values(),
        )
        if compound_request.should_block:
            reply = compound_request.build_reply()
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # 3. Parameter Extraction & Processing Pipeline (Atomic Transaction with Optimistic Lock)
        task_patch_v2_active = is_task_patch_v2_enabled()
        norm_v2_active = is_normalization_contract_v2_enabled()
        validate_normalization_runtime_flags(task_patch_v2_active, norm_v2_active)

        new_slots, _previous_unresolved, expected_version = self.slot_store.snapshot()
        # unresolved is turn-scoped diagnostic state.  Old parse/normalization
        # failures must not be carried into unrelated later turns; durable
        # per-field problems live on Slot.status/validation_error instead.
        new_unresolved: list = []

        task_type_slot = new_slots.get("task_type_key")
        task_type_key = (
            task_type_slot.value
            if task_type_slot
            and task_type_slot.status == "valid"
            and task_type_slot.value is not None
            else None
        )
        had_task_type_key_at_turn_start = task_type_key is not None
        current_state = self.slot_store.get_task_state()
        state_before_turn = dict(current_state)

        if task_type_key:
            schema = self.builder.get_schema(task_type_key, self.mode)
            for f in schema:
                k = f.get("key")
                if k and k not in new_slots:
                    new_slots[k] = Slot(slot_name=k, value_type=f.get("type", "string"), status="missing")

        merged_updates = {}
        merged_updates_meta = {}
        payload_mutation_failed = False
        mutation_failure_result = None
        list_mutations = []

        extraction_res = {}
        proposed_pending_rov = list(self._pending_rov_candidates)
        turn_unresolved: list = []

        def record_unresolved(result: dict) -> None:
            for item in result.get("unresolved", []):
                if item not in turn_unresolved:
                    turn_unresolved.append(item)
                if item not in new_unresolved:
                    new_unresolved.append(item)

        def reply_write_without_candidates() -> str:
            """依据事务结果回复，优先通过 LLM 依据处理事实向用户解释原因并引导。"""
            if has_acknowledge_action:
                self._switch_dialogue_mode(
                    "task_collection",
                    source="interaction_plan",
                    reason="无任务候选时执行结构化软警告确认",
                )
                if self.phase == "blocked_soft":
                    return self._handle_soft_warning_confirmation(
                        user_message,
                        request_id,
                    )
                reply = (
                    "当前没有等待确认的软警告，未执行忽略操作。"
                    "任务状态和已填写参数均未改变。"
                )
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply

            unresolved = list(turn_unresolved)
            if not unresolved:
                unresolved.append("模型没有返回可验证的任务字段候选")

            missing = (
                self.builder.get_missing_fields(
                    task_type_key,
                    self.mode,
                    self.slot_store.get_task_state(),
                )
                if task_type_key
                else []
            )
            knowledge_context = self.kb.get_context_for_state(self.task_state)
            constraint_context = self._run_constraint_check(set(), purpose="interactive")

            messages = build_responder_messages(
                task_state=self.task_state,
                built_json=self._last_built_json,
                missing_fields=missing,
                mode=self.mode,
                phase=self.phase,
                knowledge_context=knowledge_context,
                constraint_context=constraint_context,
                conversation_history=self.conversation_history,
                latest_user_message=user_message,
                ROV2type=self.kb.ROV2type,
                support_task=self.kb.get_supported_task(),
                slot_snapshot=self.slot_store.get_slot_snapshot(),
                accepted_updates={},
                unresolved_inputs=unresolved,
            )
            model_reply = self._safe_llm_chat(
                messages,
                temperature=0.7,
                max_tokens=1500,
                role=ModelRole.TASK_RESPONDER,
            )
            model_reply = self._safe_llm_filter_reply(model_reply, role=ModelRole.FILTER_REPLY)

            reply = self._ground_write_reply(
                model_reply,
                accepted_updates={},
                unresolved_inputs=unresolved,
                missing_fields=missing if missing else None,
            )
            if task_type_key is None:
                supported = self.kb.get_all_task_type_values()
                if (
                    supported
                    and "当前支持的任务类型" not in reply
                    and not any(st in reply for st in supported)
                ):
                    reply += " 当前支持的任务类型：" + "、".join(supported) + "。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        if task_type_key is None:
            # Stage 1: Extract task type
            extraction_res = self.extractor.extract_updates(
                user_message, current_state,
                task_type_key=None,
                task_type_map=self.kb.get_task_type_map(),
                required=None,
                conversation_history=self.conversation_history,
                allow_empty_for_side_effect=has_acknowledge_action,
            )

            if is_task_patch_v2_enabled():
                allowed_stage1 = {"task_type", "task_type_key", "emergency_mode"}
                patch = build_task_patch(extraction_res, allowed_keys=allowed_stage1)
                stage1_updates, _, patch_unresolved = task_patch_to_legacy_updates(patch)
                for u in patch_unresolved:
                    if u not in turn_unresolved:
                        turn_unresolved.append(u)
                    if u not in new_unresolved:
                        new_unresolved.append(u)
                if not stage1_updates:
                    if new_unresolved:
                        self.slot_store.commit_transaction(
                            new_slots,
                            new_unresolved,
                            request_id=request_id,
                            expected_version=expected_version,
                        )
                    return reply_write_without_candidates()
                for k, cand_info in stage1_updates.items():
                    merged_updates[k] = cand_info["value"]
                    merged_updates_meta[k] = cand_info
            else:
                if not extraction_res.get("slot_candidates"):
                    record_unresolved(extraction_res)
                    if new_unresolved:
                        self.slot_store.commit_transaction(
                            new_slots,
                            new_unresolved,
                            request_id=request_id,
                            expected_version=expected_version,
                        )
                    return reply_write_without_candidates()

                stage1_updates = {}
                for candidate in extraction_res.get("slot_candidates", []):
                    k = candidate["canonical_key"]
                    v = candidate["normalized_value"]
                    cand_info = {
                        "value": v,
                        "raw_value": candidate.get("raw_value"),
                        "confidence": candidate.get("confidence", 1.0),
                        "source": self._source_for_resolution_method(candidate.get("resolution_method"))
                    }
                    stage1_updates[k] = cand_info
                    merged_updates[k] = v
                    merged_updates_meta[k] = cand_info
                record_unresolved(extraction_res)

            self._apply_updates_in_transaction(stage1_updates, new_slots)

            task_type_slot = new_slots.get("task_type_key")
            task_type_key = (
                task_type_slot.value
                if task_type_slot
                and task_type_slot.status == "valid"
                and task_type_slot.value is not None
                else None
            )

        # WRITE 已由 TurnPlanner 结合上下文判定。不要再根据原句关键词决定是否
        # 调用参数抽取，否则自然表达会在模型判断后被第二道语义门静默丢弃。
        should_extract_task_parameters = bool(task_type_key)

        apply_plan: NormalizationApplyPlan | None = None

        if should_extract_task_parameters:
            # Stage 2: Extract task parameters
            current_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
            field_defs = self.builder.get_schema(task_type_key, self.mode)
            required_field_defs = self.builder.get_required(task_type_key, self.mode, current_state)
            extraction_res = self.extractor.extract_updates(
                user_message, current_state,
                task_type_key=task_type_key,
                task_type_map=self.kb.get_task_type_map(),
                required=required_field_defs,
                ROV2type=self.kb.ROV2type,
                conversation_history=self.conversation_history,
                allow_empty_for_side_effect=has_acknowledge_action,
                allow_task_type_transition=True,
            )
            if task_patch_v2_active:
                # Validate the raw extractor protocol before any semantic or
                # target-schema projection can remove malformed foreign
                # entries.  This is pure and preserves TaskPatch V2's atomic
                # fail-closed boundary for both candidates and mutations.
                build_task_patch(extraction_res, allowed_keys=None)

            (
                extracted_task_type_updates,
                duplicate_task_selector_error,
            ) = self._task_selector_updates_from_extraction(extraction_res)
            extraction_selector_error = next(
                (
                    item
                    for item in extraction_res.get("unresolved", [])
                    if "同轮具体任务类型互相冲突" in str(item)
                ),
                None,
            )
            (
                pending_task_type_key,
                effective_task_type_key,
                task_type_change_locked,
                task_type_preflight_error,
            ) = self._resolve_task_type_update_context(
                extracted_task_type_updates,
                new_slots,
            )
            task_type_preflight_error = (
                duplicate_task_selector_error
                or extraction_selector_error
                or task_type_preflight_error
            )
            if task_type_preflight_error:
                self._record_task_type_update_error(
                    new_slots,
                    task_type_preflight_error,
                )
                if task_type_preflight_error not in turn_unresolved:
                    turn_unresolved.append(task_type_preflight_error)
                if task_type_preflight_error not in new_unresolved:
                    new_unresolved.append(task_type_preflight_error)
                self.slot_store.commit_transaction(
                    new_slots,
                    new_unresolved,
                    request_id=request_id,
                    expected_version=expected_version,
                )
                return reply_write_without_candidates()

            effective_task_type_key = effective_task_type_key or task_type_key
            transition_state = dict(current_state)
            transition_state_active = False
            if (
                pending_task_type_key
                and pending_task_type_key != task_type_key
                and not task_type_change_locked
            ):
                transition_state_active = True
                transition_state = self._build_task_transition_state(
                    current_state,
                    task_type_key,
                    pending_task_type_key,
                )
                transition_shared_keys = self._task_transition_shared_field_keys(
                    task_type_key,
                    pending_task_type_key,
                )
                (
                    discovery_updates,
                    discovery_touched_keys,
                ) = self._normalize_transition_discovery_candidates(
                    extraction_res,
                    pending_task_type_key,
                    transition_state,
                    transition_shared_keys,
                )
                for touched_key in discovery_touched_keys:
                    transition_state.pop(touched_key, None)
                transition_state.update(discovery_updates)
                target_required = self.builder.get_required(
                    pending_task_type_key,
                    self.mode,
                    transition_state,
                )
                target_extraction = self.extractor.extract_updates(
                    user_message,
                    transition_state,
                    task_type_key=pending_task_type_key,
                    task_type_map=self.kb.get_task_type_map(),
                    required=target_required,
                    ROV2type=self.kb.ROV2type,
                    conversation_history=self.conversation_history,
                    allow_empty_for_side_effect=has_acknowledge_action,
                )
                if task_patch_v2_active:
                    build_task_patch(target_extraction, allowed_keys=None)
                (
                    _target_task_type_updates,
                    duplicate_target_selector_error,
                ) = self._task_selector_updates_from_extraction(
                    target_extraction,
                )
                duplicate_target_selector_error = (
                    duplicate_target_selector_error
                    or next(
                        (
                            item
                            for item in target_extraction.get("unresolved", [])
                            if "同轮具体任务类型互相冲突" in str(item)
                        ),
                        None,
                    )
                )
                if duplicate_target_selector_error:
                    self._record_task_type_update_error(
                        new_slots,
                        duplicate_target_selector_error,
                    )
                    if duplicate_target_selector_error not in turn_unresolved:
                        turn_unresolved.append(duplicate_target_selector_error)
                    if duplicate_target_selector_error not in new_unresolved:
                        new_unresolved.append(duplicate_target_selector_error)
                    self.slot_store.commit_transaction(
                        new_slots,
                        new_unresolved,
                        request_id=request_id,
                        expected_version=expected_version,
                    )
                    return reply_write_without_candidates()
                extraction_res = self._merge_task_transition_extractions(
                    extraction_res,
                    target_extraction,
                    transition_shared_keys,
                )
                (
                    final_task_type_updates,
                    duplicate_final_selector_error,
                ) = self._task_selector_updates_from_extraction(extraction_res)
                (
                    _final_pending_task_type_key,
                    final_effective_task_type_key,
                    _final_task_type_change_locked,
                    final_preflight_error,
                ) = self._resolve_task_type_update_context(
                    final_task_type_updates,
                    new_slots,
                )
                final_preflight_error = (
                    duplicate_final_selector_error or final_preflight_error
                )
                if (
                    final_preflight_error
                    or final_effective_task_type_key != pending_task_type_key
                ):
                    error = final_preflight_error or (
                        "目标任务二次抽取结果与首次任务类型不一致，请只指定一个任务类型。"
                    )
                    self._record_task_type_update_error(new_slots, error)
                    if error not in turn_unresolved:
                        turn_unresolved.append(error)
                    if error not in new_unresolved:
                        new_unresolved.append(error)
                    self.slot_store.commit_transaction(
                        new_slots,
                        new_unresolved,
                        request_id=request_id,
                        expected_version=expected_version,
                    )
                    return reply_write_without_candidates()
                effective_task_type_key = final_effective_task_type_key
                (
                    transition_updates,
                    transition_touched_keys,
                ) = self._normalize_transition_discovery_candidates(
                    extraction_res,
                    pending_task_type_key,
                    transition_state,
                    transition_shared_keys,
                )
                for touched_key in transition_touched_keys:
                    transition_state.pop(touched_key, None)
                transition_state.update(transition_updates)

            extraction_res = self._scope_confirmed_recommendation(
                extraction_res,
                plan,
                user_message,
            )
            extraction_res = self._scope_visible_ordinal_selections(
                extraction_res,
                user_message,
                self.builder.get_required(
                    effective_task_type_key,
                    self.mode,
                    transition_state,
                ),
            )
            field_defs = self.builder.get_schema(effective_task_type_key, self.mode)
            effective_schema_keys = {
                str(field.get("key"))
                for field in field_defs
                if field.get("key")
            }
            # Pre-filter equipment_class candidates compatibility promotion
            raw_candidates = extraction_res.get("slot_candidates", [])
            filtered_candidates = []
            for candidate in raw_candidates:
                if isinstance(candidate, dict) and candidate.get("canonical_key") == "equipment_class":
                    projected = self._project_legacy_equipment_class_candidate(
                        candidate,
                        effective_task_type_key,
                        transition_state,
                    )
                    if projected is not None:
                        filtered_candidates.append(projected)
                else:
                    filtered_candidates.append(candidate)

            projected_candidates, filter_unresolved = self.slot_filter.filter_candidates(
                task_type_key=effective_task_type_key,
                effective_schema_keys=effective_schema_keys,
                candidates=filtered_candidates,
                task_type_change_locked=task_type_change_locked,
                pending_task_type_key=pending_task_type_key,
                active_task_type_key=task_type_key,
            )
            extraction_res["slot_candidates"] = projected_candidates
            if filter_unresolved:
                extraction_res.setdefault("unresolved", []).extend(filter_unresolved)

            mention_guidance = self.slot_filter.check_non_template_oilfield_mention(
                task_type_key=effective_task_type_key,
                effective_schema_keys=effective_schema_keys,
                user_message=user_message,
                unresolved_list=extraction_res.get("unresolved", []),
                oilfield_linker=self.oilfield_linker,
            )
            if mention_guidance:
                extraction_res.setdefault("unresolved", []).append(mention_guidance)

            self._normalize_payload_list_mutations(extraction_res, user_message, new_slots)

            filtered_mutations = []
            for mutation in extraction_res.get("list_mutations", []):
                # Structural validation belongs to TaskPatch.  Preserve
                # malformed entries here so V2 rejects them atomically instead
                # of downgrading a missing/non-string field into an ordinary
                # foreign-schema unresolved item.
                if (
                    not isinstance(mutation, dict)
                    or not isinstance(mutation.get("field"), str)
                    or not mutation.get("field", "").strip()
                ):
                    filtered_mutations.append(mutation)
                    continue
                mutation_key = mutation["field"].strip()
                if mutation_key in effective_schema_keys:
                    filtered_mutations.append(mutation)
                    continue
                message = (
                    f"列表字段 {mutation_key or '未知字段'} 不属于目标任务 "
                    f"{effective_task_type_key}，未写入。"
                )
                extraction_res.setdefault("unresolved", []).append(message)
            extraction_res["list_mutations"] = filtered_mutations

            # Payload and other dynamically constrained fields must be
            # evaluated against the robot explicitly selected in this same
            # turn.  Project that robot tuple into an isolated sandbox first;
            # never mutate the transaction merely to compute allowed values.
            evaluation_slots, effective_state = (
                self._build_post_update_evaluation_context(
                    new_slots,
                    effective_task_type_key,
                    transition_state,
                    extraction_res,
                    transition_state_active=transition_state_active,
                )
            )
            required_field_defs = self.builder.get_required(
                effective_task_type_key,
                self.mode,
                effective_state,
            )

            if task_patch_v2_active:
                allowed_stage2 = self.extractor._allowed_candidate_keys(
                    effective_task_type_key,
                    field_defs,
                )
                patch = build_task_patch(extraction_res, allowed_keys=allowed_stage2)

                if norm_v2_active:
                    current_state_dict = dict(effective_state)

                    def allowed_resolver(fdef: dict[str, Any], state: dict[str, Any]) -> list[Any] | None:
                        return self.builder._resolve_allowed(
                            fdef,
                            effective_task_type_key,
                            state,
                        )

                    normalized_patch = normalize_task_patch(
                        patch,
                        field_defs,
                        current_state_dict,
                        allowed_resolver,
                        passthrough_keys=NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS,
                    )
                    apply_plan = normalized_task_patch_to_apply_plan(normalized_patch)

                    stage2_updates = {}
                    for succ in apply_plan.successful_updates:
                        stage2_updates[succ.key] = {
                            "value": succ.value,
                            "raw_value": succ.raw_value,
                            "confidence": succ.confidence,
                            "source": succ.source,
                        }
                        merged_updates[succ.key] = succ.value
                        merged_updates_meta[succ.key] = stage2_updates[succ.key]

                    for p in apply_plan.passthrough_slot_updates:
                        stage2_updates[p.key] = {
                            "value": p.candidate_value,
                            "raw_value": p.raw_value,
                            "confidence": p.confidence,
                            "source": p.source,
                        }
                        merged_updates[p.key] = p.candidate_value
                        merged_updates_meta[p.key] = stage2_updates[p.key]

                    for u in apply_plan.unresolved:
                        if u not in turn_unresolved:
                            turn_unresolved.append(u)
                        if u not in new_unresolved:
                            new_unresolved.append(u)

                    list_mutations = [
                        {
                            "op": m.operation,
                            "operation": m.operation,
                            "field": m.field,
                            "items": list(m.items) if m.items else [],
                            "target_items": list(m.target_items) if m.target_items else [],
                            "raw_text": m.raw_text,
                            "confidence": m.confidence,
                            "source": m.source,
                        }
                        for m in apply_plan.list_mutations
                    ]
                else:
                    stage2_updates, list_mutations, patch_unresolved = task_patch_to_legacy_updates(patch)
                    for u in patch_unresolved:
                        if u not in turn_unresolved:
                            turn_unresolved.append(u)
                        if u not in new_unresolved:
                            new_unresolved.append(u)
                    for k, cand_info in stage2_updates.items():
                        merged_updates[k] = cand_info["value"]
                        merged_updates_meta[k] = cand_info
            else:
                record_unresolved(extraction_res)
                stage2_updates = {}
                for candidate in extraction_res.get("slot_candidates", []):
                    k = candidate["canonical_key"]
                    v = candidate["normalized_value"]
                    if k == "equipment_model":
                        k = "equipment_type"
                    cand_info = {
                        "value": v,
                        "raw_value": candidate.get("raw_value"),
                        "confidence": candidate.get("confidence", 1.0),
                        "source": self._source_for_resolution_method(candidate.get("resolution_method"))
                    }
                    stage2_updates[k] = cand_info
                    merged_updates[k] = v
                    merged_updates_meta[k] = cand_info
                list_mutations = extraction_res.get("list_mutations", [])
            payload_mutation_failed = False
            mutation_failure_result = None

            if list_mutations:
                # Use the same post-update sandbox as ordinary V2
                # normalization.  In particular, a same-turn robot change
                # must affect payload add/remove validation immediately.
                mutation_slots = copy.deepcopy(evaluation_slots)
                for mutation in list_mutations:
                    m_field = mutation.get("field")
                    if m_field == "payload":
                        stage2_updates.pop("payload", None)
                        merged_updates.pop("payload", None)
                        merged_updates_meta.pop("payload", None)

                        mut_res = self.slot_store.apply_list_mutation(
                            mutation_slots,
                            mutation,
                            required_schema=field_defs,
                            payload_catalog=self.kb.assets.get("payload_catalog"),
                            allowed_values_resolver=lambda f: self.builder.resolve_allowed_values(
                                f,
                                effective_task_type_key,
                                effective_state,
                            ),
                        )
                        if mut_res.get("success"):
                            new_payload_val = mut_res.get("new_value")
                            merged_updates["payload"] = new_payload_val
                            merged_updates_meta["payload"] = {
                                "value": new_payload_val,
                                "raw_value": mutation.get("raw_text"),
                                "confidence": mutation.get("confidence", 0.95),
                                "source": mutation.get("source", "user_input"),
                            }
                            stage2_updates["payload"] = merged_updates_meta["payload"]
                        else:
                            payload_mutation_failed = True
                            mutation_failure_result = mut_res
                            payload_slot = new_slots.get("payload")
                            if payload_slot is None:
                                payload_slot = Slot(
                                    "payload",
                                    value_type="list",
                                    status="missing",
                                )
                                new_slots["payload"] = payload_slot
                            payload_slot.raw_value = mutation.get("raw_text")
                            payload_slot.source = mutation.get(
                                "source",
                                "user_input",
                            )
                            payload_slot.confidence = mutation.get(
                                "confidence",
                                0.95,
                            )
                            payload_slot.validation_error = mut_res.get("error")
                            break
                    else:
                        # List mutations are currently schema-supported only
                        # for payload. Keep the isolated sandbox contract if a
                        # future list field is introduced instead of mutating
                        # the transaction before validation completes.
                        payload_mutation_failed = True
                        mutation_failure_result = {
                            "error": f"不支持的列表字段 '{m_field}'",
                        }
                        break

            raw_stage2 = self._merge_coordinate_updates(user_message, {k: v.get("value") if isinstance(v, dict) else v for k, v in stage2_updates.items()}, required_field_defs)
            for k, v in raw_stage2.items():
                if k not in stage2_updates:
                    c_info = {"value": v, "raw_value": user_message, "confidence": 1.0, "source": "rule_parser"}
                    stage2_updates[k] = c_info
                    merged_updates_meta[k] = c_info
                merged_updates[k] = v

            if transition_state_active:
                self._clear_non_inherited_transition_slots(new_slots)
            raw_linked = self._link_oilfield_update_in_transaction({k: v.get("value") if isinstance(v, dict) else v for k, v in stage2_updates.items()}, new_slots, user_message=user_message)
            if "oilfield_name" in stage2_updates and "oilfield_name" not in raw_linked:
                stage2_updates.pop("oilfield_name", None)
                merged_updates.pop("oilfield_name", None)
                merged_updates_meta.pop("oilfield_name", None)

            for k, v in raw_linked.items():
                if k.startswith("__"):
                    stage2_updates[k] = v
                    continue
                old_info = stage2_updates.get(k)
                old_raw = old_info.get("raw_value") if isinstance(old_info, dict) else None
                c_info = {
                    "value": v,
                    "raw_value": old_raw if old_raw is not None else str(v),
                    "confidence": old_info.get("confidence", 1.0) if isinstance(old_info, dict) else 1.0,
                    "source": old_info.get("source", "entity_linker") if isinstance(old_info, dict) else "entity_linker",
                }
                stage2_updates[k] = c_info
                merged_updates_meta[k] = c_info
                merged_updates[k] = v

            turn_unresolved = self._filter_robot_selection_unresolved(
                turn_unresolved,
                stage2_updates,
            )
            new_unresolved = self._filter_robot_selection_unresolved(
                new_unresolved,
                stage2_updates,
            )

            _has_conflict = any(s.status == "conflict" for s in new_slots.values())
            has_successful_mutation = any(m.get("field") == "payload" for m in list_mutations)
            if not stage2_updates and not _has_conflict and not turn_unresolved and not has_successful_mutation and (apply_plan is None or not apply_plan.failures):
                if new_unresolved:
                    self.slot_store.commit_transaction(
                        new_slots,
                        new_unresolved,
                        request_id=request_id,
                        expected_version=expected_version,
                    )
                return reply_write_without_candidates()

            # Scoped & Negation-Safe Conflict resolution check
            slot_name_aliases = {
                "support_vessel": ["支持船", "船", "工作船", "母船"],
                "equipment_type": ["设备", "机器人", "rov", "auv"],
                "water_depth": ["水深", "深度"],
                "cable_type": ["管缆类型", "缆线", "电缆"],
                "payload": ["载荷", "工具", "传感器", "抓手", "配备"],
                "oilfield_name": ["油田", "油田名称"],
            }
            has_negation_confirm = any(nc in user_message for nc in ["不确认", "不修改", "不要修改", "先不确认"])
            has_explicit_upd = bool(stage2_updates)

            conflict_slots = [k for k, s in new_slots.items() if s.status == "conflict" and s.candidate_value is not None]
            is_ambiguous_global_confirm = (
                len(conflict_slots) >= 2
                and user_message.strip() in ("确认这个修改", "确认修改", "好的", "确认", "确定修改")
                and not has_explicit_upd
            )

            if not is_ambiguous_global_confirm:
                for k, slot in list(new_slots.items()):
                    if slot.status == "conflict" and slot.candidate_value is not None:
                        raw_ext = stage2_updates.get(k)
                        extracted_cand_v = raw_ext.get("value") if isinstance(raw_ext, dict) else raw_ext
                        # 1. 显式输入与 candidate_value 完全一致
                        if extracted_cand_v is not None and extracted_cand_v == slot.candidate_value:
                            slot.value = slot.candidate_value
                            slot.status = "valid"
                            slot.candidate_value = None
                            slot.validation_error = None
                            continue

                        # 2. 如果包含其他新槽位修改（如"水深改成500米"），不得顺带确认本冲突槽位
                        if has_explicit_upd and k not in stage2_updates and not any(alias in user_message for alias in slot_name_aliases.get(k, [k])):
                            continue

                        # 3. 检查针对具体槽位 k 的定向确认/取消
                        k_aliases = slot_name_aliases.get(k, [k])
                        msg_targets_k = any(alias in user_message for alias in k_aliases) or (slot.candidate_value and str(slot.candidate_value) in user_message)

                        if msg_targets_k:
                            is_cancel_k = any(c_kw in user_message for c_kw in ["取消", "放弃", "不要", "不修改", "不用"])
                            is_confirm_k = any(c_kw in user_message for c_kw in ["确认", "确定", "好的", "可以", "使用", "改为"]) and not is_cancel_k

                            if is_confirm_k and not has_negation_confirm:
                                slot.value = slot.candidate_value
                                slot.status = "valid"
                                slot.candidate_value = None
                                slot.validation_error = None
                                if k in stage2_updates:
                                    del stage2_updates[k]
                            elif is_cancel_k:
                                slot.status = "valid"
                                slot.candidate_value = None
                                slot.validation_error = None
                                if k in stage2_updates:
                                    del stage2_updates[k]

            if apply_plan is not None:
                self._apply_normalized_plan_in_transaction(
                    apply_plan,
                    new_slots,
                    allow_overwrite=had_task_type_key_at_turn_start,
                    transition_from_task_type_key=(
                        task_type_key
                        if effective_task_type_key != task_type_key
                        else None
                    ),
                    transition_to_task_type_key=(
                        effective_task_type_key
                        if effective_task_type_key != task_type_key
                        else None
                    ),
                )
                task_type_updates = {
                    k: v for k, v in stage2_updates.items()
                    if k in ("task_type", "task_type_key")
                }
                for k, info in task_type_updates.items():
                    val = info.get("value") if isinstance(info, dict) else info
                    if val is not None and val != "":
                        self._handle_task_type_update_in_transaction(k, val, new_slots)

                applied_keys = {outcome.key for outcome in apply_plan.successful_updates} | {f.key for f in apply_plan.failures}
                extra_updates = {
                    k: v for k, v in stage2_updates.items()
                    if (k.startswith("equipment_") or k not in applied_keys)
                    and k not in ("task_type", "task_type_key")
                }
                if extra_updates:
                    self._apply_updates_in_transaction(
                        extra_updates,
                        new_slots,
                        allow_overwrite=had_task_type_key_at_turn_start,
                    )
            else:
                # The transition was already cleared before oilfield linking
                # at the shared Stage-2 seam.  Clearing again here would erase
                # newly linked target-task metadata.
                self._apply_updates_in_transaction(
                    stage2_updates,
                    new_slots,
                    allow_overwrite=had_task_type_key_at_turn_start,
                    transition_slots_already_cleared=transition_state_active,
                )

            for key in stage2_updates:
                slot = new_slots.get(key)
                if (
                    slot
                    and slot.status in ("invalid", "conflict")
                    and slot.validation_error
                ):
                    detail = f"{FIELD_LABELS.get(key, key)}：{slot.validation_error}"
                    if detail not in turn_unresolved:
                        turn_unresolved.append(detail)

            if "rov_description" in stage2_updates:
                all_rovs = self.kb.get_all_rovs()
                proposed_pending_rov = self.extractor.resolve_rov_description(
                    stage2_updates["rov_description"].get("value") if isinstance(stage2_updates["rov_description"], dict) else str(stage2_updates["rov_description"]),
                    all_rovs,
                    (
                        new_slots["task_type_key"].value
                        if new_slots.get("task_type_key")
                        and new_slots["task_type_key"].status == "valid"
                        else None
                    )
                )
        else:
            if extraction_res.get("unresolved"):
                for u in extraction_res["unresolved"]:
                    if u not in new_unresolved:
                        new_unresolved.append(u)

        # 强制保障：当油田已标准识别且用户未输入显式坐标时，强制使用油田权威基准坐标，消除大模型幻觉坐标
        of_id_slot = new_slots.get("oilfield_entity_id")
        if of_id_slot and of_id_slot.status == "valid" and of_id_slot.value:
            has_explicit_coord = False
            if user_message:
                kw = ["北纬", "南纬", "东经", "西经", "纬度", "经度", "坐标", "lat", "lon", "coord"]
                if any(k in user_message.lower() for k in kw) or re.search(r"[（(]\s*[-+]?\d+(?:\.\d*)?\s*[,，、/]\s*[-+]?\d+(?:\.\d*)?\s*[）)]", user_message):
                    has_explicit_coord = True
            if not has_explicit_coord:
                try:
                    ctx_res = self.oilfield_linker.evaluate_context(entity_id=str(of_id_slot.value))
                    if ctx_res and ctx_res.default_coordinates:
                        if "oilfield_coordinates" not in new_slots:
                            new_slots["oilfield_coordinates"] = Slot("oilfield_coordinates")
                        new_slots["oilfield_coordinates"].value = ctx_res.default_coordinates
                        new_slots["oilfield_coordinates"].status = "valid"
                        new_slots["oilfield_coordinates"].source = "oilfield_default"
                except Exception:
                    pass

        # Compute proposed mode change without mutating self.mode before commit
        proposed_mode = self.mode
        if merged_updates.get("emergency_mode") is True:
            proposed_mode = "emergency"
        elif merged_updates.get("emergency_mode") is False:
            proposed_mode = "normal"

        # Compute changed fields based on proposed updates
        changed_fields = set()
        for k, v in merged_updates.items():
            if k not in ("emergency_mode", "rov_description", "__clear_oilfield_name", "__clear_pending_oilfield") and v is not None and v != "":
                old_val = self.slot_store.slots.get(k).value if self.slot_store.slots.get(k) else None
                if old_val != v:
                    changed_fields.add(k)

        proposed_whitelist = {item for item in self._soft_whitelist if item[0] not in changed_fields}

        # Normalize and validate inside transaction working dict new_slots
        curr_task_type_slot = new_slots.get("task_type_key")
        curr_task_type_key = (
            curr_task_type_slot.value
            if curr_task_type_slot
            and curr_task_type_slot.status == "valid"
            and curr_task_type_slot.value is not None
            else None
        )
        skip_keys = None
        if apply_plan is not None:
            # Dynamic allowed-value fields were normalized before specialized
            # robot updates were committed.  Revalidate them once more against
            # the final authoritative robot tuple.  Static scalar fields may
            # safely keep the V2 single-normalization fast path.
            dynamic_keys = self._dynamic_allowed_schema_keys(field_defs)
            skip_keys = set(apply_plan.normalized_schema_keys) - dynamic_keys
        self._normalize_and_validate_in_transaction(new_slots, curr_task_type_key, skip_schema_keys=skip_keys)

        curr_task_type_key = new_slots.get("task_type_key").value if (new_slots.get("task_type_key") and new_slots.get("task_type_key").status == "valid") else None

        # Auto-generate internal_id (UUIDv4) and task_id inside new_slots BEFORE commit
        if curr_task_type_key:
            internal_id_slot = new_slots.get("internal_id")
            if not internal_id_slot or internal_id_slot.status != "valid" or not internal_id_slot.value:
                new_uuid = str(uuid.uuid4())
                if "internal_id" not in new_slots:
                    new_slots["internal_id"] = Slot("internal_id")
                new_slots["internal_id"].value = new_uuid
                new_slots["internal_id"].status = "valid"
                new_slots["internal_id"].source = "auto"
                new_slots["internal_id"].raw_value = None
                new_slots["internal_id"].value_type = "string"

            task_id_slot = new_slots.get("task_id")
            # 草稿阶段仅预览编号：不消耗正式序号，不写 valid status。
            # 只有当前没有任何 preview 或已有 valid 编号（来自正式 reserve）时才刷新预览。
            # 正式编号在用户确认发布时由 _publish_confirmed_task 调用 reserve_task_id() 分配。
            existing_valid = (
                task_id_slot
                and task_id_slot.status == "valid"
                and task_id_slot.value is not None
                and validate_task_id_for_task_type(str(task_id_slot.value), curr_task_type_key, self.kb.task_schemas)
                and task_id_slot.source == "auto_reserved"  # 只有正式预约的才保留
            )
            if not existing_valid:
                try:
                    preview_id = self.builder.preview_task_id(curr_task_type_key)
                except IdReservationError:
                    # 底层 ID 序列配置错误或 counter 损坏时 Fail Closed，不吞异常
                    raise
                except Exception as _preview_err:
                    logger.warning(
                        "[DM] non-fatal preview_task_id failed for %s: %s", curr_task_type_key, _preview_err
                    )
                    preview_id = None
                if preview_id is not None:
                    if "task_id" not in new_slots:
                        new_slots["task_id"] = Slot("task_id")
                    # candidate_value: 预估编号，仅供展示，不进入 TaskIntent
                    new_slots["task_id"].candidate_value = preview_id
                    new_slots["task_id"].status = "candidate"
                    new_slots["task_id"].source = "auto_preview"
                    new_slots["task_id"].value = None          # 正式值为空，防止被 prepare() 误用
                    new_slots["task_id"].raw_value = None
                    new_slots["task_id"].value_type = "string"
                    new_slots["task_id"].validation_error = None

        proposed_phase = self.phase

        # Check required missing in working new_slots
        if curr_task_type_key:
            required_schema = self.builder.get_schema(curr_task_type_key, proposed_mode)
            user_req_schema = [f for f in required_schema if f.get("type") not in ("auto", "fixed")]
            built = self.slot_store.get_built_json()
            missing = self.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: self.builder.resolve_allowed_values(
                    field,
                    curr_task_type_key,
                    self.task_state,
                ),
            )
            self._last_missing = missing
            cand_missing = [f for f in required_schema if f.get("type") not in ("auto", "fixed") and (not new_slots.get(f["key"]) or new_slots[f["key"]].status != "valid" or new_slots[f["key"]].value is None)]
        else:
            built = {}
            missing = [{"key": "task_type", "label": "任务类型", "type": "string",
                        "allowed_values": self.kb.get_all_task_type_values()}]
            self._last_missing = missing
            cand_missing = missing

        # 维持严格 SSOT：_last_built_json 完全由 self.slot_store.get_built_json() 派生
        self._last_built_json = built

        # Auto-generate intent_id inside new_slots BEFORE commit when all required slots are present or when revising a done task
        if old_phase == "done" or (curr_task_type_key and not cand_missing):
            intent_id_slot = new_slots.get("intent_id")
            if old_phase == "done" or not intent_id_slot or intent_id_slot.status != "valid" or not intent_id_slot.value:
                today = get_current_datetime().strftime("%Y%m%d")
                from .task_intent_builder import get_task_dir
                task_dir = get_task_dir(create=False)
                from .id_sequence import next_daily_id
                ti_intent_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])
                if "intent_id" not in new_slots:
                    new_slots["intent_id"] = Slot("intent_id")
                new_slots["intent_id"].value = ti_intent_id
                new_slots["intent_id"].status = "valid"
                new_slots["intent_id"].source = "auto"
                new_slots["intent_id"].raw_value = None

        if old_phase == "done":
            proposed_phase = "confirming" if not cand_missing else "collecting"
        elif not cand_missing and proposed_phase not in ("blocked_hard", "blocked_soft", "confirming", "done"):
            proposed_phase = "confirming"

        # Atomic single commit with optimistic version validation
        self.slot_store.commit_transaction(
            new_slots,
            new_unresolved,
            request_id=request_id,
            expected_version=expected_version,
        )

        if old_phase == "done":
            self.final_result = None

        # Apply proposed instance state AFTER successful commit
        self.mode = proposed_mode
        self._transition_phase(proposed_phase, reason="task_modified")
        self._soft_whitelist = proposed_whitelist
        self._pending_rov_candidates = proposed_pending_rov

        # Re-derive from slot_store (SSOT)
        self.task_state = self.slot_store.get_task_state()
        if curr_task_type_key:
            required_schema = self.builder.get_schema(curr_task_type_key, self.mode)
            user_req_schema = [f for f in required_schema if f.get("type") not in ("auto", "fixed")]
            built = self.slot_store.get_built_json()
            missing = self.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: self.builder.resolve_allowed_values(
                    field,
                    curr_task_type_key,
                    self.task_state,
                ),
            )
            self._last_missing = missing
        else:
            built = {}
            missing = [{"key": "task_type", "label": "任务类型", "type": "string",
                        "allowed_values": self.kb.get_all_task_type_values()}]
            self._last_missing = missing
        self._last_built_json = built

        self.task_start_now = self.is_start_time_near_now()


        pending_oilfield_reply = self._build_pending_oilfield_reply()
        if pending_oilfield_reply:
            self._transition_phase("collecting", reason="oilfield_clarification_needed")
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": pending_oilfield_reply})
            return pending_oilfield_reply


        # 约束检查
        ALL_FIELDS = {"task_type", "start_time", "end_time", "cable_position", "cable_type", "start_point", "end_point",
                      "water_depth", "equipment_family", "equipment_type", "equipment_name", "equipment_unit_id",
                      "payload", "support_vessel", "oilfield_name",
                      "oilfield_coordinates", "wellhead_id"}

        if not missing and self.phase not in ("blocked_hard", "blocked_soft"):
            constraint_context = self._run_constraint_check(ALL_FIELDS, purpose="preview")
        elif not missing and self.phase == "blocked_soft":
            constraint_context = self._run_constraint_check(changed_fields, purpose="preview")
        elif not missing and self.phase == "blocked_hard":
            constraint_context = self._run_constraint_check(ALL_FIELDS, purpose="preview")
        else:
            constraint_context = self._run_constraint_check(changed_fields, purpose="interactive")

        # 约束处理可能把 blocked_soft/blocked_hard 解除为 collecting。若此时
        # 必填字段已经齐全，必须继续完成本轮状态收敛，不能停在“无缺失但仍收集”。
        if (
            not missing
            and self.phase == "collecting"
            and constraint_context.get("type") == "none"
        ):
            self._transition_phase(
                "confirming",
                reason="constraints_resolved_and_required_slots_complete",
            )

        # 知识上下文
        knowledge_context = self.kb.get_context_for_state(self.task_state)
        accepted_updates = self._get_committed_turn_updates(
            merged_updates,
            state_before_turn,
        )

        # 生成回复
        messages = build_responder_messages(
            task_state=self.task_state,
            built_json=built,
            missing_fields=missing,
            mode=self.mode,
            phase=self.phase,
            knowledge_context=knowledge_context,
            constraint_context=constraint_context,
            conversation_history=self.conversation_history,
            latest_user_message=user_message,
            ROV2type=self.kb.ROV2type,
            support_task=self.kb.get_supported_task(),
            slot_snapshot=self.slot_store.get_slot_snapshot(),
            accepted_updates=accepted_updates,
            unresolved_inputs=turn_unresolved,
        )
        reply = self._safe_llm_chat(messages, temperature=0.7, max_tokens=1500, role=ModelRole.TASK_RESPONDER)
        reply = self._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)
        reply = self._ground_write_reply(
            reply,
            accepted_updates=accepted_updates,
            unresolved_inputs=turn_unresolved,
            missing_fields=missing,
            display_updates=self._get_committed_update_display_values(
                accepted_updates
            ),
        )
        reply = self._ensure_constraint_details(reply, constraint_context)

        if payload_mutation_failed and mutation_failure_result:
            err_msg = mutation_failure_result.get("error") or "载荷修改操作失败。"
            if accepted_updates:
                reply = f"{reply}\n注意：载荷操作失败：{err_msg}"
            else:
                reply = f"操作失败：{err_msg}"

        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    # --------------------------------------------------------------------------
    # 参数更新与规范化
    # --------------------------------------------------------------------------
    def _ground_write_reply(
        self_or_reply: object,
        model_reply: str = "",
        *,
        accepted_updates: dict,
        unresolved_inputs: list,
        missing_fields: list[dict] | None = None,
        display_updates: dict | None = None,
    ) -> str:
        """在 LLM 自然语言回复后追加事实锚点摘要，防止回复内容与实际写入状态不一致。

        设计原则：
        - ``model_reply`` 作为主体自然语言回复，原样保留，不得丢弃。
        - 仅将 FIELD_LABELS 中有中文标签的用户可见字段写入摘要，内部元数据字段不得出现。
        - 当 ``accepted_updates`` 非空时，在回复末尾追加一行简明的字段确认摘要。
        - 当 LLM 回复为空时，退化为纯摘要模式（兜底）。
        """
        if isinstance(self_or_reply, str):
            self_obj = None
            actual_model_reply = self_or_reply
        else:
            self_obj = self_or_reply
            actual_model_reply = model_reply

        unresolved = [str(item) for item in unresolved_inputs if str(item).strip()]

        # 只展示 FIELD_LABELS 中有中文标签的用户可见字段
        user_visible_updates = {
            key: value
            for key, value in accepted_updates.items()
            if key in FIELD_LABELS
        }

        suffix_parts: list[str] = []
        if user_visible_updates:
            committed = []
            for key, value in user_visible_updates.items():
                label = FIELD_LABELS[key]
                display_value = (display_updates or {}).get(key, value)
                rendered = coord_parser.format_slot_display_value(key, display_value)
                committed.append(f"{label}：{rendered}")
            suffix_parts.append("✅ 已记录：" + "；".join(committed) + "。")

        if unresolved:
            suffix_parts.append("⚠️ 未写入或仍需确认：" + "；".join(unresolved) + "。")

        if missing_fields is not None:
            labels = [
                str(item.get("label") or item.get("key"))
                for item in missing_fields
                if isinstance(item, dict) and (item.get("label") or item.get("key"))
            ]
            if labels:
                next_labels = labels[:3]
                suffix_parts.append("仍需补充：" + "、".join(next_labels) + "。")
                if any(isinstance(item, dict) and item.get("key") == "payload" for item in missing_fields[:3]):
                    eq_type = str(
                        (accepted_updates or {}).get("equipment_type")
                        or ((display_updates or {}).get("equipment_type"))
                        or ""
                    )
                    if not eq_type and self_obj and hasattr(self_obj, "slot_store") and self_obj.slot_store:
                        eq_slot = self_obj.slot_store.slots.get("equipment_type")
                        if eq_slot and eq_slot.status == "valid" and eq_slot.value:
                            eq_type = str(eq_slot.value)
                    if eq_type and self_obj and hasattr(self_obj, "kb") and self_obj.kb:
                        robot = self_obj.kb.get_rov(eq_type)
                        if robot:
                            ob_list = robot.get("onboard_payloads", [])
                            sp_list = robot.get("supported_payloads", [])
                            if ob_list:
                                guidance_msg = self_obj.capability_adapter.format_payload_guidance(
                                    "",
                                    [{"key": "payload", "equipment_type": eq_type, "onboard_payloads": ob_list}]
                                )
                                suffix_parts.append(guidance_msg)
            elif user_visible_updates:
                suffix_parts.append("所有必填字段已收集完成，任务尚未发布。")

        suffix = "\n".join(suffix_parts)

        # 区分有提交和无提交的响应安全规则：
        # 1. 若 LLM 回复为空，退化为纯摘要模式（兜底）。
        if not actual_model_reply or not str(actual_model_reply).strip():
            if not accepted_updates:
                reply = "本轮没有任务字段通过验证，因此未写入任务状态。"
                if suffix:
                    reply = f"{reply}\n{suffix}"
                return reply
            return suffix if suffix else "已写入本轮通过验证的字段。"

        # 2. 若有 LLM 回复，以 LLM 自然语言回复为主体，末尾追加规则生成的客观校验/记录摘要。
        import re

        model_reply_str = str(actual_model_reply)
        if not accepted_updates:
            for false_claim in ("已创建", "指令已下发", "已经设置"):
                if false_claim in model_reply_str:
                    model_reply_str = model_reply_str.replace(false_claim, "未写入任务状态")
            if "未写入" not in model_reply_str and not any("未写入" in p for p in suffix_parts):
                suffix_parts.insert(0, "⚠️ 本轮未写入任务状态。")

        def _normalize(text: object) -> str:
            if not isinstance(text, str):
                text = str(text)
            return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)

        norm_reply = _normalize(model_reply_str)
        deduped_parts = []
        for part in suffix_parts:
            norm_part = _normalize(part)
            # 检查整段是否已存在（严格或去空）
            if part in model_reply_str or (norm_part and norm_part in norm_reply):
                continue
            # 检查特定模式
            if part.startswith("✅ 已记录：") and ("✅ 已记录：" in model_reply_str or "✅已记录：" in model_reply_str):
                labels_in_reply = all(
                    FIELD_LABELS[k] in model_reply_str
                    for k in user_visible_updates
                )
                if labels_in_reply:
                    continue
            if part.startswith("仍需补充：") and ("仍需补充：" in model_reply_str or "仍需补充" in model_reply_str):
                continue
            if part.startswith("⚠️ 未写入或仍需确认：") and ("⚠️ 未写入或仍需确认：" in model_reply_str or "未写入或仍需确认" in model_reply_str):
                continue
            if part.startswith("【提示】") or "已搭载" in part:
                if any(kw in model_reply_str for kw in ("已搭载", "自带载荷", "已具备", "【提示】", "替换、增加或减少")):
                    continue
            deduped_parts.append(part)

        deduped_suffix = "\n".join(deduped_parts)
        if deduped_suffix:
            if isinstance(actual_model_reply, str):
                return f"{model_reply_str.rstrip()}\n\n{deduped_suffix}".rstrip()
            return f"{model_reply_str}\n\n{deduped_suffix}"
        return model_reply_str.strip() if isinstance(actual_model_reply, str) else model_reply_str

    @staticmethod
    def _filter_robot_selection_unresolved(
        unresolved_items: list,
        accepted_updates: dict | None,
    ) -> list:
        """清理机器人选择迁移后的 unresolved 噪声。

        equipment_class 已经是内部派生元数据，不再是 schema 采集字段；同一个
        raw phrase 如果已被 family/type/unit 成功写入，下游层级对同 raw 的失败
        fan-out 也不应继续展示给用户。
        """
        if not unresolved_items:
            return []

        accepted_raws: set[str] = set()
        for key, info in (accepted_updates or {}).items():
            if key not in {
                "equipment_family",
                "equipment_type",
                "equipment_unit_id",
                "equipment_name",
            }:
                continue
            raw = info.get("raw_value") if isinstance(info, dict) else None
            value = info.get("value") if isinstance(info, dict) else info
            for item in (raw, value):
                if item is not None and str(item).strip():
                    accepted_raws.add(str(item).strip())

        filtered: list = []
        for item in unresolved_items:
            text = str(item)
            if "equipment_class" in text:
                continue
            match = re.search(
                r"(equipment_family|equipment_type|equipment_unit_id|equipment_name) 表达“([^”]+)”",
                text,
            )
            if match and match.group(2).strip() in accepted_raws:
                continue
            if item not in filtered:
                filtered.append(item)
        return filtered

    def _project_legacy_equipment_class_candidate(
        self,
        candidate: dict,
        task_type_key: str | None,
        task_state: dict | None,
    ) -> dict | None:
        """Map a legacy equipment_class candidate to family when unambiguous."""
        raw_value = candidate.get("raw_value", candidate.get("normalized_value"))
        normalized_value = candidate.get("normalized_value", raw_value)
        class_id = self.kb._resolve_class_key(str(normalized_value or ""))
        if not class_id and raw_value is not None:
            class_id = self.kb._resolve_class_key(str(raw_value))
        if not class_id or not task_type_key:
            return None
        try:
            domain = self.kb.get_feasible_robot_selection_domain(
                task_type_key,
                task_state,
            )
        except Exception:
            return None
        class_node = next(
            (
                item
                for item in domain.get("classes", [])
                if item.get("class_id") == class_id
            ),
            None,
        )
        families = class_node.get("families", []) if class_node else []
        if len(families) != 1:
            return None
        family = families[0]
        projected = dict(candidate)
        projected["canonical_key"] = "equipment_family"
        projected["normalized_value"] = family.get("full_name") or family.get("family_id")
        projected.setdefault("raw_value", raw_value)
        projected["resolution_method"] = "legacy_class_to_single_family"
        return projected

    def _get_committed_update_display_values(self, accepted_updates: dict) -> dict:
        """从领域配置生成写入回执的展示值，不改变 SlotStore 标准值。"""
        display = {}
        for k, v in (accepted_updates or {}).items():
            if k == "equipment_class" and v is not None:
                class_config = self.kb.get_robot_classes().get(str(v), {})
                display["equipment_class"] = class_config.get(
                    "full_name",
                    v,
                )
            else:
                display[k] = coord_parser.format_slot_display_value(k, v)
        return display

    def _get_committed_turn_updates(
        self,
        proposed_updates: dict,
        state_before_turn: dict,
    ) -> dict:
        """返回本轮已由 SlotStore 提交的用户字段更新。"""
        if not proposed_updates:
            return {}

        # 内部元数据字段：由 oilfield linker 等中间件写入，仅供后端推理，不得面向用户展示
        _INTERNAL_METADATA_KEYS = {
            "raw_oilfield_name",
            "oilfield_match_status",
            "oilfield_match_confidence",
            "oilfield_match_evidence",
            "oilfield_match_candidates",
            "oilfield_entity_id",
            "pending_oilfield_name",
            "pending_oilfield_candidates",
        }
        ignored_keys = {
            "task_id",
            "intent_id",
            "internal_id",
            "task_type_key",
            "emergency_mode",
            "rov_description",
            "__clear_oilfield_name",
            "__clear_pending_oilfield",
        } | _INTERNAL_METADATA_KEYS
        accepted: dict = {}
        for key, value in self.task_state.items():
            if key in ignored_keys or key.startswith("__") or value is None:
                continue
            if key not in proposed_updates and state_before_turn.get(key) == value:
                continue
            slot = self.slot_store.slots.get(key)
            if slot and slot.status == "valid":
                accepted[key] = value
        return accepted



    def _link_oilfield_update_in_transaction(self, updates: dict, new_slots: dict, user_message: str = "") -> dict:
        task_type_key = updates.get("task_type_key") or (
            new_slots["task_type_key"].value
            if new_slots.get("task_type_key")
            and new_slots["task_type_key"].status == "valid"
            else None
        )
        if task_type_key:
            field_defs = self.builder.get_schema(task_type_key, self.mode)
            schema_keys = {str(field.get("key")) for field in field_defs if field.get("key")}
            if not self.slot_filter.supports_oilfield_slots(schema_keys):
                linked = dict(updates)
                linked.pop("oilfield_name", None)
                linked.pop("raw_oilfield_name", None)
                return linked

        raw_name = updates.get("oilfield_name") or updates.get("raw_oilfield_name")
        if isinstance(raw_name, dict):
            raw_name = raw_name.get("value")

        coords = (
            updates.get("oilfield_coordinates")
            or updates.get("start_point")
            or updates.get("cable_position")
            or next(
                (
                    new_slots[key].value
                    for key in (
                        "oilfield_coordinates",
                        "start_point",
                        "cable_position",
                    )
                    if new_slots.get(key)
                    and new_slots[key].status == "valid"
                    and new_slots[key].value is not None
                ),
                None,
            )
        )
        linked = dict(updates)

        # 反向映射：检查坐标是否包含在知识库某油田范围内
        matched_entity_by_coords = self.oilfield_linker.find_entity_by_coords(coords) if coords else None

        if not raw_name:
            if matched_entity_by_coords:
                # 坐标落入已知油田，反向自动跟随推导油田名称
                raw_name = matched_entity_by_coords.get("name")
            else:
                # 坐标不落入任何已知油田，且用户未提供油田名称：
                # 若坐标被更新且原槽位绑定了知识库油田，解绑原知识库油田，让用户提供名称占位
                existing_entity_id_slot = new_slots.get("oilfield_entity_id")
                if (
                    "oilfield_coordinates" in updates
                    or "start_point" in updates
                    or "cable_position" in updates
                ) and existing_entity_id_slot and existing_entity_id_slot.value is not None:
                    linked.pop("oilfield_name", None)
                    if "oilfield_name" in new_slots:
                        new_slots["oilfield_name"].value = None
                        new_slots["oilfield_name"].status = "missing"
                    if "oilfield_entity_id" in new_slots:
                        new_slots["oilfield_entity_id"].value = None
                        new_slots["oilfield_entity_id"].status = "missing"
                    linked["__clear_oilfield_name"] = True
                return linked

        match = self.oilfield_linker.link(str(raw_name), coords)

        for k in ("raw_oilfield_name", "oilfield_match_status", "oilfield_match_confidence", "oilfield_match_evidence", "oilfield_match_candidates"):
            if k not in new_slots:
                new_slots[k] = Slot(slot_name=k)

        new_slots["raw_oilfield_name"].value = match.raw
        new_slots["raw_oilfield_name"].status = "valid"
        new_slots["oilfield_match_status"].value = match.status
        new_slots["oilfield_match_status"].status = "valid"
        new_slots["oilfield_match_confidence"].value = match.confidence
        new_slots["oilfield_match_confidence"].status = "valid"
        new_slots["oilfield_match_evidence"].value = match.evidence
        new_slots["oilfield_match_evidence"].status = "valid"
        new_slots["oilfield_match_candidates"].value = match.candidates
        new_slots["oilfield_match_candidates"].status = "valid"

        if match.status == "accepted" and match.standard_name:
            linked["oilfield_name"] = match.standard_name
            if "oilfield_name" not in new_slots:
                new_slots["oilfield_name"] = Slot("oilfield_name")
            new_slots["oilfield_name"].value = match.standard_name
            new_slots["oilfield_name"].status = "valid"
            if "oilfield_entity_id" not in new_slots:
                new_slots["oilfield_entity_id"] = Slot("oilfield_entity_id")
            new_slots["oilfield_entity_id"].value = match.entity_id
            new_slots["oilfield_entity_id"].status = "valid"
            linked["__clear_pending_oilfield"] = True

            # 自动映射油田坐标（若用户未上报自定义坐标）
            existing_coord_slot = new_slots.get("oilfield_coordinates")
            has_user_custom_coord = (
                "oilfield_coordinates" in updates
                or (
                    existing_coord_slot
                    and existing_coord_slot.status == "valid"
                    and existing_coord_slot.value is not None
                    and getattr(existing_coord_slot, "source", None) != "oilfield_default"
                )
            )
            if not has_user_custom_coord and match.entity_id:
                try:
                    ctx_res = self.oilfield_linker.evaluate_context(entity_id=match.entity_id)
                    if ctx_res and ctx_res.default_coordinates:
                        default_coord = ctx_res.default_coordinates
                        linked["oilfield_coordinates"] = default_coord
                        if "oilfield_coordinates" not in new_slots:
                            new_slots["oilfield_coordinates"] = Slot("oilfield_coordinates")
                        new_slots["oilfield_coordinates"].value = default_coord
                        new_slots["oilfield_coordinates"].status = "valid"
                        new_slots["oilfield_coordinates"].source = "oilfield_default"
                except Exception:
                    pass
        elif match.raw and not matched_entity_by_coords and not match.candidates:
            # 用户显式输入了自定义名称（如“自设A区”），且坐标不属于知识库任何油田：
            # 允许自定义名称作为 oilfield_name 生效（自定义名称占位），entity_id 为 None
            linked["oilfield_name"] = match.raw
            if "oilfield_name" not in new_slots:
                new_slots["oilfield_name"] = Slot("oilfield_name")
            new_slots["oilfield_name"].value = match.raw
            new_slots["oilfield_name"].status = "valid"
            new_slots["oilfield_name"].source = "user_input"
            if "oilfield_entity_id" not in new_slots:
                new_slots["oilfield_entity_id"] = Slot("oilfield_entity_id")
            new_slots["oilfield_entity_id"].value = None
            new_slots["oilfield_entity_id"].status = "missing"
            linked["__clear_pending_oilfield"] = True
        else:
            linked.pop("oilfield_name", None)
            for k in ("pending_oilfield_name", "pending_oilfield_candidates"):
                if k not in new_slots:
                    new_slots[k] = Slot(slot_name=k)
            new_slots["pending_oilfield_name"].value = match.raw
            new_slots["pending_oilfield_name"].status = "valid"
            new_slots["pending_oilfield_candidates"].value = match.candidates
            new_slots["pending_oilfield_candidates"].status = "valid"
            linked["__clear_oilfield_name"] = True
        return linked

    def _apply_updates_in_transaction(
        self,
        updates: dict,
        new_slots: dict,
        allow_overwrite: bool = False,
        transition_slots_already_cleared: bool = False,
    ):
        # main extractor 会携带 raw/confidence/source；LHL 归一化器只接收值本身。
        # 在事务入口拆开二者，既保留确定性归一化，也保留槽位审计信息。
        update_meta: dict[str, dict] = {}
        plain_updates: dict = {}
        # equipment_specification 是结构化 typed dict，其 "value" 字段是规格量值，不是 meta 包装。
        # 所有 equipment_specification 值应直接透传，不拆包。
        _spec_passthrough_keys = set()
        for key, item in updates.items():
            if key in _spec_passthrough_keys:
                plain_updates[key] = item
            elif isinstance(item, dict) and "value" in item:
                value = item.get("value")
                plain_updates[key] = value
                update_meta[key] = {
                    "raw_value": item.get("raw_value", value),
                    "confidence": item.get("confidence", 1.0),
                    "source": item.get("source", "user_input"),
                }
            else:
                plain_updates[key] = item
        updates = plain_updates

        task_type_slot = new_slots.get("task_type_key")
        task_type_key = (
            task_type_slot.value
            if task_type_slot and task_type_slot.status == "valid"
            else None
        )
        (
            pending_task_type_key,
            normalization_task_type_key,
            _task_type_change_locked,
            task_type_preflight_error,
        ) = self._resolve_task_type_update_context(updates, new_slots)
        if task_type_preflight_error:
            self._record_task_type_update_error(
                new_slots,
                task_type_preflight_error,
            )
            return

        if (
            pending_task_type_key
            and task_type_key
            and pending_task_type_key != task_type_key
            and not _task_type_change_locked
            and not transition_slots_already_cleared
        ):
            self._clear_non_inherited_transition_slots(new_slots)
            task_type_slot = new_slots.get("task_type_key")

        if updates.get("__clear_oilfield_name"):
            if "oilfield_name" in new_slots:
                new_slots["oilfield_name"].value = None
                new_slots["oilfield_name"].status = "missing"
            if "oilfield_entity_id" in new_slots:
                new_slots["oilfield_entity_id"].value = None
                new_slots["oilfield_entity_id"].status = "missing"
        if updates.get("__clear_pending_oilfield"):
            if "pending_oilfield_name" in new_slots:
                new_slots["pending_oilfield_name"].value = None
                new_slots["pending_oilfield_name"].status = "missing"
            if "pending_oilfield_candidates" in new_slots:
                new_slots["pending_oilfield_candidates"].value = None
                new_slots["pending_oilfield_candidates"].status = "missing"

        equipment_keys = {
            "equipment_class",
            "equipment_family",
            "equipment_type",
            "equipment_name",
            "equipment_unit_id",
        }
        passthrough_keys = {
            "task_type",
            "task_type_key",
            "emergency_mode",
            "rov_description",
            "oilfield_name",
            "__clear_oilfield_name",
            "__clear_pending_oilfield",
            "task_id",
            "intent_id",
            "internal_id",
        }

        # Schema fields in a task-switch turn belong to the target task. Keep
        # the real task-type mutation in the normal apply loop (so task-id
        # locks and cascade invalidation remain atomic), but select the target
        # schema for this transaction's normalization.
        failures = {}
        if normalization_task_type_key:
            evaluation_slots = copy.deepcopy(new_slots)
            evaluation_task_slot = evaluation_slots.get("task_type_key")
            if evaluation_task_slot is None:
                evaluation_task_slot = Slot(
                    "task_type_key",
                    value_type="string",
                )
                evaluation_slots["task_type_key"] = evaluation_task_slot
            evaluation_task_slot.value = normalization_task_type_key
            evaluation_task_slot.status = "valid"
            evaluation_task_slot.candidate_value = None
            evaluation_task_slot.validation_error = None

            evaluation_equipment_updates = {
                key: value
                for key, value in updates.items()
                if key in equipment_keys
            }
            if evaluation_equipment_updates:
                self._project_equipment_updates_for_evaluation(
                    evaluation_equipment_updates,
                    evaluation_slots,
                    normalization_task_type_key,
                )
            current_state = {
                key: slot.value
                for key, slot in evaluation_slots.items()
                if slot.status == "valid" and slot.value is not None
            }
            if pending_task_type_key:
                current_state["task_type_key"] = pending_task_type_key
            schema_updates = {
                k: v for k, v in updates.items()
                if k not in equipment_keys and k not in passthrough_keys
            }
            norm_res = self.normalizer.normalize_updates_with_failures(
                schema_updates,
                self.builder.get_schema(normalization_task_type_key, self.mode),
                current_state,
                lambda field_def, state: self.builder._resolve_allowed(
                    field_def,
                    normalization_task_type_key,
                    state,
                ),
            )
            norm_schema = norm_res.normalized_updates
            failures = norm_res.failures
            eq_updates = {k: v for k, v in updates.items() if k in equipment_keys}
            pass_updates = {k: v for k, v in updates.items() if k in passthrough_keys}
            updates = {**norm_schema, **pass_updates, **eq_updates}

        skip = {
            "emergency_mode",
            "rov_description",
            "__clear_oilfield_name",
            "__clear_pending_oilfield",
            "task_id",
            "intent_id",
            "internal_id",
            *equipment_keys,
        }

        for key, value in updates.items():
            if key in skip or value is None or value == "":
                continue
            if key in ("task_type", "task_type_key"):
                self._handle_task_type_update_in_transaction(key, value, new_slots)
                continue
            self._apply_slot_update_in_transaction(
                key,
                value,
                new_slots,
                allow_overwrite,
            )
            slot = new_slots.get(key)
            meta = update_meta.get(key)
            if slot and meta:
                slot.raw_value = meta["raw_value"]
                slot.confidence = meta["confidence"]
                slot.source = meta["source"]

        for key, failure in failures.items():
            slot = new_slots.get(key)
            meta = update_meta.get(key)
            raw_val = failure.raw_value
            msg = failure.message

            candidate_val = raw_val
            original_raw = (
                meta.get("raw_value")
                if meta and meta.get("raw_value") is not None
                else (str(raw_val) if raw_val is not None else "")
            )

            if slot and slot.status in ("valid", "conflict") and slot.value is not None:
                slot.status = "conflict"
                slot.candidate_value = candidate_val
                slot.raw_value = str(original_raw)
                slot.validation_error = msg
            else:
                if slot is None:
                    slot = Slot(slot_name=key)
                    new_slots[key] = slot
                slot.value = None
                slot.status = "invalid"
                slot.candidate_value = candidate_val
                slot.raw_value = str(original_raw)
                slot.validation_error = msg

            if meta:
                slot.confidence = meta.get("confidence", 1.0)
                slot.source = meta.get("source", "user_input")

        if "emergency_mode" in updates:
            em_val = updates["emergency_mode"]
            if em_val is True:
                if "emergency_mode" not in new_slots:
                    new_slots["emergency_mode"] = Slot("emergency_mode")
                new_slots["emergency_mode"].value = True
                new_slots["emergency_mode"].status = "valid"
                self.mode = "emergency"
            elif em_val is False:
                if "emergency_mode" in new_slots:
                    new_slots["emergency_mode"].value = False
                    new_slots["emergency_mode"].status = "valid"
                self.mode = "normal"

        self._handle_equipment_updates_in_transaction(
            updates,
            new_slots,
            allow_overwrite,
        )
        for key in (
            "equipment_class",
            "equipment_family",
            "equipment_type",
            "equipment_name",
            "equipment_unit_id",
        ):
            slot = new_slots.get(key)
            meta = update_meta.get(key)
            if slot and meta:
                slot.raw_value = meta["raw_value"]
                slot.confidence = meta["confidence"]
                slot.source = meta["source"]

        self._auto_collapse_robot_cascade(new_slots, allow_overwrite)

    def _apply_normalized_plan_in_transaction(
        self,
        plan: NormalizationApplyPlan,
        new_slots: dict,
        allow_overwrite: bool = False,
        transition_from_task_type_key: str | None = None,
        transition_to_task_type_key: str | None = None,
    ) -> None:
        """根据 NormalizationApplyPlan 修改 working dict new_slots。"""
        # 1. 成功 outcomes 写入 new_slots
        for succ in plan.successful_updates:
            key = succ.key
            value = succ.value
            slot = new_slots.get(key)

            if (
                slot
                and slot.status == "valid"
                and slot.value is not None
                and slot.value != value
                and not allow_overwrite
            ):
                slot.status = "conflict"
                slot.candidate_value = value
                slot.raw_value = str(succ.raw_value) if succ.raw_value is not None else str(value)
                slot.confidence = succ.confidence
                slot.source = succ.source
                slot.validation_error = None
            else:
                if slot is None:
                    slot = Slot(slot_name=key)
                    new_slots[key] = slot

                slot.value = value
                slot.status = "valid"
                slot.candidate_value = None
                slot.raw_value = str(succ.raw_value) if succ.raw_value is not None else str(value)
                slot.confidence = succ.confidence
                slot.source = succ.source
                slot.validation_error = None

        # 2. 失败 outcomes 写入 new_slots
        for failure in plan.failures:
            key = failure.key
            slot = new_slots.get(key)
            cand_val = failure.candidate_value
            raw_val = failure.raw_value
            raw_str = str(raw_val) if raw_val is not None else ""

            if slot and slot.status in ("valid", "conflict") and slot.value is not None:
                slot.status = "conflict"
                slot.candidate_value = cand_val
                slot.raw_value = raw_str
                slot.confidence = failure.confidence
                slot.source = failure.source
                slot.validation_error = failure.error_message
            else:
                if slot is None:
                    slot = Slot(slot_name=key)
                    new_slots[key] = slot
                slot.value = None
                slot.status = "invalid"
                slot.candidate_value = cand_val
                slot.raw_value = raw_str
                slot.confidence = failure.confidence
                slot.source = failure.source
                slot.validation_error = failure.error_message

        if not (
            transition_from_task_type_key
            and transition_to_task_type_key
            and transition_from_task_type_key != transition_to_task_type_key
        ):
            self._auto_collapse_robot_cascade(new_slots, allow_overwrite)

    @staticmethod
    def _source_for_resolution_method(resolution_method: str | None) -> str:
        source_map = {
            "canonical_exact": "user_input",
            "alias_exact": "alias_mapping",
            "llm_semantic": "llm_semantic_match",
            "type_normalization": "user_input",
            "assistant_recommendation": "assistant_recommendation",
            "visible_ordinal_selection": "assistant_option_selection",
        }
        return source_map.get(resolution_method, "user_input")

    def _scope_confirmed_recommendation(
        self,
        extraction_result: dict,
        plan: Any,
        user_message: str,
    ) -> dict:
        """接受推荐时只授权同一机器人层级，其他字段仍按正常抽取处理。"""
        if (
            plan is None
            or plan.operation != "WRITE"
            or plan.relation != "recommend"
        ):
            return extraction_result
        if parse_ordinal_reference(user_message) is not None:
            # 编号候选由通用可见来源门禁处理，不能套用机器人单一推荐协议。
            return extraction_result

        result = copy.deepcopy(extraction_result or {})
        result.setdefault("slot_candidates", [])
        result.setdefault("list_mutations", [])
        result.setdefault("unresolved", [])

        target_key = RECOMMENDATION_FIELD_BY_SUBJECT.get(plan.subject_type or "")
        if not target_key:
            return extraction_result
        selected = plan.subject_text
        field_def = self._missing_field_definition(target_key or "")
        allowed_values = list((field_def or {}).get("allowed_values") or [])
        if not allowed_values and target_key:
            task_key = self.task_state.get("task_type_key")
            allowed_values = list(
                self.builder.resolve_allowed_values(
                    field_def or {"key": target_key},
                    task_key,
                    self.task_state,
                )
                or []
            )
        semantic_field_def = dict(field_def or {})
        task_key = self.task_state.get("task_type_key")
        if target_key and task_key:
            required = self.builder.get_required(
                task_key,
                self.mode,
                self.task_state,
            )
            authoritative = next(
                (
                    item
                    for item in required
                    if isinstance(item, dict) and item.get("key") == target_key
                ),
                {},
            )
            if authoritative.get("alias_mappings"):
                semantic_field_def["alias_mappings"] = authoritative.get("alias_mappings")
        semantic_field_def["allowed_values"] = allowed_values
        resolved_selected = (
            ParameterExtractor._match_allowed_value(selected, allowed_values)
            or ParameterExtractor._match_alias_value(selected, semantic_field_def)
            if selected
            else None
        )

        previous_assistant = (
            self.conversation_history[-1].get("content", "")
            if self.conversation_history
            and self.conversation_history[-1].get("role") == "assistant"
            else ""
        )

        candidate_terms = {selected} if selected else set()
        if resolved_selected:
            candidate_terms.add(resolved_selected)
        raw_mention = getattr(plan, "raw_mention", None)
        if raw_mention:
            candidate_terms.add(raw_mention)
        if user_message:
            candidate_terms.add(user_message.strip())

        kb_inst = getattr(self, "kb", None) or getattr(getattr(self, "validator", None), "kb", None)
        alias_map = getattr(kb_inst, "alias_map", {}) if kb_inst else {}
        for k, v in alias_map.items():
            if k in candidate_terms or v in candidate_terms:
                candidate_terms.add(k)
                candidate_terms.add(v)

        if kb_inst and hasattr(kb_inst, "get_aliases_for_term"):
            for term in list(candidate_terms):
                aliases = kb_inst.get_aliases_for_term(term)
                if aliases:
                    candidate_terms.update(aliases)

        in_allowed = not allowed_values or any(t in allowed_values for t in candidate_terms if t)
        in_previous = any(t in previous_assistant for t in candidate_terms if t and len(t) > 1)

        valid_provenance = bool(
            target_key
            and selected
            and in_allowed
            and in_previous
        )

        if not valid_provenance:
            # 来源校验失败时，不清空 extractor 已抽取的 robot cascade candidates，
            # 让后续正常的 _handle_equipment_updates_in_transaction 流程继续处理。
            # 记录 unresolved 以便告知用户但不阻断写入。
            result["unresolved"].append(
                "无法验证所接受的推荐与紧邻上一轮助手建议及当前合法候选一致"
            )
            return result

        # valid_provenance 通过：才清除 extractor 可能产生的其他 robot cascade candidates，
        # 改用推荐协议注入唯一授权候选，防止 extractor 和推荐协议产生冲突写入。
        result["slot_candidates"] = [
            candidate
            for candidate in result["slot_candidates"]
            if candidate.get("canonical_key") not in ROBOT_CASCADE_FIELDS
        ]

        result["slot_candidates"].append(
            {
                "raw_key": "上一轮明确推荐",
                "canonical_key": target_key,
                "raw_value": user_message,
                "normalized_value": resolved_selected or selected,
                "confidence": plan.confidence,
                "resolution_method": "assistant_recommendation",
            }
        )
        return result

    def _scope_visible_ordinal_selections(
        self,
        extraction_result: dict,
        user_message: str,
        required_fields: list[dict],
    ) -> dict:
        """只授权紧邻助手回复中真实可见的编号候选选择。"""
        result = copy.deepcopy(extraction_result or {})
        candidates = result.setdefault("slot_candidates", [])
        result.setdefault("list_mutations", [])
        unresolved = result.setdefault("unresolved", [])
        required_by_key = {
            str(field.get("key")): field
            for field in required_fields or []
            if isinstance(field, dict) and field.get("key")
        }
        previous_assistant = (
            self.conversation_history[-1].get("content", "")
            if self.conversation_history
            and self.conversation_history[-1].get("role") == "assistant"
            else ""
        )

        authorized: list[dict] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            reference = parse_ordinal_reference(candidate.get("raw_value"))
            if reference is None and len(candidates) == 1:
                reference = parse_ordinal_reference(user_message)
            if reference is None:
                authorized.append(candidate)
                continue

            key = str(candidate.get("canonical_key") or "")
            field_definition = required_by_key.get(key) or {}
            allowed_values = list(field_definition.get("allowed_values") or [])
            if not allowed_values:
                # 数字、时间等非枚举值不依赖候选列表顺序，保持原抽取结果。
                authorized.append(candidate)
                continue

            selected = candidate.get("normalized_value")
            selected_values = selected if isinstance(selected, list) else [selected]
            valid_visible_selection = bool(
                len(selected_values) == 1
                and isinstance(selected_values[0], str)
                and selected_values[0] in allowed_values
                and visible_ordinal_matches_candidate(
                    previous_assistant,
                    reference,
                    selected_values[0],
                    build_candidate_terms(field_definition),
                )
            )
            if valid_visible_selection:
                accepted = copy.deepcopy(candidate)
                accepted["resolution_method"] = "visible_ordinal_selection"
                accepted["source"] = "assistant_option_selection"
                authorized.append(accepted)
                continue

            message = (
                f"{FIELD_LABELS.get(key, key or '该字段')}的编号选择“{reference.raw_text}”"
                "无法对应紧邻上一轮助手明确展示的可见候选，未写入"
            )
            if message not in unresolved:
                unresolved.append(message)

        result["slot_candidates"] = authorized
        return result

    @staticmethod
    def _apply_slot_update_in_transaction(
        key: str,
        value: Any,
        new_slots: dict,
        allow_overwrite: bool,
    ) -> None:
        """把一个候选值写入临时槽位；正式状态只能由后续 commit 生效。"""
        slot = new_slots.get(key)
        if (
            slot
            and slot.status == "valid"
            and slot.value is not None
            and slot.value != value
            and not allow_overwrite
        ):
            slot.status = "conflict"
            slot.candidate_value = value
            slot.raw_value = str(value)
            slot.validation_error = None
            return

        if slot is None:
            slot = Slot(slot_name=key)
            new_slots[key] = slot

        slot.value = value
        slot.status = "candidate"
        slot.candidate_value = None
        slot.raw_value = str(value)
        slot.validation_error = None

    def _auto_collapse_robot_cascade(self, new_slots: dict, allow_overwrite: bool = False) -> None:
        """
        [Issue #40] 四级级联自动收敛处理。
        只在 task_type_key 为 valid 时生效。
        规则：
        1. 前置校验：若已有槽位不属于当前 task 的 feasible_domain，作废该槽位及所有下游依赖。
        2. 逐级计算可行候选数 (candidate count)：
           - count == 0: Fail Closed (若槽位状态非 conflict/invalid，设为 invalid)；
           - count == 1: 自动绑定唯一 canonical value (status="valid", source="auto")，继续下一层推导；
           - count > 1: 停止自动收敛，等待用户选择该层。
        """
        task_type_slot = new_slots.get("task_type_key")
        if not task_type_slot or task_type_slot.status != "valid" or not task_type_slot.value:
            return

        task_type_key = str(task_type_slot.value)
        try:
            task_state = {
                key: copy.deepcopy(slot.value)
                for key, slot in new_slots.items()
                if slot.status == "valid" and slot.value is not None
            }
            domain = self.kb.get_feasible_robot_selection_domain(
                task_type_key,
                task_state,
            )
            # The admission domain contains only task/class/capability and
            # registry hierarchy rules.  It deliberately excludes mutable
            # task facts (depth/payload) and runtime telemetry.  Keeping this
            # second view lets us distinguish a structurally stale selector
            # from an explicit, structurally valid selector that is currently
            # infeasible and must remain visible to the Validator.
            admission_domain = self.kb.get_feasible_robot_selection_domain(
                task_type_key,
            )
        except Exception as exc:
            logger.warning("[DialogueManager] Failed to build feasible robot domain for task '%s': %s", task_type_key, exc)
            if "equipment_class" not in new_slots:
                new_slots["equipment_class"] = Slot("equipment_class")
            slot = new_slots["equipment_class"]
            slot.status = "invalid"
            slot.value = None
            slot.source = "system_candidate_resolution"
            slot.validation_error = str(exc)
            return

        from src.slot_store import (
            invalidate_robot_cascade_dependents,
            reset_slot_to_missing,
        )

        # Auto-bound values express candidate uniqueness, not a durable user
        # preference. Recompute only the automatic suffix below the deepest
        # explicit user choice. Automatic ancestors of an explicit family,
        # variant, or unit must remain in place so that choice can still be
        # validated against its parent chain.
        cascade_keys = (
            "equipment_class",
            "equipment_family",
            "equipment_type",
            "equipment_unit_id",
        )
        for key in cascade_keys:
            slot = new_slots.get(key)
            if (
                slot
                and slot.status == "invalid"
                and slot.source == "system_candidate_resolution"
            ):
                reset_slot_to_missing(
                    slot,
                    source="system_candidate_recompute",
                )

        explicit_indices = [
            index
            for index, key in enumerate(cascade_keys)
            if (
                (slot := new_slots.get(key))
                and slot.status == "valid"
                and slot.value is not None
                and slot.source != "auto"
            )
        ]
        deepest_explicit = max(explicit_indices, default=-1)
        for index, key in enumerate(cascade_keys):
            slot = new_slots.get(key)
            if (
                index > deepest_explicit
                and slot
                and slot.status == "valid"
                and slot.source == "auto"
            ):
                reset_slot_to_missing(slot, source="system_candidate_recompute")

        # ── 1. 前置校验：分离“静态归属”和“当前可行性” ──
        # 任务类型/父层切换后已不在 admission domain 的旧值必须清理，
        # 无论其是否来自用户。但对仍在 admission domain、只因水深/
        # 载荷/运行状态而不在 feasible domain 的明确用户选择，必须保留
        # 给 Validator 产生硬约束；只重算 source=auto 的候选结论。
        def should_invalidate(
            slot: Any,
            *,
            admitted: bool,
            feasible: bool,
            level_index: int,
        ) -> bool:
            if not admitted:
                return True
            return (
                not feasible
                and bool(slot and slot.source == "auto")
                and level_index > deepest_explicit
            )

        def class_node(selection_domain: dict, class_id: str | None) -> dict | None:
            return next(
                (
                    item
                    for item in selection_domain.get("classes", [])
                    if item.get("class_id") == class_id
                ),
                None,
            )

        def family_node(parent: dict | None, family_id: str | None) -> dict | None:
            return next(
                (
                    item
                    for item in (parent or {}).get("families", [])
                    if item.get("family_id") == family_id
                ),
                None,
            )

        def variant_node(parent: dict | None, variant_id: str | None) -> dict | None:
            return next(
                (
                    item
                    for item in (parent or {}).get("variants", [])
                    if item.get("variant_id") == variant_id
                ),
                None,
            )

        cls_slot = new_slots.get("equipment_class")
        if cls_slot and cls_slot.status == "valid" and cls_slot.value:
            resolved_cls = self.kb._resolve_class_key(str(cls_slot.value))
            admitted_cls = class_node(admission_domain, resolved_cls) is not None
            feasible_cls = class_node(domain, resolved_cls) is not None
            if should_invalidate(
                cls_slot,
                admitted=admitted_cls,
                feasible=feasible_cls,
                level_index=0,
            ):
                invalidate_robot_cascade_dependents(new_slots, ["equipment_class"])
                reset_slot_to_missing(cls_slot, source="system_dependency_invalidation")

        fam_slot = new_slots.get("equipment_family")
        if fam_slot and fam_slot.status == "valid" and fam_slot.value:
            cur_cls = new_slots.get("equipment_class")
            cur_cls_id = (
                self.kb._resolve_class_key(str(cur_cls.value))
                if cur_cls and cur_cls.status == "valid" and cur_cls.value
                else None
            )
            resolved_fam_id = self.kb._resolve_family_key(str(fam_slot.value))
            canonical_cls_id = self.kb.get_ancestor_by_level(resolved_fam_id, "class", source_level="family") if resolved_fam_id else None
            if (cur_cls is None or cur_cls.status not in ("valid", "conflict", "candidate") or not cur_cls.value) and canonical_cls_id:
                if "equipment_class" not in new_slots:
                    new_slots["equipment_class"] = Slot("equipment_class")
                cls_s = new_slots["equipment_class"]
                cls_s.value = canonical_cls_id
                cls_s.status = "valid"
                cls_s.source = "auto"
                cls_s.raw_value = canonical_cls_id
                cls_s.confidence = 1.0
                cls_s.candidate_value = None
                cls_s.validation_error = None
                cur_cls_id = canonical_cls_id

            parent_matches = cur_cls_id is None or cur_cls_id == canonical_cls_id
            admitted_fam = family_node(
                class_node(admission_domain, canonical_cls_id),
                resolved_fam_id,
            ) is not None and parent_matches
            feasible_fam = family_node(
                class_node(domain, canonical_cls_id),
                resolved_fam_id,
            ) is not None and parent_matches
            if should_invalidate(
                fam_slot,
                admitted=admitted_fam,
                feasible=feasible_fam,
                level_index=1,
            ):
                invalidate_robot_cascade_dependents(new_slots, ["equipment_family"])
                reset_slot_to_missing(fam_slot, source="system_dependency_invalidation")

        type_slot = new_slots.get("equipment_type")
        if type_slot and type_slot.status == "valid" and type_slot.value:
            cur_cls = new_slots.get("equipment_class")
            cur_fam = new_slots.get("equipment_family")
            cur_cls_id = (
                self.kb._resolve_class_key(str(cur_cls.value))
                if cur_cls and cur_cls.status == "valid" and cur_cls.value
                else None
            )
            cur_fam_id = (
                self.kb._resolve_family_key(str(cur_fam.value))
                if cur_fam and cur_fam.status == "valid" and cur_fam.value
                else None
            )
            try:
                resolved_variant = self.kb._resolve_robot_variant_exact(
                    str(type_slot.value),
                )
            except RobotSelectionDataError:
                resolved_variant = None
            resolved_variant_id = (
                resolved_variant.get("variant_id") if resolved_variant else None
            )
            canonical_cls_id = self.kb.get_ancestor_by_level(resolved_variant_id, "class", source_level="variant") if resolved_variant_id else None
            canonical_fam_id = self.kb.get_ancestor_by_level(resolved_variant_id, "family", source_level="variant") if resolved_variant_id else None

            if (cur_fam is None or cur_fam.status not in ("valid", "conflict", "candidate") or not cur_fam.value) and canonical_fam_id:
                if "equipment_family" not in new_slots:
                    new_slots["equipment_family"] = Slot("equipment_family")
                fam_s = new_slots["equipment_family"]
                fam_cfg = self.kb.robot_fleet.get("robot_families", {}).get(canonical_fam_id, {})
                fam_name = fam_cfg.get("full_name", canonical_fam_id)
                fam_s.value = fam_name
                fam_s.status = "valid"
                fam_s.source = "auto"
                fam_s.raw_value = fam_name
                fam_s.confidence = 1.0
                fam_s.candidate_value = None
                fam_s.validation_error = None
                cur_fam_id = canonical_fam_id

            if (cur_cls is None or cur_cls.status not in ("valid", "conflict", "candidate") or not cur_cls.value) and canonical_cls_id:
                if "equipment_class" not in new_slots:
                    new_slots["equipment_class"] = Slot("equipment_class")
                cls_s = new_slots["equipment_class"]
                cls_s.value = canonical_cls_id
                cls_s.status = "valid"
                cls_s.source = "auto"
                cls_s.raw_value = canonical_cls_id
                cls_s.confidence = 1.0
                cls_s.candidate_value = None
                cls_s.validation_error = None
                cur_cls_id = canonical_cls_id
            parents_match = (
                (cur_cls_id is None or cur_cls_id == canonical_cls_id)
                and (cur_fam_id is None or cur_fam_id == canonical_fam_id)
            )
            admitted_var = variant_node(
                family_node(
                    class_node(admission_domain, canonical_cls_id),
                    canonical_fam_id,
                ),
                resolved_variant_id,
            ) is not None and parents_match
            feasible_var = variant_node(
                family_node(
                    class_node(domain, canonical_cls_id),
                    canonical_fam_id,
                ),
                resolved_variant_id,
            ) is not None and parents_match
            if should_invalidate(
                type_slot,
                admitted=admitted_var,
                feasible=feasible_var,
                level_index=2,
            ):
                invalidate_robot_cascade_dependents(new_slots, ["equipment_type"])
                reset_slot_to_missing(type_slot, source="system_dependency_invalidation")

        unit_slot = new_slots.get("equipment_unit_id")
        if unit_slot and unit_slot.status == "valid" and unit_slot.value:
            cur_cls = new_slots.get("equipment_class")
            cur_fam = new_slots.get("equipment_family")
            cur_type = new_slots.get("equipment_type")
            cur_cls_id = (
                self.kb._resolve_class_key(str(cur_cls.value))
                if cur_cls and cur_cls.status == "valid" and cur_cls.value
                else None
            )
            cur_fam_id = (
                self.kb._resolve_family_key(str(cur_fam.value))
                if cur_fam and cur_fam.status == "valid" and cur_fam.value
                else None
            )
            try:
                resolved_variant = (
                    self.kb._resolve_robot_variant_exact(str(cur_type.value))
                    if cur_type and cur_type.status == "valid" and cur_type.value
                    else None
                )
                resolved_unit = self.kb._resolve_robot_unit_exact(
                    str(unit_slot.value),
                    task_type_key,
                )
            except RobotSelectionDataError:
                resolved_variant = None
                resolved_unit = None
            resolved_variant_id = (
                resolved_variant.get("variant_id") if resolved_variant else None
            )
            resolved_unit_id = (
                resolved_unit.get("unit_id") if resolved_unit else None
            )
            unit_robot = resolved_unit.get("robot") if resolved_unit else None
            canonical_unit_cls_id = (
                unit_robot.get("robot_class") if unit_robot else None
            )
            canonical_unit_fam_id = (
                unit_robot.get("family_id") if unit_robot else None
            )
            canonical_unit_var_id = (
                unit_robot.get("variant_id") if unit_robot else None
            )
            parents_match = (
                (cur_cls_id is None or cur_cls_id == canonical_unit_cls_id)
                and (cur_fam_id is None or cur_fam_id == canonical_unit_fam_id)
                and (
                    resolved_variant_id is None
                    or resolved_variant_id == canonical_unit_var_id
                )
            )
            admission_var = variant_node(
                family_node(
                    class_node(admission_domain, canonical_unit_cls_id),
                    canonical_unit_fam_id,
                ),
                canonical_unit_var_id,
            )
            feasible_var_node = variant_node(
                family_node(
                    class_node(domain, canonical_unit_cls_id),
                    canonical_unit_fam_id,
                ),
                canonical_unit_var_id,
            )
            admitted_unit = any(
                item.get("unit_id") == resolved_unit_id
                for item in (admission_var or {}).get("units", [])
            ) and parents_match
            feasible_unit = any(
                item.get("unit_id") == resolved_unit_id
                for item in (feasible_var_node or {}).get("units", [])
            ) and parents_match
            if should_invalidate(
                unit_slot,
                admitted=admitted_unit,
                feasible=feasible_unit,
                level_index=3,
            ):
                reset_slot_to_missing(unit_slot, source="system_dependency_invalidation")

        # A valid deeper selector has one authoritative registry lineage.
        # Restore/migration callers may legitimately provide only Family,
        # Variant, or Unit, so materialize only missing ancestors before the
        # normal forward collapse.  Explicit ancestors were already checked
        # above and are never overwritten here.
        canonical_state = {
            key: copy.deepcopy(slot.value)
            for key, slot in new_slots.items()
            if key in cascade_keys
            and slot.status == "valid"
            and slot.value is not None
        }
        if canonical_state:
            try:
                canonical_selection = (
                    self.kb.validate_robot_selection_from_task_state(
                        {
                            "task_type_key": task_type_key,
                            **canonical_state,
                        },
                        require_unit=False,
                    )
                )
            except RobotSelectionDataError:
                canonical_selection = None

            if isinstance(canonical_selection, dict):
                canonical_ancestors = (
                    ("equipment_class", "robot_class"),
                    ("equipment_family", "family_name"),
                    ("equipment_type", "equipment_type"),
                )
                for slot_key, result_key in canonical_ancestors:
                    canonical_value = canonical_selection.get(result_key)
                    current_slot = new_slots.get(slot_key)
                    if (
                        isinstance(canonical_value, str)
                        and canonical_value.strip()
                        and (
                            current_slot is None
                            or (
                                current_slot.status == "missing"
                                and current_slot.value is None
                            )
                        )
                    ):
                        if current_slot is None:
                            current_slot = Slot(slot_key)
                            new_slots[slot_key] = current_slot
                        current_slot.value = canonical_value
                        current_slot.status = "valid"
                        current_slot.source = "auto"
                        current_slot.raw_value = canonical_value
                        current_slot.confidence = 1.0
                        current_slot.candidate_value = None
                        current_slot.validation_error = None

        # ── 2. 逐级四级 auto-collapse ──

        # Level 1: equipment_class
        classes = domain["classes"]
        cls_slot = new_slots.get("equipment_class")
        if cls_slot and cls_slot.status in ("invalid", "conflict", "unresolved", "candidate"):
            return
        if not cls_slot or cls_slot.status != "valid" or not cls_slot.value:
            if len(classes) == 0:
                if "equipment_class" not in new_slots:
                    new_slots["equipment_class"] = Slot("equipment_class")
                slot = new_slots["equipment_class"]
                if slot.status not in ("conflict", "invalid"):
                    slot.status = "invalid"
                    slot.value = None
                    slot.source = "system_candidate_resolution"
                    slot.validation_error = f"No feasible robot class for task '{task_type_key}'"
                return
            elif len(classes) == 1:
                cls_info = classes[0]
                if "equipment_class" not in new_slots:
                    new_slots["equipment_class"] = Slot("equipment_class")
                cls_slot = new_slots["equipment_class"]
                cls_slot.value = cls_info["full_name"]
                cls_slot.status = "valid"
                cls_slot.source = "auto"
                cls_slot.raw_value = cls_info["full_name"]
                cls_slot.confidence = 1.0
                cls_slot.validation_error = None
            else:
                # > 1 候选且未指定 -> 停止自动收敛
                return

        # Level 2: equipment_family
        cur_cls_val = str(new_slots["equipment_class"].value)
        cur_cls_id = self.kb._resolve_class_key(cur_cls_val)
        cls_node = next((c for c in classes if c["class_id"] == cur_cls_id), None)
        if not cls_node:
            return

        families = cls_node["families"]
        fam_slot = new_slots.get("equipment_family")
        if fam_slot and fam_slot.status in ("invalid", "conflict", "unresolved", "candidate"):
            return
        if not fam_slot or fam_slot.status != "valid" or not fam_slot.value:
            if len(families) == 0:
                if "equipment_family" not in new_slots:
                    new_slots["equipment_family"] = Slot("equipment_family")
                slot = new_slots["equipment_family"]
                if slot.status not in ("conflict", "invalid"):
                    slot.status = "invalid"
                    slot.value = None
                    slot.source = "system_candidate_resolution"
                    slot.validation_error = f"No feasible robot family under class '{cur_cls_id}' for task '{task_type_key}'"
                return
            elif len(families) == 1:
                fam_info = families[0]
                if "equipment_family" not in new_slots:
                    new_slots["equipment_family"] = Slot("equipment_family")
                fam_slot = new_slots["equipment_family"]
                fam_slot.value = fam_info["full_name"]
                fam_slot.status = "valid"
                fam_slot.source = "auto"
                fam_slot.raw_value = fam_info["full_name"]
                fam_slot.confidence = 1.0
                fam_slot.validation_error = None
            else:
                # > 1 候选且未指定 -> 停止自动收敛
                return

        # Level 3: equipment_type
        cur_fam_val = str(new_slots["equipment_family"].value)
        cur_fam_id = self.kb.resolve_robot_family_id(cur_fam_val, task_type_key)
        fam_node = next((f for f in families if f["family_id"] == cur_fam_id), None)
        if not fam_node:
            return

        variants = fam_node["variants"]
        type_slot = new_slots.get("equipment_type")
        if type_slot and type_slot.status in ("invalid", "conflict", "unresolved", "candidate"):
            return
        if not type_slot or type_slot.status != "valid" or not type_slot.value:
            if len(variants) == 0:
                if "equipment_type" not in new_slots:
                    new_slots["equipment_type"] = Slot("equipment_type")
                slot = new_slots["equipment_type"]
                if slot.status not in ("conflict", "invalid"):
                    slot.status = "invalid"
                    slot.value = None
                    slot.source = "system_candidate_resolution"
                    slot.validation_error = f"No feasible robot variant under family '{cur_fam_id}' for task '{task_type_key}'"
                return
            elif len(variants) == 1:
                var_info = variants[0]
                if "equipment_type" not in new_slots:
                    new_slots["equipment_type"] = Slot("equipment_type")
                type_slot = new_slots["equipment_type"]
                type_slot.value = var_info["full_name"]
                type_slot.status = "valid"
                type_slot.source = "auto"
                type_slot.raw_value = var_info["full_name"]
                type_slot.confidence = 1.0
                type_slot.validation_error = None
            else:
                # > 1 候选且未指定 -> 停止自动收敛
                return

        # Level 4: equipment_unit_id
        cur_type_val = str(new_slots["equipment_type"].value)
        var_node = next((v for v in variants if v["full_name"] == cur_type_val or v["variant_id"] == cur_type_val), None)
        if not var_node:
            return

        units = var_node["units"]
        unit_slot = new_slots.get("equipment_unit_id")
        if unit_slot and unit_slot.status in ("invalid", "conflict", "unresolved", "candidate"):
            return
        if not unit_slot or unit_slot.status != "valid" or not unit_slot.value:
            if len(units) == 0:
                if "equipment_unit_id" not in new_slots:
                    new_slots["equipment_unit_id"] = Slot("equipment_unit_id")
                slot = new_slots["equipment_unit_id"]
                if slot.status not in ("conflict", "invalid"):
                    slot.status = "invalid"
                    slot.value = None
                    slot.source = "system_candidate_resolution"
                    slot.validation_error = f"No fleet units configured for variant '{cur_type_val}'"
                return
            elif len(units) == 1:
                unit_info = units[0]
                if "equipment_unit_id" not in new_slots:
                    new_slots["equipment_unit_id"] = Slot("equipment_unit_id")
                unit_slot = new_slots["equipment_unit_id"]
                unit_slot.value = unit_info["unit_id"]
                unit_slot.status = "valid"
                unit_slot.source = "auto"
                unit_slot.raw_value = unit_info["unit_id"]
                unit_slot.confidence = 1.0
                unit_slot.validation_error = None
            else:
                # > 1 候选且未指定 -> 停止自动收敛
                return

    def _handle_equipment_updates_in_transaction(
        self,
        updates: dict,
        new_slots: dict,
        allow_overwrite: bool,
    ) -> None:
        """统一处理机器人类别、系列、型号和单机编号的四级层级联动与依赖失效。"""
        import copy
        from src.slot_store import (
            ROBOT_CASCADE_DEPENDENCIES,
            reset_slot_to_missing,
            invalidate_robot_cascade_dependents,
        )

        EQUIPMENT_KEYS = (
            "equipment_class",
            "equipment_family",
            "equipment_type",
            "equipment_unit_id",
            "equipment_name",
        )

        equipment_updates = {}
        for key in EQUIPMENT_KEYS:
            val = updates.get(key)
            if isinstance(val, dict):
                if "value" in val and len(val) <= 4 and ("raw_value" in val or "source" in val or "confidence" in val):
                    val = val.get("value")
            if val not in (None, ""):
                if isinstance(val, str):
                    val = val.strip()
                equipment_updates[key] = val

        # 兼容旧设备规格输入: 如果没有 equipment_type，但提供了 equipment_specification（含 variant_id），映射为 equipment_type
        if "equipment_type" not in equipment_updates and "equipment_specification" in updates:
            spec_val = updates.get("equipment_specification")
            if isinstance(spec_val, dict) and "variant_id" in spec_val:
                vid = spec_val.get("variant_id")
                var_info = self.kb.get_rov(vid) if hasattr(self, "kb") and self.kb else None
                if var_info and var_info.get("full_name"):
                    equipment_updates["equipment_type"] = var_info.get("full_name")
                elif vid:
                    equipment_updates["equipment_type"] = vid

        if not equipment_updates:
            return

        # 保存 5 槽完整前置快照
        equipment_before = {
            k: copy.deepcopy(new_slots[k])
            for k in EQUIPMENT_KEYS
            if k in new_slots
        }

        task_type_slot = new_slots.get("task_type_key")
        task_type = (
            task_type_slot.value
            if task_type_slot
            and task_type_slot.status == "valid"
            and task_type_slot.value is not None
            else None
        )

        def _unwrap(v):
            return v.get("value") if isinstance(v, dict) else v

        # 辅助函数：校验/推演失败时，原子回滚 new_slots 并标记目标 Slot
        def _rollback_and_fail(target_key: str, candidate_val: Any, error_msg: str, force_conflict: bool = False):
            for k in EQUIPMENT_KEYS:
                if k in equipment_before:
                    new_slots[k] = copy.deepcopy(equipment_before[k])
                elif k in new_slots:
                    del new_slots[k]

            prior_slot = equipment_before.get(target_key)
            has_prior_valid_value = (
                prior_slot is not None
                and prior_slot.status in ("valid", "conflict")
                and prior_slot.value is not None
            )
            if force_conflict or has_prior_valid_value:
                target_slot = copy.deepcopy(prior_slot) if prior_slot else Slot(slot_name=target_key)
                target_slot.status = "conflict"
                target_slot.candidate_value = candidate_val
                target_slot.validation_error = error_msg
                new_slots[target_key] = target_slot
            else:
                default_vtype = BASE_SLOT_TYPES.get(target_key, "string")
                target_slot = copy.deepcopy(prior_slot) if prior_slot else Slot(slot_name=target_key, value_type=default_vtype)
                target_slot.status = "invalid"
                target_slot.value = None
                target_slot.value_type = default_vtype
                target_slot.candidate_value = candidate_val
                target_slot.validation_error = error_msg
                new_slots[target_key] = target_slot

        if not task_type:
            # 仅在 task_type 为 missing 时尝试根据设备自动补全唯一关联的任务类型，不得强改 candidate 状态
            task_type_slot = new_slots.get("task_type_key")
            if not task_type_slot or task_type_slot.status == "missing" or task_type_slot.value is None:
                inferred_tt_key = None
                for key in ("equipment_unit_id", "equipment_name", "equipment_type", "equipment_family", "equipment_class"):
                    val = equipment_updates.get(key)
                    if val:
                        val_str = str(val.get("value") if isinstance(val, dict) else val)
                        resolved_unit = self.kb.resolve_robot_unit(val_str, None)
                        resolved_family = self.kb.resolve_robot_family(val_str, None) if not resolved_unit else None
                        cls_id = None
                        family_id = None
                        if resolved_unit:
                            robot = resolved_unit.get("robot") or {}
                            cls_id = robot.get("robot_class")
                            family_id = robot.get("family_id")
                        elif resolved_family:
                            cls_id = resolved_family.get("robot_class")
                            family_id = resolved_family.get("family_id")
                        elif key == "equipment_class":
                            cls_id = self.kb._resolve_class_key(val_str)

                        if cls_id or family_id:
                            families_cfg = self.kb.robot_fleet.get("robot_families", {}) or {}
                            matched = [
                                t_k
                                for t_k, t_c in self.kb.task_schemas.get("task_templates", {}).items()
                                if any(
                                    set(t_c.get("required_capabilities", []) or []).issubset(
                                        set(f_cfg.get("capabilities", []) or [])
                                    )
                                    for f_id, f_cfg in families_cfg.items()
                                    if isinstance(f_cfg, dict)
                                    and (
                                        (family_id and f_id == family_id)
                                        or (
                                            not family_id
                                            and cls_id
                                            and f_cfg.get("robot_class") == cls_id
                                        )
                                    )
                                )
                            ]
                            if len(matched) == 1:
                                inferred_tt_key = matched[0]
                                break

                if inferred_tt_key:
                    self._handle_task_type_update_in_transaction("task_type_key", inferred_tt_key, new_slots)
                    task_type_slot = new_slots.get("task_type_key")
                    task_type = (
                        task_type_slot.value
                        if task_type_slot and task_type_slot.status == "valid"
                        else None
                    )

        if not task_type:
            for target_key in (
                "equipment_unit_id",
                "equipment_name",
                "equipment_type",
                "equipment_family",
                "equipment_class",
            ):
                if target_key in equipment_updates:
                    _rollback_and_fail(
                        target_key,
                        equipment_updates[target_key],
                        "Task type must be confirmed before robot selection",
                    )
                    return

        # Conflict Fence
        if not allow_overwrite:
            highest_conflict_key = None
            highest_candidate_val = None
            highest_conflict_reason = None

            # 1. 检查 equipment_class
            cls_in = equipment_updates.get("equipment_class")
            if cls_in:
                active_cls_slot = equipment_before.get("equipment_class")
                if active_cls_slot and active_cls_slot.status in ("valid", "conflict") and active_cls_slot.value is not None:
                    res_cls_id = self.kb._resolve_class_key(str(cls_in))
                    active_cls_id = self.kb._resolve_class_key(str(active_cls_slot.value)) or str(active_cls_slot.value)
                    if res_cls_id and active_cls_id and res_cls_id != active_cls_id:
                        highest_conflict_key = "equipment_class"
                        highest_candidate_val = cls_in
                        highest_conflict_reason = f"Robot class '{cls_in}' conflicts with active valid class '{active_cls_slot.value}'"

            # 2. 检查 equipment_family (若 class 未冲突)
            if not highest_conflict_key:
                fam_in = equipment_updates.get("equipment_family")
                if fam_in:
                    active_fam_slot = equipment_before.get("equipment_family")
                    if active_fam_slot and active_fam_slot.status in ("valid", "conflict") and active_fam_slot.value is not None:
                        res_fam = self.kb.resolve_robot_family(str(fam_in), task_type)
                        if res_fam:
                            active_fam_id = self.kb.resolve_robot_family_id(str(active_fam_slot.value), task_type)
                            if res_fam.get("family_id") and active_fam_id and res_fam.get("family_id") != active_fam_id:
                                highest_conflict_key = "equipment_family"
                                highest_candidate_val = fam_in
                                highest_conflict_reason = f"Robot family '{fam_in}' conflicts with active valid family '{active_fam_slot.value}'"

            # 3. 检查 equipment_type (若 class/family 未冲突)
            if not highest_conflict_key:
                type_in = equipment_updates.get("equipment_type")
                if type_in and "equipment_unit_id" not in equipment_updates:
                    active_type_slot = equipment_before.get("equipment_type")
                    if active_type_slot and active_type_slot.status in ("valid", "conflict") and active_type_slot.value is not None:
                        res_var = self.kb.get_rov_for_task(str(type_in), task_type)
                        if res_var and res_var.get("full_name") != active_type_slot.value:
                            highest_conflict_key = "equipment_type"
                            highest_candidate_val = type_in
                            highest_conflict_reason = f"Robot variant '{type_in}' conflicts with active valid type '{active_type_slot.value}'"

            # 4. 检查 equipment_unit_id
            if not highest_conflict_key:
                unit_in = equipment_updates.get("equipment_unit_id")
                if unit_in:
                    active_unit_slot = equipment_before.get("equipment_unit_id")
                    if active_unit_slot and active_unit_slot.status in ("valid", "conflict") and active_unit_slot.value is not None:
                        res_unit = self.kb.resolve_robot_unit(str(unit_in), task_type)
                        if res_unit and res_unit.get("unit_id") != active_unit_slot.value:
                            highest_conflict_key = "equipment_unit_id"
                            highest_candidate_val = unit_in
                            highest_conflict_reason = f"Robot unit '{unit_in}' conflicts with active valid unit '{active_unit_slot.value}'"

            if highest_conflict_key:
                _rollback_and_fail(
                    highest_conflict_key,
                    highest_candidate_val,
                    highest_conflict_reason,
                    force_conflict=True,
                )
                return

        # 在沙盒中推演
        sandbox_slots = copy.deepcopy(new_slots)
        changed_parents = []

        # 1. equipment_class 更新
        class_update = equipment_updates.get("equipment_class")
        if class_update:
            resolved_class_id = self.kb._resolve_class_key(str(class_update))
            try:
                if not resolved_class_id:
                    classes = self.kb.list_robot_classes(task_type)
                    for c in classes:
                        if c.get("class_id") == class_update or c.get("display_name") == class_update:
                            resolved_class_id = c.get("class_id")
                            break

                if task_type:
                    allowed_classes = [c.get("class_id") for c in self.kb.list_robot_classes(task_type)]
                    if resolved_class_id not in allowed_classes:
                        resolved_class_id = None
            except RobotSelectionDataError as _err:
                _rollback_and_fail(
                    "equipment_class",
                    class_update,
                    f"{_err.error_code}: {_err}",
                )
                return

            if resolved_class_id:
                class_slot = sandbox_slots.get("equipment_class")
                old_class = (
                    class_slot.value
                    if class_slot and class_slot.status == "valid"
                    else None
                )
                if allow_overwrite and old_class and old_class != resolved_class_id:
                    changed_parents.append("equipment_class")
                self._apply_slot_update_in_transaction(
                    "equipment_class",
                    resolved_class_id,
                    sandbox_slots,
                    allow_overwrite,
                )
                sandbox_slots["equipment_class"].status = "valid"
            else:
                _rollback_and_fail(
                    "equipment_class",
                    class_update,
                    f"Robot class '{class_update}' is unknown or not allowed for task '{task_type}'",
                )
                return

        # 2. equipment_family 更新
        family_update = equipment_updates.get("equipment_family")
        resolved_family = None
        if family_update:
            resolved_family = self.kb.resolve_robot_family(str(family_update), task_type)
            if not resolved_family and not task_type:
                resolved_family = self.kb.resolve_robot_family(str(family_update), None)
            if resolved_family:
                explicit_class_in_turn = "equipment_class" in equipment_updates
                active_class_slot = sandbox_slots.get("equipment_class")
                active_class_val = (
                    active_class_slot.value
                    if active_class_slot and active_class_slot.status == "valid"
                    else None
                )
                active_class = self.kb._resolve_class_key(str(active_class_val)) if active_class_val else None
                target_class = resolved_family.get("robot_class")

                if explicit_class_in_turn and active_class and target_class != active_class:
                    f_slot = sandbox_slots.get("equipment_family") or Slot(slot_name="equipment_family")
                    f_slot.status = "invalid"
                    f_slot.value = None
                    f_slot.candidate_value = family_update
                    f_slot.validation_error = f"Family '{family_update}' does not belong to selected class '{active_class}'"
                    sandbox_slots["equipment_family"] = f_slot
                elif not allow_overwrite and active_class and target_class != active_class:
                    _rollback_and_fail(
                        "equipment_family",
                        family_update,
                        f"Family '{family_update}' conflicts with active class '{active_class}'",
                        force_conflict=True,
                    )
                    return
                else:
                    if active_class and active_class != target_class:
                        changed_parents.append("equipment_class")
                    self._apply_slot_update_in_transaction(
                        "equipment_class",
                        target_class,
                        sandbox_slots,
                        allow_overwrite,
                    )
                    sandbox_slots["equipment_class"].status = "valid"
                    family_slot = sandbox_slots.get("equipment_family")
                    current_family_id = (
                        self.kb.resolve_robot_family_id(str(family_slot.value), task_type)
                        if family_slot and family_slot.value and family_slot.status == "valid"
                        else None
                    )
                    if (
                        allow_overwrite
                        and current_family_id
                        and current_family_id != resolved_family.get("family_id")
                    ):
                        changed_parents.append("equipment_family")
                    self._apply_slot_update_in_transaction(
                        "equipment_family",
                        resolved_family.get("full_name", family_update),
                        sandbox_slots,
                        allow_overwrite,
                    )
                    sandbox_slots["equipment_family"].status = "valid"
            else:
                _rollback_and_fail(
                    "equipment_family",
                    family_update,
                    f"Unknown robot family '{family_update}' for task '{task_type}'",
                )
                return

        # 3. equipment_type (model_variant) 更新
        variant_update = equipment_updates.get("equipment_type")
        selected_variant = None
        if variant_update:
            active_fam_slot = sandbox_slots.get("equipment_family")
            active_family = (
                active_fam_slot.value
                if active_fam_slot and active_fam_slot.status == "valid"
                else None
            )
            active_fam_info = self.kb.resolve_robot_family(str(active_family), task_type) if active_family else None
            active_fam_id = active_fam_info.get("family_id") if active_fam_info else None

            # 全局解算 target variant
            selected_variant = self.kb.get_rov_for_task(
                str(variant_update),
                task_type,
                active_fam_id,
            )
            if not selected_variant:
                selected_variant = self.kb.get_rov_for_task(
                    str(variant_update),
                    task_type,
                    None,
                )
            if not selected_variant and not task_type:
                selected_variant = self.kb.get_rov_for_task(
                    str(variant_update),
                    None,
                    None,
                )
            if not selected_variant:
                selected_variant = self.kb.get_rov(str(variant_update))

            if selected_variant and task_type and not self.kb.robot_matches_task(selected_variant, task_type):
                selected_variant = None

            if selected_variant:
                robot_cls = selected_variant.get("robot_class")
                fam_id = selected_variant.get("family_id")
                fam_full = selected_variant.get("family_full_name")

                explicit_fam_in_turn = "equipment_family" in equipment_updates
                explicit_fam_id = (
                    resolved_family.get("family_id") if resolved_family else None
                )
                if explicit_fam_in_turn and explicit_fam_id and explicit_fam_id != fam_id:
                    t_slot = sandbox_slots.get("equipment_type") or Slot(slot_name="equipment_type")
                    t_slot.status = "invalid"
                    t_slot.value = None
                    t_slot.candidate_value = variant_update
                    t_slot.validation_error = f"Variant '{variant_update}' does not belong to selected family '{family_update}'"
                    sandbox_slots["equipment_type"] = t_slot

                    # 清理/作废旧下级槽位，防止形成跨类目混合状态
                    for key_to_clear in ("equipment_unit_id", "equipment_name"):
                        if key_to_clear in sandbox_slots:
                            s = sandbox_slots[key_to_clear]
                            s.value = None
                            s.status = "missing"
                            s.validation_error = None

                    for k in EQUIPMENT_KEYS:
                        if k in sandbox_slots:
                            new_slots[k] = sandbox_slots[k]
                    return
                elif not allow_overwrite and active_fam_id and active_fam_id != fam_id:
                    _rollback_and_fail(
                        "equipment_type",
                        variant_update,
                        f"Variant '{variant_update}' conflicts with active family '{active_family}'",
                        force_conflict=True,
                    )
                    return

                old_variant_slot = sandbox_slots.get("equipment_type")
                old_variant_val = (
                    old_variant_slot.value
                    if old_variant_slot and old_variant_slot.status == "valid"
                    else None
                )
                new_variant_val = selected_variant.get("full_name", variant_update)
                if allow_overwrite and old_variant_val and old_variant_val != new_variant_val:
                    changed_parents.append("equipment_type")
                self._apply_slot_update_in_transaction(
                    "equipment_class",
                    robot_cls,
                    sandbox_slots,
                    allow_overwrite,
                )
                sandbox_slots["equipment_class"].status = "valid"
                self._apply_slot_update_in_transaction(
                    "equipment_family",
                    fam_full,
                    sandbox_slots,
                    allow_overwrite,
                )
                sandbox_slots["equipment_family"].status = "valid"
                self._apply_slot_update_in_transaction(
                    "equipment_type",
                    new_variant_val,
                    sandbox_slots,
                    allow_overwrite,
                )
                sandbox_slots["equipment_type"].status = "valid"
            else:
                _rollback_and_fail(
                    "equipment_type",
                    variant_update,
                    f"Unknown model variant '{variant_update}'",
                )
                return

        # 4. equipment_unit_id / equipment_name 更新
        unit_update = (
            _unwrap(equipment_updates.get("equipment_unit_id"))
            or _unwrap(equipment_updates.get("equipment_name"))
        )
        if unit_update:
            variant_context = (
                selected_variant.get("full_name")
                if selected_variant
                else None
            )
            resolved_unit = self.kb.resolve_robot_unit(
                str(unit_update),
                task_type,
                str(variant_context) if variant_context else None,
            )
            if not resolved_unit and not task_type:
                resolved_unit = self.kb.resolve_robot_unit(
                    str(unit_update),
                    None,
                )

            if not resolved_unit:
                unit_raw = None
                raw_item = updates.get("equipment_unit_id") or updates.get("equipment_name")
                if isinstance(raw_item, dict):
                    unit_raw = raw_item.get("raw_value")
                if unit_raw and isinstance(unit_raw, str) and unit_raw != str(unit_update):
                    resolved_unit = self.kb.resolve_robot_unit(
                        str(unit_raw),
                        task_type,
                        str(variant_context) if variant_context else None,
                    )
                    if not resolved_unit and not task_type:
                        resolved_unit = self.kb.resolve_robot_unit(
                            str(unit_raw),
                            None,
                        )

            if not resolved_unit and hasattr(self.kb, "resolve_robot_unit_from_text") and getattr(self, "conversation_history", None):
                last_user_msg = next((m.get("content") for m in reversed(self.conversation_history) if isinstance(m, dict) and m.get("role") == "user"), "")
                if last_user_msg:
                    resolved_unit = self.kb.resolve_robot_unit_from_text(last_user_msg, task_type)

            if resolved_unit and task_type and not self.kb.robot_matches_task(resolved_unit.get("robot"), task_type):
                resolved_unit = None

            if resolved_unit:
                unit_variant = resolved_unit["robot"]
                unit_robot_cls = unit_variant.get("robot_class")
                unit_fam_id = unit_variant.get("family_id")
                unit_fam_full = unit_variant.get("family_full_name")
                unit_vid = unit_variant.get("variant_id")
                unit_variant_full = unit_variant.get("full_name")

                explicit_class_in_turn = equipment_updates.get("equipment_class")
                explicit_family_in_turn = equipment_updates.get("equipment_family")
                explicit_type_in_turn = equipment_updates.get("equipment_type")

                explicit_cls_mismatch = (
                    explicit_class_in_turn is not None
                    and self.kb._resolve_class_key(str(explicit_class_in_turn)) != unit_robot_cls
                )
                explicit_fam_mismatch = False
                if explicit_family_in_turn is not None:
                    explicit_fam_resolved = self.kb.resolve_robot_family_id(str(explicit_family_in_turn), task_type)
                    explicit_fam_mismatch = (
                        explicit_fam_resolved is not None
                        and explicit_fam_resolved != unit_fam_id
                    )
                explicit_type_mismatch = False
                if explicit_type_in_turn is not None:
                    exp_v = self.kb.get_rov(str(explicit_type_in_turn))
                    exp_vid = exp_v.get("variant_id") if exp_v else explicit_type_in_turn
                    if exp_vid and exp_vid != unit_vid:
                        explicit_type_mismatch = True

                parent_mismatch = explicit_cls_mismatch or explicit_fam_mismatch or explicit_type_mismatch

                if parent_mismatch:
                    _rollback_and_fail(
                        "equipment_unit_id",
                        unit_update,
                        f"Unit '{unit_update}' belongs to variant '{unit_variant_full}' but explicitly selected parent is mismatched",
                    )
                    return

                # 四级组合权威校验
                try:
                    self.kb.validate_static_robot_selection(
                        unit_robot_cls,
                        unit_fam_id,
                        unit_variant_full,
                        resolved_unit["unit_id"],
                        task_type,
                    )
                except RobotSelectionDataError as _v_exc:
                    _rollback_and_fail(
                        "equipment_unit_id",
                        unit_update,
                        f"{_v_exc.error_code}: {_v_exc}",
                        force_conflict=True,
                    )
                    return

                # 四级校验通过，更新 sandbox
                self._apply_slot_update_in_transaction(
                    "equipment_class",
                    unit_robot_cls,
                    sandbox_slots,
                    allow_overwrite,
                )
                sandbox_slots["equipment_class"].status = "valid"
                self._apply_slot_update_in_transaction(
                    "equipment_family",
                    unit_fam_full,
                    sandbox_slots,
                    allow_overwrite,
                )
                sandbox_slots["equipment_family"].status = "valid"
                self._apply_slot_update_in_transaction(
                    "equipment_type",
                    unit_variant_full,
                    sandbox_slots,
                    allow_overwrite,
                )
                sandbox_slots["equipment_type"].status = "valid"
                self._apply_slot_update_in_transaction(
                    "equipment_unit_id",
                    resolved_unit["unit_id"],
                    sandbox_slots,
                    allow_overwrite,
                )
                sandbox_slots["equipment_unit_id"].status = "valid"
                if "equipment_name" in sandbox_slots:
                    self._apply_slot_update_in_transaction(
                        "equipment_name",
                        resolved_unit.get("display_name", resolved_unit["unit_id"]),
                        sandbox_slots,
                        allow_overwrite,
                    )
                    sandbox_slots["equipment_name"].status = "valid"
            else:
                _rollback_and_fail(
                    "equipment_unit_id",
                    unit_update,
                    f"Unknown fleet unit '{unit_update}'",
                )
                return

        # 执行层级依赖失效
        robot_cascade_preserve_keys = set(equipment_updates.keys())
        if "payload" in updates:
            robot_cascade_preserve_keys.add("payload")
        if changed_parents:
            invalidate_robot_cascade_dependents(
                sandbox_slots,
                changed_parents,
                preserve_keys=robot_cascade_preserve_keys,
            )
            unit_slot = sandbox_slots.get("equipment_unit_id")
            if not (
                unit_slot
                and unit_slot.status == "valid"
                and unit_slot.value not in (None, "")
            ):
                self.slot_store.validation_result = None

        # 事务生效
        for k in EQUIPMENT_KEYS:
            if k in sandbox_slots:
                new_slots[k] = sandbox_slots[k]
        if changed_parents and "payload" in sandbox_slots:
            new_slots["payload"] = sandbox_slots["payload"]

        # 若当前 task_type_key 为空，且设备类别已推导确定，自动推导唯一的关联任务类型
        cur_tt_slot = new_slots.get("task_type_key")
        if not cur_tt_slot or cur_tt_slot.status != "valid" or not cur_tt_slot.value:
            eq_cls_slot = new_slots.get("equipment_class")
            if eq_cls_slot and eq_cls_slot.status == "valid" and eq_cls_slot.value:
                cls_id = self.kb._resolve_class_key(str(eq_cls_slot.value))
                if cls_id:
                    families_cfg = self.kb.robot_fleet.get("robot_families", {}) or {}
                    matched_templates = [
                        t_key
                        for t_key, t_cfg in self.kb.task_schemas.get("task_templates", {}).items()
                        if any(
                            set(t_cfg.get("required_capabilities", []) or []).issubset(
                                set(f_cfg.get("capabilities", []) or [])
                            )
                            for f_cfg in families_cfg.values()
                            if isinstance(f_cfg, dict) and f_cfg.get("robot_class") == cls_id
                        )
                    ]
                    if len(matched_templates) == 1:
                        inferred_key = matched_templates[0]
                        self._handle_task_type_update_in_transaction("task_type_key", inferred_key, new_slots)


    def _resolve_task_type_target(self, key: str, value: object) -> str | None:
        """Resolve either task selector field through one authoritative rule."""
        if not isinstance(value, str):
            return None
        task_type_map = self.kb.get_task_type_map()
        templates = self.kb.task_schemas.get("task_templates", {})

        if value in task_type_map:
            return task_type_map[value]
        if key == "task_type_key" and value in templates:
            return str(value)
        return None

    def _task_transition_shared_field_keys(
        self,
        current_task_type_key: str,
        target_task_type_key: str,
    ) -> set[str]:
        """Return schema facts that are safe to inherit across a task switch.

        A matching key is not enough: both schemas must declare the same field
        contract.  Task-specific payload and robot selectors are always
        re-evaluated in the target domain even though their YAML keys match.
        """
        current_by_key = {
            str(field.get("key")): field
            for field in self.builder.get_schema(current_task_type_key, self.mode)
            if isinstance(field, dict) and field.get("key")
        }
        target_by_key = {
            str(field.get("key")): field
            for field in self.builder.get_schema(target_task_type_key, self.mode)
            if isinstance(field, dict) and field.get("key")
        }
        ignored_contract_keys = {"label"}

        def comparable(field: dict) -> dict:
            return {
                key: copy.deepcopy(value)
                for key, value in field.items()
                if key not in ignored_contract_keys
            }

        return {
            key
            for key in current_by_key.keys() & target_by_key.keys()
            if key not in TASK_TRANSITION_NON_INHERITED_FIELDS
            and current_by_key[key].get("type") not in {"auto", "fixed"}
            and target_by_key[key].get("type") not in {"auto", "fixed"}
            and comparable(current_by_key[key]) == comparable(target_by_key[key])
        }

    def _build_task_transition_state(
        self,
        current_state: dict,
        current_task_type_key: str,
        target_task_type_key: str,
    ) -> dict:
        """Project current facts into one clean target-task evaluation view."""
        shared_keys = self._task_transition_shared_field_keys(
            current_task_type_key,
            target_task_type_key,
        )
        target_state = {
            key: copy.deepcopy(value)
            for key, value in current_state.items()
            if key in shared_keys and value is not None
        }
        target_state["task_type_key"] = target_task_type_key
        return target_state

    def _build_post_update_evaluation_context(
        self,
        new_slots: dict,
        target_task_type_key: str,
        base_state: dict,
        extraction: dict,
        *,
        transition_state_active: bool,
    ) -> tuple[dict, dict]:
        """Project same-turn robot selectors into an isolated evaluation view.

        Dynamic schema values (notably payload) depend on the selected Variant.
        Reuse the KnowledgeBase static tuple authority to derive a canonical
        Class -> Family -> Variant -> Unit view before normalizing dependent
        fields.  The real transaction and its specialized equipment handler
        are not mutated or invoked here.
        """
        sandbox_slots = copy.deepcopy(new_slots)
        if transition_state_active:
            self._clear_non_inherited_transition_slots(sandbox_slots)

        task_slot = sandbox_slots.get("task_type_key")
        if task_slot is None:
            task_slot = Slot("task_type_key", value_type="string")
            sandbox_slots["task_type_key"] = task_slot
        task_slot.value = target_task_type_key
        task_slot.status = "valid"
        task_slot.candidate_value = None
        task_slot.validation_error = None

        # The transition discovery pass may already have normalized safe
        # shared facts (for example water_depth/start_time) that are not yet
        # committed to ``new_slots``.  Materialize those facts only inside the
        # evaluation sandbox so the robot feasibility domain sees the same
        # target-task context that dependent fields will use.
        for key, value in base_state.items():
            if key == "task_type_key" or value is None:
                continue
            slot = sandbox_slots.get(key)
            if slot is None:
                slot = Slot(key, value_type=BASE_SLOT_TYPES.get(key, "string"))
                sandbox_slots[key] = slot
            if slot.status not in ("missing", "valid"):
                continue
            slot.value = copy.deepcopy(value)
            slot.status = "valid"
            slot.candidate_value = None
            slot.validation_error = None

        equipment_updates: dict[str, dict[str, Any]] = {}
        for candidate in extraction.get("slot_candidates", []):
            if not isinstance(candidate, dict):
                continue
            key = str(candidate.get("canonical_key") or "")
            if key == "equipment_model":
                key = "equipment_type"
            if key not in ROBOT_CASCADE_FIELDS and key != "equipment_name":
                continue
            value = candidate.get("normalized_value")
            if value is None or value == "":
                continue
            equipment_updates[key] = {
                "value": value,
                "raw_value": candidate.get("raw_value", value),
                "confidence": candidate.get("confidence", 1.0),
                "source": self._source_for_resolution_method(
                    candidate.get("resolution_method")
                ),
            }

        projected_equipment = False
        if equipment_updates:
            projected_equipment = self._project_equipment_updates_for_evaluation(
                equipment_updates,
                sandbox_slots,
                target_task_type_key,
            )

        effective_state = {
            key: copy.deepcopy(slot.value)
            for key, slot in sandbox_slots.items()
            if slot.status == "valid" and slot.value is not None
        }
        # Safe shared values extracted in the discovery pass have not yet been
        # committed to sandbox_slots; overlay them for target-schema catalogs.
        effective_state.update(copy.deepcopy(base_state))
        effective_state["task_type_key"] = target_task_type_key

        if equipment_updates:
            # A failed same-turn robot selector must not silently fall back to
            # the previous robot when evaluating its dependent payload.
            for key in (*ROBOT_CASCADE_FIELDS, "equipment_name"):
                effective_state.pop(key, None)
            if projected_equipment:
                for key in (*ROBOT_CASCADE_FIELDS, "equipment_name"):
                    slot = sandbox_slots.get(key)
                    if slot and slot.status == "valid" and slot.value is not None:
                        effective_state[key] = copy.deepcopy(slot.value)

        return sandbox_slots, effective_state

    def _project_equipment_updates_for_evaluation(
        self,
        updates: dict,
        sandbox_slots: dict,
        task_type_key: str,
    ) -> bool:
        """Purely project a canonical robot lineage for dynamic-value lookup.

        This is intentionally not the mutating equipment handler.  It invokes
        the same KnowledgeBase static tuple authority on an isolated state and
        materializes only its canonical result, keeping the real equipment
        handler as the single transaction commit path.
        """
        selection_state: dict[str, Any] = {"task_type_key": task_type_key}

        def value_of(value: Any) -> Any:
            return value.get("value") if isinstance(value, dict) else value

        for key in ROBOT_CASCADE_FIELDS:
            value = value_of(updates.get(key))
            if value not in (None, ""):
                selection_state[key] = value
        if "equipment_unit_id" not in selection_state:
            equipment_name = value_of(updates.get("equipment_name"))
            if equipment_name not in (None, ""):
                selection_state["equipment_unit_id"] = equipment_name

        if len(selection_state) == 1:
            return False
        try:
            canonical = self.kb.validate_robot_selection_from_task_state(
                selection_state,
                require_unit=False,
            )
        except (RobotSelectionDataError, TypeError, ValueError):
            return False
        if not isinstance(canonical, dict):
            return False

        # The canonical result describes the deepest explicitly selected
        # level.  Any older descendant not present in that result belongs to
        # the previous selection and must not constrain dynamic values in this
        # evaluation sandbox (for example, Class-only AUV after an old ROV).
        for key in (*ROBOT_CASCADE_FIELDS, "equipment_name"):
            slot = sandbox_slots.get(key)
            if slot is not None:
                reset_slot_to_missing(
                    slot,
                    source="evaluation_projection",
                )

        canonical_values = {
            "equipment_class": canonical.get("robot_class"),
            "equipment_family": canonical.get("family_name"),
            "equipment_type": canonical.get("equipment_type")
            or canonical.get("variant_name"),
            "equipment_unit_id": canonical.get("unit_id"),
            "equipment_name": canonical.get("unit_display_name"),
        }
        for key, value in canonical_values.items():
            if value is None or value == "":
                continue
            slot = sandbox_slots.get(key)
            if slot is None:
                slot = Slot(key, value_type=BASE_SLOT_TYPES.get(key, "string"))
                sandbox_slots[key] = slot
            slot.value = copy.deepcopy(value)
            slot.status = "valid"
            slot.candidate_value = None
            slot.validation_error = None

        # A Class- or Family-only selector can still have exactly one feasible
        # descendant chain.  Collapse that chain now, while still operating on
        # the isolated sandbox, so payload and other dynamic catalogs are
        # evaluated against the same final Variant that the real transaction
        # will auto-bind later.
        self._auto_collapse_robot_cascade(sandbox_slots)
        return True

    @staticmethod
    def _dynamic_allowed_schema_keys(field_defs: list[dict]) -> set[str]:
        """Fields whose allowed values depend on the finalized robot tuple."""
        dynamic_refs = {
            "supported_payloads",
            "onboard_payloads",
            "all_payloads",
        }
        result: set[str] = set()
        for field in field_defs:
            if not isinstance(field, dict) or not field.get("key"):
                continue
            ref = str(field.get("allowed_values_ref") or "")
            if ref in dynamic_refs or ref.startswith("payload_options."):
                result.add(str(field["key"]))
        return result

    @staticmethod
    def _clear_non_inherited_transition_slots(new_slots: dict) -> None:
        """Remove stale task-specific facts before target-task updates apply."""
        for key in TASK_TRANSITION_NON_INHERITED_FIELDS:
            slot = new_slots.get(key)
            if slot is not None:
                reset_slot_to_missing(
                    slot,
                    source="task_type_change_invalidation",
                )

    def _normalize_transition_discovery_candidates(
        self,
        extraction: dict,
        target_task_type_key: str,
        base_state: dict,
        shared_keys: set[str],
    ) -> tuple[dict, set[str]]:
        """Normalize safe first-pass siblings before building target catalogs.

        Touched keys are returned separately so an invalid new value can evict
        a stale inherited fact rather than leaving it to constrain the target
        robot domain.
        """
        raw_updates: dict[str, object] = {}
        for candidate in extraction.get("slot_candidates", []):
            if not isinstance(candidate, dict):
                continue
            key = str(candidate.get("canonical_key") or "")
            if key in shared_keys:
                raw_updates[key] = candidate.get("normalized_value")
        if not raw_updates:
            return {}, set()

        normalized = self.normalizer.normalize_updates_with_failures(
            raw_updates,
            self.builder.get_schema(target_task_type_key, self.mode),
            base_state,
            lambda field_def, state: self.builder._resolve_allowed(
                field_def,
                target_task_type_key,
                state,
            ),
        )
        return normalized.normalized_updates, set(raw_updates)

    @staticmethod
    def _merge_task_transition_extractions(
        initial: dict,
        target: dict,
        shared_keys: set[str] | None = None,
    ) -> dict:
        """Merge a selector-discovery pass with a target-schema pass.

        Target-schema ordinary fields and list mutations are authoritative.
        Task selectors from both passes are retained so the shared preflight
        can reject category or concrete-operation disagreement instead of
        silently choosing whichever extraction happened last.
        """
        initial = initial if isinstance(initial, dict) else {}
        target = target if isinstance(target, dict) else {}
        selector_keys = {"task_type", "task_type_key"}
        initial_candidates = [
            copy.deepcopy(candidate)
            for candidate in initial.get("slot_candidates", [])
            if isinstance(candidate, dict)
        ]
        target_candidates = [
            copy.deepcopy(candidate)
            for candidate in target.get("slot_candidates", [])
            if isinstance(candidate, dict)
        ]
        initial_selectors = [
            candidate
            for candidate in initial_candidates
            if candidate.get("canonical_key") in selector_keys
        ]
        target_selectors = [
            candidate
            for candidate in target_candidates
            if candidate.get("canonical_key") in selector_keys
        ]
        target_ordinary = [
            candidate
            for candidate in target_candidates
            if candidate.get("canonical_key") not in selector_keys
        ]
        target_ordinary_keys = {
            str(candidate.get("canonical_key"))
            for candidate in target_ordinary
        }
        candidates = [
            candidate
            for candidate in initial_candidates
            if candidate.get("canonical_key") in (shared_keys or set())
            and str(candidate.get("canonical_key")) not in target_ordinary_keys
        ]
        candidates.extend(target_ordinary)
        for selector_key in ("task_type", "task_type_key"):
            first = [
                candidate
                for candidate in initial_selectors
                if candidate.get("canonical_key") == selector_key
            ]
            second = [
                candidate
                for candidate in target_selectors
                if candidate.get("canonical_key") == selector_key
            ]
            def unique_values(items: list[dict]) -> list[object]:
                values: list[object] = []
                for item in items:
                    value = item.get("normalized_value")
                    if not any(value == existing for existing in values):
                        values.append(value)
                return values

            first_values = unique_values(first)
            second_values = unique_values(second)
            if first and second and first_values != second_values:
                candidates.extend([*first, *second])
            else:
                candidates.extend(second or first)
        return {
            "slot_candidates": candidates,
            "list_mutations": copy.deepcopy(
                target.get("list_mutations", [])
                if isinstance(target.get("list_mutations", []), list)
                else []
            ),
            "unresolved": copy.deepcopy(
                target.get("unresolved", [])
                if isinstance(target.get("unresolved", []), list)
                else []
            ),
        }

    @staticmethod
    def _task_selector_updates_from_extraction(
        extraction: dict,
    ) -> tuple[dict[str, object], str | None]:
        """Return task selectors, rejecting duplicate values in one result."""
        updates: dict[str, object] = {}
        for candidate in extraction.get("slot_candidates", []):
            if not isinstance(candidate, dict):
                continue
            key = candidate.get("canonical_key")
            if key not in {"task_type", "task_type_key"}:
                continue
            value = candidate.get("normalized_value")
            if not isinstance(value, str) or not value.strip():
                return {}, "任务类型字段必须是非空字符串，请重新指定任务类型。"
            value = value.strip()
            if key in updates and updates[key] != value:
                return {}, "同轮具体任务类型互相冲突，请只指定一种任务操作。"
            updates[str(key)] = value
        return updates, None

    def _resolve_task_type_update_context(
        self,
        updates: dict,
        new_slots: dict,
    ) -> tuple[str | None, str | None, bool, str | None]:
        """Preflight task selectors and choose the schema for sibling updates.

        A reserved task ID locks the category, but does not turn ordinary
        sibling updates into an all-or-nothing operation. In that case sibling
        values are evaluated against the current task schema. Conflicting task
        selectors are different: there is no authoritative target schema, so
        the turn must stop before any field is mutated.
        """
        task_type_slot = new_slots.get("task_type_key")
        current_task_type_key = (
            task_type_slot.value
            if task_type_slot
            and task_type_slot.status == "valid"
            and task_type_slot.value is not None
            else None
        )
        resolved_targets = {
            target
            for key in ("task_type", "task_type_key")
            if key in updates
            for target in [self._resolve_task_type_target(key, updates[key])]
            if target is not None
        }
        if len(resolved_targets) > 1:
            logger.warning(
                "[DialogueManager] Rejecting conflicting task type targets: %s",
                sorted(resolved_targets),
            )
            return (
                None,
                current_task_type_key,
                False,
                "同轮任务类型信息互相冲突，请只指定一个任务类型。",
            )

        task_type_map = self.kb.get_task_type_map()
        concrete_task_values = {
            value
            for key in ("task_type", "task_type_key")
            if key in updates
            for value in [updates[key]]
            if isinstance(value, str) and value in task_type_map
        }
        if len(concrete_task_values) > 1:
            logger.warning(
                "[DialogueManager] Rejecting conflicting concrete task values: %s",
                sorted(concrete_task_values),
            )
            return (
                None,
                current_task_type_key,
                False,
                "同轮具体任务类型互相冲突，请只指定一种任务操作。",
            )

        pending_task_type_key = (
            next(iter(resolved_targets)) if resolved_targets else None
        )
        existing_task_id = new_slots.get("task_id")
        task_type_change_locked = bool(
            pending_task_type_key
            and current_task_type_key
            and pending_task_type_key != current_task_type_key
            and existing_task_id
            and existing_task_id.status == "valid"
            and existing_task_id.value
        )
        effective_task_type_key = (
            current_task_type_key
            if task_type_change_locked
            else (pending_task_type_key or current_task_type_key)
        )
        return (
            pending_task_type_key,
            effective_task_type_key,
            task_type_change_locked,
            None,
        )

    @staticmethod
    def _record_task_type_update_error(new_slots: dict, message: str) -> None:
        slot = new_slots.get("task_type_key")
        if slot is None:
            slot = Slot("task_type_key")
            new_slots["task_type_key"] = slot
        slot.validation_error = message

    def _handle_task_type_update_in_transaction(self, key: str, value: str, new_slots: dict):
        task_type_map = self.kb.get_task_type_map()
        templates = self.kb.task_schemas.get("task_templates", {})
        target_key = self._resolve_task_type_target(key, value)

        existing_task_id = new_slots.get("task_id")
        old_task_type_slot = new_slots.get("task_type_key")
        old_task_type_key = (
            old_task_type_slot.value
            if old_task_type_slot
            and old_task_type_slot.status == "valid"
            and old_task_type_slot.value is not None
            else None
        )

        # 如果已有 valid 的 task_id，禁止原地跨类别修改任务类型 (Lock task category)
        if existing_task_id and existing_task_id.status == "valid" and existing_task_id.value:
            if target_key and old_task_type_key and target_key != old_task_type_key:
                err_msg = f"任务编号已锁定 ({existing_task_id.value})，无法直接修改任务类别。如需更换类别，请先取消或新建任务。"
                logger.warning(
                    "[DialogueManager] Rejecting task category modification from %s to %s because task_id %s is already locked.",
                    old_task_type_key,
                    target_key,
                    existing_task_id.value,
                )
                if "task_type_key" in new_slots:
                    new_slots["task_type_key"].validation_error = err_msg
                return

        if target_key:
            if value in task_type_map:
                new_slots["task_type"].value = value
                new_slots["task_type"].status = "valid"
                new_slots["task_type_key"].value = target_key
                new_slots["task_type_key"].status = "valid"
            elif key == "task_type_key" and value in templates:
                new_slots["task_type_key"].value = value
                new_slots["task_type_key"].status = "valid"
                values = templates[value].get("task_type_values", [])
                if len(values) == 1:
                    new_slots["task_type"].value = values[0]
                    new_slots["task_type"].status = "valid"

        if target_key:
            required_fields = self.builder.get_schema(target_key, self.mode)
            schema_keys = {f["key"] for f in required_fields}

            # A candidate/conflict/invalid/unresolved robot selector belongs
            # to the task context in which it was produced.  When the task
            # type actually changes, it must not block the new task's
            # admission-domain collapse.  Keep valid committed selectors for
            # the normal cross-task admission check; clear only the first
            # non-effective selector and its dependent suffix.
            if target_key != old_task_type_key:
                from .slot_store import (
                    invalidate_robot_cascade_dependents,
                    reset_slot_to_missing,
                )

                for selector_key in (
                    "equipment_class",
                    "equipment_family",
                    "equipment_type",
                    "equipment_unit_id",
                ):
                    selector_slot = new_slots.get(selector_key)
                    if not selector_slot or selector_slot.status not in {
                        "candidate",
                        "conflict",
                        "invalid",
                        "unresolved",
                    }:
                        continue
                    invalidate_robot_cascade_dependents(
                        new_slots,
                        [selector_key],
                    )
                    reset_slot_to_missing(
                        selector_slot,
                        source="task_type_change_invalidation",
                    )
                    if selector_key == "equipment_unit_id":
                        equipment_name_slot = new_slots.get("equipment_name")
                        if equipment_name_slot is not None:
                            reset_slot_to_missing(
                                equipment_name_slot,
                                source="task_type_change_invalidation",
                            )
                    break

            # Clean up old dynamic slots in new_slots that do not belong to BASE_SLOT_TYPES, schema_keys, or ALLOWED_INTERNAL_SLOTS
            from .slot_store import BASE_SLOT_TYPES, ALLOWED_INTERNAL_SLOTS
            to_remove = [
                k for k in list(new_slots.keys())
                if k not in BASE_SLOT_TYPES and k not in schema_keys and k not in ALLOWED_INTERNAL_SLOTS
            ]
            for k in to_remove:
                del new_slots[k]

            for f in required_fields:
                fkey = f["key"]
                ftype = f.get("type", "string")
                if fkey not in new_slots:
                    new_slots[fkey] = Slot(slot_name=fkey, value_type=ftype)
                else:
                    new_slots[fkey].value_type = ftype

            self._auto_collapse_robot_cascade(new_slots, allow_overwrite=True)

    def _handle_rov_description_in_transaction(self, description: str, new_slots: dict):
        all_rovs = self.kb.get_all_rovs()
        task_type_slot = new_slots.get("task_type_key")
        task_type_key = (
            task_type_slot.value
            if task_type_slot
            and task_type_slot.status == "valid"
            and task_type_slot.value is not None
            else None
        )
        candidates = self.extractor.resolve_rov_description(
            description, all_rovs, task_type_key
        )
        self._pending_rov_candidates = candidates
        if candidates:
            new_slots["_rov_candidates"].value = [
                {"model": r["model"], "full_name": r["full_name"],
                 "category": r["category"], "available": True}
                for r in candidates[:3]
            ]
            new_slots["_rov_candidates"].status = "valid"

    def _normalize_and_validate_in_transaction(
        self,
        new_slots: dict,
        task_type_key: str | None,
        skip_schema_keys: set[str] | frozenset[str] | None = None,
    ):
        if not task_type_key:
            return

        schema = self.builder.get_schema(task_type_key, self.mode)

        for field_def in schema:
            key = field_def["key"]
            if skip_schema_keys and key in skip_schema_keys:
                continue
            ftype = field_def["type"]
            slot = new_slots.get(key)
            if not slot or slot.status in ("conflict", "invalid") or key.startswith("equipment_"):
                continue

            target_val = slot.candidate_value if slot.candidate_value is not None else slot.value
            if target_val is None or (isinstance(target_val, list) and len(target_val) == 0):
                if isinstance(target_val, list) and len(target_val) == 0 and slot.status != "conflict":
                    slot.status = "missing"
                continue

            temp_state = {
                state_key: (
                    state_slot.candidate_value
                    if state_slot.candidate_value is not None
                    else state_slot.value
                )
                for state_key, state_slot in new_slots.items()
                if (
                    state_slot.status not in ("invalid", "missing")
                    and (
                        state_slot.value is not None
                        or state_slot.candidate_value is not None
                    )
                    # Robot hierarchy fields are authoritative only after the
                    # dedicated equipment handler marks them valid.  A
                    # restored candidate/conflict must not influence dynamic
                    # payload or other schema candidate normalization.
                    and (
                        not state_key.startswith("equipment_")
                        or state_slot.status == "valid"
                    )
                )
            }

            allowed = self.builder._resolve_allowed(field_def, task_type_key, temp_state)
            if allowed:
                raw = target_val
                if ftype == "list":
                    normalized = self.normalizer.normalize(raw, allowed, ftype)
                else:
                    normalized = self.normalizer.normalize(str(raw), allowed, ftype)

                if normalized is not None:
                    slot.value = normalized
                    slot.candidate_value = None
                    slot.status = "valid"
                    if key != "payload" or slot.validation_error is None:
                        slot.validation_error = None
                else:
                    slot.status = "invalid"
                    slot.candidate_value = raw
                    slot.validation_error = f"Value '{raw}' could not be normalized to allowed options: {allowed}"
            else:
                if ftype == "datetime":
                    val_str = str(target_val)
                    pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$"
                    if re.match(pattern, val_str):
                        slot.value = val_str
                        slot.candidate_value = None
                        slot.status = "valid"
                        slot.validation_error = None
                    else:
                        slot.status = "invalid"
                        slot.candidate_value = target_val
                        slot.validation_error = f"Invalid datetime format: {val_str}. Expected YYYY-MM-DDTHH:MM:SS"
                elif ftype == "coord":
                    coord = self.builder._validate_coord(target_val)
                    if coord:
                        slot.value = coord
                        slot.candidate_value = None
                        slot.status = "valid"
                        slot.validation_error = None
                    else:
                        slot.status = "invalid"
                        slot.candidate_value = target_val
                        slot.validation_error = f"Invalid coordinate format: {target_val}"
                elif ftype == "number":
                    num = self.builder._validate_number(target_val)
                    if num is not None:
                        slot.value = num
                        slot.candidate_value = None
                        slot.status = "valid"
                        slot.validation_error = None
                    else:
                        slot.status = "invalid"
                        slot.candidate_value = target_val
                        slot.validation_error = f"Invalid number: {target_val}"
                else:
                    slot.value = target_val
                    slot.candidate_value = None
                    slot.status = "valid"
                    slot.validation_error = None

        # Non-equipment facts become authoritative only after normalization.
        # Equipment updates are validated and promoted inside the dedicated
        # four-level handler.  Never scan/promote arbitrary equipment
        # candidates here: a restored candidate is pending user confirmation,
        # not a value submitted by this transaction.
        # Recompute the robot domain now so water_depth/start_time/payload from
        # this same transaction can participate in candidate convergence.
        self._auto_collapse_robot_cascade(new_slots, allow_overwrite=True)


        # 字段自身的格式/候选合法性与任务组合约束是两类状态：
        # 例如“水深 600m”和“最大水深 500m 的设备”均可被正确录入，
        # 但二者组合会触发硬约束。硬约束由对话阶段 blocked_hard 管理，
        # 不能把已合法录入的关联字段重新标记为 invalid，否则前端会误报缺失。

    def _resolve_pending_oilfield_confirmation(
        self,
        user_message: str,
        request_id: str = "req_default",
        pending_action: str | None = None,
        subject_text: str | None = None,
    ) -> str | None:
        pending_slot = self.slot_store.slots.get("pending_oilfield_name")
        if not pending_slot or not pending_slot.value or pending_slot.status != "valid":
            return None
        if pending_action == "reject" or (
            pending_action is None and self._user_cancelled_oilfield(user_message)
        ):
            oil_slot = self.slot_store.slots.get("oilfield_name")
            clear_oil = ("oilfield_name", "oilfield_entity_id") if (not oil_slot or oil_slot.status != "valid") else ()
            self._commit_internal_slot_values(
                {},
                clear_keys=(
                    "pending_oilfield_name",
                    "pending_oilfield_candidates",
                    *clear_oil,
                ),
            )
            self._rebuild_cache()
            return "已取消当前待确认油田名称，请提供标准的油田名称（例如：流花11-1油田、陵水17-2油田等），或补充油田坐标。"


        if pending_action not in {"confirm", None}:
            return None
        if pending_action is None and not self._user_confirmed_oilfield(user_message):
            return None

        candidate = self._top_pending_oilfield_candidate(subject_text or user_message)
        if not candidate:
            return self._build_pending_oilfield_reply()

        confirmed_name = candidate.get("name")
        self._commit_internal_slot_values(
            {
                "oilfield_name": confirmed_name,
                "raw_oilfield_name": confirmed_name,
                "oilfield_entity_id": candidate.get("id"),
                "oilfield_match_status": "accepted",
                "oilfield_match_confidence": candidate.get("confidence"),
                "oilfield_match_evidence": candidate.get("evidence", []),
            },
            clear_keys=(
                "pending_oilfield_name",
                "pending_oilfield_candidates",
            ),
        )
        self._rebuild_cache()
        return f"已确认油田名称为“{confirmed_name}”，我会按这个标准名称继续收集任务信息。"

    def _build_pending_oilfield_reply(self) -> str | None:
        task_type_slot = self.slot_store.slots.get("task_type_key")
        task_type_key = task_type_slot.value if task_type_slot and task_type_slot.status == "valid" else None
        if task_type_key:
            field_defs = self.builder.get_schema(task_type_key, self.mode)
            schema_keys = {str(field.get("key")) for field in field_defs if field.get("key")}
            if not self.slot_filter.supports_oilfield_slots(schema_keys):
                return None

        pending_slot = self.slot_store.slots.get("pending_oilfield_name")
        raw_name = pending_slot.value if (pending_slot and pending_slot.status == "valid") else None
        oil_slot = self.slot_store.slots.get("oilfield_name")
        has_oilfield = oil_slot.value if (oil_slot and oil_slot.status == "valid") else None
        if not raw_name or has_oilfield:
            return None

        candidate = self._top_pending_oilfield_candidate()
        if candidate:
            name = candidate.get("name")
            return f"我识别到油田名称“{raw_name}”，疑似为“{name}”。请确认是否采用该标准油田名称？"
        return (
            f"我识别到油田名称“{raw_name}”，但没有匹配到标准油田。"
            "请提供标准的油田名称（例如：流花11-1油田、陵水17-2油田等），或补充油田坐标。"
        )

    def _top_pending_oilfield_candidate(self, user_message: str = "") -> dict | None:
        cand_slot = self.slot_store.slots.get("pending_oilfield_candidates")
        candidates = cand_slot.value if (cand_slot and cand_slot.status == "valid") else None
        if isinstance(candidates, list) and candidates:
            if user_message:
                for c in candidates:
                    if isinstance(c, dict) and c.get("name") and c.get("name") in user_message:
                        return c
            candidate = candidates[0]
            if isinstance(candidate, dict) and candidate.get("name"):
                return candidate
        return None

    def _user_confirmed_oilfield(self, message: str) -> bool:
        keywords = ["是", "对", "就是", "采用", "确认", "确定", "可以", "好的", "ok", "使用"]
        negations = ["不", "别", "不要", "不是", "取消"]
        msg = message.strip().lower()
        if any(neg in msg for neg in negations):
            return False
        return any(kw in msg for kw in keywords)

    def _user_cancelled_oilfield(self, message: str) -> bool:
        msg = message.strip()
        if any(neg in msg for neg in ["不是要取消", "不是取消", "不要取消", "不取消", "别取消", "不要修改", "不修改"]):
            return False
        if any(mod_kw in msg for mod_kw in ["改成", "修改", "水深", "支持船", "管缆", "设备", "载荷"]) and "油田" not in msg:
            return False
        if "任务" in msg or "取消任务" in msg:
            return False

        pending_slot = self.slot_store.slots.get("pending_oilfield_name")
        pending_name = pending_slot.value if (pending_slot and pending_slot.status == "valid") else None
        if pending_name:
            import re
            mentioned_oilfields = re.findall(r"[\u4e00-\u9fa50-9\-]+油田", msg)
            if mentioned_oilfields:
                p_norm = pending_name.replace("油田", "").strip()
                matched_any = any(p_norm in m or m.replace("油田", "").strip() in p_norm for m in mentioned_oilfields)
                if not matched_any:
                    return False

        keywords = ["不是", "不对", "否", "错了", "重新", "取消油田", "不要此油田", "这个油田不对", "不要"]
        return any(kw in msg for kw in keywords) or msg in ("不要", "取消", "不对", "不是")



    # --------------------------------------------------------------------------
    # 约束检查（硬解除后检查软）
    # --------------------------------------------------------------------------

    def _merge_oilfield_context_violations(self, new_violations: list[Violation]) -> list[Violation]:
        task_type_key = self.task_state.get("task_type_key")
        if task_type_key:
            schema_keys = {
                str(field.get("key"))
                for field in self.builder.get_schema(task_type_key, self.mode)
                if isinstance(field, dict) and field.get("key")
            }
            # Oilfield linker metadata is task-scoped.  Even if an old/legacy
            # snapshot leaked an entity id, it must not inject C028/C029 into a
            # known task whose schema has no oilfield contract.  A missing task
            # key is retained as a fail-closed compatibility path for direct
            # validation of pre-schema/legacy state.
            if "oilfield_name" not in schema_keys:
                return new_violations

        entity_id = self.task_state.get("oilfield_entity_id")
        if not entity_id:
            return new_violations

        coords = self.task_state.get("oilfield_coordinates") or self.task_state.get("start_point")
        water_depth = self.task_state.get("water_depth")

        try:
            ctx_res = self.oilfield_linker.evaluate_context(
                entity_id=entity_id,
                coordinates=coords if coords is not None else _UNSET,
                water_depth=water_depth if water_depth is not None else _UNSET,
            )
            if ctx_res and ctx_res.issues:
                merged = list(new_violations)
                existing_ids = {v.constraint_id for v in merged}
                applicable_ids = None
                if task_type_key:
                    applicable_ids = {
                        str(item.get("id"))
                        for item in self.kb.get_constraints()
                        if isinstance(item, dict)
                        and (
                            task_type_key in (item.get("applies_to") or [])
                            or "all" in (item.get("applies_to") or [])
                        )
                    }
                for issue in ctx_res.issues:
                    if (
                        (
                            applicable_ids is None
                            or issue.constraint_id in applicable_ids
                        )
                        and issue.constraint_id not in existing_ids
                    ):
                        merged.append(
                            Violation(
                                constraint_id=issue.constraint_id,
                                constraint_name=issue.constraint_name,
                                check_type=issue.check_type,
                                severity=issue.severity,
                                message=issue.message,
                                related_fields=list(issue.related_fields),
                            )
                        )
                return merged
        except Exception as exc:
            merged = list(new_violations)
            merged.append(
                Violation(
                    constraint_id="C029",
                    constraint_name="油田上下文计算异常",
                    check_type="oilfield_context_failure",
                    severity="hard",
                    message=f"油田上下文计算失败，安全熔断: {exc}",
                    related_fields=["oilfield_name"],
                )
            )
            return merged

        return new_violations

    def _is_state_snapshot_stale(self) -> bool:
        """检查当前 validation_result 中绑定的 state_snapshot 是否已过时或与 state.yaml 不一致。"""
        val_res = getattr(self.slot_store, "validation_result", None)
        if not val_res:
            return True
        state_snap = getattr(val_res, "state_snapshot", None)
        if not state_snap or not isinstance(state_snap, dict):
            return False
        unit_id = state_snap.get("unit_id") or self.task_state.get("equipment_unit_id")
        if unit_id and isinstance(unit_id, str):
            try:
                curr_snap = self.kb.get_unit_state_snapshot(unit_id)
                if not curr_snap or not isinstance(curr_snap, dict):
                    return True
                if curr_snap.get("state_version") != state_snap.get("state_version"):
                    return True
                if curr_snap.get("updated_at") != state_snap.get("updated_at"):
                    return True
                if curr_snap.get("state") != state_snap.get("state"):
                    return True
            except Exception:
                return True
        else:
            try:
                if hasattr(self.kb, "state_info") and self.kb.state_info.get_store_version() != state_snap.get("store_version", 0):
                    return True
            except Exception:
                return True
        return False

    def _run_constraint_check(self, changed_fields: set[str], purpose: str = "interactive") -> dict:
        """执行约束检查，返回上下文"""
        if not changed_fields and self.phase not in ("blocked_hard", "blocked_soft") and not self._is_state_snapshot_stale():
            state_snap = getattr(self.slot_store.validation_result, "state_snapshot", None)
            return {"type": "none", "violations": [], "hard_refusal_counts": {}, "state_snapshot": state_snap}

        val_res = self._refresh_validation(purpose=purpose, changed_fields=changed_fields)
        state_snap = val_res.state_snapshot
        new_violations = self._merge_oilfield_context_violations(val_res.violations)

        current_hard = [
            v for v in new_violations
            if v.severity == "hard" and (purpose != "interactive" or v.constraint_id not in ("CLASS_NOT_ALLOWED_FOR_TASK", "FAMILY_CLASS_MISMATCH"))
        ]
        current_soft = [
            v for v in new_violations
            if v.severity == "soft" and not self._is_whitelisted(v)
        ]
        current_blockers = current_hard + current_soft

        # 处理 soft 阻塞升级为 hard / 维持 / 解除
        if self.phase == "blocked_soft":
            if current_hard:
                self._transition_phase("blocked_hard", reason="soft_upgraded_to_hard")
                self._blocking_violations = current_blockers
                for v in current_hard:
                    if v.constraint_id not in self._hard_refusal_counts:
                        self._hard_refusal_counts[v.constraint_id] = 0

                return {
                    "type": "hard",
                    "violations": current_hard,
                    "hard_refusal_counts": dict(self._hard_refusal_counts),
                    "state_snapshot": state_snap,
                }

            if current_soft:
                self._blocking_violations = current_soft
                return {
                    "type": "soft",
                    "violations": current_soft,
                    "hard_refusal_counts": {},
                    "state_snapshot": state_snap,
                }

            self._blocking_violations = []
            self._transition_phase("collecting", reason="soft_warning_resolved")
            return {
                "type": "none",
                "violations": [],
                "hard_refusal_counts": {},
                "state_snapshot": state_snap,
            }

        # 处理 hard 阻塞维持 / 降级为 soft / 解除
        if self.phase == "blocked_hard":
            if current_hard:
                self._blocking_violations = current_blockers
                for v in current_hard:
                    self._hard_refusal_counts[v.constraint_id] = \
                        self._hard_refusal_counts.get(v.constraint_id, 0) + 1

                final_ids = {
                    cid for cid, cnt in self._hard_refusal_counts.items()
                    if cnt >= HARD_REFUSAL_LIMIT
                }
                if final_ids:
                    self._transition_phase("rejected", reason="hard_refusal_limit_reached")
                    self._blocking_violations = []
                    return {
                        "type": "hard_rejected",
                        "violations": current_hard,
                        "hard_refusal_counts": dict(self._hard_refusal_counts),
                        "state_snapshot": state_snap,
                    }

                warn_ids = {
                    cid for cid, cnt in self._hard_refusal_counts.items()
                    if cnt == HARD_REFUSAL_LIMIT - 1
                }
                ctx_type = "hard_final_warning" if warn_ids else "hard"
                return {
                    "type": ctx_type,
                    "violations": current_hard,
                    "hard_refusal_counts": dict(self._hard_refusal_counts),
                    "state_snapshot": state_snap,
                }
            else:
                # 硬约束解除，清除计数
                resolved_ids = set(self._hard_refusal_counts.keys())
                for cid in resolved_ids:
                    self._hard_refusal_counts.pop(cid, None)

                if current_soft and purpose in ("preview", "publish"):
                    self._transition_phase("blocked_soft", reason="hard_downgraded_to_soft")
                    self._blocking_violations = current_soft
                    return {
                        "type": "soft",
                        "violations": current_soft,
                        "hard_refusal_counts": {},
                        "state_snapshot": state_snap,
                    }

                self._transition_phase("collecting", reason="hard_constraint_resolved")
                self._blocking_violations = []
                return {
                    "type": "none",
                    "violations": [],
                    "hard_refusal_counts": {},
                    "state_snapshot": state_snap,
                }

        # collecting / confirming 状态下的新违规
        if self.phase in ("collecting", "confirming"):
            if current_hard:
                self._transition_phase("blocked_hard", reason="hard_constraint_detected")
                self._blocking_violations = current_blockers
                for v in current_hard:
                    if v.constraint_id not in self._hard_refusal_counts:
                        self._hard_refusal_counts[v.constraint_id] = 0
                return {
                    "type": "hard",
                    "violations": current_hard,
                    "hard_refusal_counts": dict(self._hard_refusal_counts),
                    "state_snapshot": state_snap,
                }

            if current_soft:
                # 统一规则：所有的软警告都在任务字段收集完毕进行统一检查（purpose in ("preview", "publish") 或 confirming 阶段）。
                # 在字段收集阶段（collecting 且 purpose == "interactive"），软警告不中断槽位收集，只有硬约束可以在收集过程中即时触发阻断。
                if self.phase != "collecting" or purpose in ("preview", "publish"):
                    self._transition_phase("blocked_soft", reason="soft_warning_detected")
                    self._blocking_violations = current_soft
                    return {
                        "type": "soft",
                        "violations": current_soft,
                        "hard_refusal_counts": {},
                        "kb_alternatives": self._get_kb_alternatives_for_violations(current_soft),
                        "state_snapshot": state_snap,
                    }

        res = {"type": "none", "violations": [], "hard_refusal_counts": {}, "state_snapshot": state_snap}
        if current_blockers:
            res["kb_alternatives"] = self._get_kb_alternatives_for_violations(current_blockers)
        return res

    def _get_kb_alternatives_for_violations(self, violations: list) -> list[dict]:
        """从 KnowledgeBase 中检索真实的合规替代设备，严禁凭空编造非 KB 型号。"""
        task_type_key = self.task_state.get("task_type_key")
        water_depth = self.task_state.get("water_depth")
        if not task_type_key:
            return []

        try:
            wd = float(water_depth) if water_depth is not None else None
        except (ValueError, TypeError):
            wd = None

        valid_robots = self.kb.get_task_allowed_robot_variants(task_type_key)
        if wd is not None:
            valid_robots = [
                r for r in valid_robots
                if r.get("max_depth_m") is not None and float(r.get("max_depth_m")) >= wd
            ]

        curr_eq = self.task_state.get("equipment_type")
        alts = []
        for r in valid_robots:
            name = r.get("full_name") or r.get("name")
            if name and name != curr_eq:
                alts.append({
                    "name": name,
                    "max_depth_m": r.get("max_depth_m"),
                    "capabilities": r.get("capabilities") or [],
                })
        return alts[:3]

    # --------------------------------------------------------------------------
    # 工具方法
    # --------------------------------------------------------------------------

    def _merge_coordinate_updates(
        self,
        user_message: str,
        updates: dict,
        required: list[dict] | None,
    ) -> dict:
        coord_fields = {
            item["key"]
            for item in (required or [])
            if item.get("type") == "coord" and item.get("key")
        }
        coord_updates = parse_coordinate_updates(
            user_message,
            coord_fields,
            current_state=self.task_state,
            proposed_updates=updates,
        )
        if not coord_updates:
            return updates
        merged = dict(updates)
        merged.update(coord_updates)
        return merged

    def _invalidate_whitelist(self, changed_fields: set[str]):
        if changed_fields:
            self._soft_whitelist -= {e for e in self._soft_whitelist if e[0] in changed_fields}

    def _is_whitelisted(self, v: Violation) -> bool:
        res = getattr(self.slot_store, "validation_result", None)
        if res is None:
            return False

        curr_fp = getattr(res, "validation_fingerprint", None)
        state_snap = getattr(res, "state_snapshot", None)
        # 非遥测类约束（例如时间、区域风险）在尚未选择机器人时没有
        # state_snapshot。ValidationAcknowledgement 与 UI 契约均使用 ("", 0)
        # 表示该合法空状态；白名单校验必须采用同一标准值，否则确认会被永久判旧。
        curr_state_ver = (
            state_snap.get("state_version")
            if isinstance(state_snap, dict)
            else 0
        )
        curr_status_ref = (
            state_snap.get("status_ref")
            if isinstance(state_snap, dict)
            else ""
        )
        curr_state_ver = 0 if curr_state_ver is None else curr_state_ver
        curr_status_ref = curr_status_ref or ""

        if not curr_fp:
            return False

        acks = getattr(self.slot_store, "validation_acknowledgements", [])
        for ack in acks:
            ack_cid = getattr(ack, "constraint_id", None) if not isinstance(ack, dict) else ack.get("constraint_id")
            if ack_cid != v.constraint_id:
                continue

            ack_tv = getattr(ack, "task_version", None) if not isinstance(ack, dict) else ack.get("task_version")
            ack_vv = getattr(ack, "validation_version", None) if not isinstance(ack, dict) else ack.get("validation_version")
            ack_fp = getattr(ack, "validation_fingerprint", None) if not isinstance(ack, dict) else ack.get("validation_fingerprint")
            ack_sref = getattr(ack, "status_ref", None) if not isinstance(ack, dict) else ack.get("status_ref")
            ack_sver = getattr(ack, "state_version", None) if not isinstance(ack, dict) else ack.get("state_version")

            ack_val = getattr(ack, "value", None) if not isinstance(ack, dict) else ack.get("value")

            if (
                ack_tv == getattr(res, "task_version", 1)
                and ack_vv == getattr(res, "validation_version", 1)
                and ack_fp == curr_fp
                and ack_sref == curr_status_ref
                and ack_sver == curr_state_ver
            ):
                return True

            # 对于 check_type == 'state_timestamp' (如 C019)，只要环境观察值未改变，在补充槽位过程中保持白名单有效
            if (
                getattr(v, "check_type", None) == "state_timestamp"
                and ack_val is not None
                and ack_val == getattr(v, "observed_value", None)
            ):
                return True

            # 若单机状态版本与引用未变，且针对该约束的观察值一致，白名单保持有效
            if (
                ack_tv <= getattr(res, "task_version", 1)
                and ack_sref == curr_status_ref
                and ack_sver == curr_state_ver
                and (
                    ack_val is None
                    or ack_val == getattr(v, "observed_value", None)
                )
            ):
                return True

            # 兼容基于字段与值的 _soft_whitelist 检查
            for f in getattr(v, "related_fields", []):
                val = self.task_state.get(f)
                if val is not None and (f, str(val), v.constraint_id) in self._soft_whitelist:
                    return True

        # 若 _soft_whitelist 中包含相关字段且字段值未变，同样放行
        for f in getattr(v, "related_fields", []):
            val = self.task_state.get(f)
            if val is not None and (f, str(val), v.constraint_id) in self._soft_whitelist:
                return True

        return False

    @staticmethod
    def _is_business_identity_query(message: str) -> bool:
        text = message.strip().lower()
        identity_patterns = (
            "你是什么", "你是谁", "你是啥", "你的身份", "你叫什么",
            "介绍一下你自己", "自我介绍", "你能做什么", "你有什么功能", "what are you", "who are you",
        )

        return any(pattern in text for pattern in identity_patterns)


    @staticmethod
    def _user_confirmed(message: str) -> bool:
        keywords = ["确认", "没问题", "发布", "提交", "ok", "好的", "可以", "确定"]
        return any(kw in message.lower() for kw in keywords)

    @staticmethod
    def _is_final_publish_confirmation(message: str) -> bool:
        """仅识别明确具有‘发布/提交当前任务’语义的独立指令。"""
        text = re.sub(r"[\s，,。.!！?？、；;：:]+", "", message).lower()
        return text in {
            "确认发布",
            "确认并发布",
            "确认发布任务",
            "发布任务",
            "发布",
            "立即发布",
            "现在发布",
            "确认提交",
            "确认并提交",
            "提交任务",
            "提交",
            "确认开始",
            "确认开始任务",
        }

    @staticmethod
    def _is_confirmation_only(message: str) -> bool:
        """仅识别不携带参数更新的独立泛确认/认可指令。"""
        text = re.sub(r"[\s，,。.!！?？、；;：:]+", "", message).lower()
        return text in {
            "确认",
            "确认无误",
            "确认开始",
            "开始",
            "开始任务",
            "确定",
            "没问题",
            "好的",
            "可以",
            "ok",
            "继续",
        }

    @staticmethod
    def _is_ignore_warning(message: str) -> bool:
        """仅识别明确具有忽略/无视软警告语义的独立控制指令。"""
        text = re.sub(r"[\s，,。.!！?？、；;：:]+", "", message).lower()
        negated = ["不忽略", "不要忽略", "不能忽略", "别忽略", "不无视", "不要无视", "不是忽略"]
        if any(neg in text for neg in negated):
            return False
        return text in {
            "忽略警告",
            "忽略软警告",
            "忽略",
            "无视警告",
            "无视软警告",
            "忽略此警告",
            "忽略当前警告",
            "无视此警告",
            "无视当前警告",
            "接受风险",
            "忽略风险",
            "无视",
        }



    def _ensure_constraint_details(self, reply: str, constraint_context: dict) -> str:
        """Append canonical hard-blocking details omitted or paraphrased by the LLM."""
        context_type = str((constraint_context or {}).get("type") or "")
        if not context_type.startswith("hard"):
            return reply

        violations = [
            violation
            for violation in ((constraint_context or {}).get("violations") or [])
            if getattr(violation, "severity", "") == "hard"
        ]
        import re

        reply_str = str(reply)

        def _normalize(text: object) -> str:
            if not isinstance(text, str):
                text = str(text)
            return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)

        norm_reply = _normalize(reply_str)

        missing = []
        for violation in violations:
            msg = getattr(violation, "message", "") or ""
            cid = getattr(violation, "id", "") or getattr(violation, "constraint_id", "") or ""
            name = getattr(violation, "name", "") or getattr(violation, "constraint_name", "") or ""

            # 1. 严格包含
            if msg and str(msg) in reply_str:
                continue
            # 2. 规范化包含（忽略空格/标点差异）
            if msg and _normalize(msg) in norm_reply:
                continue
            # 3. 如果 reply 已经包含了约束 ID 或约束名称
            if cid and str(cid) in reply_str:
                continue
            if name and (_normalize(name) in norm_reply or str(name) in reply_str):
                continue
            missing.append(violation)

        if not missing:
            return reply

        details = self.validator.format_violations(missing)
        if isinstance(reply, str):
            return f"{reply.rstrip()}\n\n{details}" if reply.strip() else details
        return f"{reply}\n\n{details}"

    def _reject_hard_constraint_bypass(self, user_message: str) -> str:
        """Reject confirmation/ignore commands while hard violations remain."""
        violations = [
            violation
            for violation in self._blocking_violations
            if violation.severity == "hard"
        ]
        if not violations:
            val_res = self._refresh_validation(purpose="interactive")
            violations = [
                v for v in val_res.violations
                if v.severity == "hard"
            ]

        reply = "硬性约束不能通过确认或忽略警告绕过。请先修正以下问题后再发布任务。"
        if violations:
            reply = f"{reply}\n\n{self.validator.format_violations(violations)}"
            self._blocking_violations = violations

        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    @staticmethod
    def _user_cancelled(message: str) -> bool:
        negated_cancel = ["不是要取消", "不是取消", "不要取消", "别取消", "不取消", "免取消"]
        if any(neg in message for neg in negated_cancel):
            return False

        if any(mod_kw in message for mod_kw in ["修改", "参数", "设置", "载荷", "水深", "支持船", "管缆", "油田", "设备", "工具"]) and "任务" not in message:
            return False
        keywords = ["取消任务", "放弃任务", "终止任务", "取消", "放弃", "不要了", "终止", "退出"]
        return any(kw in message for kw in keywords)


    @staticmethod
    def _user_requested_modification(message: str) -> bool:
        """判断用户是否明确要求覆盖已经录入的参数。"""
        keywords = (
            "修改",
            "改成",
            "改为",
            "改到",
            "更改",
            "更换",
            "换成",
            "换为",
            "调整",
            "重新设置",
            "设置为",
            "设为",
            "替换",
        )
        return any(keyword in message for keyword in keywords)

    def _normalize_payload_list_mutations(
        self,
        extraction_res: dict,
        user_message: str,
        current_slots: dict,
    ) -> None:
        """兜底防护：当 LLM 抽取的 extraction_res 将 payload 误放入 slot_candidates 时，
        基于用户增量/减量意图或现有槽位，自动转换为 list_mutations（op: add/remove），
        防止列表字段被整体覆盖。
        """
        mutations = extraction_res.get("list_mutations")
        if not isinstance(mutations, list):
            mutations = []
            extraction_res["list_mutations"] = mutations

        has_payload_mutation = any(
            isinstance(m, dict) and m.get("field") == "payload"
            for m in mutations
        )
        if has_payload_mutation:
            return

        candidates = extraction_res.get("slot_candidates")
        if not isinstance(candidates, list):
            return

        payload_cands = [
            c for c in candidates
            if isinstance(c, dict) and c.get("canonical_key") == "payload"
        ]
        if not payload_cands:
            return

        cand = payload_cands[0]
        val = cand.get("normalized_value")
        if val is None:
            val = cand.get("raw_value")
        items = val if isinstance(val, list) else ([val] if val is not None else [])
        if not items:
            return

        msg = str(user_message or "")
        add_kws = ("添加", "加装", "增加", "加上", "还要", "补充", "带上", "携带", "配置", "配合", "还要带", "加个")
        remove_kws = ("删除", "去掉", "移除", "不要", "取消", "别带")
        replace_kws = ("替换", "改成", "换成", "重置", "覆盖")

        is_add = any(kw in msg for kw in add_kws)
        is_remove = any(kw in msg for kw in remove_kws)
        is_replace = any(kw in msg for kw in replace_kws)

        payload_slot = current_slots.get("payload")
        has_existing_payload = bool(
            payload_slot
            and payload_slot.status == "valid"
            and isinstance(payload_slot.value, list)
            and len(payload_slot.value) > 0
        )

        if is_add or (has_existing_payload and not is_replace and not is_remove):
            extraction_res["slot_candidates"] = [
                c for c in candidates
                if isinstance(c, dict) and c.get("canonical_key") != "payload"
            ]
            mutations.append({
                "field": "payload",
                "operation": "add",
                "items": items,
                "target_items": [],
                "raw_text": msg,
                "confidence": cand.get("confidence", 0.95),
                "source": "user_input",
            })
        elif is_remove:
            extraction_res["slot_candidates"] = [
                c for c in candidates
                if isinstance(c, dict) and c.get("canonical_key") != "payload"
            ]
            mutations.append({
                "field": "payload",
                "operation": "remove",
                "items": items,
                "target_items": [],
                "raw_text": msg,
                "confidence": cand.get("confidence", 0.95),
                "source": "user_input",
            })

    # --------------------------------------------------------------------------
    # 状态查询与重置
    # --------------------------------------------------------------------------

    def get_status(self) -> dict:
        filled: dict = {}
        missing_display: list[dict] = []

        for k, v in self._last_built_json.items():
            if k.startswith("_"):
                continue
            label = FIELD_LABELS.get(k, k)
            filled[k] = {"label": label, "value": v}

        missing_source = self._last_missing
        task_type_key = self.task_state.get("task_type_key")
        if task_type_key:
            try:
                schema = self.builder.get_schema(task_type_key, self.mode)
                user_req_schema = [
                    field for field in schema
                    if field.get("type") not in ("auto", "fixed")
                ]
                missing_source = self.slot_store.get_missing_slots(
                    user_req_schema,
                    allowed_values_resolver=lambda field: self.builder.resolve_allowed_values(
                        field,
                        task_type_key,
                        self.task_state,
                    ),
                )
                self._last_missing = missing_source
            except Exception as exc:
                logger.warning(
                    "[DialogueManager] Failed to derive status missing fields from SlotStore: %s",
                    exc,
                )

        for m in missing_source:
            missing_display.append({
                "key": m["key"],
                "label": m["label"],
                "allowed_values": m.get("allowed_values", []),
            })

        return {
            "phase": self.phase,
            "workflow_phase": "validating" if self.phase in ("blocked_soft", "blocked_hard") else self.phase,
            "mode": self.mode,
            "dialogue_mode": self.dialogue_mode,
            "last_mode_transition": copy.deepcopy(self.last_mode_transition),
            "mode_transition_history": copy.deepcopy(self.mode_transition_history),
            "control_state": self.control_state,
            "last_control_request": copy.deepcopy(self.last_control_request),
            "filled": filled,
            "missing": missing_display,
            "whitelisted_soft": sorted({e[2] for e in self._soft_whitelist}),
        }

    def get_final_result(self) -> dict | None:
        return self.final_result

    def reset(self):
        with self._session_lock:
            self.conversation_history = []
            self.slot_store = SlotStore(self.kb)
            self.task_state = self.slot_store.get_task_state()
            self.mode = "normal"
            self.phase = "collecting"
            self.final_result = None
            self.awaiting_final_confirm = False
            self.task_start_now = False
            self._blocking_violations = []
            self._soft_whitelist = set()
            self._hard_refusal_counts = {}
            self._pending_rov_candidates = []
            self._last_built_json = {}
            self._last_missing = []
            self.control_state = "idle"
            self.last_control_request = None
            self.dialogue_mode = "task_collection"
            self.last_mode_transition = None
            self.mode_transition_history = []
            self._run_session_state_shadow_check(checkpoint="reset")

    def _commit_internal_slot_values(
        self,
        values: dict,
        clear_keys: tuple[str, ...] = (),
    ) -> None:
        """提交可信的内部派生值，保持 SlotStore 为唯一状态源。"""
        new_slots = self.slot_store.clone_slots()
        for key in clear_keys:
            slot = new_slots.get(key)
            if slot is None:
                continue
            slot.value = None
            slot.status = "missing"
            slot.candidate_value = None
            slot.raw_value = None
            slot.validation_error = None

        for key, value in values.items():
            if value is None:
                continue
            slot = new_slots.get(key)
            if slot is None:
                slot = Slot(slot_name=key)
                new_slots[key] = slot
            slot.value = value
            slot.status = "valid"
            slot.candidate_value = None
            slot.raw_value = None
            slot.validation_error = None

        self.slot_store.commit_transaction(
            new_slots,
            self.slot_store.unresolved,
        )
        self.task_state = self.slot_store.get_task_state()

    # --------------------------------------------------------------------------
    # 时间判断
    # --------------------------------------------------------------------------

    def is_start_time_near_now(self, time_window_minutes: int = 60) -> bool:
        return self.validator._is_task_start_now(self.task_state, time_window_minutes=time_window_minutes)

    # --------------------------------------------------------------------------
    # 缓存重建
    # --------------------------------------------------------------------------

    def _rebuild_cache(self, commit_derived: bool = True) -> None:
        """根据当前 slot_store 重新构建 task_state, _last_built_json 和 _last_missing"""
        self.task_state = self.slot_store.get_task_state()
        task_type_key = self.task_state.get("task_type_key")
        eq_type = self.task_state.get("equipment_type") or self.task_state.get("equipment_name")
        family_slot = self.slot_store.slots.get("equipment_family")
        family_is_materializable = family_slot is None or (
            family_slot.status == "missing" and family_slot.value is None
        )
        if (
            commit_derived
            and eq_type
            and not self.task_state.get("equipment_family")
            and family_is_materializable
        ):
            rov = self.kb.get_rov(eq_type)
            family = (rov.get("family_full_name") if rov else None) or (rov.get("family") if rov else None) or "ROV"

            self._commit_internal_slot_values({"equipment_family": family})
            self.task_state["equipment_family"] = family

        if task_type_key:
            b_dict, missing = self.builder.build(self.task_state, task_type_key, self.mode)
            if commit_derived and "task_id" in b_dict and not self.task_state.get("task_id"):
                self._commit_internal_slot_values(
                    {"task_id": b_dict["task_id"]}
                )
            self._last_missing = missing
        else:
            self._last_missing = [{
                "key": "task_type",
                "label": "任务类型",
                "type": "string",
                "allowed_values": self.kb.get_all_task_type_values()
            }]
        self.task_state = self.slot_store.get_task_state()
        self._last_built_json = self.slot_store.get_built_json()
        self.task_start_now = self.is_start_time_near_now()

    def export_snapshot(self) -> dict:
        """导出 Issue #10 会话状态快照。"""
        if is_session_state_v2_enabled():
            _ = self._build_session_state_contract()

        return {
            "snapshot_version": 2,
            "session_id": self.session_id,
            "conversation_history": copy.deepcopy(self.conversation_history),
            "phase": self.phase,
            "mode": self.mode,
            "dialogue_mode": self.dialogue_mode,
            "last_mode_transition": copy.deepcopy(self.last_mode_transition),
            "mode_transition_history": copy.deepcopy(self.mode_transition_history),
            "control_state": self.control_state,
            "last_control_request": copy.deepcopy(self.last_control_request),
            "slot_store": self.slot_store.export_snapshot(),
            "task_state": copy.deepcopy(self.task_state),
        }

    # --------------------------------------------------------------------------
    # 历史快照恢复
    # --------------------------------------------------------------------------

    def load_snapshot(self, snapshot: dict) -> None:
        """原子恢复旧版扁平快照和 snapshot_version=2 完整快照。"""
        session_state_v2_active = is_session_state_v2_enabled()

        with self._session_lock:
            candidate = DialogueManager(
                llm=self.llm,
                kb=self.kb,
                session_id=self.session_id,
            )
            # Legacy restore does not own this field, so preserve its existing
            # value unless the SessionState contract explicitly replaces it.
            candidate.awaiting_final_confirm = self.awaiting_final_confirm
            candidate._load_snapshot_in_place(
                snapshot,
                session_state_v2_active=session_state_v2_active,
            )
            self._commit_snapshot_runtime_state(candidate)
            self._run_session_state_shadow_check(checkpoint="load_snapshot")

    def _commit_snapshot_runtime_state(self, candidate: "DialogueManager") -> None:
        """Commit a fully validated candidate restore to the live manager."""
        runtime_fields = (
            "session_id",
            "conversation_history",
            "slot_store",
            "task_state",
            "mode",
            "phase",
            "final_result",
            "awaiting_final_confirm",
            "task_start_now",
            "_blocking_violations",
            "_soft_whitelist",
            "_hard_refusal_counts",
            "_pending_rov_candidates",
            "_last_built_json",
            "_last_missing",
            "control_state",
            "last_control_request",
            "dialogue_mode",
            "last_mode_transition",
            "mode_transition_history",
        )
        for field_name in runtime_fields:
            setattr(self, field_name, getattr(candidate, field_name))

    def _load_snapshot_in_place(
        self,
        snapshot: dict,
        *,
        session_state_v2_active: bool,
    ) -> None:
        """Restore and validate a snapshot on an isolated candidate manager."""
        if session_state_v2_active:
            contract_state = session_state_from_legacy_snapshot(snapshot)
        else:
            contract_state = None

        if not isinstance(snapshot, dict):
            raise ValueError("History snapshot must be a dictionary")

        if "session_id" in snapshot and snapshot["session_id"] is not None:
            self.session_id = str(snapshot["session_id"])

        conversation_history = snapshot.get("conversation_history", [])
        if not isinstance(conversation_history, list):
            raise ValueError("conversation_history must be a list")

        mode = snapshot.get("mode", "normal")
        phase = snapshot.get("phase", "collecting")

        if type(mode) is not str or mode not in VALID_TASK_MODES:
            raise ValueError(f"Invalid task mode in snapshot: {mode}")
        if type(phase) is not str or phase not in VALID_PHASES:
            raise ValueError(f"Invalid task phase in snapshot: {phase}")

        # 校验模式与控制快照字段（原子校验，失败则不更改内存状态）
        valid_modes = {"task_collection", "knowledge_qa", "emergency_intervention", "uncertain"}
        valid_control = {"idle", "stop_requested", "pause_requested", "abort_requested", "cancel_requested"}

        dialogue_mode = snapshot.get("dialogue_mode", "task_collection")
        if not isinstance(dialogue_mode, str) or dialogue_mode not in valid_modes:
            raise ValueError(f"Invalid dialogue_mode in snapshot: {dialogue_mode}")
        if dialogue_mode == "uncertain":
            dialogue_mode = "knowledge_qa"

        control_state = snapshot.get("control_state", "idle")
        if not isinstance(control_state, str) or control_state not in valid_control:
            raise ValueError(f"Invalid control_state in snapshot: {control_state}")

        mode_transition_history = snapshot.get("mode_transition_history", [])
        if not isinstance(mode_transition_history, list):
            raise ValueError("mode_transition_history must be a list")

        def _val_trans(item: Any) -> dict:
            if not isinstance(item, dict):
                raise ValueError("Transition item must be a dictionary")
            from_m = item.get("from")
            to_m = item.get("to")
            if not isinstance(from_m, str) or from_m not in valid_modes:
                raise ValueError(f"Invalid 'from' mode: {from_m}")
            if not isinstance(to_m, str) or to_m not in valid_modes:
                raise ValueError(f"Invalid 'to' mode: {to_m}")

            conf = item.get("confidence", 1.0)
            if isinstance(conf, bool):
                raise ValueError("confidence cannot be bool")
            if not isinstance(conf, (int, float)) or not math.isfinite(float(conf)) or not (0.0 <= float(conf) <= 1.0):
                raise ValueError(f"Invalid confidence: {conf}")

            changed_at = item.get("changed_at")
            if not isinstance(changed_at, str) or not changed_at.strip():
                raise ValueError("Invalid changed_at in transition")

            try:
                parsed = datetime.fromisoformat(changed_at)
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError(f"changed_at must be timezone-aware ISO format: {changed_at}")
            except Exception as e:
                raise ValueError(f"Invalid timezone-aware ISO timestamp '{changed_at}': {e}")

            return item

        validated_history = [_val_trans(t) for t in mode_transition_history]

        last_mode_transition = snapshot.get("last_mode_transition")
        if last_mode_transition is not None:
            last_mode_transition = _val_trans(last_mode_transition)

        last_control_request = snapshot.get("last_control_request")
        if last_control_request is not None:
            if not isinstance(last_control_request, dict):
                raise ValueError("last_control_request must be a dictionary")
            act = last_control_request.get("action")
            if not isinstance(act, str) or act not in {"stop", "pause", "abort", "cancel"}:
                raise ValueError(f"Invalid action in last_control_request: {act}")
            st = last_control_request.get("status")
            if not isinstance(st, str) or st != "requested":
                raise ValueError(f"Invalid status in last_control_request: {st}")

        # 校验 control_state 与 last_control_request 的严格一致性
        if last_control_request is None:
            if control_state != "idle":
                raise ValueError("control_state must be 'idle' when last_control_request is None")
        else:
            act = last_control_request["action"]
            expected_state = f"{act}_requested"
            if control_state != expected_state:
                raise ValueError(
                    f"Mismatched control_state '{control_state}' for action '{act}' (expected '{expected_state}')"
                )

        candidate_store = None

        if "slot_store" in snapshot and isinstance(snapshot.get("slot_store"), dict):
            candidate_store = SlotStore.from_snapshot(snapshot["slot_store"], self.kb)

        if candidate_store is None:
            # 兼容没有 snapshot_version/slot_store 的旧历史记录。
            legacy_state = snapshot.get("task_state", {})
            if not isinstance(legacy_state, dict):
                raise ValueError("task_state must be a dictionary")
            candidate_store = SlotStore(self.kb)
            task_type_key = legacy_state.get("task_type_key")
            if task_type_key:
                required_fields = self.builder.get_schema(task_type_key, mode)
                candidate_store.init_task_slots(required_fields)

            new_slots = candidate_store.clone_slots()
            for key, value in legacy_state.items():
                vtype = normalize_slot_value_type(None, value)
                if key in new_slots:
                    new_slots[key].value = copy.deepcopy(value)
                    new_slots[key].status = "valid"
                    new_slots[key].value_type = vtype
                else:
                    new_slots[key] = Slot(
                        slot_name=key,
                        value=copy.deepcopy(value),
                        status="valid",
                        value_type=vtype,
                    )
            if task_type_key and ("internal_id" not in new_slots or new_slots["internal_id"].value is None):
                new_slots["internal_id"] = Slot(
                    slot_name="internal_id",
                    value=str(uuid.uuid4()),
                    status="valid",
                    value_type="string",
                    source="snapshot_migration",
                )
            # Route the migrated flat state through SlotStore's authoritative
            # snapshot validator before committing it to the candidate
            # manager.  This keeps legacy restore behavior aligned with v2
            # snapshots (including exact selector rules and alias migration)
            # without mutating the live manager on failure.
            migrated_snapshot = candidate_store.export_snapshot()
            migrated_snapshot["snapshot_schema_version"] = None
            migrated_snapshot["slots"] = {
                key: slot.to_dict()
                for key, slot in new_slots.items()
            }
            migrated_snapshot["unresolved"] = []
            candidate_store.restore_snapshot(migrated_snapshot)


        # 候选 SlotStore 校验系统标识合法性与互斥结构完整性
        cand_internal_slot = candidate_store.slots.get("internal_id")
        cand_internal = cand_internal_slot.value if (cand_internal_slot and cand_internal_slot.status == "valid" and cand_internal_slot.value is not None) else None

        cand_task_id_slot = candidate_store.slots.get("task_id")
        cand_task_id = cand_task_id_slot.value if (cand_task_id_slot and cand_task_id_slot.status == "valid" and cand_task_id_slot.value is not None) else None

        cand_task_type_key_slot = candidate_store.slots.get("task_type_key")
        cand_task_type_key = cand_task_type_key_slot.value if (cand_task_type_key_slot and cand_task_type_key_slot.status == "valid" and cand_task_type_key_slot.value is not None) else None

        if cand_internal is not None:
            if not _ti_builder_module.validate_uuid4(str(cand_internal)):
                raise SnapshotValidationError(f"Invalid internal_id UUIDv4 in candidate snapshot: {cand_internal}")
            if cand_task_type_key is None:
                raise SnapshotValidationError(f"internal_id {cand_internal} present in candidate snapshot but task_type_key is missing")

        if cand_task_id is not None:
            if not validate_task_id(str(cand_task_id)):
                raise SnapshotValidationError(f"Invalid task_id format in candidate snapshot: {cand_task_id}")
            if not cand_task_type_key:
                raise SnapshotValidationError(f"task_id {cand_task_id} present in candidate snapshot but task_type_key is missing")
            if not validate_task_id_for_task_type(str(cand_task_id), cand_task_type_key, self.kb.task_schemas):
                raise SnapshotValidationError(f"task_id {cand_task_id} does not match task_type_key {cand_task_type_key} in candidate snapshot")
            if cand_internal is None or cand_task_type_key is None:
                raise SnapshotValidationError("v2 candidate snapshot with valid task_id must contain internal_id, task_id, and task_type_key simultaneously")

        # 候选 SlotStore 完整校验通过后再一次性替换，避免半恢复状态泄漏。
        self.conversation_history = copy.deepcopy(conversation_history)
        self.slot_store = candidate_store
        self.task_state = self.slot_store.get_task_state()

        if session_state_v2_active and contract_state is not None:
            self._apply_session_state_contract(contract_state)
        else:
            self.mode = mode
            self._switch_dialogue_mode(
                dialogue_mode,
                source="snapshot_restore",
                reason="restore validated snapshot state",
                restore_transition_state=(last_mode_transition, validated_history),
            )
            self._set_execution_control_state(
                control_state,
                last_control_request,
                source="snapshot_restore",
                reason="restore validated snapshot state",
            )

        self.final_result = None
        self.task_start_now = False
        # 清空阻塞与白名单，重新构建缓存
        self._blocking_violations = []
        self._soft_whitelist = set()
        self._hard_refusal_counts = {}
        self._pending_rov_candidates = []
        self._rebuild_cache(commit_derived=False)

        # ── 快照 Intent ID 校验与 done 阶段完整性校验 ──
        intent_slot = self.slot_store.slots.get("intent_id")
        intent_id_val = intent_slot.value if (intent_slot and intent_slot.status == "valid") else None
        is_valid_id = bool(intent_id_val and validate_intent_id(str(intent_id_val)))

        _REQUIRED_INTENT_KEYS = {
            "intent_id", "task_type", "priority", "time",
            "location", "task", "equipment", "conditions"
        }
        if phase == "done":
            validated = False
            _loaded_intent = None
            if is_valid_id and intent_id_val:
                try:
                    task_dir = _ti_builder_module.get_task_dir(create=False)
                    with _ti_builder_module.TaskPublishLock(task_dir):
                        pub_file = task_dir / f"task_intent_{intent_id_val}.json"
                        if pub_file.is_symlink():
                            logger.warning("[load_snapshot] final file is a symlink, rejecting done phase")
                        elif pub_file.is_file():
                            with open(pub_file, "r", encoding="utf-8") as _f:
                                _data = json.load(_f)
                            if isinstance(_data, dict) and _REQUIRED_INTENT_KEYS.issubset(_data.keys()):
                                _file_task_type = _data.get("task_type")
                                _file_intent_id = _data.get("intent_id")
                                _file_internal_id = _data.get("internal_id")
                                _file_task_id = _data.get("task_id")
                                _file_ver = _data.get("schema_version")

                                snap_internal_slot = self.slot_store.slots.get("internal_id")
                                snap_internal = snap_internal_slot.value if (snap_internal_slot and snap_internal_slot.status == "valid") else None

                                snap_task_id_slot = self.slot_store.slots.get("task_id")
                                snap_task_id = snap_task_id_slot.value if (snap_task_id_slot and snap_task_id_slot.status == "valid") else None

                                is_snap_v2 = bool(snap_internal or snap_task_id)
                                is_file_v2 = bool(_file_ver == 2 or _file_internal_id or _file_task_id)

                                if is_snap_v2 != is_file_v2:
                                    logger.warning("[load_snapshot] schema version mismatch: is_snap_v2=%s vs is_file_v2=%s", is_snap_v2, is_file_v2)
                                elif _file_intent_id != intent_id_val:
                                    logger.warning("[load_snapshot] intent_id mismatch in final file")
                                elif is_snap_v2 and (not _file_internal_id or snap_internal != _file_internal_id):
                                    logger.warning("[load_snapshot] internal_id mismatch or missing in final file: snap=%s vs file=%s", snap_internal, _file_internal_id)
                                elif is_snap_v2 and (not _file_task_id or snap_task_id != _file_task_id):
                                    logger.warning("[load_snapshot] task_id mismatch or missing in final file: snap=%s vs file=%s", snap_task_id, _file_task_id)
                                elif not _ti_builder_module.validate_task_intent(_data, self.kb.task_schemas):
                                    logger.warning("[load_snapshot] invalid task_type or TaskIntent structure in final file: %r", _file_task_type)
                                else:
                                    validated = True
                                    _loaded_intent = copy.deepcopy(_data)
                            else:
                                logger.warning("[load_snapshot] final file missing required keys")
                        else:
                            logger.warning("[load_snapshot] final file not found: %s", pub_file)
                except Exception as _e:
                    logger.warning("[load_snapshot] done-phase validation error: %s", _e)

            if validated:
                self._transition_phase(
                    "done",
                    source="snapshot_restore",
                    reason="validated published task file",
                )
                self.final_result = _loaded_intent
            else:
                self._transition_phase(
                    "collecting",
                    source="snapshot_restore",
                    reason="published task validation failed",
                )
                today = get_current_datetime().strftime("%Y%m%d")
                task_dir = _ti_builder_module.get_task_dir(create=False)
                new_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])
                new_slots = self.slot_store.clone_slots()
                if "intent_id" not in new_slots:
                    new_slots["intent_id"] = Slot("intent_id")
                new_slots["intent_id"].value = new_id
                new_slots["intent_id"].value_type = "string"
                new_slots["intent_id"].status = "valid"
                new_slots["intent_id"].source = "auto"
                self.slot_store.commit_transaction(new_slots, self.slot_store.unresolved)
                self.task_state = self.slot_store.get_task_state()
                self._last_built_json = self.slot_store.get_built_json()
        else:
            self._transition_phase(
                phase,
                source="snapshot_restore",
                reason="restore validated snapshot phase",
            )
            if not is_valid_id:
                today = get_current_datetime().strftime("%Y%m%d")
                task_dir = _ti_builder_module.get_task_dir(create=False)
                new_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])
                new_slots = self.slot_store.clone_slots()
                if "intent_id" not in new_slots:
                    new_slots["intent_id"] = Slot("intent_id")
                new_slots["intent_id"].value = new_id
                new_slots["intent_id"].value_type = "string"
                new_slots["intent_id"].status = "valid"
                new_slots["intent_id"].source = "auto"
                self.slot_store.commit_transaction(new_slots, self.slot_store.unresolved)
                self.task_state = self.slot_store.get_task_state()
                self._last_built_json = self.slot_store.get_built_json()

        if session_state_v2_active:
            _ = self._build_session_state_contract()
