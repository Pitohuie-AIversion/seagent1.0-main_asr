"""
src/handlers/slot_filling.py - 槽位填报与消歧生命周期处理器

职责：
1. 搭载工具就地调整/补丁提取；
2. 任务参数抽取（ParameterExtractor）与规范化应用（FieldNormalizer/NormalizationContract）；
3. 槽位状态存储与事务更新（SlotStore, TaskSlotFilter）；
4. 交互式多候选消歧（ROV候选、油田候选、工具组合候选）；
5. 缺失参数指引与下步提问生成；
6. 事实锚点回复（ground_write_reply）及回执展示生成。
"""

from __future__ import annotations

import copy
import logging
import re
import uuid
from typing import Any, List, Optional

from .base import BaseDialogueHandler, DialogueContext, HandlerResult
from .equipment_cascade import EquipmentCascadeResolver
from .task_transition import TaskTransitionManager
from .payload_mutation import PayloadMutationManager
from .write_reply_grounder import WriteReplyGrounder
from .oilfield_confirmation import OilfieldConfirmationHandler
from .slot_transaction import SlotTransactionManager
from .slot_extraction_pipeline import SlotExtractionPipeline
from src.extraction.model_profile import (
    ModelRole,
    is_normalization_contract_v2_enabled,
    is_task_patch_v2_enabled,
)
from src.slots.normalization_contract import (
    NormalizationApplyPlan,
    validate_normalization_runtime_flags,
)
from src.slots.task_patch import build_task_patch, task_patch_to_legacy_updates
from src.slots.slot_store import Slot
from src.temporal.simulated_time import get_current_datetime
from src.dispatch.id_sequence import next_daily_id, IdReservationError, validate_task_id_for_task_type
from src.extraction.prompts import build_responder_messages

logger = logging.getLogger("src.dialogue_manager")


class SlotFillingHandler(BaseDialogueHandler):
    """槽位填报与消歧生命周期处理器"""

    def __init__(self, manager: Any) -> None:
        super().__init__(manager)
        self.equipment_cascade = EquipmentCascadeResolver(manager)
        self.task_transition = TaskTransitionManager(manager)
        self.payload_mutation = PayloadMutationManager(manager)
        self.reply_grounder = WriteReplyGrounder(manager)
        self.oilfield_confirmation = OilfieldConfirmationHandler(manager)
        self.slot_transaction = SlotTransactionManager(manager)
        self.extraction_pipeline = SlotExtractionPipeline(manager, slot_filling_handler=self)

    @property
    def kb(self) -> Any:
        return getattr(self.manager, "kb", None)

    @property
    def task_state(self) -> Any:
        return getattr(self.manager, "task_state", None)

    def __getattr__(self, name: str) -> Any:
        for sub in (
            'equipment_cascade',
            'task_transition',
            'payload_mutation',
            'reply_grounder',
            'oilfield_confirmation',
            'slot_transaction',
            'extraction_pipeline',
        ):
            if sub in self.__dict__ and hasattr(self.__dict__[sub], name):
                return getattr(self.__dict__[sub], name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def can_handle(self, ctx: DialogueContext) -> bool:
        """
        判断是否由本处理器在门禁阶段介入：
        当前仅当用户发起载荷就地调整指令时在 Level 3 拦截处理。
        普通槽位抽取由主流程显式调用 execute_slot_filling 完成。
        """
        return self.payload_mutation.can_handle(ctx)

    def handle(self, ctx: DialogueContext) -> HandlerResult:
        """执行槽位就地修改（载荷卡片重置等）"""
        return self.payload_mutation.handle(ctx)

    @staticmethod
    def is_payload_modification_request(user_message: str) -> bool:
        """判断用户是否明确请求重新选择/修改/配置载荷（且不属于取消修改指令）。"""
        return PayloadMutationManager.is_payload_modification_request(user_message)

    def handle_payload_modification(self, user_message: str) -> str | None:
        """打开载荷编辑卡片，保留已提交配置。"""
        return self.payload_mutation.handle_payload_modification(user_message)

    def normalize_payload_list_mutations(
        self,
        extraction_res: dict,
        user_message: str,
        current_slots: dict,
    ) -> None:
        """兜底防护：转换 LLM 误抽取的 payload 候选为 list_mutations。"""
        return self.payload_mutation.normalize_payload_list_mutations(
            extraction_res,
            user_message,
            current_slots,
        )

    @staticmethod
    def filter_robot_selection_unresolved(
        unresolved_items: list,
        accepted_updates: dict | None,
    ) -> list:
        """清理机器人选择迁移后的 unresolved 噪声。"""
        return WriteReplyGrounder.filter_robot_selection_unresolved(
            unresolved_items,
            accepted_updates,
        )

    def project_legacy_equipment_class_candidate(
        self,
        candidate: dict,
        task_type_key: str | None,
        task_state: dict | None,
    ) -> dict | None:
        """Map a legacy equipment_class candidate to family when unambiguous."""
        return self.reply_grounder.project_legacy_equipment_class_candidate(
            candidate,
            task_type_key,
            task_state,
        )

    def get_committed_update_display_values(self, accepted_updates: dict) -> dict:
        """从领域配置生成写入回执的展示值，不改变 SlotStore 标准值。"""
        return self.reply_grounder.get_committed_update_display_values(accepted_updates)

    def get_committed_turn_updates(
        self,
        proposed_updates: dict,
        state_before_turn: dict,
    ) -> dict:
        """返回本轮已由 SlotStore 提交的用户字段更新。"""
        return self.reply_grounder.get_committed_turn_updates(
            proposed_updates,
            state_before_turn,
        )

    @staticmethod
    def ground_write_reply(
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
        """在 LLM 自然语言回复后追加事实锚点摘要，防止回复内容与实际写入状态不一致。"""
        return WriteReplyGrounder.ground_write_reply(
            self_or_reply,
            model_reply,
            accepted_updates=accepted_updates,
            unresolved_inputs=unresolved_inputs,
            missing_fields=missing_fields,
            display_updates=display_updates,
            task_state=task_state,
            constraint_context=constraint_context,
            payload_configuration=payload_configuration,
        )


    def reply_write_without_candidates(
        self,
        user_message: str,
        request_id: Optional[str] = None,
        turn_unresolved: Optional[List[str]] = None,
        task_type_key: Optional[str] = None,
        has_acknowledge_action: bool = False,
        refresh_constraints: bool = True,
    ) -> str:
        """依据事务结果回复，优先通过 LLM 依据处理事实向用户解释原因并引导。"""
        manager = self.manager
        if has_acknowledge_action:
            manager._switch_dialogue_mode(
                "task_collection",
                source="interaction_plan",
                reason="无任务候选时执行结构化软警告确认",
            )
            if manager.phase == "blocked_soft":
                return manager._handle_soft_warning_confirmation(
                    user_message,
                    request_id,
                )
            reply = (
                "当前没有等待确认的软警告，未执行忽略操作。"
                "任务状态和已填写参数均未改变。"
            )
            manager.conversation_history.append({"role": "user", "content": user_message})
            manager.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        unresolved = list(turn_unresolved or [])
        if not unresolved:
            unresolved.append("模型没有返回可验证的任务字段候选")

        req_fields = manager.builder.get_required(task_type_key, manager.mode, manager.slot_store.get_task_state()) if task_type_key else []
        missing = manager.slot_store.get_missing_slots(req_fields) if task_type_key else []
        if refresh_constraints:
            constraint_context = manager._run_constraint_check(set(), purpose="interactive")
        else:
            # Discussion uses the already saved warnings. Revalidation can change
            # phase/refusal counts, so it must not run for an explicit edit pause.
            validation = manager.slot_store.validation_result
            constraint_context = {
                "type": {"blocked_hard": "hard", "blocked_soft": "soft"}.get(manager.phase, "none"),
                "violations": list(manager._blocking_violations),
                "hard_refusal_counts": dict(manager._hard_refusal_counts),
                "state_snapshot": getattr(validation, "state_snapshot", None),
                "runtime_validation_deferred": any(
                    getattr(item, "check_type", "") == "future_task_runtime_notice"
                    for item in (getattr(validation, "violations", None) or [])
                ),
            }
        knowledge_context = manager.kb.get_context_for_state(
            manager.task_state,
            include_runtime_state=not constraint_context.get("runtime_validation_deferred", False),
        )

        messages = build_responder_messages(
            task_state=manager.task_state,
            built_json=manager._last_built_json,
            missing_fields=missing,
            mode=manager.mode,
            phase=manager.phase,
            knowledge_context=knowledge_context,
            constraint_context=constraint_context,
            conversation_history=manager.conversation_history,
            latest_user_message=user_message,
            ROV2type=manager.kb.ROV2type,
            support_task=manager.kb.get_supported_task(),
            slot_snapshot=manager.slot_store.get_slot_snapshot(),
            accepted_updates={},
            unresolved_inputs=unresolved,
        )
        model_reply = manager._safe_llm_chat(
            messages,
            temperature=0.7,
            max_tokens=1500,
            role=ModelRole.TASK_RESPONDER,
        )
        model_reply = manager._safe_llm_filter_reply(model_reply, role=ModelRole.FILTER_REPLY)
        if task_type_key and (not model_reply or not model_reply.strip() or (len(model_reply.strip()) <= 30 and "已就绪" in model_reply and manager.phase == "collecting" and not manager.task_state.get("start_time"))):
            tv = manager.kb.task_schemas.get("task_templates", {}).get(task_type_key, {})
            task_name = tv.get("display_name") or tv.get("name") or task_type_key
            if manager.phase in ("blocked_hard", "blocked_soft"):
                model_reply = f"当前任务【{task_name}】存在约束限制，请根据提示调整作业参数。"
            elif manager.phase == "confirming":
                model_reply = f"当前任务【{task_name}】参数已齐备，请核对是否确认发布。"
            else:
                model_reply = f"已为您开启【{task_name}】任务规划。请提供具体作业参数（如管缆类型、作业区域与执行机器人）。"

        reply = self.ground_write_reply(
            model_reply,
            accepted_updates={},
            unresolved_inputs=unresolved,
            missing_fields=missing if missing else None,
            task_state=manager.task_state,
            constraint_context=constraint_context,
            payload_configuration=self.reply_grounder.get_payload_configuration(),
        )
        if task_type_key is None:
            supported = manager.kb.get_all_task_type_values()
            if (
                supported
                and "当前支持的任务类型" not in reply
                and not any(st in reply for st in supported)
            ):
                reply += " 当前支持的任务类型：" + "、".join(supported) + "。"
        manager.conversation_history.append({"role": "user", "content": user_message})
        manager.conversation_history.append({"role": "assistant", "content": reply})
        return reply


    @staticmethod
    def _check_task_patch_v2_enabled() -> bool:
        import sys
        _dm_mod = sys.modules.get("src.dialogue_manager")
        fn = getattr(_dm_mod, "is_task_patch_v2_enabled", is_task_patch_v2_enabled) if _dm_mod else is_task_patch_v2_enabled
        return fn()

    @staticmethod
    def _check_normalization_contract_v2_enabled() -> bool:
        import sys
        _dm_mod = sys.modules.get("src.dialogue_manager")
        fn = getattr(_dm_mod, "is_normalization_contract_v2_enabled", is_normalization_contract_v2_enabled) if _dm_mod else is_normalization_contract_v2_enabled
        return fn()

    def execute_slot_filling(
        self,
        ctx: DialogueContext,
        *,
        route: Any = None,
        plan: Any = None,
        has_acknowledge_action: bool = False,
    ) -> str:
        """执行端到端槽位抽取、规范化过滤、消歧、原子事务提交与事实锚点回复落地。"""
        turn_ctx = self._prepare_slot_filling_turn(ctx)

        # Stage 1: 当初始没有 task_type_key 时执行任务类型提取
        if turn_ctx["task_type_key"] is None:
            early_reply = self._execute_stage1_task_type(
                ctx=ctx,
                turn_ctx=turn_ctx,
                has_acknowledge_action=has_acknowledge_action,
            )
            if early_reply is not None:
                return early_reply

        # Stage 2: 参数抽取管道与默认坐标填充
        early_pipeline_reply, pipeline_res = self._execute_parameter_pipeline_and_defaults(
            ctx=ctx,
            turn_ctx=turn_ctx,
            route=route,
            plan=plan,
            has_acknowledge_action=has_acknowledge_action,
        )
        if early_pipeline_reply is not None:
            return early_pipeline_reply

        # Stage 3: 槽位事务预校验、自动 ID 生成与原子提交
        eval_ctx = self._commit_turn_transaction(
            ctx=ctx,
            turn_ctx=turn_ctx,
            pipeline_res=pipeline_res,
        )

        # Stage 4 & 5: 约束评估与回复生成
        return self._evaluate_and_generate_reply(
            ctx=ctx,
            turn_ctx=turn_ctx,
            pipeline_res=pipeline_res,
            eval_ctx=eval_ctx,
        )

    def _prepare_slot_filling_turn(self, ctx: DialogueContext) -> dict:
        manager = self.manager
        user_message = ctx.user_message
        request_id = ctx.request_id
        old_phase = ctx.old_phase

        task_patch_v2_active = self._check_task_patch_v2_enabled()
        norm_v2_active = self._check_normalization_contract_v2_enabled()
        validate_normalization_runtime_flags(task_patch_v2_active, norm_v2_active)

        new_slots, _previous_unresolved, expected_version = manager.slot_store.snapshot()
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
        current_state = manager.slot_store.get_task_state()
        state_before_turn = dict(current_state)
        reference_candidates = {
            item["canonical_key"]: item
            for item in [
                *getattr(manager, "_pending_referential_candidates", []),
                *ctx.metadata.get("referential_candidates", []),
            ]
        }
        ctx.metadata["referential_candidates"] = list(reference_candidates.values())

        if task_type_key:
            schema = manager.builder.get_schema(task_type_key, manager.mode)
            for f in schema:
                k = f.get("key")
                if k and k not in new_slots:
                    new_slots[k] = Slot(slot_name=k, value_type=f.get("type", "string"), status="missing")

        return {
            "user_message": user_message,
            "request_id": request_id,
            "old_phase": old_phase,
            "new_slots": new_slots,
            "new_unresolved": new_unresolved,
            "expected_version": expected_version,
            "task_type_key": task_type_key,
            "had_task_type_key_at_turn_start": had_task_type_key_at_turn_start,
            "state_before_turn": state_before_turn,
            "reference_candidates": reference_candidates,
            "merged_updates": {},
            "merged_updates_meta": {},
            "turn_unresolved": [],
            "proposed_pending_rov": list(manager._pending_rov_candidates),
            "task_patch_v2_active": task_patch_v2_active,
            "norm_v2_active": norm_v2_active,
            "extraction_res": {},
        }

    def _execute_stage1_task_type(
        self,
        ctx: DialogueContext,
        turn_ctx: dict,
        *,
        has_acknowledge_action: bool,
    ) -> str | None:
        manager = self.manager
        user_message = turn_ctx["user_message"]
        request_id = turn_ctx["request_id"]
        new_slots = turn_ctx["new_slots"]
        new_unresolved = turn_ctx["new_unresolved"]
        turn_unresolved = turn_ctx["turn_unresolved"]
        expected_version = turn_ctx["expected_version"]
        reference_candidates = turn_ctx["reference_candidates"]
        current_state = turn_ctx["state_before_turn"]
        merged_updates = turn_ctx["merged_updates"]
        merged_updates_meta = turn_ctx["merged_updates_meta"]

        def record_unresolved(result: dict) -> None:
            for item in result.get("unresolved", []):
                if item not in turn_unresolved:
                    turn_unresolved.append(item)
                if item not in new_unresolved:
                    new_unresolved.append(item)

        def reply_without_candidates() -> str:
            if turn_ctx["task_type_key"] is None and reference_candidates:
                manager._pending_referential_candidates = copy.deepcopy(list(reference_candidates.values()))
                turn_unresolved.append("已暂存所选对象，尚未写入任务参数；请先选择任务类型，再按该任务校验这些选择。")
            return self.reply_write_without_candidates(
                user_message=user_message,
                request_id=request_id,
                turn_unresolved=turn_unresolved,
                task_type_key=turn_ctx["task_type_key"],
                has_acknowledge_action=has_acknowledge_action,
            )

        extraction_res = manager.extractor.extract_updates(
            user_message, current_state,
            task_type_key=None,
            task_type_map=manager.kb.get_task_type_map(),
            required=None,
            conversation_history=manager.conversation_history,
            allow_empty_for_side_effect=has_acknowledge_action,
        )
        extraction_res = self.merge_referential_candidates(
            extraction_res,
            [item for item in reference_candidates.values()
             if item["canonical_key"] in {"task_type", "task_type_key"}],
        )
        turn_ctx["extraction_res"] = extraction_res

        selectors, selector_error = manager._task_selector_updates_from_extraction(extraction_res)
        _, _, _, context_error = manager._resolve_task_type_update_context(selectors, new_slots)
        task_type_error = selector_error or context_error
        if task_type_error:
            record_unresolved(extraction_res)
            record_unresolved({"unresolved": [task_type_error]})
            manager._record_task_type_update_error(new_slots, task_type_error)
            manager.slot_store.commit_transaction(
                new_slots,
                new_unresolved,
                request_id=request_id,
                expected_version=expected_version,
            )
            return reply_without_candidates()

        if self._check_task_patch_v2_enabled():
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
                    manager.slot_store.commit_transaction(
                        new_slots,
                        new_unresolved,
                        request_id=request_id,
                        expected_version=expected_version,
                    )
                return reply_without_candidates()
            for k, cand_info in stage1_updates.items():
                merged_updates[k] = cand_info["value"]
                merged_updates_meta[k] = cand_info
        else:
            if not extraction_res.get("slot_candidates"):
                record_unresolved(extraction_res)
                if new_unresolved:
                    manager.slot_store.commit_transaction(
                        new_slots,
                        new_unresolved,
                        request_id=request_id,
                        expected_version=expected_version,
                    )
                return reply_without_candidates()

            stage1_updates = {}
            for candidate in extraction_res.get("slot_candidates", []):
                k = candidate["canonical_key"]
                v = candidate["normalized_value"]
                cand_info = {
                    "value": v,
                    "raw_value": candidate.get("raw_value"),
                    "confidence": candidate.get("confidence", 1.0),
                    "source": manager._source_for_resolution_method(candidate.get("resolution_method")),
                }
                stage1_updates[k] = cand_info
                merged_updates[k] = v
                merged_updates_meta[k] = cand_info
            record_unresolved(extraction_res)

        manager._apply_updates_in_transaction(stage1_updates, new_slots)

        task_type_slot = new_slots.get("task_type_key")
        turn_ctx["task_type_key"] = (
            task_type_slot.value
            if task_type_slot
            and task_type_slot.status == "valid"
            and task_type_slot.value is not None
            else None
        )
        return None

    def _execute_parameter_pipeline_and_defaults(
        self,
        ctx: DialogueContext,
        turn_ctx: dict,
        *,
        route: Any,
        plan: Any,
        has_acknowledge_action: bool,
    ) -> tuple[str | None, Any]:
        manager = self.manager
        user_message = turn_ctx["user_message"]
        new_slots = turn_ctx["new_slots"]
        turn_unresolved = turn_ctx["turn_unresolved"]
        new_unresolved = turn_ctx["new_unresolved"]
        merged_updates = turn_ctx["merged_updates"]
        merged_updates_meta = turn_ctx["merged_updates_meta"]
        task_type_key = turn_ctx["task_type_key"]
        expected_version = turn_ctx["expected_version"]
        had_task_type_key_at_turn_start = turn_ctx["had_task_type_key_at_turn_start"]

        should_extract_task_parameters = bool(task_type_key)
        pipeline_res = None

        if should_extract_task_parameters:
            current_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
            pipeline_res = self.extraction_pipeline.run_pipeline(
                ctx=ctx,
                task_type_key=task_type_key,
                current_state=current_state,
                has_acknowledge_action=has_acknowledge_action,
                task_patch_v2_active=turn_ctx["task_patch_v2_active"],
                norm_v2_active=turn_ctx["norm_v2_active"],
                new_slots=new_slots,
                turn_unresolved=turn_unresolved,
                new_unresolved=new_unresolved,
                merged_updates=merged_updates,
                merged_updates_meta=merged_updates_meta,
                route=route,
                plan=plan,
                expected_version=expected_version,
                had_task_type_key_at_turn_start=had_task_type_key_at_turn_start,
            )
            if pipeline_res.early_return_reply is not None:
                return pipeline_res.early_return_reply, None

            turn_ctx["proposed_pending_rov"] = pipeline_res.proposed_pending_rov
            turn_ctx["task_type_key"] = pipeline_res.effective_task_type_key or task_type_key
        else:
            extraction_res = turn_ctx.get("extraction_res", {})
            if extraction_res.get("unresolved"):
                for u in extraction_res["unresolved"]:
                    if u not in new_unresolved:
                        new_unresolved.append(u)

        # 填充油田默认坐标
        of_id_slot = new_slots.get("oilfield_entity_id")
        if of_id_slot and of_id_slot.status == "valid" and of_id_slot.value:
            saved_entity = manager.slot_store.slots.get("oilfield_entity_id")
            saved_coordinates = manager.slot_store.slots.get("oilfield_coordinates")
            same_entity_custom_coordinates = bool(
                saved_entity and saved_entity.status == "valid" and saved_entity.value == of_id_slot.value
                and saved_coordinates and saved_coordinates.status == "valid"
                and saved_coordinates.value is not None and saved_coordinates.source != "oilfield_default"
            )
            has_explicit_coord = False
            if user_message:
                kw = ["北纬", "南纬", "东经", "西经", "纬度", "经度", "坐标", "lat", "lon", "coord"]
                if any(k in user_message.lower() for k in kw) or re.search(r"[（(]\s*[-+]?\d+(?:\.\d*)?\s*[,，、/]\s*[-+]?\d+(?:\.\d*)?\s*[）)]", user_message):
                    has_explicit_coord = True
            if not has_explicit_coord and not same_entity_custom_coordinates:
                try:
                    ctx_res = manager.oilfield_linker.evaluate_context(entity_id=str(of_id_slot.value))
                    if ctx_res and ctx_res.default_coordinates:
                        if "oilfield_coordinates" not in new_slots:
                            new_slots["oilfield_coordinates"] = Slot("oilfield_coordinates")
                        new_slots["oilfield_coordinates"].value = ctx_res.default_coordinates
                        new_slots["oilfield_coordinates"].status = "valid"
                        new_slots["oilfield_coordinates"].source = "oilfield_default"
                except Exception as exc:
                    logger.debug("Failed to populate oilfield default coordinates for %s: %s", of_id_slot.value, exc)

        return None, pipeline_res

    def _commit_turn_transaction(
        self,
        ctx: DialogueContext,
        turn_ctx: dict,
        pipeline_res: Any,
    ) -> dict:
        manager = self.manager
        request_id = turn_ctx["request_id"]
        old_phase = turn_ctx["old_phase"]
        new_slots = turn_ctx["new_slots"]
        new_unresolved = turn_ctx["new_unresolved"]
        expected_version = turn_ctx["expected_version"]
        merged_updates = turn_ctx["merged_updates"]
        state_before_turn = turn_ctx["state_before_turn"]
        proposed_pending_rov = turn_ctx["proposed_pending_rov"]

        apply_plan = getattr(pipeline_res, "apply_plan", None)
        field_defs = getattr(pipeline_res, "field_defs", [])
        payload_mutation_failed = getattr(pipeline_res, "payload_mutation_failed", False)

        proposed_mode = manager.mode
        if merged_updates.get("emergency_mode") is True:
            proposed_mode = "emergency"
        elif merged_updates.get("emergency_mode") is False:
            proposed_mode = "normal"

        changed_fields = set()
        for k, v in merged_updates.items():
            if k not in ("emergency_mode", "rov_description", "__clear_oilfield_name", "__clear_pending_oilfield") and v is not None and v != "":
                old_val = manager.slot_store.slots.get(k).value if manager.slot_store.slots.get(k) else None
                if old_val != v:
                    changed_fields.add(k)

        proposed_whitelist = {item for item in manager._soft_whitelist if item[0] not in changed_fields}

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
            dynamic_keys = manager._dynamic_allowed_schema_keys(field_defs)
            skip_keys = set(apply_plan.normalized_schema_keys) - dynamic_keys
        manager._normalize_and_validate_in_transaction(new_slots, curr_task_type_key, skip_schema_keys=skip_keys)

        curr_task_type_key = new_slots.get("task_type_key").value if (new_slots.get("task_type_key") and new_slots.get("task_type_key").status == "valid") else None

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
            existing_valid = (
                task_id_slot
                and task_id_slot.status == "valid"
                and task_id_slot.value is not None
                and validate_task_id_for_task_type(str(task_id_slot.value), curr_task_type_key, manager.kb.task_schemas)
                and task_id_slot.source == "auto_reserved"
            )
            if not existing_valid:
                try:
                    preview_id = manager.builder.preview_task_id(curr_task_type_key)
                except IdReservationError:
                    raise
                except Exception as _preview_err:
                    logger.warning("[DM] non-fatal preview_task_id failed for %s: %s", curr_task_type_key, _preview_err)
                    preview_id = None
                if preview_id is not None:
                    if "task_id" not in new_slots:
                        new_slots["task_id"] = Slot("task_id")
                    new_slots["task_id"].candidate_value = preview_id
                    new_slots["task_id"].status = "candidate"
                    new_slots["task_id"].source = "auto_preview"
                    new_slots["task_id"].value = None
                    new_slots["task_id"].raw_value = None
                    new_slots["task_id"].value_type = "string"
                    new_slots["task_id"].validation_error = None

        proposed_phase = manager.phase

        if curr_task_type_key:
            required_schema = manager.builder.get_schema(curr_task_type_key, proposed_mode)
            user_req_schema = [f for f in required_schema if f.get("type") not in ("auto", "fixed")]
            built = manager.slot_store.get_built_json()
            missing = manager.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: manager.builder.resolve_allowed_values(
                    field,
                    curr_task_type_key,
                    manager.task_state,
                ),
            )
            manager._last_missing = missing
            cand_missing = [f for f in required_schema if f.get("type") not in ("auto", "fixed") and (not new_slots.get(f["key"]) or new_slots[f["key"]].status != "valid" or new_slots[f["key"]].value is None)]
        else:
            built = {}
            missing = [{"key": "task_type", "label": "任务类型", "type": "string",
                        "allowed_values": manager.kb.get_all_task_type_values()}]
            manager._last_missing = missing
            cand_missing = missing

        manager._last_built_json = built

        if old_phase == "done" or (curr_task_type_key and not cand_missing):
            intent_id_slot = new_slots.get("intent_id")
            if old_phase == "done" or not intent_id_slot or intent_id_slot.status != "valid" or not intent_id_slot.value:
                today = get_current_datetime().strftime("%Y%m%d")
                from src.dispatch.task_intent_builder import get_task_dir
                task_dir = get_task_dir(create=False)
                from src.dispatch.id_sequence import next_daily_id
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

        manager.slot_store.commit_transaction(
            new_slots,
            new_unresolved,
            request_id=request_id,
            expected_version=expected_version,
        )
        manager._pending_referential_candidates = []

        if old_phase == "done":
            manager.final_result = None
            manager.ros2_dispatch = None
            manager._pending_published_intent = None

        manager.mode = proposed_mode
        manager._transition_phase(proposed_phase, reason="task_modified")
        manager._soft_whitelist = proposed_whitelist
        manager._pending_rov_candidates = proposed_pending_rov

        manager.task_state = manager.slot_store.get_task_state()
        if getattr(manager, "editing_slot", None) == "payload":
            context_changed = any(
                state_before_turn.get(key) != manager.task_state.get(key)
                for key in ("task_type_key", "equipment_type")
            )
            payload = manager.slot_store.slots.get("payload")
            payload_committed = (
                "payload" in merged_updates and not payload_mutation_failed
                and payload is not None and payload.status == "valid"
            )
            if context_changed or payload_committed:
                manager.editing_slot = None

        if curr_task_type_key:
            required_schema = manager.builder.get_schema(curr_task_type_key, manager.mode)
            user_req_schema = [f for f in required_schema if f.get("type") not in ("auto", "fixed")]
            built = manager.slot_store.get_built_json()
            missing = manager.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: manager.builder.resolve_allowed_values(
                    field,
                    curr_task_type_key,
                    manager.task_state,
                ),
            )
            manager._last_missing = missing
        else:
            built = {}
            missing = [{"key": "task_type", "label": "任务类型", "type": "string",
                        "allowed_values": manager.kb.get_all_task_type_values()}]
            manager._last_missing = missing
        manager._last_built_json = built
        manager.task_start_now = manager.is_start_time_near_now()

        return {
            "changed_fields": changed_fields,
            "missing": missing,
            "built": built,
            "curr_task_type_key": curr_task_type_key,
        }

    def _evaluate_and_generate_reply(
        self,
        ctx: DialogueContext,
        turn_ctx: dict,
        pipeline_res: Any,
        eval_ctx: dict,
    ) -> str:
        manager = self.manager
        user_message = turn_ctx["user_message"]
        turn_unresolved = turn_ctx["turn_unresolved"]
        merged_updates = turn_ctx["merged_updates"]
        state_before_turn = turn_ctx["state_before_turn"]
        changed_fields = eval_ctx["changed_fields"]
        missing = eval_ctx["missing"]
        built = eval_ctx["built"]

        pending_oilfield_reply = manager._build_pending_oilfield_reply()
        if pending_oilfield_reply:
            manager._transition_phase("collecting", reason="oilfield_clarification_needed")
            manager.conversation_history.append({"role": "user", "content": user_message})
            manager.conversation_history.append({"role": "assistant", "content": pending_oilfield_reply})
            return pending_oilfield_reply

        ALL_FIELDS = {
            "task_type", "start_time", "end_time", "cable_position", "cable_type", "start_point", "end_point",
            "water_depth", "equipment_family", "equipment_type", "equipment_name", "equipment_unit_id",
            "payload", "support_vessel", "oilfield_name",
            "oilfield_coordinates", "wellhead_id",
        }

        if not missing and manager.phase not in ("blocked_hard", "blocked_soft"):
            constraint_context = manager._run_constraint_check(ALL_FIELDS, purpose="preview")
        elif not missing and manager.phase == "blocked_soft":
            constraint_context = manager._run_constraint_check(changed_fields, purpose="preview")
        elif not missing and manager.phase == "blocked_hard":
            constraint_context = manager._run_constraint_check(ALL_FIELDS, purpose="preview")
        else:
            constraint_context = manager._run_constraint_check(changed_fields, purpose="interactive")

        if (
            not missing
            and manager.phase == "collecting"
            and constraint_context.get("type") == "none"
        ):
            manager._transition_phase(
                "confirming",
                reason="constraints_resolved_and_required_slots_complete",
            )

        knowledge_context = manager.kb.get_context_for_state(
            manager.task_state,
            include_runtime_state=not constraint_context.get("runtime_validation_deferred", False),
        )
        accepted_updates = self.get_committed_turn_updates(
            merged_updates,
            state_before_turn,
        )

        payload_mutation_failed = getattr(pipeline_res, "payload_mutation_failed", False)
        mutation_failure_result = getattr(pipeline_res, "mutation_failure_result", None)
        if payload_mutation_failed and mutation_failure_result:
            error = mutation_failure_result.get("error") or "载荷修改操作失败。"
            reply = self.ground_write_reply(
                "",
                accepted_updates=accepted_updates,
                unresolved_inputs=[*turn_unresolved, f"载荷操作失败：{error}"],
                missing_fields=missing,
                display_updates=self.get_committed_update_display_values(accepted_updates),
                task_state=manager.task_state,
                constraint_context=constraint_context,
                payload_configuration=self.reply_grounder.get_payload_configuration(),
            )
            manager.conversation_history.extend([
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": reply},
            ])
            return reply

        messages = build_responder_messages(
            task_state=manager.task_state,
            built_json=built,
            missing_fields=missing,
            mode=manager.mode,
            phase=manager.phase,
            knowledge_context=knowledge_context,
            constraint_context=constraint_context,
            conversation_history=manager.conversation_history,
            latest_user_message=user_message,
            ROV2type=manager.kb.ROV2type,
            support_task=manager.kb.get_supported_task(),
            slot_snapshot=manager.slot_store.get_slot_snapshot(),
            accepted_updates=accepted_updates,
            unresolved_inputs=turn_unresolved,
        )
        reply = manager._safe_llm_chat(messages, temperature=0.7, max_tokens=1500, role=ModelRole.TASK_RESPONDER)
        reply = manager._safe_llm_filter_reply(reply, role=ModelRole.FILTER_REPLY)
        reply = self.ground_write_reply(
            reply,
            accepted_updates=accepted_updates,
            unresolved_inputs=turn_unresolved,
            missing_fields=missing,
            display_updates=self.get_committed_update_display_values(accepted_updates),
            task_state=manager.task_state,
            constraint_context=constraint_context,
            payload_configuration=self.reply_grounder.get_payload_configuration(),
        )
        reply = manager._ensure_constraint_details(reply, constraint_context)

        manager.conversation_history.append({"role": "user", "content": user_message})
        manager.conversation_history.append({"role": "assistant", "content": reply})
        return reply


    def resolve_pending_oilfield_confirmation(
        self,
        user_message: str,
        request_id: str = "req_default",
        pending_action: str | None = None,
        subject_text: str | None = None,
    ) -> str | None:
        return self.oilfield_confirmation.resolve_pending_oilfield_confirmation(
            user_message,
            request_id=request_id,
            pending_action=pending_action,
            subject_text=subject_text,
        )

    def build_pending_oilfield_reply(self) -> str | None:
        return self.oilfield_confirmation.build_pending_oilfield_reply()

    def top_pending_oilfield_candidate(self, user_message: str = "") -> dict | None:
        return self.oilfield_confirmation.top_pending_oilfield_candidate(user_message)

    def user_confirmed_oilfield(self, message: str) -> bool:
        return self.oilfield_confirmation.user_confirmed_oilfield(message)

    def user_cancelled_oilfield(self, message: str) -> bool:
        return self.oilfield_confirmation.user_cancelled_oilfield(message)

    def process_referential_carryover(self, user_message: str) -> str:
        return self.oilfield_confirmation.process_referential_carryover(user_message)

    def link_oilfield_update_in_transaction(
        self,
        updates: dict,
        new_slots: dict,
        user_message: str = "",
        extracted_oilfield: str | None = None,
    ) -> dict:
        return self.oilfield_confirmation.link_oilfield_update_in_transaction(
            updates,
            new_slots,
            user_message=user_message,
            extracted_oilfield=extracted_oilfield,
        )

    def _apply_updates_in_transaction(
        self,
        updates: dict,
        new_slots: dict,
        allow_overwrite: bool = False,
        transition_slots_already_cleared: bool = False,
    ):
        return self.slot_transaction._apply_updates_in_transaction(
            updates=updates,
            new_slots=new_slots,
            allow_overwrite=allow_overwrite,
            transition_slots_already_cleared=transition_slots_already_cleared,
        )

    def _apply_normalized_plan_in_transaction(
        self,
        plan: NormalizationApplyPlan,
        new_slots: dict,
        allow_overwrite: bool = False,
        transition_from_task_type_key: str | None = None,
        transition_to_task_type_key: str | None = None,
        recompute_equipment: bool = True,
    ) -> None:
        return self.slot_transaction._apply_normalized_plan_in_transaction(
            plan=plan,
            new_slots=new_slots,
            allow_overwrite=allow_overwrite,
            transition_from_task_type_key=transition_from_task_type_key,
            transition_to_task_type_key=transition_to_task_type_key,
            recompute_equipment=recompute_equipment,
        )

    @staticmethod
    def _apply_slot_update_in_transaction(
        key: str,
        value: Any,
        new_slots: dict,
        allow_overwrite: bool,
    ) -> None:
        return SlotTransactionManager._apply_slot_update_in_transaction(
            key=key,
            value=value,
            new_slots=new_slots,
            allow_overwrite=allow_overwrite,
        )

    def _normalize_and_validate_in_transaction(
        self,
        new_slots: dict,
        task_type_key: str | None,
        skip_schema_keys: set[str] | frozenset[str] | None = None,
    ):
        return self.slot_transaction._normalize_and_validate_in_transaction(
            new_slots=new_slots,
            task_type_key=task_type_key,
            skip_schema_keys=skip_schema_keys,
        )

    # 别名兼容
    apply_updates_in_transaction = _apply_updates_in_transaction
    apply_normalized_plan_in_transaction = _apply_normalized_plan_in_transaction
    apply_slot_update_in_transaction = _apply_slot_update_in_transaction
    normalize_and_validate_in_transaction = _normalize_and_validate_in_transaction

    # ---------------------------------------------------------
    # 显式委托方法定义：避免隐式 __getattr__ 反射乒乓与无限递归
    # ---------------------------------------------------------

    # Equipment Cascade 显式委托
    def auto_collapse_robot_cascade(self, *args, **kwargs) -> Any:
        return self.equipment_cascade.auto_collapse_robot_cascade(*args, **kwargs)

    def project_equipment_updates_for_evaluation(self, *args, **kwargs) -> Any:
        return self.equipment_cascade.project_equipment_updates_for_evaluation(*args, **kwargs)

    def scope_confirmed_recommendation(self, *args, **kwargs) -> Any:
        return self.equipment_cascade.scope_confirmed_recommendation(*args, **kwargs)

    def scope_visible_ordinal_selections(self, *args, **kwargs) -> Any:
        return self.equipment_cascade.scope_visible_ordinal_selections(*args, **kwargs)

    @staticmethod
    def source_for_resolution_method(resolution_method: str | None) -> str:
        return EquipmentCascadeResolver.source_for_resolution_method(resolution_method)

    def handle_equipment_updates_in_transaction(self, *args, **kwargs) -> Any:
        return self.equipment_cascade.handle_equipment_updates_in_transaction(*args, **kwargs)

    # Task Transition 显式委托
    def build_task_transition_state(self, *args, **kwargs) -> Any:
        return self.task_transition.build_task_transition_state(*args, **kwargs)

    def clear_non_inherited_transition_slots(self, *args, **kwargs) -> Any:
        return self.task_transition.clear_non_inherited_transition_slots(*args, **kwargs)

    def merge_task_transition_extractions(self, *args, **kwargs) -> Any:
        return self.task_transition.merge_task_transition_extractions(*args, **kwargs)

    def normalize_transition_discovery_candidates(self, *args, **kwargs) -> Any:
        return self.task_transition.normalize_transition_discovery_candidates(*args, **kwargs)

    def record_task_type_update_error(self, *args, **kwargs) -> Any:
        return self.task_transition.record_task_type_update_error(*args, **kwargs)

    def resolve_task_type_target(self, *args, **kwargs) -> Any:
        return self.task_transition.resolve_task_type_target(*args, **kwargs)

    def resolve_task_type_update_context(self, *args, **kwargs) -> Any:
        return self.task_transition.resolve_task_type_update_context(*args, **kwargs)

    def task_selector_updates_from_extraction(self, *args, **kwargs) -> Any:
        return self.task_transition.task_selector_updates_from_extraction(*args, **kwargs)

    def task_transition_shared_field_keys(self, *args, **kwargs) -> Any:
        return self.task_transition.task_transition_shared_field_keys(*args, **kwargs)

    def handle_rov_description_in_transaction(self, *args, **kwargs) -> Any:
        return self.task_transition.handle_rov_description_in_transaction(*args, **kwargs)

    def handle_task_type_update_in_transaction(self, *args, **kwargs) -> Any:
        return self.task_transition.handle_task_type_update_in_transaction(*args, **kwargs)

    def build_post_update_evaluation_context(self, *args, **kwargs) -> Any:
        return self.task_transition.build_post_update_evaluation_context(*args, **kwargs)

    def dynamic_allowed_schema_keys(self, *args, **kwargs) -> Any:
        return self.task_transition.dynamic_allowed_schema_keys(*args, **kwargs)

    # Oilfield Confirmation 显式委托
    @staticmethod
    def merge_referential_candidates(extraction: dict, candidates: list[dict]) -> dict:
        return OilfieldConfirmationHandler.merge_referential_candidates(extraction, candidates)

    # 下划线别名映射（保持与旧代码及测试向下兼容）
    _auto_collapse_robot_cascade = auto_collapse_robot_cascade
    _project_equipment_updates_for_evaluation = project_equipment_updates_for_evaluation
    _scope_confirmed_recommendation = scope_confirmed_recommendation
    _scope_visible_ordinal_selections = scope_visible_ordinal_selections
    _source_for_resolution_method = source_for_resolution_method
    _handle_equipment_updates_in_transaction = handle_equipment_updates_in_transaction
    _handle_rov_description_in_transaction = handle_rov_description_in_transaction
    _handle_task_type_update_in_transaction = handle_task_type_update_in_transaction
