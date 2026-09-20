"""
src/handlers/write_reply_grounder.py - 槽位提交事实锚点与回复展示生成器

职责：
1. 过滤机器人选型迁移后的 unresolved 噪声（filter_robot_selection_unresolved）；
2. 投影遗留 equipment_class 候选到 family 节点（project_legacy_equipment_class_candidate）；
3. 提取 SlotStore 提交展示值与本轮已接受更新（get_committed_turn_updates / get_committed_update_display_values）；
4. 生成事实锚点回复摘要，确保自然语言回复与底层槽位状态完全一致（ground_write_reply）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.extraction import coord_parser
from ..constants import FIELD_LABELS

class WriteReplyGrounder:
    """槽位提交事实锚点与回复展示生成器"""

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    def __getattr__(self, name: str) -> Any:
        if "manager" in self.__dict__ and self.manager is not None:
            return getattr(self.manager, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")


    @staticmethod
    def filter_robot_selection_unresolved(
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

    def project_legacy_equipment_class_candidate(
        self,
        candidate: dict,
        task_type_key: str | None,
        task_state: dict | None,
    ) -> dict | None:
        """Map a legacy equipment_class candidate to family when unambiguous."""
        manager = self.manager
        raw_value = candidate.get("raw_value", candidate.get("normalized_value"))
        normalized_value = candidate.get("normalized_value", raw_value)
        class_id = manager.kb._resolve_class_key(str(normalized_value or ""))
        if not class_id and raw_value is not None:
            class_id = manager.kb._resolve_class_key(str(raw_value))
        if not class_id or not task_type_key:
            return None
        try:
            domain = manager.kb.get_feasible_robot_selection_domain(
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

    def get_committed_update_display_values(self, accepted_updates: dict) -> dict:
        """从领域配置生成写入回执的展示值，不改变 SlotStore 标准值。"""
        manager = self.manager
        display = {}
        for k, v in (accepted_updates or {}).items():
            if k == "equipment_class" and v is not None:
                class_config = manager.kb.get_robot_classes().get(str(v), {})
                display["equipment_class"] = class_config.get(
                    "full_name",
                    v,
                )
            else:
                display[k] = coord_parser.format_slot_display_value(k, v)
        return display

    def get_committed_turn_updates(
        self,
        proposed_updates: dict,
        state_before_turn: dict,
    ) -> dict:
        """返回本轮已由 SlotStore 提交的用户字段更新。"""
        manager = self.manager
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
        for key, value in manager.task_state.items():
            if key in ignored_keys or key.startswith("__") or value is None:
                continue
            if key not in proposed_updates and state_before_turn.get(key) == value:
                continue
            slot = manager.slot_store.slots.get(key)
            if slot and slot.status == "valid":
                accepted[key] = value
        # An intentionally emptied required list is committed but "missing".
        # It is absent from task_state; still report that successful clearing.
        for key, value in proposed_updates.items():
            slot = manager.slot_store.slots.get(key)
            if (key in FIELD_LABELS and value == [] and slot
                    and slot.value == [] and slot.status == "missing"
                    and not slot.validation_error):
                accepted[key] = []
        return accepted

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
    ) -> str:
        """Render a WRITE receipt only from committed state and validation facts.

        The legacy model-reply arguments remain accepted for callers, but model
        prose cannot attest a mutation, publication, or current device health.
        READ explanations are generated by their separate routing path.
        """
        visible = {key: value for key, value in accepted_updates.items() if key in FIELD_LABELS}
        resolved_labels = {FIELD_LABELS[key] for key in visible}
        unresolved = []
        for item in unresolved_inputs:
            text = str(item).strip()
            if not text:
                continue
            if any(label in text for label in resolved_labels) and (
                "无法解析" in text or "Invalid datetime format" in text
            ):
                continue
            if text not in unresolved:
                unresolved.append(text)

        parts = []
        if visible:
            committed = [
                f"{FIELD_LABELS[key]}：{'已清空' if value == [] else coord_parser.format_slot_display_value(key, (display_updates or {}).get(key, value))}"
                for key, value in visible.items()
            ]
            parts.append("✅ 已记录：" + "；".join(committed) + "。")
        else:
            parts.append("本轮未写入任务状态。")
        if unresolved:
            parts.append("⚠️ 未写入或仍需确认：" + "；".join(unresolved) + "。")

        # Show retained facts too: failed deletes or time edits must not let a
        # preceding assistant claim become the user's apparent task state.
        retained = []
        for key in ("task_type", "wellhead_id", "equipment_type", "payload", "start_time", "end_time"):
            value = (task_state or {}).get(key)
            if key in visible or value is None:
                continue
            retained.append(f"{FIELD_LABELS[key]}：{coord_parser.format_slot_display_value(key, value)}")
        if retained:
            parts.append("当前已保存：" + "；".join(retained) + "。")

        context = constraint_context or {}
        violations = context.get("violations") or []
        has_hard = False
        has_soft = False
        for violation in violations:
            def field(name: str, default: str = "") -> str:
                return violation.get(name, default) if isinstance(violation, dict) else getattr(violation, name, default)
            severity = field("severity")
            has_hard = has_hard or severity == "hard"
            has_soft = has_soft or severity == "soft"
            message = field("message")
            code = field("constraint_id") or field("code")
            # Soft-warning details belong to the existing sidebar. Hard
            # blockers must remain explicit in both conversation and sidebar.
            if message and severity == "hard":
                detail = f"⚠️ {code + '：' if code else ''}{message}"
                if detail not in parts:
                    parts.append(detail)

        labels = [str(item.get("label") or item.get("key"))
                  for item in (missing_fields or [])
                  if isinstance(item, dict) and (item.get("label") or item.get("key"))]
        if labels:
            parts.append("仍需补充：" + "、".join(labels[:3]) + "。")
        if has_hard or str(context.get("type", "")).startswith("hard"):
            parts.append("任务尚未发布，请先修正上述硬约束问题。")
        elif has_soft or str(context.get("type", "")).startswith("soft"):
            parts.append("任务尚未发布，仍有软警告待处理。请查看右侧看板后修改相关参数；如接受这些软警告，可明确回复“忽略软警告”后继续。")
        elif missing_fields is not None and not labels:
            if unresolved:
                parts.append("任务尚未发布，请先处理上述未写入或待确认的信息。")
            else:
                parts.append("所有必填字段已收集完成，任务尚未发布。请核对草稿后再确认发布。")
        return "\n".join(parts)
