"""
src/handlers/task_commit.py - 任务确认与持久化提交生命周期处理器

职责：
1. 已归档任务（done）防护（杜绝重复发布、杜绝非法就地篡改）；
2. 最终确认阶段（confirming）交互决策与发布全流程（_handle_final_publish_confirmation）；
3. 原子发布持久化提交（TaskIntentBuilder, 锁机制与事务回滚隔离）；
4. 会话历史快照恢复与安全校验（_load_snapshot_in_place）；
5. 用户端数据安全脱敏（sanitize_user_facing_json）。
"""

from __future__ import annotations

import copy
import json
import logging
import math
from typing import Any, Optional

from .base import BaseDialogueHandler, DialogueContext, HandlerResult
from ..slot_store import Slot, SnapshotValidationError, ValidationAcknowledgement
from ..task_intent_builder import TaskIntentBuilder
from ..id_sequence import validate_intent_id, validate_task_id, validate_task_id_for_task_type, next_daily_id
from ..exceptions import TaskPersistenceError, IntentIdConflict, IdReservationError, TaskRollbackError
from ..simulated_time import get_current_datetime
from ..session_state import VALID_TASK_MODES, VALID_PHASES, session_state_from_legacy_snapshot
from .. import task_intent_builder as _ti_builder_module

logger = logging.getLogger("src.dialogue_manager")

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


class TaskCommitHandler(BaseDialogueHandler):
    """任务确认与持久化提交生命周期处理器"""

    def can_handle(self, ctx: DialogueContext) -> bool:
        """
        判断是否处于提交相关阶段：
        1. phase == 'done' 时的重复确认或原地修改请求；
        2. phase == 'confirming' 时的最终发布确认或取消/修改；
        3. awaiting_final_confirm 标志位激活时。
        """
        phase = self.manager.phase
        user_message = ctx.user_message

        if phase == "done":
            if (
                self.manager._is_confirmation_only(user_message)
                or self.manager._is_final_publish_confirmation(user_message)
                or self.manager._user_requested_modification(user_message)
            ):
                return True

        if phase == "confirming" or getattr(self.manager, "awaiting_final_confirm", False):
            if (
                self.manager._is_confirmation_only(user_message)
                or self.manager._is_final_publish_confirmation(user_message)
            ):
                return True

        return False

    def handle(self, ctx: DialogueContext) -> HandlerResult:
        """执行已完成任务防护或最终确认发布"""
        phase = self.manager.phase
        user_message = ctx.user_message
        request_id = ctx.request_id

        # 1. phase == "done" 防护
        if phase == "done":
            # 重复确认防护
            if (
                self.manager._is_confirmation_only(user_message)
                or self.manager._is_final_publish_confirmation(user_message)
            ):
                self.manager._switch_dialogue_mode(
                    "task_collection",
                    source="user_confirmation",
                    reason="已发布任务重复确认",
                )
                intent_id = self.manager.task_state.get("intent_id") or self.manager._last_built_json.get("intent_id")
                intent_detail = f"（intent_id: {intent_id}）" if intent_id else ""
                reply = f"任务已发布成功{intent_detail}，无需重复发布。"
                self.manager.conversation_history.append({"role": "user", "content": user_message})
                self.manager.conversation_history.append({"role": "assistant", "content": reply})
                return HandlerResult.success(reply=reply)

            # 就地篡改防护
            if self.manager._user_requested_modification(user_message):
                self.manager._switch_dialogue_mode(
                    "task_collection",
                    source="user_modification",
                    reason="已发布任务原地修改拒绝",
                )
                intent_id = self.manager.task_state.get("intent_id") or (
                    self.manager._last_built_json.get("intent_id")
                    if isinstance(self.manager._last_built_json, dict)
                    else None
                )
                intent_detail = f"（任务ID: {intent_id}）" if intent_id else ""
                reply = f"当前任务已正式确认发布{intent_detail}并归档，无法就地修改参数。如需调整，请点击“重新开始”创建新任务，或提交工单变更申请。"
                self.manager.conversation_history.append({"role": "user", "content": user_message})
                self.manager.conversation_history.append({"role": "assistant", "content": reply})
                return HandlerResult.success(reply=reply)

        # 2. confirming 状态下的最终确认
        if phase == "confirming" or getattr(self.manager, "awaiting_final_confirm", False):
            if self.manager._is_final_publish_confirmation(user_message):
                reply = self.manager._handle_final_publish_confirmation(user_message, request_id)
                return HandlerResult.success(reply=reply)
            elif self.manager._is_confirmation_only(user_message):
                reply = "当前任务尚未发布。如确认无误，请回复‘确认发布’；如需调整，可直接说明要修改的参数。"
                self.manager.conversation_history.append({"role": "user", "content": user_message})
                self.manager.conversation_history.append({"role": "assistant", "content": reply})
                return HandlerResult.success(reply=reply)

        return HandlerResult.not_handled()

    def _handle_final_publish_confirmation(self, user_message: str, request_id: str) -> str:
        """confirming 阶段的唯一正式确认发布处理。

        使用已有 SlotStore 内的 valid intent_id 关联并发布文件。
        不重新调用 extractor，不修改 SlotStore。
        """
        dm = self.manager
        if dm.phase != "confirming":
            reply = "当前没有处于等待确认状态的任务。"
            dm.conversation_history.append({"role": "user", "content": user_message})
            dm.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        prev_phase = dm.phase
        prev_snap = dm.slot_store.export_snapshot()
        prev_whitelist = copy.deepcopy(dm._soft_whitelist)
        prev_missing = copy.deepcopy(dm._last_missing)
        prev_pending_rov = copy.deepcopy(dm._pending_rov_candidates)
        prev_blocking_violations = copy.deepcopy(dm._blocking_violations)
        prev_hist = list(dm.conversation_history)
        prev_task_start_now = dm.task_start_now

        task_type_key = dm.task_state.get("task_type_key")
        cand_state = copy.deepcopy(dm.task_state)
        cand_built = copy.deepcopy(dm._last_built_json)
        is_task_now = dm.is_start_time_near_now()
        unit_id = cand_state.get("equipment_unit_id") or cand_built.get("equipment_unit_id")
        if not unit_id and dm.slot_store.slots.get("equipment_unit_id"):
            unit_slot = dm.slot_store.slots.get("equipment_unit_id")
            if unit_slot and unit_slot.status == "valid":
                unit_id = unit_slot.value
        # 运行时设备可用性重新校验 (Issue #12)
        # 仅即时执行任务才在发布瞬间核验当前单机遥测时效与实时可用性。未来任务延后至执行前动态校验。
        if unit_id and is_task_now:
            runtime_res = dm.kb.state_info.check_runtime_availability(str(unit_id))
            if not runtime_res.get("available"):
                # 遥测过期表示发布时无法确认当前就绪性，不是任务本身触发了
                # 硬约束。保持 confirming 并拒绝本次发布，等遥测刷新后可直接重试。
                # 离线、忙碌、状态缺失/损坏等真实不可用性仍按硬阻断处理。
                if runtime_res.get("reason_code") != "STATE_EXPIRED":
                    dm._transition_phase("blocked_hard", reason="runtime_equipment_unavailable")
                reply = runtime_res.get("message") or f"无法发布任务：机器人 {unit_id} 当前不可用。"
                dm.conversation_history.append({"role": "user", "content": user_message})
                dm.conversation_history.append({"role": "assistant", "content": reply})
                return reply

        # 最终约束全量检查（包含 C020 设备总体状态与 C019 遥测新鲜度核验）
        val_res = dm._refresh_validation(purpose="publish")
        all_violations = val_res.violations
        has_hard = dm.validator.has_hard_violations(all_violations) or val_res.overall_status == "validation_error"
        unwhitelisted_soft = [v for v in all_violations if v.severity == "soft" and not dm._is_whitelisted(v)]

        # 检查缺失（排除 auto 和 fixed 等由系统自动管理的字段，如 task_id）
        if task_type_key:
            req_schema = dm.builder.get_schema(task_type_key, dm.mode)
            user_req_schema = [f for f in req_schema if f.get("type") not in ("auto", "fixed")]
            missing = dm.slot_store.get_missing_slots(user_req_schema)
        else:
            missing = [{"key": "task_type", "label": "任务类型"}]

        if missing or has_hard or unwhitelisted_soft:
            if has_hard:
                dm._transition_phase("blocked_hard", reason="publish_hard_constraint_detected")
                hard_violations = [v for v in all_violations if v.severity == "hard"]
                dm._blocking_violations = hard_violations
                if hard_violations:
                    reply = "\n".join(v.message for v in hard_violations)
                else:
                    reply = val_res.error.get("message") if (val_res and val_res.error) else "当前任务参数包含硬性约束冲突，无法发布。"
            elif unwhitelisted_soft:
                dm._transition_phase("blocked_soft", reason="publish_soft_warning_detected")
                dm._blocking_violations = unwhitelisted_soft
                reply = "\n".join(v.message for v in unwhitelisted_soft)
            else:
                dm._transition_phase("collecting", reason="publish_slots_missing")
                reply = "当前任务参数不满足发布条件，请补充或修正参数。"
            dm.conversation_history.append({"role": "user", "content": user_message})
            dm.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # 检查 intent_id 是否在 SlotStore/built_json 中有效存在 (Fail Closed)
        intent_id = cand_built.get("intent_id") or cand_state.get("intent_id")
        intent_slot = dm.slot_store.slots.get("intent_id")
        if not intent_id or not intent_slot or intent_slot.status != "valid" or not validate_intent_id(intent_slot.value):
            reply = "当前任务缺少唯一任务标识(intent_id)，无法完成确认发布。"
            dm.conversation_history.append({"role": "user", "content": user_message})
            dm.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        # 准备发布：正式且唯一预约任务业务编号（在跨进程锁内原子递增）。
        # reserve_task_id() 的返回值是唯一权威正式编号，必须覆盖任何草稿阶段的 preview。
        # 整个发布流程（reserve -> commit -> prepare -> staging -> publish）包含在单一 try...except 回滚保护块内。
        try:
            official_task_id = dm.builder.reserve_task_id(task_type_key)

            # 正式编号通过 SlotStore 单一事务写入工作副本，维持 SSOT 与乐观锁 (store_version / version)
            publish_slots = copy.deepcopy(dm.slot_store.slots)
            if "task_id" not in publish_slots:
                publish_slots["task_id"] = Slot("task_id")
            publish_slots["task_id"].value = official_task_id
            publish_slots["task_id"].status = "valid"
            publish_slots["task_id"].source = "auto_reserved"
            publish_slots["task_id"].candidate_value = None
            publish_slots["task_id"].raw_value = None
            publish_slots["task_id"].value_type = "string"
            publish_slots["task_id"].validation_error = None

            expected_version = dm.slot_store.version
            dm.slot_store.commit_transaction(
                publish_slots,
                list(dm.slot_store.unresolved),
                request_id=request_id,
                expected_version=expected_version,
            )

            # 提交后从 SlotStore 统一重新派生权威状态，绝对不手工篡改各缓存副本
            dm.task_state = dm.slot_store.get_task_state()
            dm._last_built_json = dm.slot_store.get_built_json()

            cand_state = dict(dm.task_state)
            cand_built = dict(dm._last_built_json)

            # TOCTOU 防线：在最终写盘发布前核对 state_version (仅即时任务需要防线)
            if unit_id and is_task_now and val_res and getattr(val_res, "state_snapshot", None):
                try:
                    current_state_snap = dm.kb.get_unit_state_snapshot(str(unit_id))
                    if current_state_snap.get("state_version") != val_res.state_snapshot.get("state_version"):
                        re_val_res = dm._refresh_validation(purpose="publish")
                        # 重新校验后执行全量门禁检查：
                        # validation_error / blocked_hard -> 阻断并回滚
                        if re_val_res.overall_status in ("blocked_hard", "validation_error"):
                            raise TaskPersistenceError(f"单机 {unit_id} 的状态遥测在确认发布过程中发生变更，触发阻断告警。")
                        elif re_val_res.overall_status == "blocked_soft":
                            valid_acks = dm._get_valid_acknowledgements(re_val_res)
                            unacked = [
                                v for v in (re_val_res.violations or [])
                                if getattr(v, "severity", "") == "soft" and not any(a.constraint_id == v.constraint_id for a in valid_acks)
                            ]
                            if unacked:
                                dm._transition_phase("blocked_soft", reason="telemetry_soft_warning_detected")
                                dm._blocking_violations = unacked
                                raise TaskPersistenceError(f"单机 {unit_id} 的状态遥测在确认发布过程中发生变更，触发未确认的软性告警。")
                        elif re_val_res.overall_status == "pending_runtime_validation" and dm.task_start_now:
                            raise TaskPersistenceError(f"单机 {unit_id} 的状态遥测在确认发布过程中发生变更，实时任务无法在缺乏遥测时发布。")
                        val_res = re_val_res
                except Exception as check_exc:
                    if isinstance(check_exc, TaskPersistenceError):
                        raise check_exc
                    raise TaskPersistenceError(f"发布前单机状态复核失败 (fail closed): {check_exc}") from check_exc

            # 准备发布：仅传入匹配当前 validation_result 的有效确认
            valid_acknowledgements = dm._get_valid_acknowledgements(val_res)
            ti_builder = TaskIntentBuilder(dm.kb)
            ti_json_artifact = ti_builder.prepare(
                task_state=cand_state,
                built_json=cand_built,
                mode=dm.mode,
                task_type_key=task_type_key,
                intent_id=intent_id,
                validation_result=val_res,
                validation_acknowledgements=valid_acknowledgements,
            )
            staging_file = ti_builder.create_staging(ti_json_artifact)

            expected_state_ver = val_res.state_snapshot.get("state_version") if (val_res and val_res.state_snapshot) else None
            if unit_id and expected_state_ver is not None and hasattr(dm.kb, "state_info") and hasattr(dm.kb.state_info, "guard_unit_state_version"):
                with dm.kb.state_info.guard_unit_state_version(str(unit_id), expected_state_ver):
                    ti_builder.publish_staging(staging_file, ti_json_artifact)
            else:
                ti_builder.publish_staging(staging_file, ti_json_artifact)
        except Exception as exc:
            # 回滚：包含 reserve, commit_transaction, prepare, create_staging, publish_staging 在内的全流程失败保护
            target_phase = dm.phase if dm.phase == "blocked_soft" else prev_phase
            target_blocking = copy.deepcopy(dm._blocking_violations) if dm.phase == "blocked_soft" else prev_blocking_violations
            dm._transition_phase(target_phase, reason="publish_rollback")
            dm.final_result = None

            rollback_failed = False
            rollback_err = None
            if prev_snap:
                try:
                    dm.slot_store.restore_snapshot(prev_snap)
                except Exception as rb_e:
                    rollback_failed = True
                    rollback_err = rb_e

            dm.task_state = dm.slot_store.get_task_state()
            dm._last_built_json = dm.slot_store.get_built_json()
            dm._soft_whitelist = prev_whitelist
            dm._pending_rov_candidates = prev_pending_rov
            dm._blocking_violations = target_blocking
            dm.conversation_history = prev_hist
            dm.task_start_now = prev_task_start_now

            dm._last_missing = prev_missing

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
        dm._transition_phase("done", reason="publish_success")
        dm.task_state = dm.slot_store.get_task_state()
        dm._last_built_json = dm.slot_store.get_built_json()
        dm.final_result = ti_json_artifact
        dm.task_start_now = dm.is_start_time_near_now()

        user_facing_built = sanitize_user_facing_json(cand_built)
        if dm.task_start_now:
            reply = (f"✅ 信息收集完成，当前为【立即执行任务】，任务已生成并下发。\n"
                     f"{json.dumps(user_facing_built, ensure_ascii=False, indent=2)}")
        else:
            reply = (f"✅ 信息收集完成，当前为【未来规划任务】，已加入计划池。\n"
                     f"{json.dumps(user_facing_built, ensure_ascii=False, indent=2)}")
        dm.conversation_history.append({"role": "user", "content": user_message})
        dm.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    def _load_snapshot_in_place(
        self,
        snapshot: dict,
        *,
        session_state_v2_active: bool,
    ) -> None:
        """Restore and validate a snapshot on an isolated candidate manager."""
        dm = self.manager
        if session_state_v2_active:
            contract_state = session_state_from_legacy_snapshot(snapshot)
        else:
            contract_state = None

        if not isinstance(snapshot, dict):
            raise ValueError("History snapshot must be a dictionary")

        if "session_id" in snapshot and snapshot["session_id"] is not None:
            dm.session_id = str(snapshot["session_id"])

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
            if not isinstance(changed_at, str):
                raise ValueError("changed_at must be string")

            return {
                "from": from_m,
                "to": to_m,
                "source": str(item.get("source", "system")),
                "reason": str(item.get("reason", "")),
                "confidence": float(conf),
                "changed_at": changed_at,
            }

        validated_mode_history = [_val_trans(x) for x in mode_transition_history]

        raw_whitelist = snapshot.get("soft_whitelist", [])
        if not isinstance(raw_whitelist, list):
            raise ValueError("soft_whitelist must be a list")

        whitelist_tuples = []
        for item in raw_whitelist:
            if isinstance(item, list) and len(item) == 3:
                whitelist_tuples.append((str(item[0]), str(item[1]), str(item[2])))
            else:
                raise ValueError("Invalid soft_whitelist entry format")

        last_missing = snapshot.get("last_missing", [])
        if not isinstance(last_missing, list):
            raise ValueError("last_missing must be a list")

        pending_rov = snapshot.get("pending_rov_candidates", None)
        if pending_rov is not None and not isinstance(pending_rov, list):
            raise ValueError("pending_rov_candidates must be a list or None")

        task_state_data = snapshot.get("task_state", {})
        if not isinstance(task_state_data, dict):
            raise ValueError("task_state must be a dictionary")

        # 尝试恢复 slot_store（失败会抛出 SnapshotValidationError 并保证原子性）
        dm.slot_store.restore_snapshot(task_state_data)

        # 成功通过所有严格校验后，统一应用到管理器
        dm.mode = mode
        dm.conversation_history = copy.deepcopy(conversation_history)
        dm._soft_whitelist = set(whitelist_tuples)
        dm._last_missing = copy.deepcopy(last_missing)
        dm._pending_rov_candidates = copy.deepcopy(pending_rov)
        dm.task_state = dm.slot_store.get_task_state()
        dm._last_built_json = dm.slot_store.get_built_json()
        dm.task_start_now = dm.is_start_time_near_now()
        dm.dialogue_mode = dialogue_mode
        dm.control_state = control_state
        dm.mode_transition_history = validated_mode_history

        # snapshot 恢复后，重新对齐最新的外部环境约束
        task_type_key = dm.task_state.get("task_type_key")
        if task_type_key:
            _ = dm._refresh_validation(purpose="interactive")
        else:
            dm.slot_store.validation_result = None

        # 迁移并更新 intent_id (若存在)
        raw_intent_id = dm.slot_store.slots.get("intent_id")
        intent_id_val = getattr(raw_intent_id, "value", None)
        is_valid_id = intent_id_val and validate_intent_id(intent_id_val)

        if phase == "done":
            validated = False
            _loaded_intent = None
            if is_valid_id:
                try:
                    pub_file = _ti_builder_module.get_published_filepath(intent_id_val)
                    if pub_file and pub_file.exists():
                        with open(pub_file, "r", encoding="utf-8") as _f:
                            _data = json.load(_f)

                        task_req = _data.get("task_request") if isinstance(_data, dict) else None
                        if isinstance(_data, dict) and isinstance(task_req, dict):
                            _file_intent_id = task_req.get("intent_id")
                            _file_internal_id = task_req.get("internal_id")
                            _file_task_id = task_req.get("task_id")
                            _file_task_type = task_req.get("task_type")

                            is_snap_v2 = isinstance(snapshot.get("task_intent"), dict) and snapshot["task_intent"].get("schema_version") == 2
                            is_file_v2 = _data.get("schema_version") == 2
                            snap_ti = snapshot.get("task_intent") or {}
                            snap_req = snap_ti.get("task_request") or {}
                            snap_internal = snap_req.get("internal_id")
                            snap_task_id = snap_req.get("task_id")

                            if _file_intent_id and _file_task_type:
                                if is_snap_v2 != is_file_v2:
                                    logger.warning("[load_snapshot] schema version mismatch: is_snap_v2=%s vs is_file_v2=%s", is_snap_v2, is_file_v2)
                                elif _file_intent_id != intent_id_val:
                                    logger.warning("[load_snapshot] intent_id mismatch in final file")
                                elif is_snap_v2 and (not _file_internal_id or snap_internal != _file_internal_id):
                                    logger.warning("[load_snapshot] internal_id mismatch or missing in final file: snap=%s vs file=%s", snap_internal, _file_internal_id)
                                elif is_snap_v2 and (not _file_task_id or snap_task_id != _file_task_id):
                                    logger.warning("[load_snapshot] task_id mismatch or missing in final file: snap=%s vs file=%s", snap_task_id, _file_task_id)
                                elif not _ti_builder_module.validate_task_intent(_data, dm.kb.task_schemas):
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
                dm._transition_phase(
                    "done",
                    source="snapshot_restore",
                    reason="validated published task file",
                )
                dm.final_result = _loaded_intent
            else:
                dm._transition_phase(
                    "collecting",
                    source="snapshot_restore",
                    reason="published task validation failed",
                )
                today = get_current_datetime().strftime("%Y%m%d")
                task_dir = _ti_builder_module.get_task_dir(create=False)
                new_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])
                new_slots = dm.slot_store.clone_slots()
                if "intent_id" not in new_slots:
                    new_slots["intent_id"] = Slot("intent_id")
                new_slots["intent_id"].value = new_id
                new_slots["intent_id"].value_type = "string"
                new_slots["intent_id"].status = "valid"
                new_slots["intent_id"].source = "auto"
                dm.slot_store.commit_transaction(new_slots, dm.slot_store.unresolved)
                dm.task_state = dm.slot_store.get_task_state()
                dm._last_built_json = dm.slot_store.get_built_json()
        else:
            dm._transition_phase(
                phase,
                source="snapshot_restore",
                reason="restore validated snapshot phase",
            )
            if not is_valid_id:
                today = get_current_datetime().strftime("%Y%m%d")
                task_dir = _ti_builder_module.get_task_dir(create=False)
                new_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])
                new_slots = dm.slot_store.clone_slots()
                if "intent_id" not in new_slots:
                    new_slots["intent_id"] = Slot("intent_id")
                new_slots["intent_id"].value = new_id
                new_slots["intent_id"].value_type = "string"
                new_slots["intent_id"].status = "valid"
                new_slots["intent_id"].source = "auto"
                dm.slot_store.commit_transaction(new_slots, dm.slot_store.unresolved)
                dm.task_state = dm.slot_store.get_task_state()
                dm._last_built_json = dm.slot_store.get_built_json()

        if session_state_v2_active:
            _ = dm._build_session_state_contract()
