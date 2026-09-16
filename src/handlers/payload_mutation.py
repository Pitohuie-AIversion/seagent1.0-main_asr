"""
src/handlers/payload_mutation.py - 载荷修改与列表变异处理器

职责：
1. 识别用户是否请求重新选择/修改/配置载荷；
2. 调出载荷配置卡片并重置相关槽位与阶段；
3. 自动转换 LLM 误抽取的 payload 槽位候选为 list_mutations（增量/减量保护）。
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any


from .base import BaseDialogueHandler, DialogueContext, HandlerResult
from ..slot_store import Slot

logger = logging.getLogger("src.dialogue_manager")


class PayloadMutationManager(BaseDialogueHandler):
    """载荷修改与列表变异管理器"""

    def __getattr__(self, name: str) -> Any:
        return getattr(self.manager, name)


    def can_handle(self, ctx: DialogueContext) -> bool:
        return self.is_payload_modification_request(ctx.user_message)

    def handle(self, ctx: DialogueContext) -> HandlerResult:
        if self.is_payload_modification_request(ctx.user_message):
            reply = self.handle_payload_modification(ctx.user_message)
            if reply is not None:
                return HandlerResult.success(reply=reply)
        return HandlerResult.not_handled()

    @staticmethod
    def is_payload_modification_request(user_message: str) -> bool:
        """判断用户是否明确请求重新选择/修改/配置载荷（且不属于取消修改指令）。"""
        msg = (user_message or "").strip().lower()
        if any(neg in msg for neg in ["取消", "放弃", "不要", "不修改", "不用"]):
            return False

        direct_keywords = (
            "修改载荷", "重新选择载荷", "重新选载荷", "重新配置载荷",
            "修改payload", "重选载荷", "更换载荷", "换载荷", "重置载荷",
            "清除载荷", "调出载荷卡片", "载荷卡片", "重配置载荷", "修改工具",
            "重新选择工具", "更换工具", "换工具"
        )
        if any(kw in msg for kw in direct_keywords):
            return True

        has_target = any(t in msg for t in ["载荷", "payload", "工具"])
        has_action = any(a in msg for a in ["修改", "重选", "重新选择", "更换", "重新配置", "重置", "清除"])
        return bool(has_target and has_action)

    def handle_payload_modification(self, user_message: str) -> str | None:
        """用户请求重新选择/修改/配置载荷时，重置 payload 槽位为 missing 并调整阶段供前端调出卡片。"""
        manager = self.manager
        payload_slot = manager.slot_store.slots.get("payload")
        task_type_slot = manager.slot_store.slots.get("task_type_key")

        task_type_val = (
            task_type_slot.value if (task_type_slot and task_type_slot.value)
            else manager.task_state.get("task_type_key")
        )

        if payload_slot is None and task_type_val:
            schema = manager.builder.get_schema(task_type_val, manager.mode)
            for f in schema:
                k = f.get("key")
                if k and k not in manager.slot_store.slots:
                    manager.slot_store.slots[k] = Slot(slot_name=k, value_type=f.get("type", "string"), status="missing")
            payload_slot = manager.slot_store.slots.get("payload")

        if payload_slot is None:
            existing_payload = manager.task_state.get("payload")
            payload_slot = Slot(
                slot_name="payload",
                value=None,
                candidate_value=existing_payload,
                value_type="list",
                status="missing",
                source="user",
            )
            manager.slot_store.slots["payload"] = payload_slot

        if payload_slot is not None:
            existing_val = payload_slot.candidate_value or copy.deepcopy(payload_slot.value)
            payload_slot.candidate_value = existing_val if existing_val is not None else None
            payload_slot.status = "missing"
            payload_slot.value = None
            payload_slot.validation_error = None
            payload_slot.source = "user"
            manager.slot_store.slots["payload"] = payload_slot
            manager.slot_store.version += 1

            if manager.phase in ("confirming", "validating", "blocked_soft", "blocked_hard"):
                manager._transition_phase("collecting", reason="user_requested_payload_modification")

            manager._blocking_violations = []
            manager._switch_dialogue_mode("task_collection", source="user_payload_modification", reason="用户请求重新配置/修改载荷")

            if task_type_slot and task_type_slot.value:
                req_fields = manager.builder.get_required(task_type_slot.value, manager.mode, manager.slot_store.get_task_state())
                manager._last_missing = manager.slot_store.get_missing_slots(req_fields)

            reply = "已为您重新调出载荷配置卡片。请在下方对话区域或卡片中重新选择与配置要携带的工具。"
            manager.conversation_history.append({"role": "user", "content": user_message})
            manager.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        return None

    def normalize_payload_list_mutations(
        self,
        extraction_res: dict,
        user_message: str,
        current_slots: dict,
    ) -> None:
        """兜底防护：当 LLM 抽取的 extraction_res 将 payload 误放入 slot_candidates 时，
        增减请求保留列表增量语义，首次配置及全量改选使用 set，
        明确提及旧载荷的替换使用带 target_items 的 replace。
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

        items = []
        for cand in payload_cands:
            val = cand.get("normalized_value")
            if val is None:
                val = cand.get("raw_value")
            if isinstance(val, list):
                for item in val:
                    if item and item not in items:
                        items.append(item)
            elif val and val not in items:
                items.append(val)

        # 展平顿号、逗号分隔的内部工具项
        flattened_items = []
        for it in items:
            if isinstance(it, str):
                cleaned = it.strip(" \t\n\r'\"[]()")
                parts = [p.strip(" \t\n\r'\"") for p in re.split(r"[,，、\n]+", cleaned) if p.strip(" \t\n\r'\"")]
                for p in parts:
                    if p and p not in flattened_items:
                        flattened_items.append(p)
            elif it and it not in flattened_items:
                flattened_items.append(it)
        items = flattened_items

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

        max_confidence = max((c.get("confidence", 0.95) for c in payload_cands if isinstance(c, dict)), default=0.95)

        target_items = []
        if is_remove:
            op = "remove"
        elif is_replace:
            # A direct candidate only contains the new list.  A full-list
            # reassignment must not become a targetless replace (an append).
            prefix = re.split("|".join(replace_kws), msg, maxsplit=1)[0]
            if has_existing_payload:
                target_items = [
                    item for item in payload_slot.value
                    if isinstance(item, str) and item and item in prefix
                ]
            op = "replace" if target_items else "set"
        elif is_add or has_existing_payload:
            op = "add"
        else:
            # 首次配置或直接输入载荷列表时，全量设置新载荷集合
            op = "set"

        extraction_res["slot_candidates"] = [
            c for c in candidates
            if isinstance(c, dict) and c.get("canonical_key") != "payload"
        ]
        mutations.append({
            "field": "payload",
            "operation": op,
            "items": items,
            "target_items": target_items,
            "raw_text": msg,
            "confidence": max_confidence,
            "source": "user_input",
        })
