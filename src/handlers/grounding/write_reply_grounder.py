"""
src/handlers/grounding/write_reply_grounder.py - 槽位提交事实锚点与回复展示生成器门面

职责：
作为高层组装流水线门面，协调子模块完成事实锚点校验、断言清洗与回执组装：
- assertion_scrubber: 未提交断言与幻觉清洗；
- field_grounder: 槽位事实、时间窗口、水深与载荷断言对齐校验；
- receipt_builder: 权威客观回执构建、候选投影与回复组装。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.constants import FIELD_LABELS
from src.extraction import coord_parser
from src.telemetry_evidence import ground_telemetry_reply

from .assertion_scrubber import (
    clean_hallucinations_and_syntax,
    remove_model_receipts,
    remove_write_off_topic_template,
    remove_write_welcome_template,
    scrub_uncommitted_slot_assertions,
    scrub_uncommitted_start_time_assertions,
    scrub_uncommitted_vessel_assertions,
)
from .field_grounder import (
    FACT_FIELD_ALIASES,
    ground_blocked_publish_instructions,
    ground_field_assertions,
    ground_payload_assertions,
    ground_payload_classification,
    ground_time_windows,
    ground_water_depth_assertions,
    remove_unsupported_now_gloss,
    time_claim_matches,
)
from .receipt_builder import (
    build_authoritative_receipt,
    compose_final_reply,
    filter_robot_selection_unresolved,
    project_legacy_equipment_class_candidate,
    repair_missing_field_block,
)

logger = logging.getLogger(__name__)


class WriteReplyGrounder:
    """槽位提交事实锚点与回复展示生成器门面"""

    _FACT_FIELD_ALIASES = FACT_FIELD_ALIASES

    # 导出静态与类方法以维持 100% 单元测试及外部调用兼容
    filter_robot_selection_unresolved = staticmethod(filter_robot_selection_unresolved)
    project_legacy_equipment_class_candidate = staticmethod(project_legacy_equipment_class_candidate)
    _remove_model_receipts = staticmethod(remove_model_receipts)
    _ground_payload_classification = staticmethod(ground_payload_classification)
    _ground_water_depth_assertions = staticmethod(ground_water_depth_assertions)
    _time_claim_matches = staticmethod(time_claim_matches)
    _ground_time_windows = staticmethod(ground_time_windows)
    _remove_unsupported_now_gloss = staticmethod(remove_unsupported_now_gloss)
    _repair_missing_field_block = staticmethod(repair_missing_field_block)
    _ground_field_assertions = staticmethod(ground_field_assertions)
    _ground_payload_assertions = staticmethod(ground_payload_assertions)
    _scrub_uncommitted_vessel_assertions = staticmethod(scrub_uncommitted_vessel_assertions)
    _scrub_uncommitted_start_time_assertions = staticmethod(scrub_uncommitted_start_time_assertions)
    _scrub_uncommitted_slot_assertions = staticmethod(scrub_uncommitted_slot_assertions)
    _ground_blocked_publish_instructions = staticmethod(ground_blocked_publish_instructions)
    _remove_write_welcome_template = staticmethod(remove_write_welcome_template)
    _remove_write_off_topic_template = staticmethod(remove_write_off_topic_template)
    _build_authoritative_receipt = staticmethod(build_authoritative_receipt)
    _clean_hallucinations_and_syntax = classmethod(lambda cls, scrubbed, **kwargs: clean_hallucinations_and_syntax(
        scrubbed, repair_missing_fn=repair_missing_field_block, **kwargs
    ))
    _compose_final_reply = staticmethod(compose_final_reply)

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    @property
    def kb(self) -> Any:
        return getattr(self.manager, "kb", None)

    @property
    def task_state(self) -> Any:
        return getattr(self.manager, "task_state", None)

    def get_committed_update_display_values(self, accepted_updates: dict) -> dict:
        """从领域配置生成写入回执的展示值，不改变 SlotStore 标准值。"""
        manager = self.manager
        display = {}
        for k, v in (accepted_updates or {}).items():
            if k == "equipment_class" and v is not None:
                class_config = manager.kb.get_robot_classes().get(str(v), {}) if (manager and manager.kb) else {}
                display["equipment_class"] = class_config.get(
                    "full_name",
                    v,
                )
            else:
                display[k] = coord_parser.format_slot_display_value(k, v)
        return display

    def get_payload_configuration(self) -> dict | None:
        """Use the same selected robot as the task's knowledge context."""
        state = self.manager.task_state if self.manager else {}
        kb = self.manager.kb if self.manager else None
        if not kb:
            return None
        equipment = state.get("equipment_type") or state.get("equipment_name")
        if state.get("equipment_unit_id"):
            unit = kb.resolve_robot_unit(
                str(state["equipment_unit_id"]), state.get("task_type_key"),
                str(state["equipment_type"]) if state.get("equipment_type") else None,
            )
            if unit:
                return unit.get("robot")
        return kb.get_rov_for_task(str(equipment), state.get("task_type_key")) if equipment else None

    def get_committed_turn_updates(
        self,
        proposed_updates: dict,
        state_before_turn: dict,
    ) -> dict:
        """返回本轮已由 SlotStore 提交的用户字段更新。"""
        manager = self.manager
        if not proposed_updates or not manager:
            return {}

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
        for key, value in manager.task_state.items():
            if key in ignored_keys or key.startswith("__") or value is None:
                continue
            if key not in proposed_updates and state_before_turn.get(key) == value:
                continue
            slot = manager.slot_store.slots.get(key) if hasattr(manager, "slot_store") else None
            if slot and slot.status == "valid":
                accepted[key] = value
        for key, value in proposed_updates.items():
            slot = manager.slot_store.slots.get(key) if hasattr(manager, "slot_store") else None
            if (key in FIELD_LABELS and value == [] and slot
                    and slot.value == [] and slot.status == "missing"
                    and not slot.validation_error):
                accepted[key] = []
        return accepted

    @classmethod
    def _ground_conversational_assertions(
        cls,
        actual_model_reply: str,
        *,
        visible: dict,
        task_state: dict | None,
        context: dict,
        payload_configuration: dict | None,
        has_hard: bool,
        has_soft: bool,
    ) -> str:
        """对模型自然语言回复进行事实接地与断言对齐。"""
        cleaned_reply = cls._remove_write_off_topic_template(
            cls._remove_write_welcome_template(str(actual_model_reply).strip())
        )
        cleaned_reply = cls._remove_model_receipts(cleaned_reply)
        cleaned_reply = cls._remove_unsupported_now_gloss(cleaned_reply)
        cleaned_reply = cls._ground_payload_classification(cleaned_reply, payload_configuration)
        cleaned_reply = cls._ground_water_depth_assertions(cleaned_reply, visible, task_state or {})
        cleaned_reply = cls._ground_time_windows(cleaned_reply, visible, task_state or {})
        cleaned_reply = cls._ground_field_assertions(cleaned_reply, visible, task_state or {})
        cleaned_reply = cls._ground_payload_assertions(cleaned_reply, visible, task_state or {})
        scrubbed = cls._scrub_uncommitted_slot_assertions(
            cleaned_reply,
            visible, task_state or {},
        )
        scrubbed = ground_telemetry_reply(scrubbed, context, task_state=task_state)

        blocked_hard = has_hard or str(context.get("type", "")).startswith("hard")
        blocked_soft = has_soft or str(context.get("type", "")).startswith("soft")
        if blocked_hard or blocked_soft:
            scrubbed = cls._ground_blocked_publish_instructions(scrubbed, hard=blocked_hard)
            if blocked_soft and not blocked_hard:
                scrubbed = re.sub(r"任务尚未发布，仍有软警告待处理[。.]", "", scrubbed)
        return scrubbed

    @classmethod
    def ground_write_reply(
        cls,
        self_or_reply: object = "",
        model_reply: str = "",
        *,
        accepted_updates: dict,
        unresolved_inputs: list,
        missing_fields: list[dict] | None = None,
        display_updates: dict | None = None,
        task_state: dict | None = None,
        constraint_context: dict | None = None,
        payload_configuration: dict | None = None,
    ) -> str:
        """Render a grounded WRITE response combining conversational prose with factual receipts.

        Design principles:
        1. Model conversational prose is preserved to maintain human-like dialogue;
        2. Uncommitted claims, unauthorized publications, and false health assurances are scrubbed;
        3. Objective receipts (accepted, unresolved, retained, constraints, missing) serve as the ground truth;
        4. When model prose is empty or purely false, safely degrades to deterministic receipt.
        """
        if isinstance(self_or_reply, str):
            actual_model_reply = self_or_reply
        else:
            actual_model_reply = model_reply if isinstance(model_reply, str) else ""

        # 1. 构建权威客观回执
        (
            receipt,
            parts,
            visible,
            unresolved,
            has_hard,
            has_soft,
            labels,
            violations,
        ) = cls._build_authoritative_receipt(
            accepted_updates=accepted_updates,
            unresolved_inputs=unresolved_inputs,
            missing_fields=missing_fields,
            display_updates=display_updates,
            task_state=task_state,
            constraint_context=constraint_context,
            actual_model_reply=actual_model_reply,
        )

        if (visible.get("payload") == [] or (not visible and not task_state)
                or not actual_model_reply or not str(actual_model_reply).strip()):
            return receipt

        context = constraint_context or {}

        # 2. 对自然语言断言进行事实接地
        scrubbed = cls._ground_conversational_assertions(
            actual_model_reply,
            visible=visible,
            task_state=task_state,
            context=context,
            payload_configuration=payload_configuration,
            has_hard=has_hard,
            has_soft=has_soft,
        )

        # 3. 越权声明、幻觉与语法排版清洗
        scrubbed = cls._clean_hallucinations_and_syntax(
            scrubbed,
            actual_model_reply=actual_model_reply,
            visible=visible,
            task_state=task_state,
            violations=violations,
            labels=labels,
            missing_fields=missing_fields,
        )

        # 4. 融合自然语言与权威回执
        blocked_hard = has_hard or str(context.get("type", "")).startswith("hard")
        blocked_soft = has_soft or str(context.get("type", "")).startswith("soft")
        return cls._compose_final_reply(
            scrubbed,
            parts,
            receipt,
            blocked_hard=blocked_hard,
            blocked_soft=blocked_soft,
        )
