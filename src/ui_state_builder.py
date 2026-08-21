"""
ui_state_builder.py

统一 UI 状态构建模块。

从 DialogueManager 的当前状态构建前端所需的完整 ui_state 字典。
作为 /api/chat、/api/session/state、/api/history/load 的唯一状态构建来源。
"""

from __future__ import annotations

from contextlib import nullcontext
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .dialogue_manager import DialogueManager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# actions & read_only 计算
# ─────────────────────────────────────────────────────────────────────────────

_PHASE_ACTIONS: dict = {
    "collecting": {
        "can_send": True,
        "can_modify": True,
        "can_confirm": False,
        "can_ignore_soft_warning": False,
        "can_publish": False,
        "can_cancel": True,
    },
    "confirming": {
        "can_send": True,
        "can_modify": True,
        "can_confirm": True,
        "can_ignore_soft_warning": False,
        "can_publish": True,
        "can_cancel": True,
    },
    "blocked_soft": {
        "can_send": True,
        "can_modify": True,
        "can_confirm": False,
        "can_ignore_soft_warning": True,
        "can_publish": False,
        "can_cancel": True,
    },
    "blocked_hard": {
        "can_send": True,
        "can_modify": True,
        "can_confirm": False,
        "can_ignore_soft_warning": False,
        "can_publish": False,
        "can_cancel": True,
    },
    "done": {
        "can_send": True,
        "can_modify": False,
        "can_confirm": False,
        "can_ignore_soft_warning": False,
        "can_publish": False,
        "can_cancel": False,
    },
    "rejected": {
        "can_send": True,
        "can_modify": False,
        "can_confirm": False,
        "can_ignore_soft_warning": False,
        "can_publish": False,
        "can_cancel": False,
    },
}

_NORMAL_CHAT_ACTIONS: dict = {
    "can_send": True,
    "can_modify": False,
    "can_confirm": False,
    "can_ignore_soft_warning": False,
    "can_publish": False,
    "can_cancel": False,
}


def _compute_actions(phase: str, dialogue_mode: str) -> dict:
    """
    根据 phase 和 dialogue_mode 计算当前允许的操作集合。
    终态发布物仍保持只读，但不关闭对话输入；后端允许 READ/CLARIFY，
    后续 WRITE 则沿安全事务创建新草稿，不覆盖原发布物。
    """
    if phase in ("done", "rejected"):
        return dict(_PHASE_ACTIONS[phase])
    if dialogue_mode != "task_collection":
        return dict(_NORMAL_CHAT_ACTIONS)
    return dict(_PHASE_ACTIONS.get(phase, _PHASE_ACTIONS["collecting"]))


def _compute_read_only(phase: str, dialogue_mode: str = "task_collection") -> bool:
    """
    只要任务进入终态（done / rejected），任务对象均严格为只读。
    """
    return phase in ("done", "rejected")


# ─────────────────────────────────────────────────────────────────────────────
# slot 合并
# ─────────────────────────────────────────────────────────────────────────────

def _build_slots(
    manager: "DialogueManager",
    slot_snapshot: dict[str, Any] | None = None,
) -> list:
    """
    将 builder.get_schema() 与 slot_store.get_slot_snapshot() 合并。

    核心规则（P1-1 修复）：
    不再因为 dialogue_mode != "task_collection" 就直接清空 slots！
    只要 task_state 中存在 task_type_key，就返回任务槽位完整状态，确保知识问答期间已有任务视图不丢失。
    """
    task_type_key = None
    try:
        if hasattr(manager, "task_state") and isinstance(manager.task_state, dict):
            task_type_key = manager.task_state.get("task_type_key")
    except Exception:
        pass

    if not task_type_key:
        return []

    try:
        schema_fields = manager.builder.get_schema(task_type_key, manager.mode)
    except Exception as exc:
        logger.warning("build_frontend_ui_state: get_schema 失败 task_type_key=%r: %s", task_type_key, exc)
        return []

    if slot_snapshot is None:
        try:
            slot_snapshot = manager.slot_store.get_slot_snapshot()
        except Exception as exc:
            logger.warning("build_frontend_ui_state: get_slot_snapshot 失败: %s", exc)
            slot_snapshot = {}

    resolver = None
    try:
        if hasattr(manager.builder, "resolve_allowed_values"):
            def _make_resolver(builder, ttk, ts):
                def _resolver(field_def):
                    return builder.resolve_allowed_values(field_def, ttk, ts)
                return _resolver
            resolver = _make_resolver(manager.builder, task_type_key, manager.task_state)
    except Exception:
        pass

    result = []
    for field_def in schema_fields:
        key = field_def.get("key", "")
        ftype = field_def.get("type", "string")

        if ftype in ("auto", "fixed"):
            continue

        label_raw = field_def.get("label", key)
        if isinstance(label_raw, str):
            label = {"zh": label_raw, "en": label_raw}
        elif isinstance(label_raw, dict):
            label = label_raw
        else:
            label = {"zh": str(label_raw), "en": str(label_raw)}

        allowed_values = []
        if resolver is not None:
            try:
                allowed_values = list(resolver(field_def) or [])
            except Exception:
                allowed_values = list(field_def.get("allowed_values") or [])
        else:
            allowed_values = list(field_def.get("allowed_values") or [])

        slot_data = slot_snapshot.get(key, {})

        # P2-3 修复：区分 schema_type 与 SlotStore 的 canonical value_type
        canonical_value_type = slot_data.get("value_type", "string")

        slot_entry = {
            "key": key,
            "label": label,
            "schema_type": ftype,
            "value_type": canonical_value_type,
            "required": True,
            "allowed_values": allowed_values,
            "value": slot_data.get("value"),
            "raw_value": slot_data.get("raw_value"),
            "status": slot_data.get("status", "missing"),
            "source": slot_data.get("source"),
            "confidence": slot_data.get("confidence"),
            "validation_error": slot_data.get("validation_error"),
            "candidate_value": slot_data.get("candidate_value"),
            "version": slot_data.get("version", 0),
        }
        result.append(slot_entry)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 约束状态 (P1-2 修复：使用 Issue #14 权威 ValidationResult)
# ─────────────────────────────────────────────────────────────────────────────

def _serialize_violation(v: Any) -> dict[str, Any] | None:
    """序列化 Violation 结构，保留完整上下文（check_type, observed_value, threshold 等）。"""
    try:
        if isinstance(v, dict):
            raw = v
        elif hasattr(v, "to_dict"):
            raw = v.to_dict()
        else:
            raw = {
                "constraint_id": getattr(v, "constraint_id", ""),
                "constraint_name": getattr(v, "constraint_name", ""),
                "message": getattr(v, "message", str(v)),
                "severity": getattr(v, "severity", "warning"),
                "related_fields": getattr(v, "related_fields", []),
                "check_type": getattr(v, "check_type", ""),
                "observed_value": getattr(v, "observed_value", None),
                "threshold": getattr(v, "threshold", None),
            }

        related_fields = list(raw.get("related_fields") or [])
        code = str(raw.get("code") or raw.get("constraint_id") or "")
        field = str(raw.get("field") or (related_fields[0] if related_fields else ""))

        return {
            "code": code,
            "constraint_id": str(raw.get("constraint_id") or code),
            "constraint_name": str(raw.get("constraint_name") or ""),
            "message": str(raw.get("message") or ""),
            "severity": str(raw.get("severity") or "warning"),
            "field": field,
            "related_fields": related_fields,
            "check_type": str(raw.get("check_type") or ""),
            "observed_value": raw.get("observed_value"),
            "threshold": raw.get("threshold"),
        }
    except Exception:
        return None


_ROBOT_STATE_SELECTOR_KEYS = {
    "equipment_family",
    "equipment_type",
    "equipment_unit_id",
    "equipment_name",
}


def _valid_slot_value(slot_snapshot: dict[str, Any], key: str) -> Any:
    slot_data = slot_snapshot.get(key)
    if not isinstance(slot_data, dict):
        return None
    if slot_data.get("status") != "valid":
        return None
    value = slot_data.get("value")
    return value if value not in (None, "") else None


def _resolve_current_status_ref(manager: "DialogueManager", unit_id: str) -> str | None:
    try:
        state_info = getattr(getattr(manager, "kb", None), "state_info", None)
        resolver = getattr(state_info, "resolve_status_ref", None)
        if callable(resolver):
            resolved = resolver(unit_id)
            return str(resolved) if resolved not in (None, "") else None
    except Exception:
        return None
    return None


def _state_snapshot_matches_current_unit(
    manager: "DialogueManager",
    state_snapshot: Any,
) -> bool:
    """Runtime state may only be exposed for the currently valid concrete unit."""
    if not isinstance(state_snapshot, dict) or not state_snapshot:
        return True

    try:
        slot_snapshot = manager.slot_store.get_slot_snapshot()
    except Exception:
        return True
    if not isinstance(slot_snapshot, dict):
        return True

    # Older unit tests and callers may construct a manager without robot slots.
    # In that case there is no current selector evidence to compare against.
    if not any(key in slot_snapshot for key in _ROBOT_STATE_SELECTOR_KEYS):
        return True

    current_unit = _valid_slot_value(slot_snapshot, "equipment_unit_id")
    if current_unit is None:
        return False
    current_unit = str(current_unit)

    snapshot_unit = (
        state_snapshot.get("unit_id")
        or state_snapshot.get("equipment_unit_id")
    )
    if snapshot_unit not in (None, ""):
        return str(snapshot_unit) == current_unit

    snapshot_status_ref = state_snapshot.get("status_ref")
    current_status_ref = _resolve_current_status_ref(manager, current_unit)
    if snapshot_status_ref not in (None, "") and current_status_ref:
        return str(snapshot_status_ref) == current_status_ref

    # If a concrete unit exists but the snapshot lacks comparable identifiers,
    # keep compatibility with legacy ValidationResult producers.
    return True


def _build_constraint_state(manager: "DialogueManager") -> dict:
    """
    构建约束状态。
    P1-2 修复 & 五、六要求：优先使用 ValidationResult 权威结构，严禁 OR 匹配过宽识别 acknowledgement。
    支持 ValidationResult 对象与 dict 结构。
    """
    hard_violations = []
    soft_warnings = []
    ignored_soft_warnings = []
    legacy_acknowledgements = []
    all_serialized_violations = []
    overall_status = "none"
    validated_at = None
    task_version = 0
    validation_version = 0
    validation_fingerprint = None
    state_snapshot = {}
    error_msg = None
    source = "none"

    # 1. 尝试使用 SlotStore 中的 ValidationResult
    val_result = None
    try:
        if hasattr(manager.slot_store, "validation_result"):
            val_result = manager.slot_store.validation_result
    except Exception:
        pass

    if val_result is not None:
        source = "validation_result"
        try:
            if isinstance(val_result, dict):
                overall_status = val_result.get("overall_status") or val_result.get("status", "none")
                validated_at = val_result.get("validated_at")
                task_version = val_result.get("task_version", 0)
                validation_version = val_result.get("validation_version", 0)
                validation_fingerprint = val_result.get("validation_fingerprint")
                state_snapshot = val_result.get("state_snapshot") or {}
                raw_violations = val_result.get("violations", []) or []
                error_msg = val_result.get("error")
            else:
                overall_status = getattr(val_result, "overall_status", "none")
                validated_at = getattr(val_result, "validated_at", None)
                task_version = getattr(val_result, "task_version", 0)
                validation_version = getattr(val_result, "validation_version", 0)
                validation_fingerprint = getattr(val_result, "validation_fingerprint", None)
                state_snapshot = getattr(val_result, "state_snapshot", {}) or {}
                raw_violations = getattr(val_result, "violations", []) or []
                error_msg = getattr(val_result, "error", None)

            for v in raw_violations:
                v_dict = _serialize_violation(v)
                if v_dict is None:
                    continue
                all_serialized_violations.append(v_dict)
                if v_dict["severity"] == "hard":
                    hard_violations.append(v_dict)
                else:
                    soft_warnings.append(v_dict)

            # 获取有效 acknowledgement:
            # 优先调用 DialogueManager 独有的 _get_valid_acknowledgements 过滤逻辑
            valid_ack_objs = []
            if hasattr(manager, "_get_valid_acknowledgements") and callable(manager._get_valid_acknowledgements):
                try:
                    res_acks = manager._get_valid_acknowledgements(val_result)
                    if isinstance(res_acks, list):
                        valid_ack_objs = res_acks
                except Exception:
                    valid_ack_objs = []

            status_ref = state_snapshot.get("status_ref", "") if isinstance(state_snapshot, dict) else ""
            state_ver = state_snapshot.get("state_version", 0) if isinstance(state_snapshot, dict) else 0
            violation_ids = {v["constraint_id"] for v in soft_warnings}

            raw_acks = getattr(manager.slot_store, "validation_acknowledgements", []) or []
            for ack in raw_acks:
                ack_dict = ack.to_dict() if hasattr(ack, "to_dict") else (ack if isinstance(ack, dict) else {})
                ack_cid = ack_dict.get("constraint_id")
                ack_tv = ack_dict.get("task_version")
                ack_vv = ack_dict.get("validation_version")
                ack_fp = ack_dict.get("validation_fingerprint")
                ack_sr = ack_dict.get("status_ref")
                ack_sv = ack_dict.get("state_version")

                # 状态与关联确认判定：在 valid_ack_objs 中，或遥测状态与校验指纹一致且历史版本有效
                is_valid = (
                    ack in valid_ack_objs or
                    (
                        ack_cid in violation_ids and
                        ack_tv is not None and
                        ack_tv <= task_version and
                        ack_sr == status_ref and
                        ack_sv == state_ver and
                        (ack_fp == validation_fingerprint or ack_fp is None)
                    )
                )

                if is_valid:
                    ignored_soft_warnings.append(ack_dict)
                else:
                    legacy_acknowledgements.append(ack_dict)

            # 从 soft_warnings 中移除已被有效确认忽略的条目，
            # 避免已忽略的警告仍在 soft_warnings 里出现，导致前端持续展示。
            if ignored_soft_warnings:
                ignored_cids = {a.get("constraint_id") for a in ignored_soft_warnings}
                soft_warnings = [w for w in soft_warnings if w.get("constraint_id") not in ignored_cids]

        except Exception as exc:
            logger.warning("解析 ValidationResult 失败，退回降级逻辑: %s", exc)
            val_result = None

    # 2. 降级逻辑（当 validation_result 不存在时）
    if val_result is None:
        source = "fallback"
        violations = []
        try:
            violations = list(manager._blocking_violations or [])
        except AttributeError:
            pass

        for v in violations:
            v_dict = _serialize_violation(v)
            if v_dict is None:
                continue
            all_serialized_violations.append(v_dict)
            if v_dict["severity"] == "hard":
                hard_violations.append(v_dict)
            else:
                soft_warnings.append(v_dict)

        try:
            whitelist = manager._soft_whitelist or set()
            for item in whitelist:
                if isinstance(item, (tuple, list)) and len(item) >= 3:
                    ignored_soft_warnings.append({
                        "field": item[0],
                        "value": item[1],
                        "constraint_id": item[2],
                    })
        except AttributeError:
            pass

        # 降级路径同样从 soft_warnings 剔除已白名单的条目
        if ignored_soft_warnings:
            ignored_cids = {a.get("constraint_id") for a in ignored_soft_warnings}
            soft_warnings = [w for w in soft_warnings if w.get("constraint_id") not in ignored_cids]

        phase = getattr(manager, "phase", "collecting")
        if phase == "blocked_hard":
            overall_status = "blocked_hard"
        elif phase == "blocked_soft":
            overall_status = "blocked_soft"
        elif hard_violations:
            overall_status = "blocked_hard"
        elif soft_warnings:
            overall_status = "warning"

    if not _state_snapshot_matches_current_unit(manager, state_snapshot):
        source = "stale_validation_result"
        overall_status = "none"
        validated_at = None
        task_version = 0
        validation_version = 0
        validation_fingerprint = None
        state_snapshot = {}
        error_msg = None
        hard_violations = []
        soft_warnings = []
        ignored_soft_warnings = []
        legacy_acknowledgements = []
        all_serialized_violations = []

    return {
        "source": source,
        "status": overall_status,
        "overall_status": overall_status,
        "validated_at": validated_at,
        "task_version": task_version,
        "validation_version": validation_version,
        "validation_fingerprint": validation_fingerprint,
        "state_snapshot": state_snapshot,
        "violations": all_serialized_violations,
        "hard_violations": hard_violations,
        "soft_warnings": soft_warnings,
        "ignored_soft_warnings": ignored_soft_warnings,
        "legacy_acknowledgements": legacy_acknowledgements,
        "error": error_msg,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

def _build_frontend_ui_state_locked(manager: "DialogueManager") -> dict:
    """
    从 DialogueManager 当前状态构建统一 ui_state 字典。
    """
    try:
        phase = getattr(manager, "phase", "collecting")
        dialogue_mode = getattr(manager, "dialogue_mode", "task_collection")
        mode = getattr(manager, "mode", "normal")

        task_type_key = None
        task_id = None
        task_id_preview = None
        try:
            if hasattr(manager, "task_state") and isinstance(manager.task_state, dict):
                task_type_key = manager.task_state.get("task_type_key")
                task_id = manager.task_state.get("task_id")
            task_id_preview = getattr(manager, "task_id_preview", None)
        except Exception:
            pass

        slot_snapshot = {}
        slot_version = 0
        try:
            slot_snapshot = manager.slot_store.get_slot_snapshot()
            slot_version = int(getattr(manager.slot_store, "version", 0) or 0)
        except Exception as exc:
            logger.warning("build_frontend_ui_state: get_slot_snapshot 失败: %s", exc)

        slots = _build_slots(manager, slot_snapshot=slot_snapshot)
        constraint_state = _build_constraint_state(manager)
        actions = _compute_actions(phase, dialogue_mode)
        read_only = _compute_read_only(phase, dialogue_mode)

        return {
            "state_status": "ok",
            "dialogue_mode": dialogue_mode,
            "mode": mode,
            "phase": phase,
            "task_type_key": task_type_key,
            "task_id": task_id,
            "task_id_preview": task_id_preview,
            "slot_version": slot_version,
            "read_only": read_only,
            "slots": slots,
            "constraint_state": constraint_state,
            "actions": actions,
        }

    except Exception as exc:
        logger.error("build_frontend_ui_state 发生内部错误，fail closed: %s", exc, exc_info=True)
        return {
            "state_status": "error",
            "error_message": str(exc),
            "dialogue_mode": "task_collection",
            "mode": "normal",
            "phase": "collecting",
            "task_type_key": None,
            "task_id": None,
            "task_id_preview": None,
            "slot_version": 0,
            "read_only": True,
            "slots": [],
            "constraint_state": {
                "status": "none",
                "overall_status": "none",
                "hard_violations": [],
                "soft_warnings": [],
                "ignored_soft_warnings": [],
            },
            "actions": {
                "can_send": False,
                "can_modify": False,
                "can_confirm": False,
                "can_ignore_soft_warning": False,
                "can_publish": False,
                "can_cancel": False,
            },
        }


def build_frontend_ui_state(manager: "DialogueManager") -> dict:
    """Build a coherent UI snapshot under the manager's per-session lock."""
    try:
        manager_lock = getattr(manager, "_session_lock", None)
        lock_context = (
            manager_lock
            if hasattr(manager_lock, "__enter__")
            else nullcontext()
        )
        with lock_context:
            return _build_frontend_ui_state_locked(manager)
    except Exception:
        return _build_frontend_ui_state_locked(manager)
