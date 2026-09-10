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

from .. import coord_parser
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

        # 只展示 FIELD_LABELS 中有中文标签的用户可见字段
        user_visible_updates = {
            key: value
            for key, value in accepted_updates.items()
            if key in FIELD_LABELS
        }
        resolved_labels = {
            FIELD_LABELS[key]
            for key in user_visible_updates
        }
        unresolved = []
        for item in unresolved_inputs:
            text = str(item).strip()
            if not text:
                continue
            if (
                any(label in text for label in resolved_labels)
                and ("无法解析" in text or "Invalid datetime format" in text)
            ):
                continue
            unresolved.append(text)

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
                    manager_obj = getattr(self_obj, "manager", self_obj)
                    if not eq_type and manager_obj and hasattr(manager_obj, "slot_store") and manager_obj.slot_store:
                        eq_slot = manager_obj.slot_store.slots.get("equipment_type")
                        if eq_slot and eq_slot.status == "valid" and eq_slot.value:
                            eq_type = str(eq_slot.value)
                    if eq_type and manager_obj and hasattr(manager_obj, "kb") and manager_obj.kb:
                        robot = manager_obj.kb.get_rov(eq_type)
                        if robot:
                            ob_list = robot.get("onboard_payloads", [])
                            if ob_list and hasattr(manager_obj, "capability_adapter"):
                                guidance_msg = manager_obj.capability_adapter.format_payload_guidance(
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
