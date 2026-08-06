"""
ui_state_builder.py

统一 UI 状态构建模块。

提供 build_frontend_ui_state(manager) 函数，从 DialogueManager 的当前状态
（phase, dialogue_mode, slot_store, _blocking_violations, _soft_whitelist,
builder.get_schema）构建前端所需的完整 ui_state 字典。

该函数是 /api/chat、/api/session/state、/api/history/load 三个接口的唯一状态
构建来源，不依赖 _last_built_json 反推 slot 状态。
"""

from __future__ import annotations

from contextlib import nullcontext
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .dialogue_manager import DialogueManager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# actions 计算
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
        "can_send": False,
        "can_modify": False,
        "can_confirm": False,
        "can_ignore_soft_warning": False,
        "can_publish": False,
        "can_cancel": False,
    },
    "rejected": {
        "can_send": False,
        "can_modify": False,
        "can_confirm": False,
        "can_ignore_soft_warning": False,
        "can_publish": False,
        "can_cancel": False,
    },
}

# 普通对话/知识问答等非任务收集模式下的 actions
_NORMAL_CHAT_ACTIONS: dict = {
    "can_send": True,
    "can_modify": False,
    "can_confirm": False,
    "can_ignore_soft_warning": False,
    "can_publish": False,
    "can_cancel": False,
}


def _compute_actions(phase: str, dialogue_mode: str) -> dict:
    """根据 phase 和 dialogue_mode 计算当前允许的操作集合。"""
    if dialogue_mode != "task_collection":
        return dict(_NORMAL_CHAT_ACTIONS)
    return dict(_PHASE_ACTIONS.get(phase, _PHASE_ACTIONS["collecting"]))


def _compute_read_only(phase: str, dialogue_mode: str = "task_collection") -> bool:
    """Only terminal task views are read-only; queries remain interactive."""
    return dialogue_mode == "task_collection" and phase in ("done", "rejected")


# ─────────────────────────────────────────────────────────────────────────────
# slot 合并
# ─────────────────────────────────────────────────────────────────────────────

def _build_slots(
    manager: "DialogueManager",
    slot_snapshot: dict[str, Any] | None = None,
) -> list:
    """
    将 builder.get_schema() 与 slot_store.get_slot_snapshot() 合并。

    - 字段顺序由 schema 决定。
    - label、allowed_values、required 来自 schema。
    - value、status 等运行时字段来自 SlotStore。
    - 仅在 dialogue_mode == task_collection 且 task_type_key 已知时返回完整列表。
    """
    task_type_key = None
    if getattr(manager, "dialogue_mode", "task_collection") != "task_collection":
        return []

    try:
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

    # 尝试获取 allowed_values resolver
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

        # auto/fixed 字段不暴露给前端字段面板
        if ftype in ("auto", "fixed"):
            continue

        label_raw = field_def.get("label", key)
        if isinstance(label_raw, str):
            label = {"zh": label_raw, "en": label_raw}
        elif isinstance(label_raw, dict):
            label = label_raw
        else:
            label = {"zh": str(label_raw), "en": str(label_raw)}

        # allowed_values：优先使用 resolver，降级到 schema 静态值
        allowed_values = []
        if resolver is not None:
            try:
                allowed_values = list(resolver(field_def) or [])
            except Exception:
                allowed_values = list(field_def.get("allowed_values") or [])
        else:
            allowed_values = list(field_def.get("allowed_values") or [])

        # 从 slot_snapshot 获取运行时状态
        slot_data = slot_snapshot.get(key, {})

        slot_entry = {
            "key": key,
            "label": label,
            "value_type": ftype,
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
# 约束状态
# ─────────────────────────────────────────────────────────────────────────────

def _serialize_violation(violation: Any) -> dict[str, Any] | None:
    """Convert Violation-like values to the small, stable UI contract."""
    try:
        if isinstance(violation, dict):
            raw = violation
        elif hasattr(violation, "to_dict"):
            raw = violation.to_dict()
        else:
            raw = {
                "constraint_id": getattr(violation, "constraint_id", ""),
                "message": getattr(violation, "message", str(violation)),
                "severity": getattr(violation, "severity", "warning"),
                "related_fields": getattr(violation, "related_fields", []),
            }

        related_fields = raw.get("related_fields") or []
        if isinstance(related_fields, str):
            related_fields = [related_fields]
        return {
            "code": str(raw.get("code") or raw.get("constraint_id") or ""),
            "message": str(raw.get("message") or ""),
            "severity": str(raw.get("severity") or "warning"),
            "field": str(raw.get("field") or (related_fields[0] if related_fields else "")),
        }
    except Exception:
        return None


    """将 _blocking_violations 和 _soft_whitelist 序列化为结构化约束状态。"""
def _build_constraint_state(manager: "DialogueManager") -> dict:
    violations = []
    try:
        violations = list(manager._blocking_violations or [])
    except AttributeError:
        pass

    hard_violations = []
    soft_warnings = []

    for violation in violations:
        v_dict = _serialize_violation(violation)
        if v_dict is None:
            continue
        if v_dict["severity"] == "hard":
            hard_violations.append(v_dict)
        else:
            soft_warnings.append(v_dict)

    # 已忽略的软警告（_soft_whitelist: set of (field, value, constraint_id)）
    ignored_soft_warnings = []
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

    phase = getattr(manager, "phase", "collecting")
    if phase == "blocked_hard":
        status = "hard_blocked"
    elif phase == "blocked_soft":
        status = "soft_warning"
    elif hard_violations:
        status = "hard_blocked"
    elif soft_warnings:
        status = "soft_warning"
    else:
        status = "none"

    return {
        "status": status,
        "hard_violations": hard_violations,
        "soft_warnings": soft_warnings,
        "ignored_soft_warnings": ignored_soft_warnings,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

def _build_frontend_ui_state_locked(manager: "DialogueManager") -> dict:
    """
    从 DialogueManager 当前状态构建统一 ui_state 字典。

    如遇不可恢复的内部错误，fail closed：返回 read_only=True 且所有 can_* = False。
    """
    try:
        phase = getattr(manager, "phase", "collecting")
        dialogue_mode = getattr(manager, "dialogue_mode", "task_collection")
        mode = getattr(manager, "mode", "normal")

        task_type_key = None
        task_id = None
        task_id_preview = None
        try:
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
