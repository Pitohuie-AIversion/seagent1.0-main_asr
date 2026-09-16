"""
src/dialogue_snapshot.py - 会话历史快照导出、反序列化校验与状态恢复管理器

职责：
1. 导出会话状态快照 (export_snapshot)；
2. 跨会话隔离原子恢复快照 (load_snapshot)；
3. 快照 schema 校验、模式迁移 (v1扁平到v2结构化) 与一致性验证 (_load_snapshot_in_place)；
4. 候选管理器运行时状态向活动管理器原子提交 (_commit_snapshot_runtime_state)。
"""

from __future__ import annotations

import copy
import json
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import src.task_intent_builder as _ti_builder_module
from src.task_intent_builder import validate_task_id_for_task_type
from .model_profile import is_session_state_v2_enabled
from .session_state import (
    VALID_TASK_MODES,
    VALID_PHASES,
    session_state_from_legacy_snapshot,
    is_session_state_v2_active,
)
from .slot_store import (
    Slot,
    SlotStore,
    SnapshotValidationError,
    normalize_slot_value_type,
)
from .id_sequence import validate_intent_id, validate_task_id, next_daily_id
from .simulated_time import get_current_datetime

logger = logging.getLogger("src.dialogue_manager")


class DialogueSnapshotManager:
    """会话快照持久化、模式迁移与隔离恢复管理器"""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def _is_v2_active(self, explicit: Optional[bool] = None) -> bool:
        return is_session_state_v2_active(explicit)

    def export_snapshot(self, session_state_v2_active: Optional[bool] = None) -> dict:
        """导出 Issue #10 会话状态快照。"""
        dm = self.manager
        if self._is_v2_active(session_state_v2_active):
            _ = dm._build_session_state_contract()

        return {
            "snapshot_version": 2,
            "session_id": dm.session_id,
            "conversation_history": copy.deepcopy(dm.conversation_history),
            "phase": dm.phase,
            "mode": dm.mode,
            "dialogue_mode": dm.dialogue_mode,
            "last_mode_transition": copy.deepcopy(dm.last_mode_transition),
            "mode_transition_history": copy.deepcopy(dm.mode_transition_history),
            "control_state": dm.control_state,
            "last_control_request": copy.deepcopy(dm.last_control_request),
            "slot_store": dm.slot_store.export_snapshot(),
            "task_state": copy.deepcopy(dm.task_state),
        }

    def load_snapshot(self, snapshot: dict, session_state_v2_active: Optional[bool] = None) -> None:
        """原子恢复旧版扁平快照和 snapshot_version=2 完整快照。"""
        dm = self.manager
        v2_active = self._is_v2_active(session_state_v2_active)

        with dm._session_lock:
            candidate = dm.__class__(
                llm=dm.llm,
                kb=dm.kb,
                session_id=dm.session_id,
            )
            # Legacy restore does not own this field, so preserve its existing
            # value unless the SessionState contract explicitly replaces it.
            candidate.awaiting_final_confirm = dm.awaiting_final_confirm
            self.load_snapshot_in_place(
                candidate,
                snapshot,
                session_state_v2_active=v2_active,
            )
            self.commit_snapshot_runtime_state(dm, candidate)
            dm._run_session_state_shadow_check(checkpoint="load_snapshot")

    @staticmethod
    def commit_snapshot_runtime_state(target: Any, candidate: Any) -> None:
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
            setattr(target, field_name, getattr(candidate, field_name))

    def load_snapshot_in_place(
        self,
        candidate: Any,
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
            candidate.session_id = str(snapshot["session_id"])

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
            candidate_store = SlotStore.from_snapshot(snapshot["slot_store"], candidate.kb)

        if candidate_store is None:
            # 兼容没有 snapshot_version/slot_store 的旧历史记录。
            legacy_state = snapshot.get("task_state", {})
            if not isinstance(legacy_state, dict):
                raise ValueError("task_state must be a dictionary")
            candidate_store = SlotStore(candidate.kb)
            task_type_key = legacy_state.get("task_type_key")
            if task_type_key:
                required_fields = candidate.builder.get_schema(task_type_key, mode)
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
            if not validate_task_id_for_task_type(str(cand_task_id), cand_task_type_key, candidate.kb.task_schemas):
                raise SnapshotValidationError(f"task_id {cand_task_id} does not match task_type_key {cand_task_type_key} in candidate snapshot")
            if cand_internal is None or cand_task_type_key is None:
                raise SnapshotValidationError("v2 candidate snapshot with valid task_id must contain internal_id, task_id, and task_type_key simultaneously")

        # 候选 SlotStore 完整校验通过后再一次性替换，避免半恢复状态泄漏。
        candidate.conversation_history = copy.deepcopy(conversation_history)
        candidate.slot_store = candidate_store
        candidate.task_state = candidate.slot_store.get_task_state()

        if session_state_v2_active and contract_state is not None:
            candidate._apply_session_state_contract(contract_state)
        else:
            candidate.mode = mode
            candidate._switch_dialogue_mode(
                dialogue_mode,
                source="snapshot_restore",
                reason="restore validated snapshot state",
                restore_transition_state=(last_mode_transition, validated_history),
            )
            candidate._set_execution_control_state(
                control_state,
                last_control_request,
                source="snapshot_restore",
                reason="restore validated snapshot state",
            )

        candidate.final_result = None
        candidate.task_start_now = False
        # 清空阻塞与白名单，重新构建缓存
        candidate._blocking_violations = []
        candidate._soft_whitelist = set()
        candidate._hard_refusal_counts = {}
        candidate._pending_rov_candidates = []
        candidate._rebuild_cache(commit_derived=False)

        # ── 快照 Intent ID 校验与 done 阶段完整性校验 ──
        intent_slot = candidate.slot_store.slots.get("intent_id")
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

                                snap_internal_slot = candidate.slot_store.slots.get("internal_id")
                                snap_internal = snap_internal_slot.value if (snap_internal_slot and snap_internal_slot.status == "valid") else None

                                snap_task_id_slot = candidate.slot_store.slots.get("task_id")
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
                                elif not _ti_builder_module.validate_task_intent(_data, candidate.kb.task_schemas):
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
                candidate._transition_phase(
                    "done",
                    source="snapshot_restore",
                    reason="validated published task file",
                )
                candidate.final_result = _loaded_intent
            else:
                candidate._transition_phase(
                    "collecting",
                    source="snapshot_restore",
                    reason="published task validation failed",
                )
                today = get_current_datetime().strftime("%Y%m%d")
                task_dir = _ti_builder_module.get_task_dir(create=False)
                new_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])
                new_slots = candidate.slot_store.clone_slots()
                if "intent_id" not in new_slots:
                    new_slots["intent_id"] = Slot("intent_id")
                new_slots["intent_id"].value = new_id
                new_slots["intent_id"].value_type = "string"
                new_slots["intent_id"].status = "valid"
                new_slots["intent_id"].source = "auto"
                candidate.slot_store.commit_transaction(new_slots, candidate.slot_store.unresolved)
                candidate.task_state = candidate.slot_store.get_task_state()
                candidate._last_built_json = candidate.slot_store.get_built_json()
        else:
            candidate._transition_phase(
                phase,
                source="snapshot_restore",
                reason="restore validated snapshot phase",
            )
            if not is_valid_id:
                today = get_current_datetime().strftime("%Y%m%d")
                task_dir = _ti_builder_module.get_task_dir(create=False)
                new_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])
                new_slots = candidate.slot_store.clone_slots()
                if "intent_id" not in new_slots:
                    new_slots["intent_id"] = Slot("intent_id")
                new_slots["intent_id"].value = new_id
                new_slots["intent_id"].value_type = "string"
                new_slots["intent_id"].status = "valid"
                new_slots["intent_id"].source = "auto"
                candidate.slot_store.commit_transaction(new_slots, candidate.slot_store.unresolved)
                candidate.task_state = candidate.slot_store.get_task_state()
                candidate._last_built_json = candidate.slot_store.get_built_json()

        if session_state_v2_active:
            _ = candidate._build_session_state_contract()
