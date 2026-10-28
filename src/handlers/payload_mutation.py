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
from src.extraction.assertion_scope import task_edits_are_paused
from src.slots.slot_store import Slot, normalize_payload_match_key

logger = logging.getLogger("src.dialogue_manager")

_REMOVE = r"删除|移除|去掉|卸下|取消携带|别带|不要带|不要"
# Consume the complete field label before treating the remainder as tool names.
_ADD = r"添加|增加|新增|加装|再带|加上|补充|配备|装载|搭载|配置|携带(?:工具\s*(?:为|是|[:：])\s*)?|带上|带|配"
_REPLACE = r"替换成|替换为|换成|换为|改成|改为"
_NEGATED_ACTION = re.compile(
    r"(?:不要|不用|无需|不能|别|不|勿)\s*"
    r"(?:(?:替我|帮我|给我|为我|擅自|自动|随意|继续|再|去)\s*)*"
    r"(?:删除|移除|去掉|卸下|清空|清除|替换|更换|换成|添加|增加|新增|加装|配备|装载|搭载|配置|再带|加上|补充|携带|带上|修改|改动|调整|变更|更改|更新|改|配)"
)
_PAYLOAD_CONDITION = re.compile(r"如果|假如|若")
_NONASSERTED_EDIT = re.compile(
    rf"{_PAYLOAD_CONDITION.pattern}|能否|是否|吗|会怎样|会怎么样|会有什么|[?？]"
)
_HISTORICAL_EDIT = re.compile(r"昨天|昨日|此前|之前|曾经|上次|上一轮|过去|历史|原先")
_CANCELLED_ACTION = re.compile(
    r"(?:取消|撤销)\s*(?:删除|移除|去掉|卸下|清空|清除|替换|更换|添加|增加|新增|"
    r"加装|配备|装载|搭载|配置|修改|改动|调整|变更|更改|更新|设置)"
)
_CANCEL_PREVIOUS_EDIT = re.compile(
    r"(?:请|先|暂时)?(?:取消|撤销)(?:这个|该|上述|刚才的?)?(?:修改|调整|改动|变更|操作)"
)
_TRAILING_EDIT_QUESTION = re.compile(
    r"[,，]\s*(?:这样)?(?:可以吗|行吗|好吗|是否可以)\s*[。.!！]?\s*$"
)
_PAUSE_PREFIX = r"(?:(?:请|本轮|这轮|这次|现在|目前|暂时|先)\s*)*"
_PAYLOAD_FIELD = (
    r"(?:(?:原有的?|现有的?|已选的?|当前的?|全部|所有)\s*)*"
    r"(?:载荷(?:配置|清单|列表)?|(?:携带的?)?工具(?:配置|清单|列表)?|payload)"
)
_PAYLOAD_ADD_CONTEXT = re.compile(
    rf"^(?P<negation>(?:不要|不用|无需|不能|别|不|勿)\s*)?"
    rf"在\s*{_PAYLOAD_FIELD}\s*(?:中|里)\s*(?:再\s*)?(?=(?:{_ADD}))",
    re.IGNORECASE,
)
_PAUSED_EDIT = (
    r"(?:不要|不用|无需|不能|别|不|勿)\s*"
    r"(?:(?:再|先|替我|帮我|擅自)\s*)*"
    r"(?:修改|改动|调整|变更|更改|更新|改|动)"
)
_PAYLOAD_PAUSE = re.compile(
    rf"(?:{_PAUSE_PREFIX}{_PAUSED_EDIT}\s*{_PAYLOAD_FIELD}"
    rf"|{_PAUSE_PREFIX}{_PAYLOAD_FIELD}\s*{_PAUSE_PREFIX}{_PAUSED_EDIT})\s*(?:了|吧)?",
    re.IGNORECASE,
)
_PAYLOAD_CANCEL = re.compile(
    rf"{_PAUSE_PREFIX}(?:(?:取消|撤销)\s*(?:本轮|这次|刚才的?)?\s*{_PAYLOAD_FIELD}"
    rf"\s*(?:的)?(?:修改|调整|改动|变更|配置|设置)?"
    rf"|{_PAYLOAD_FIELD}\s*(?:的)?(?:修改|调整|改动|变更|配置|设置)\s*(?:取消|撤销))\s*(?:了|吧)?",
    re.IGNORECASE,
)
_PAYLOAD_ASSIGNMENT = re.compile(
    r"(?:载荷(?:配置|清单|列表)?|工具(?:配置|清单|列表)?|携带工具|payload)\s*"
    r"(?:设置为|设为|设成|配置为)\s*(?P<items>.+)"
    r"|(?:载荷|工具|payload)\s*(?:为|是|[:：])\s*(?P<plain_items>.+)",
    re.IGNORECASE,
)
_PAYLOAD_DISCUSSION = re.compile(
    _PAUSE_PREFIX + r"(?:仅|只)(?:是)?\s*(?:讨论|咨询|了解|询问|解释|分析)(?:一下)?(?P<object>.+)"
)
_KEEP_EDIT_BOUNDARY = re.compile(
    rf"(?:并且|同时|并|但)\s*(?=(?:保留|保持|{_REMOVE}|{_ADD}|清空|清除|把|将|用))"
)
_CLEAR_PAYLOAD = re.compile(
    r"^(?:请|帮我|给我)?(?:清空|清除|删除|移除|卸下)(?:全部|所有)?(?:携带的?|选配的?|已选的?)?(?:工具|载荷|payload)$"
    r"|^(?:所有|全部)(?:工具|载荷)(?:都)?(?:不要|删除|移除|卸下)$"
    r"|^(?:不带任何工具|什么工具都不带|什么都不带)$", re.IGNORECASE,
)
_PENDING_PAYLOAD = re.compile(
    r"^(?:携带的?工具|工具|(?:选配)?载荷|payload)"
    r"(?:(?:和|及|与|、)(?:支持船(?:编号)?|母船))*"
    r"(?:(?:我(?:们)?|现在|目前|暂时|都|均|还)\s*)*"
    r"(?:(?:尚未|未|还没(?:有)?|没(?:有)?)(?:决定|选定|选择|确认)|未定|待定|待确认|待选择)",
    re.IGNORECASE,
)


class PayloadMutationManager(BaseDialogueHandler):
    """载荷修改与列表变异管理器"""

    @property
    def kb(self) -> Any:
        return getattr(self.manager, "kb", None)

    @property
    def task_state(self) -> Any:
        return getattr(self.manager, "task_state", None)


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
        # This pre-planner shortcut is only for standalone editor commands.
        # Questions, negated requests and mixed instructions belong to the model
        # planner; words like 载荷 and 修改 in separate clauses are not consent.
        command = re.sub(r"[\s，,。.!！?？]+", "", msg)
        actions = "|".join(re.escape(keyword) for keyword in direct_keywords)
        return bool(re.fullmatch(
            rf"(?:(?:请|麻烦|帮我|我想|我要|帮忙))*"
            rf"(?:{actions}|(?:修改|更换|重新选择|重新配置)(?:一下)?(?:载荷|payload|携带工具|工具))"
            r"(?:一下|吧|好吗)?",
            command,
        ))

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

        # A field-wide restriction applies before both card confirmation and
        # clause recovery; a later named edit must not override that scope.
        paused = self._payload_writes_are_paused(user_message)

        # A card confirmation explicitly supplies the complete selection. Keep
        # it deterministic while still using the normal validation transaction.
        selection = re.fullmatch(
            r"确认选择(?:携带工具|载荷|payload|Payloads?|Tools?)\s*[:：]\s*(.+)",
            (user_message or "").strip(),
            flags=re.IGNORECASE,
        )
        if selection and not paused and getattr(self.manager, "editing_slot", None) == "payload":
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

        explicit = [] if paused else self._explicit_payload_mutations(
            user_message, current_slots, unresolved=extraction_res.setdefault("unresolved", []),
        )
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
        add_kws = ("添加", "加装", "增加", "新增", "加上", "还要", "补充", "带上", "携带", "配置", "配合", "还要带", "加个")
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
    def _payload_writes_are_paused(message: str) -> bool:
        if task_edits_are_paused(message):
            return True
        for clause in re.split(r"[，,。；;！!？?\n]+", message or ""):
            clause = clause.strip()
            if _PAYLOAD_PAUSE.fullmatch(clause) or _PAYLOAD_CANCEL.fullmatch(clause):
                return True
            discussion = _PAYLOAD_DISCUSSION.fullmatch(clause)
            if discussion and re.fullmatch(_PAYLOAD_FIELD, discussion["object"].strip(), re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _payload_items(text: str) -> list[str]:
        result = []
        for part in re.split(r"[、,，+]|以及|和|及", text):
            part = re.sub(r"^(?:请|把|将|原来的?|当前的?|已有的?|现有的?|一套|一个|一件|个|套|件|携带的?)", "", part.strip())
            part = part.strip(" ：:。.!！")
            if not part:
                continue
            # Preserve an unknown operand so validation rejects it atomically.
            if part not in result:
                result.append(part)
        return result

    @staticmethod
    def _independent_payload_question(clause: str) -> bool:
        """Recognize a separate field query, not approval of the payload edit.

        Generic follow-ups and questions about the named tool's compatibility
        remain dependent on the preceding command.
        """
        text = clause.strip().rstrip("。.!！?？").strip()
        prefix = r"(?:(?:请问|顺便问一下|当前|现在|目前|已选|这个|这台|本次|任务|作业)(?:的)?\s*)*"
        return bool(re.fullmatch(
            prefix + r"(?:"
            r"(?:水深|海水深度)(?:是|为|有)?(?:多少(?:米)?|多深)"
            r"|(?:机器人|设备)(?:支持|可选|能选|可携带|能携带)(?:哪些|什么)(?:工具|载荷)"
            r"|(?:机器人|设备)(?:的)?(?:型号|编号|状态)(?:是|为)?(?:什么|哪个|如何|怎样|多少)"
            r")(?:呢)?", text,
        ))

    @staticmethod
    def _payload_edit_clauses(message: str):
        for sentence in re.split(r"(?<=[。；;！!？?\n])", message or ""):
            # Keep a question's comma-separated operands within its scope.
            # A separate query about another field has its own scope; "可以吗"
            # and tool-compatibility questions still qualify the payload edit.
            clauses = re.split(r"[，,。；;！!\n]", sentence)
            question = sentence.rstrip().endswith(("?", "？")) or _TRAILING_EDIT_QUESTION.search(sentence)
            if question and not (
                len(clauses) > 1
                and PayloadMutationManager._independent_payload_question(clauses[-1])
            ):
                yield sentence
                continue
            historical_prefix = ""
            conditional_prefix = ""
            for clause in clauses:
                clause = clause.strip()
                if _PAYLOAD_CONDITION.search(clause):
                    conditional_prefix = "如果"
                elif conditional_prefix:
                    # A condition remains in force across commas, including
                    # "如果需要，现在删除..."; a new sentence ends that scope.
                    clause = conditional_prefix + clause
                if _HISTORICAL_EDIT.fullmatch(clause):
                    historical_prefix = clause
                    yield clause
                    continue
                present = re.match(r"(?:现在|当前|本次|这次|本轮)\s*", clause)
                if present:
                    historical_prefix = ""
                    clause = clause[present.end():]
                elif historical_prefix:
                    yield historical_prefix + clause
                    continue
                # Apply assertion guards before splitting a keep/edit pair:
                # "保留A并删除B吗" must not become an imperative delete.
                if (re.search(r"保留|保持|不变", clause)
                        and not _NEGATED_ACTION.search(clause)
                        and not _NONASSERTED_EDIT.search(clause)
                        and not _HISTORICAL_EDIT.search(clause)
                        and not _CANCELLED_ACTION.search(clause)
                        and not _PAYLOAD_DISCUSSION.search(clause)):
                    yield from _KEEP_EDIT_BOUNDARY.split(clause)
                else:
                    yield clause

    def _payload_operand_keys(self, items: list[str]) -> set[str]:
        """Match preservation conflicts using exact names and catalog aliases."""
        if not items:
            return set()
        keys = {normalize_payload_match_key(item) for item in items}
        catalog = getattr(getattr(self.manager, "kb", None), "assets", {}).get("payload_catalog", {})
        identities = set()
        for key in keys:
            exact_names = [catalog_id for catalog_id, info in catalog.items()
                           if normalize_payload_match_key(info.get("name")) == key]
            matches = exact_names or [
                catalog_id for catalog_id, info in catalog.items()
                if any(normalize_payload_match_key(alias) == key for alias in (info.get("aliases") or []))
            ]
            # Shared broad aliases must not make two distinct explicit names
            # equivalent. An ambiguous alias itself remains conservative.
            identities.update(f"catalog:{catalog_id}" for catalog_id in matches)
            if not matches:
                identities.add(f"name:{key}")
        return identities

    def _explicit_payload_mutations(
        self, message: str, current_slots: dict, *, unresolved: list | None = None,
    ) -> list[dict] | None:
        """Recover explicit list edits; ambiguous language stays with extraction.

        Only payload operands from the current sentence are used. Negated and
        hypothetical commands never become destructive operations. The result
        still passes through the normal allowed-values and transaction checks.
        """
        terms = self._payload_terms(current_slots)
        commands = []
        guarded = False
        kept_items = []
        exclusive_keep = False
        previous_command: int | None = None
        previous_payload_clause = False

        def append_command(operation, items, targets):
            nonlocal previous_command
            previous_command = len(commands)
            commands.append((operation, items, targets))

        for clause in self._payload_edit_clauses(message):
            clause = re.sub(
                r"^(?:(?:那就|那么|那|然后|并且|再|并|但是|但|先|请你|请|帮我|给我|我想要|我想|我要|麻烦你)\s*)+",
                "", clause.strip(),
            )
            if not clause:
                continue
            if _CANCEL_PREVIOUS_EDIT.fullmatch(clause.strip("。.!！")):
                if previous_payload_clause:
                    guarded = True
                    if previous_command is not None:
                        del commands[previous_command:]
                previous_command = None
                previous_payload_clause = False
                continue
            previous_command = None
            previous_payload_clause = any(term in clause for term in terms) or bool(
                re.search(r"载荷|工具|payload", clause, re.IGNORECASE)
            )
            if _PENDING_PAYLOAD.match(clause):
                # Here 携带工具 names the undecided field; 携带 is not an
                # instruction to add a tool. Other clauses may still edit it.
                guarded = True
                continue
            contextual_add = _PAYLOAD_ADD_CONTEXT.match(clause)
            if ((contextual_add and contextual_add["negation"])
                    or _NEGATED_ACTION.search(clause) or _NONASSERTED_EDIT.search(clause)
                    or _HISTORICAL_EDIT.search(clause) or _CANCELLED_ACTION.search(clause)
                    or _PAYLOAD_DISCUSSION.fullmatch(clause)):
                guarded = guarded or previous_payload_clause
                continue
            if _CLEAR_PAYLOAD.fullmatch(clause):
                append_command("clear", [], [])
                continue
            if re.search(r"保留|保持|不变", clause):
                # A keep-only payload clause is evidence against model edits,
                # just like a negated command. Explicit edits in other clauses
                # still take precedence over the empty guarded result.
                payload_keep = any(term in clause for term in terms) or bool(
                    re.search(r"载荷|工具|payload", clause, re.IGNORECASE)
                )
                guarded = guarded or payload_keep
                before, keep_word, after = re.split(r"(保留|保持|不变)", clause, maxsplit=1)
                keep_text = after if keep_word == "保留" and after else (before or after)
                keep_text = re.sub(r"(?:继续|保持|不变|都)+$", "", keep_text).strip()
                if re.search(r"(?:只|仅)保留", clause) and payload_keep:
                    exclusive_keep = True
                if (payload_keep and re.search(r"载荷|工具|payload", keep_text, re.IGNORECASE)
                        and not re.search(r"其他|其余", keep_text)
                        and re.fullmatch(r"(?:原有|已有|现有的?|全部|所有|已选|携带的?|选配的?)*(?:载荷|工具|payload)", keep_text, re.IGNORECASE)):
                    slot = current_slots.get("payload")
                    kept_items.extend(slot.value if slot and isinstance(slot.value, list) else [])
                elif payload_keep:
                    kept_items.extend(self._payload_items(keep_text))
                # Remove+keep in one clause only takes the removal's operands.
                clause = re.split(r"保留|保持|不变", clause, maxsplit=1)[0]
                clause = re.sub(r"(?:并且|同时|并|但)\s*$", "", clause)
                # Here "携带工具" is the subject of "保持不变", not an add
                # verb followed by the unknown tool named "工具".
                if re.fullmatch(r"携带的?工具(?:配置|清单|列表)?(?:都)?", clause.strip()):
                    continue
                if not re.search(rf"{_REMOVE}|{_REPLACE}|{_ADD}|替换", clause):
                    continue
            assignment = _PAYLOAD_ASSIGNMENT.fullmatch(clause)
            if assignment:
                append_command("set", self._payload_items(
                    assignment["items"] or assignment["plain_items"],
                ), [])
                continue
            forward = re.fullmatch(rf"(?:把|将)?(.+?)(?:{_REPLACE})(.+)", clause)
            reverse = re.fullmatch(r"用(.+?)(?:替换|更换)(.+)", clause)
            if forward or reverse:
                old, new = forward.groups() if forward else reverse.groups()[::-1]
                if re.fullmatch(r"(?:全部|所有|携带的?|已选的?)?(?:载荷(?:配置)?|工具(?:清单)?|携带工具|payload)", old.strip(), re.IGNORECASE):
                    append_command("set", self._payload_items(new), [])
                elif any(term in old for term in terms) or re.search(r"工具|载荷", old):
                    append_command("replace", self._payload_items(new), self._payload_items(old))
                continue
            # A field context is not an operand. Strip it only for an explicit
            # add, after assertion guards, so a following keep clause cannot
            # erase "在现有携带工具中再添加..." as an unrecognized command.
            add_clause = _PAYLOAD_ADD_CONTEXT.sub("", clause, count=1)
            match = re.fullmatch(rf"(?P<operation>{_REMOVE}|{_ADD})(?P<items>.+)", add_clause)
            if match and (any(term in match['items'] for term in terms) or re.search(r"工具|载荷", match['items'])):
                operation = "remove" if re.fullmatch(_REMOVE, match['operation']) else "add"
                append_command(operation, self._payload_items(match['items']), [])
            elif commands and any(term in clause for term in terms) and not re.search(r"保留|不变|改|设|选", clause):
                # A comma-separated list can continue a preceding add/remove.
                op, items, targets = commands[-1]
                if op in {"add", "remove", "set"}:
                    items.extend(item for item in self._payload_items(clause) if item not in items)
                    previous_command = len(commands) - 1
        conflict = False
        kept_keys = self._payload_operand_keys(kept_items)
        for op, items, targets in commands:
            removed = items if op == "remove" else targets if op == "replace" else []
            if op == "clear" and kept_keys:
                conflict = True
            elif op == "set" and kept_keys - self._payload_operand_keys(items):
                conflict = True
            elif kept_keys & self._payload_operand_keys(removed):
                conflict = True
        if conflict or exclusive_keep:
            if unresolved is not None:
                unresolved.append(
                    "载荷指令冲突：同一工具既要求保留又要求删除或替换，请确认要保留还是移除；原有载荷配置未修改。"
                    if conflict else
                    "“只保留”涉及移除其他载荷，请确认完整工具清单，例如“载荷改成……”；原有载荷配置未修改。"
                )
            return []
        if not commands and not guarded:
            return None
        return [{"field": "payload", "operation": op, "items": items,
                 "target_items": targets, "raw_text": message, "confidence": 1.0,
                 "source": "user_input"} for op, items, targets in commands]
