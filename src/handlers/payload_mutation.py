"""
src/handlers/payload_mutation.py - 载荷修改与列表变异处理器

职责：
1. 识别用户是否请求重新选择/修改/配置载荷；
2. 管理载荷配置卡片的编辑状态，保留已提交值；
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

_REMOVE = r"删除|移除|去掉|卸下|取消携带|别带|不要带|不要"
_ADD = r"添加|增加|加装|再带|加上|补充"
_REPLACE = r"替换成|替换为|换成|换为|改成|改为"
_NEGATED_ACTION = re.compile(r"(?:不要|不用|无需|不能|别|不|勿)\s*(?:删除|移除|去掉|卸下|清空|清除|替换|更换|换成)")
_CLEAR_PAYLOAD = re.compile(
    r"^(?:请|帮我|给我)?(?:清空|清除|删除|移除|卸下)(?:全部|所有)?(?:携带的?|选配的?|已选的?)?(?:工具|载荷|payload)$"
    r"|^(?:所有|全部)(?:工具|载荷)(?:都)?(?:不要|删除|移除|卸下)$"
    r"|^(?:不带任何工具|什么工具都不带|什么都不带)$", re.IGNORECASE,
)


class PayloadMutationManager(BaseDialogueHandler):
    """载荷修改与列表变异管理器"""

    def __getattr__(self, name: str) -> Any:
        return getattr(self.manager, name)


    def can_handle(self, ctx: DialogueContext) -> bool:
        return self.is_payload_modification_request(ctx.user_message) or self.is_editor_cancel_request(ctx.user_message)

    def handle(self, ctx: DialogueContext) -> HandlerResult:
        if self.is_editor_cancel_request(ctx.user_message):
            manager = self.manager
            manager.editing_slot = None
            if manager.slot_store.version == self.__dict__.get("_opening_version"):
                manager._transition_phase(self._opening_phase, reason="payload_edit_cancelled")
            reply = "已取消载荷修改，保留原有配置。"
            manager.conversation_history.extend([
                {"role": "user", "content": ctx.user_message},
                {"role": "assistant", "content": reply},
            ])
            return HandlerResult.success(reply=reply)
        if self.is_payload_modification_request(ctx.user_message):
            reply = self.handle_payload_modification(ctx.user_message)
            if reply is not None:
                return HandlerResult.success(reply=reply)
        return HandlerResult.not_handled()

    def is_editor_cancel_request(self, user_message: str) -> bool:
        return getattr(self.manager, "editing_slot", None) == "payload" and (
            (user_message or "").strip().lower().rstrip("。.!！") in {
                "取消载荷修改", "取消修改载荷", "放弃载荷修改", "取消修改", "放弃修改",
                "cancel payload modification",
            }
        )

    @staticmethod
    def is_payload_modification_request(user_message: str) -> bool:
        """判断用户是否明确请求重新选择/修改/配置载荷（且不属于取消修改指令）。"""
        msg = (user_message or "").strip().lower()
        if any(_CLEAR_PAYLOAD.fullmatch(part.strip()) for part in re.split(r"[，,。；;！!]", msg)):
            return False  # Explicit values use the ordinary validated write transaction.
        if msg.rstrip(".!！") == "modify payload":
            return True
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
        """Open an editor without changing the authoritative payload slot."""
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
                value=copy.deepcopy(existing_payload),
                value_type="list",
                status="valid" if existing_payload else "missing",
                source="user",
            )
            manager.slot_store.slots["payload"] = payload_slot

        if payload_slot is not None:
            if getattr(manager, "editing_slot", None) != "payload":
                self._opening_phase = manager.phase
                self._opening_version = manager.slot_store.version
            manager.editing_slot = "payload"

            if manager.phase in ("confirming", "validating"):
                manager._transition_phase("collecting", reason="user_requested_payload_modification")

            manager._switch_dialogue_mode("task_collection", source="user_payload_modification", reason="用户请求重新配置/修改载荷")

            if task_type_slot and task_type_slot.value:
                req_fields = manager.builder.get_required(task_type_slot.value, manager.mode, manager.slot_store.get_task_state())
                manager._last_missing = manager.slot_store.get_missing_slots(req_fields)

            reply = "已为您重新调出载荷配置卡片，原有配置保持不变。请选择载荷后点击“确认配置”提交，或点击“取消修改”保留原配置。"
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

        # A card confirmation explicitly supplies the complete selection. Keep
        # it deterministic while still using the normal validation transaction.
        selection = re.fullmatch(
            r"确认选择(?:携带工具|载荷|payload|Payloads?|Tools?)\s*[:：]\s*(.+)",
            (user_message or "").strip(),
            flags=re.IGNORECASE,
        )
        if selection and getattr(self.manager, "editing_slot", None) == "payload":
            items = [item.strip() for item in re.split(r"[、,，\n]+", selection.group(1)) if item.strip()]
            extraction_res["slot_candidates"] = [
                candidate for candidate in extraction_res.get("slot_candidates", [])
                if not isinstance(candidate, dict) or candidate.get("canonical_key") != "payload"
            ]
            extraction_res["list_mutations"] = [
                mutation for mutation in mutations
                if not isinstance(mutation, dict) or mutation.get("field") != "payload"
            ] + [{
                "field": "payload", "operation": "set", "items": items,
                "target_items": [], "raw_text": user_message,
                "confidence": 1.0, "source": "user_input",
            }]
            return

        explicit = self._explicit_payload_mutations(user_message, current_slots)
        if explicit is not None:
            # A schema-valid model operation is still only a proposal. Explicit
            # named edits take precedence, including when the model returned no
            # candidate or incorrectly added the items the user wanted to keep.
            extraction_res["slot_candidates"] = [
                c for c in extraction_res.get("slot_candidates", [])
                if not isinstance(c, dict) or c.get("canonical_key") != "payload"
            ]
            extraction_res["list_mutations"] = [
                m for m in mutations if not isinstance(m, dict) or m.get("field") != "payload"
            ] + explicit
            if any(m["operation"] == "clear" for m in explicit):
                extraction_res["unresolved"] = [
                    item for item in extraction_res.get("unresolved", [])
                    if not re.fullmatch(r"(?:请|需要|是否|请再次)?确认(?:是否)?清空(?:全部|所有)?(?:携带)?(?:工具|载荷)[。？?]?", str(item))
                ]
            return

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

    def _payload_terms(self, current_slots: dict) -> list[str]:
        """Use configured names/aliases and committed values, not model guesses."""
        slot = current_slots.get("payload")
        terms = list(slot.value) if slot and isinstance(slot.value, list) else []
        assets = getattr(getattr(self.manager, "kb", None), "assets", {})
        for item in assets.get("payload_catalog", {}).values():
            terms.extend([item.get("name"), *(item.get("aliases") or [])])
        for task in assets.get("payload_options", {}).values():
            if isinstance(task, dict):
                for values in task.values():
                    if isinstance(values, list):
                        terms.extend(values)
        return sorted({term for term in terms if isinstance(term, str) and term}, key=len, reverse=True)

    @staticmethod
    def _payload_items(text: str) -> list[str]:
        result = []
        for part in re.split(r"[、,，+]|以及|和|及", text):
            part = re.sub(r"^(?:请|把|将|原来的?|当前的?|已有的?|一套|一个|一件|携带的?)", "", part.strip())
            part = part.strip(" ：:。.!！")
            if not part:
                continue
            # Preserve an unknown operand so validation rejects it atomically.
            if part not in result:
                result.append(part)
        return result

    def _explicit_payload_mutations(self, message: str, current_slots: dict) -> list[dict] | None:
        """Recover explicit list edits; ambiguous language stays with extraction.

        Only payload operands from the current sentence are used. Negated and
        hypothetical commands never become destructive operations. The result
        still passes through the normal allowed-values and transaction checks.
        """
        terms = self._payload_terms(current_slots)
        commands = []
        guarded = False
        for clause in re.split(r"[，,。；;！!]", message or ""):
            clause = re.sub(r"^(?:然后|并且|再|并|请|帮我|给我)\s*", "", clause.strip())
            if not clause:
                continue
            if _NEGATED_ACTION.search(clause) or re.search(r"如果|假如|能否|是否|吗|会怎样|会怎么样|会有什么|[?？]", clause):
                guarded = guarded or any(term in clause for term in terms) or bool(re.search(r"载荷|工具", clause))
                continue
            if _CLEAR_PAYLOAD.fullmatch(clause):
                commands.append(("clear", [], []))
                continue
            if re.search(r"保留|保持|不变", clause):
                # Remove+keep in one clause only takes the removal's operands.
                clause = re.split(r"保留|保持|不变", clause, maxsplit=1)[0]
                clause = re.sub(r"(?:并且|同时|并|但)\s*$", "", clause)
                if not re.search(rf"{_REMOVE}|{_REPLACE}|{_ADD}|替换", clause):
                    continue
            forward = re.fullmatch(rf"(?:把|将)?(.+?)(?:{_REPLACE})(.+)", clause)
            reverse = re.fullmatch(r"用(.+?)(?:替换|更换)(.+)", clause)
            if forward or reverse:
                old, new = forward.groups() if forward else reverse.groups()[::-1]
                if re.fullmatch(r"(?:全部|所有|携带的?|已选的?)?(?:载荷(?:配置)?|工具(?:清单)?|携带工具|payload)", old.strip(), re.IGNORECASE):
                    commands.append(("set", self._payload_items(new), []))
                elif any(term in old for term in terms) or re.search(r"工具|载荷", old):
                    commands.append(("replace", self._payload_items(new), self._payload_items(old)))
                continue
            match = re.fullmatch(rf"(?P<operation>{_REMOVE}|{_ADD})(?P<items>.+)", clause)
            if match and (any(term in match['items'] for term in terms) or re.search(r"工具|载荷", match['items'])):
                operation = "remove" if re.fullmatch(_REMOVE, match['operation']) else "add"
                commands.append((operation, self._payload_items(match['items']), []))
            elif commands and any(term in clause for term in terms) and not re.search(r"保留|不变|改|设|选", clause):
                # A comma-separated list can continue a preceding add/remove.
                op, items, targets = commands[-1]
                if op in {"add", "remove"}:
                    items.extend(item for item in self._payload_items(clause) if item not in items)
        if not commands and not guarded:
            return None
        return [{"field": "payload", "operation": op, "items": items,
                 "target_items": targets, "raw_text": message, "confidence": 1.0,
                 "source": "user_input"} for op, items, targets in commands]
