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
    def _scrub_uncommitted_slot_assertions(reply: str, visible: dict, task_state: dict) -> str:
        """Remove unsupported state assertions, preserving questions and explanations.

        A field mention or a clock value alone is not a claim that it was saved.
        In particular, missing-field questions and committed start times must
        survive when the end time has not been committed in this turn.
        """
        check_end = "end_time" not in visible
        check_payload = "payload" not in visible and bool(task_state.get("payload"))
        if not check_end and not check_payload:
            return reply

        if check_payload:
            # A state heading and its bullet values form one assertion. Removing
            # just the heading would leave unsupported payload names visible.
            reply = re.sub(
                r"(?m)^[ \t]*(?:[-*] |\d+\.\s*)?(?:\*\*)?"
                r"(?:当前|已保存|已选|携带)?(?:载荷(?:配置)?|携带工具)(?:\*\*)?"
                r"[：:][ \t]*\n(?:[ \t]*[-*+] +[^\n]+(?:\n|$))+",
                "", reply,
            )

        def scrub_sentence(match: re.Match) -> str:
            original = match.group(0)
            text = re.sub(r"[*_`]", "", original)
            clauses = re.split(r"[，,；;]", text)

            def guidance_about(names: str) -> bool:
                if re.search(r"示例|例如|比如", text):
                    return True
                named_clauses = [clause for clause in clauses if re.search(names, clause)]
                return bool(named_clauses) and all(re.search(
                    r"(?:请|需要|还需|仍需|待).{0,12}(?:提供|补充|确认|确定|选择|指定|设置|输入|参考|核对|查看|修改)"
                    r"|是否|何时|几点|什么时候|是指|指的是|表示|定义"
                    r"|(?:必须|应当|应该|需要|须|需).{0,8}(?:早于|晚于|大于|小于|一致|匹配)",
                    clause,
                ) for clause in named_clauses)

            def completed_mutation_about(names: str) -> bool:
                # A successful depth update in another clause does not turn a
                # following time/payload question into a mutation claim.
                return any(
                    re.search(names, clause) and re.search(
                        r"已(?:经|成功)?(?:为您|自动)?.{0,12}"
                        r"(?:记录|写入|更新|修改|调整|设置|设定|添加|配置|安装|移除|卸载|删除|清空)",
                        clause,
                    )
                    for clause in clauses
                )

            clocks = re.findall(r"\d{1,2}:\d{2}|[零一二三四五六七八九十\d]+[点时]", text)
            has_clock = bool(clocks)
            if check_end:
                names_end = bool(re.search(r"结束时间|终止时间|截止时间|endtime", text))
                states_end = bool(re.search(r"(?:结束|终止|截止)时间\s*(?:[：:]|(?:仍|当前|目前)?[为是])", text))
                states_window = bool(re.search(r"(?:作业|任务|执行)时间|时间窗口", text)) and len(clocks) >= 2
                if ((names_end and completed_mutation_about(r"结束时间|终止时间|截止时间|endtime"))
                        or (not guidance_about(r"结束时间|终止时间|截止时间|endtime|(?:作业|任务|执行)时间|时间窗口")
                            and (states_window or (names_end and (states_end or has_clock))))):
                    return ""
            if check_payload and re.search(r"载荷|携带工具", text):
                states_payload = bool(re.search(
                    r"(?:当前|目前|已保存|已选|携带).{0,8}(?:载荷|工具)"
                    r"|(?:载荷|携带工具)(?:配置)?\s*(?:[：:]|为|是|包括|包含|仍|依旧|依然|保持|当前|目前)",
                    text,
                ))
                if completed_mutation_about(r"载荷|携带工具") or (states_payload and not guidance_about(r"载荷|携带工具")):
                    return ""
            return original

        return re.sub(r"[^。！？!?\n]+[。！？!?]?", scrub_sentence, reply)

    @staticmethod
    def _ground_blocked_publish_instructions(reply: str, *, hard: bool) -> str:
        """Replace bypass instructions with the required action, retaining list numbering."""
        next_step = (
            "请先修正上述硬约束问题，再继续校验；当前不能确认发布。"
            if hard else
            "如接受这些软警告，请先明确回复“忽略软警告”，待系统重新校验后再确认发布。"
        )

        def correct_instruction(match: re.Match) -> str:
            original = match.group(0)
            text = re.sub(r"[*_`]", "", original)
            false_publication = re.search(r"(?:任务|指令)已(?:经)?(?:发布|下发)|已(?:成功)?发布", text)
            false_acknowledgement = re.search(
                r"已(?:经|成功)?(?:为您)?(?:记录|接受|确认|忽略|处理)"
                r"[^，,；;。]{0,120}(?:软(?:性约束)?警告|警告|风险|提示)"
                r"|(?:软(?:性约束)?警告|警告|风险)(?:处理)?\s*[：:]?\s*已(?:经|成功)?(?:被)?"
                r"(?:接受忽略|确认忽略|忽略|解除|消除|清除|取消|处理完成)"
                r"|已(?:经|成功)?(?:接受|确认)(?:忽略|豁免)"
                r"|(?:系统|警告|软警告|约束)[^，,；;。]{0,20}(?:不再|不会再)(?:阻塞|阻止|拦截)", text,
            )
            false_phase = re.search(
                r"(?:(?:现)?已(?:经)?(?:进入|转入)|(?:任务|流程)[^，,；;。]{0,8}(?:进入|转入))"
                r"(?:最终)?(?:发布确认|确认发布|confirming)", text,
            )
            if false_phase and re.search(r"尚未|还未|未曾|未能|不能|无法|不会|不应|不可", false_phase.group(0)):
                false_phase = None
            direct_action = re.search(
                r"(?:请|可以|可|直接|回复|输入|发送|点击|选择).{0,20}"
                r"(?:确认(?:并)?发布|发布(?:此)?任务|任务发布|发布(?=[。！？!?]|$))"
                r"|(?:是否|能否)(?:现在|立即|正式|直接)?\s*(?:确认)?发布(?:该|此|任务|指令)", text,
            )
            if direct_action:
                instruction = text[max(0, direct_action.start() - 6):direct_action.end()]
                if re.search(r"请勿|不要|不能|无法|不得|不应|不可", instruction):
                    direct_action = None
            if not (false_publication or false_acknowledgement or false_phase or direct_action):
                return original
            prefix = re.match(r"(\s*(?:\d+[.)、]|[-*+])\s*)", original)
            return (prefix.group(1) if prefix else "") + next_step

        return re.sub(r"[^。！？!?\n]+[。！？!?]?", correct_instruction, reply)

    @staticmethod
    def _remove_write_welcome_template(reply: str) -> str:
        """Discard the misplaced capability menu in a WRITE response only.

        The task receipt and any following task-specific question remain useful;
        a short greeting on its own is not this complete welcome template.
        """
        welcome = re.search(
            r"(?:您好[，,！!]?\s*)?SEAgent[^。\n]{0,50}系统已就绪[。.]",
            reply,
        )
        if not welcome:
            return reply
        closing = re.search(r"请直接描述您的(?:作业)?需求[^。\n]*[。.]", reply[welcome.end():])
        if not closing:
            return reply
        end = welcome.end() + closing.end()
        block = reply[welcome.start():end]
        if (re.search(r"系统提供[^。\n]*两类[^。\n]*交互能力", block)
                and "知识与状态查询" in block and "任务创建与准入" in block):
            return (reply[:welcome.start()] + reply[end:]).strip()
        return reply

    @staticmethod
    def _remove_write_off_topic_template(reply: str) -> str:
        """Discard any misplaced off-topic reject template in a WRITE response.

        A write response that commits task slots or tracks workflow must not
        tell the user to describe their underwater requirements as if rejected.
        """
        pattern = (
            r"抱歉[，,]?\s*本系统专注于[^\n。！？]*"
            r"(?:水下设备能力与状态查询|任务创建与准入校验|任务状态查询与发布管理)[^\n。！？]*"
            r"三类场景[。！？!?]?\s*"
            r"(?:请描述您的水下作业需求[^\n。！？]*[。！？!?]?)?"
        )
        cleaned = re.sub(pattern, "", reply).strip()
        cleaned = re.sub(r"请描述您的水下作业需求[，,]?或提出以上三类范围内的问题[。！？!?]?", "", cleaned).strip()
        return cleaned

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
        """Render a grounded WRITE response combining conversational prose with factual receipts.

        Design principles:
        1. Model conversational prose is preserved to maintain human-like dialogue;
        2. Uncommitted claims, unauthorized publications, and false health assurances are scrubbed;
        3. Objective receipts (accepted, unresolved, retained, constraints, missing) serve as the ground truth;
        4. When model prose is empty or purely false, safely degrades to deterministic receipt.
        """
        if isinstance(self_or_reply, str):
            actual_model_reply = self_or_reply
        else:
            actual_model_reply = model_reply if isinstance(model_reply, str) else ""

        context = constraint_context or {}
        violations = context.get("violations") or []
        severities = {v.get("severity") if isinstance(v, dict) else getattr(v, "severity", None)
                      for v in violations}
        has_hard = "hard" in severities or str(context.get("type", "")).startswith("hard")
        has_soft = "soft" in severities or str(context.get("type", "")).startswith("soft")
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
            if has_hard or has_soft:
                # Extractor "unresolved" prose is not authority to acknowledge
                # warnings or advance workflow state either.
                text = WriteReplyGrounder._ground_blocked_publish_instructions(text, hard=has_hard)
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
            cleaned_unresolved = [str(u).rstrip("。；;，,\n ") for u in unresolved if u]
            parts.append("⚠️ 未写入或仍需确认：" + "；".join(cleaned_unresolved) + "。")

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

        receipt = "\n".join(parts)

        # Clearing a list must not retain prose that claims a new payload was
        # installed. The receipt already describes the committed empty value.
        if (visible.get("payload") == [] or (not visible and not task_state)
                or not actual_model_reply or not str(actual_model_reply).strip()):
            return receipt

        cleaned_reply = WriteReplyGrounder._remove_write_off_topic_template(
            WriteReplyGrounder._remove_write_welcome_template(str(actual_model_reply).strip())
        )
        scrubbed = WriteReplyGrounder._scrub_uncommitted_slot_assertions(
            cleaned_reply,
            visible, task_state or {},
        )

        blocked_hard = has_hard or str(context.get("type", "")).startswith("hard")
        blocked_soft = has_soft or str(context.get("type", "")).startswith("soft")
        if blocked_hard or blocked_soft:
            scrubbed = WriteReplyGrounder._ground_blocked_publish_instructions(scrubbed, hard=blocked_hard)

        # 1. 越权发布与下发清洗
        scrubbed = re.sub(r"(，|,)?(已成功发布任务?|可以立即发布|立即发布|任务已下发|指令已下发)", "", scrubbed)

        # 2. 未提交更新时的成功词清洗
        if not visible:
            for false_claim in ("已创建", "已经设置", "已成功设置", "已经成功设置"):
                scrubbed = scrubbed.replace(false_claim, "未写入任务状态")
            if "未写入任务状态" not in scrubbed and "未写入" not in scrubbed:
                scrubbed = "本轮未写入任务状态。" + ("\n" + scrubbed if scrubbed else "")

        # 3. 部分提交时的“均已设置”清洗
        if "均已设置" in scrubbed:
            scrubbed = scrubbed.replace("均已设置", "已部分设置")
        if labels:
            scrubbed = re.sub(
                r"[^。！？!?\n]*(?:参数|信息|字段)[^。！？!?\n]{0,12}"
                r"(?:已(?:经)?(?:收集|填写|补充)?完整|已(?:全部)?收集(?:完成|齐全))[^。！？!?\n]*[。！？!?]?",
                "", scrubbed,
            )
        scrubbed = scrubbed.replace("任务参数已完整", "")

        # 4. 违规状态下的“所有设备正常”清洗
        if violations:
            scrubbed = re.sub(r"(，|,)?所有设备正常(。|\.|\n|$)?", "。", scrubbed)
            # 软警告详情只在侧边栏呈现，清洗模型文本中的软警告细节引用
            for v in violations:
                code = getattr(v, "constraint_id", "") or (v.get("constraint_id", "") if isinstance(v, dict) else "")
                msg = getattr(v, "message", "") or (v.get("message", "") if isinstance(v, dict) else "")
                if code:
                    scrubbed = scrubbed.replace(code, "")
                if msg:
                    scrubbed = scrubbed.replace(msg, "")

        # 清洗内部提示词泄露
        scrubbed = re.sub(r"[，。；\s]*对外可简写为[^\n。，]*[，。]?", "", scrubbed)

        # 清洗臆造的时间窗口矛盾表述
        scrubbed = re.sub(r"[^\n。，]*系统默认开始时间为[^\n。，]*[，。]?", "", scrubbed)
        scrubbed = re.sub(r"[^\n。，]*默认[^\n。，]*\d+\s*小时窗口期[^\n。，]*[，。]?", "", scrubbed)

        scrubbed = re.sub(r"[^\n。，]*请确认清空[^\n。，]*[，。]?", "", scrubbed)

        payload_val = task_state.get("payload") if task_state else None
        payload_empty = not payload_val or payload_val == [] or payload_val == [""]
        if "payload" not in visible or payload_empty:
            scrubbed = re.sub(r"[^\n。，]*已(为您|自动)?(将|把)?载荷[^\n。，]*(更新|设置|添加|配置|写入|确认为)[^\n。，]*[，。]?", "", scrubbed)
            scrubbed = re.sub(r"[^\n。，]*已(为您|自动)?(更新|设置|添加|配置|写入|确认为)[^\n。，]*载荷[^\n。，]*[，。]?", "", scrubbed)
            scrubbed = re.sub(r"[^\n。，]*已(为您|自动)?更新为(高清水下摄像机|浑水水下成像系统|单目水下成像系统)[^\n。，]*[，。]?", "", scrubbed)

        if "payload" not in visible:
            scrubbed = re.sub(r"[^\n。，]*已(删除|移除|卸载|清空)[^\n。，]*[，。]?", "", scrubbed)

        # 严禁任何未经用户确认的自动配置或替换虚假声明
        scrubbed = re.sub(r"[^\n。，]*系统已自动确认此配置[^\n。，]*[，。]?", "", scrubbed)
        scrubbed = re.sub(r"[^\n。，]*已自动为您确认此配置[^\n。，]*[，。]?", "", scrubbed)

        # 6. 去除多余标点、孤立括号与空行
        scrubbed = re.sub(r"[（\(]\s*[）\)]", "", scrubbed)
        scrubbed = re.sub(r"^[）\)]\s*", "", scrubbed)
        scrubbed = re.sub(r"\s*[（\(]$", "", scrubbed)
        if scrubbed.count("）") > scrubbed.count("（"):
            scrubbed = re.sub(r"）([。，\s]*)$", r"\1", scrubbed)
        if scrubbed.count(")") > scrubbed.count("("):
            scrubbed = re.sub(r"\)([。，\s]*)$", r"\1", scrubbed)

        scrubbed = re.sub(r"^[，。；\s]+", "", scrubbed)
        scrubbed = re.sub(r"[，。；\s]+$", "。", scrubbed)
        scrubbed = re.sub(r"([。！？])\1+", r"\1", scrubbed)
        scrubbed = scrubbed.strip()

        # 7. 去重检查：如果 scrubbed 已经包含了部分客观条目，避免在拼接时重复堆叠
        deduped_parts = []
        for part in parts:
            prefix = part.split("：")[0] if "：" in part else part
            if prefix in ("✅ 已记录", "仍需补充", "⚠️ 未写入或仍需确认", "当前已保存", "本轮未写入任务状态。"):
                if prefix.rstrip("。") in scrubbed:
                    continue
            deduped_parts.append(part)

        final_receipt = "\n".join(deduped_parts)
        if not scrubbed:
            return receipt

        # 如果清洗后 scrubbed 只是单纯重复了 "本轮未写入任务状态"，则直接返回 receipt
        if scrubbed in ("本轮未写入任务状态。", "本轮未写入任务状态"):
            return receipt

        combined = f"{scrubbed}\n{final_receipt}".strip()
        combined = re.sub(r"([。！？])\1+", r"\1", combined)
        return combined
