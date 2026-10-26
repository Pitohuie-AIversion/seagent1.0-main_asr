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
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from src.extraction import coord_parser
from src.telemetry_evidence import ground_telemetry_reply
from src.temporal.duration_parser import parse_chinese_number
from ..constants import FIELD_LABELS

class WriteReplyGrounder:
    """槽位提交事实锚点与回复展示生成器"""

    # Explicit field labels are distinct from domain discussion or recommendations.
    # Each assertion is checked against the same committed field as its receipt.
    _FACT_FIELD_ALIASES = {
        "start_time": (FIELD_LABELS["start_time"], "任务开始时间", "起始时间", "启动时间"),
        "end_time": (FIELD_LABELS["end_time"], "任务结束时间", "终止时间", "截止时间"),
        "task_type": (FIELD_LABELS.get("task_type", "任务类型"), "任务类型", "作业类型"),
        "cable_type": (FIELD_LABELS["cable_type"], "电缆类型", "管线类型", "管道类型", "缆线类型"),
        "start_point": (FIELD_LABELS["start_point"], "起始点坐标", "起点坐标", "起始点", "起点"),
        "end_point": (FIELD_LABELS["end_point"], "结束点坐标", "终点坐标", "结束点", "终点"),
        "oilfield_coordinates": (FIELD_LABELS["oilfield_coordinates"], "油田经纬度坐标", "油田经纬度", "油田坐标"),
        "equipment_type": (FIELD_LABELS["equipment_type"], "机器人型号", "作业设备型号", "作业设备"),
        "equipment_family": (FIELD_LABELS["equipment_family"], "作业机器人系列"),
        "equipment_unit_id": (FIELD_LABELS["equipment_unit_id"], "机器人编号"),
        "equipment_name": (FIELD_LABELS["equipment_name"],),
        "wellhead_id": (FIELD_LABELS.get("wellhead_id", "井口编号"), "井口", "井号"),
        "oilfield_name": (FIELD_LABELS.get("oilfield_name", "油田名称"), "作业油田", "目标油田"),
        "support_vessel": (FIELD_LABELS.get("support_vessel", "支持船编号"), "支持船", "作业母船", "母船", "作业船", "支援船", "母船支持"),
    }

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

    def get_payload_configuration(self) -> dict | None:
        """Use the same selected robot as the task's knowledge context."""
        state = self.manager.task_state
        kb = self.manager.kb
        equipment = state.get("equipment_type") or state.get("equipment_name")
        if state.get("equipment_unit_id"):
            unit = kb.resolve_robot_unit(
                str(state["equipment_unit_id"]), state.get("task_type_key"),
                str(state["equipment_type"]) if state.get("equipment_type") else None,
            )
            if unit:
                return unit.get("robot")
        return kb.get_rov_for_task(str(equipment), state.get("task_type_key")) if equipment else None

    @staticmethod
    def _remove_model_receipts(reply: str) -> str:
        """Replace model receipts, including Markdown field blocks, with real ones."""
        heading = re.compile(
            r"^[ \t]*(?:(?:#{1,6}|>|[-*+]|\d+[.)、])\s+)*"
            r"(?:\*\*|__|`)?(?:✅\s*已记录|当前已保存|仍需补充|⚠️\s*未写入或仍需确认)"
            r"(?:\*\*|__|`)?[ \t]*[：:][ \t]*(?:\*\*|__|`)?"
        )
        labels = "|".join(re.escape(label) for label in FIELD_LABELS.values())
        field_line = re.compile(rf"^(?:任务|作业)?(?:{labels})(?:\s*[：:]|[。；;]?$)")
        cleaned = []
        in_block = False
        for line in reply.splitlines():
            match = heading.match(line)
            if match:
                remainder = line[match.end():]
                # Keep a follow-up sentence even when it shares the receipt's line.
                boundary = re.search(r"[。！？!?]", remainder)
                followup = remainder[boundary.end():].lstrip("*_` ") if boundary else ""
                if followup:
                    cleaned.append(followup)
                in_block = not followup
                continue
            if in_block:
                plain = re.sub(r"[*_`]", "", line).strip()
                plain = re.sub(r"^(?:[-+]\s+|\d+[.)、]\s+)", "", plain)
                if not plain:
                    continue
                if field_line.match(plain):
                    value = re.split(r"[：:]", plain, maxsplit=1)[-1].strip()
                    if re.match(r"请|需要|还需|仍需|待|建议|推荐|可选|候选|例如|比如", value):
                        cleaned.append(line)
                        continue
                    boundary = re.search(r"[。！？!?]", line)
                    followup = line[boundary.end():].lstrip("*_` ") if boundary else ""
                    if not followup:
                        continue
                    cleaned.append(followup)
                    in_block = False
                    continue
                in_block = False
            cleaned.append(line)
        return "\n".join(cleaned)

    @staticmethod
    def _ground_payload_classification(reply: str, configuration: dict | None) -> str:
        """Correct explicit onboard/optional claims using exact catalogue names."""
        if not configuration:
            return reply
        onboard = set(configuration.get("onboard_payloads") or [])
        optional = set(configuration.get("supported_payloads") or []) - onboard
        names = onboard | optional
        if not names:
            return reply
        name_pattern = re.compile("|".join(re.escape(name) for name in sorted(names, key=len, reverse=True)))
        previous_subjects = set()

        def ground_sentence(match: re.Match) -> str:
            nonlocal previous_subjects
            original = match.group(0)
            text = re.sub(r"[*_`]", "", original)
            mentioned = set(name_pattern.findall(text))
            prior_subjects, previous_subjects = previous_subjects, mentioned
            if re.search(r"[？?]|是否|能否|为何|为什么|吗", text):
                return original
            corrections = set()
            for category in re.finditer(r"标配|自带|机载|内置|选配|外部加装|额外挂载|额外加装", text):
                prefix = text[:category.start()]
                clause = re.split(r"[，,；;]", prefix)[-1]
                if re.search(r"请|建议|推荐|例如|比如|如果|假如|可选|候选", clause):
                    continue
                if re.search(r"(?:不是|并非|不属于|不作为|无需|不必|不需要|未)[^，,；;]{0,4}$", clause):
                    continue
                if not re.search(r"是|为|属|作为|已|需|要|机器人.{0,10}的", clause):
                    continue
                subjects = set(name_pattern.findall(clause))
                if not subjects and re.search(r"该(?:设备|载荷|工具)|上述(?:设备|载荷|工具)|它", clause):
                    subjects = prior_subjects
                claims_onboard = category.group(0) in {"标配", "自带", "机载", "内置"}
                corrections.update(subjects & (optional if claims_onboard else onboard))
            if not corrections:
                return original
            corrected = "".join(
                f"{name}属于{'机载设备，无需另行加装' if name in onboard else '选配载荷'}。"
                for name in sorted(corrections)
            )
            # An independent request following the mistaken explanation is useful.
            guidance = re.search(r"[，,；;]((?:请|建议|推荐|如需|若需)[^。！？!?]*[。！？!?]?)$", original)
            return corrected + (guidance.group(1) if guidance else "")

        return re.sub(r"[^。！？!?\n]+[。！？!?]?", ground_sentence, reply)

    @staticmethod
    def _ground_water_depth_assertions(reply: str, visible: dict, task_state: dict) -> str:
        """Align explicit task-depth assertions without rewriting surrounding prose."""
        value = visible.get("water_depth", task_state.get("water_depth"))
        try:
            saved_depth = Decimal(str(value))
        except InvalidOperation:
            saved_depth = None
        if saved_depth is not None and not saved_depth.is_finite():
            saved_depth = None

        label = r"(?:任务|作业)?水深(?:[（(]\s*(?:米|m)\s*[）)])?"
        done = r"已(?:经)?(?:成功)?(?:为您|为你)?"
        verb = r"(?:记录|设置|设定|保存|更新|修改|调整)"
        assertion = re.compile(
            rf"(?:(?:{done})?(?:将|把)?{label}\s*(?:已)?从[^。！？!?\n]{{1,80}}?(?:改|调整|更新|设置)(?:为|成|到)"
            rf"|{done}{verb}(?:了)?\s*{label}"
            rf"|{done}(?:将|把)\s*{label}\s*{verb}"
            rf"|{label}\s*{done}{verb}"
            rf"|(?:当前|目前|现有|已保存的?|已记录的?)\s*{label}(?:仍)?"
            rf"|{label}\s*(?:[：:]|为|是))"
            r"\s*(?:为|是|至|到|[：:])?\s*"
            r"(?P<number>[+-]?(?:\d+(?:\.\d+)?|\.\d+)|负?[零〇一二两三四五六七八九十百千万亿点半]+?)\s*"
            r"(?P<unit>千米|公里|km|米|m|英尺|feet|ft)?(?![\d.a-zA-Z零〇一二两三四五六七八九十百千万亿点半])",
            re.IGNORECASE,
        )
        # Match through emphasis while retaining original formatting and every
        # other number, including the old value in a before/after explanation.
        positions = [index for index, char in enumerate(reply) if char not in "*_`"]
        plain = "".join(reply[index] for index in positions)
        replacements = []
        for match in assertion.finditer(plain):
            clause_start = max(plain.rfind(mark, 0, match.start()) for mark in "，,；;。！？!?\n") + 1
            clause_end = min((pos for mark in "，,；;。！？!?\n"
                              if (pos := plain.find(mark, match.end())) >= 0), default=len(plain))
            prefix = plain[clause_start:match.start()]
            suffix = plain[match.end():clause_end + 1]
            sentence_start = max(plain.rfind(mark, 0, match.start()) for mark in "。！？!?\n") + 1
            sentence_prefix = plain[sentence_start:match.start()]
            if (re.search(r"例如|比如|示例|假如|如果|假设", sentence_prefix)
                    or re.search(r"请|建议|推荐|是否|能否|尚未|还未|没有|不要|不是|并非|未曾|不能|无法", prefix)
                    or re.search(r"设备|机器人|最大|额定|上限|允许", prefix)
                    or re.search(r"[？?]|吗|示例", suffix)
                    or re.match(r"\s*(?:时|的话|以上|以下|左右|以内|以外|至|到|[-~～])", suffix)):
                continue
            if saved_depth is None:
                replacements.append((positions[match.start()], positions[match.end() - 1] + 1, ""))
                continue
            unit = (match["unit"] or "米").lower()
            factor = Decimal("1000") if unit in {"千米", "公里", "km"} else (
                Decimal("0.3048") if unit in {"英尺", "feet", "ft"} else Decimal("1")
            )
            try:
                stated_depth = Decimal(match["number"])
            except InvalidOperation:
                parsed = parse_chinese_number(match["number"].removeprefix("负"))
                if parsed is None:
                    continue
                stated_depth = Decimal(str(parsed)) * (-1 if match["number"].startswith("负") else 1)
            if stated_depth * factor == saved_depth:
                continue
            number = format(saved_depth / factor, "f")
            if "." in number:
                number = number.rstrip("0").rstrip(".")
            replacements.append((positions[match.start("number")], positions[match.end("number") - 1] + 1, number))
        for start, end, number in reversed(replacements):
            reply = reply[:start] + number + reply[end:]
        return reply

    @staticmethod
    def _time_claim_matches(saved: object, value: str) -> bool | None:
        """Compare only stated date/clock components; never invent a 'now' base."""
        try:
            expected = saved if isinstance(saved, datetime) else datetime.fromisoformat(str(saved).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        historical = r"(?:原先|原来|原定|此前|之前|旧时间|调整前|修改前)"
        value = re.sub(rf"[（(]\s*{historical}[^（）()]*[）)]", "", value)
        value = re.split(rf"[，,]\s*{historical}", value, maxsplit=1)[0]
        # Explanations may put a reference clock in parentheses and the actual
        # claimed result after "即". Do not discard that result when removing
        # historical annotations, or let relative wording bypass its check.
        equivalent_values = re.split(
            r"(?:[，,（(]\s*)?(?:也就是说|也就是|也即|即|换算(?:后)?为|折合为|对应(?:的时间)?为)\s*[：:]?\s*",
            value,
        )
        if len(equivalent_values) > 1:
            matches = [WriteReplyGrounder._time_claim_matches(saved, item) for item in equivalent_values]
            if False in matches:
                return False
            return True if True in matches else None
        value = re.split(r"[（(，,]", value, maxsplit=1)[0].strip()
        if re.search(r"[今明后昨]天|[上下本这]周|(?:小时|分钟|秒|天)(?:之|以)?[前后]", value):
            return None
        display_zone = None
        zone = re.search(r"(?:UTC|GMT)\s*([+-]\d{1,2}(?::\d{2})?)?|北京时间|(?<=\d)Z\b|([+-]\d{2}:?\d{2})[。；;]?$", value, re.IGNORECASE)
        if zone:
            offset = zone.group(1) or zone.group(2) or ("+08" if zone.group() == "北京时间" else "+00")
            sign = -1 if offset.startswith("-") else 1
            digits = offset.lstrip("+-")
            if ":" not in digits and len(digits) == 4:
                digits = digits[:2] + ":" + digits[2:]
            chunks = digits.split(":")
            try:
                display_zone = timezone(sign * timedelta(hours=int(chunks[0]), minutes=int(chunks[1]) if len(chunks) > 1 else 0))
            except ValueError:
                return False
        full = re.search(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?", value,
        )
        if full:
            try:
                claimed = datetime.fromisoformat(full.group().replace("Z", "+00:00"))
            except ValueError:
                return False
            if claimed.tzinfo is None and (display_zone is not None or expected.tzinfo is not None):
                claimed = claimed.replace(tzinfo=display_zone or expected.tzinfo)
            if claimed.tzinfo is not None and expected.tzinfo is None:
                return None
            if not re.search(r"[T ]\d{1,2}:\d{2}:\d{2}", full.group()):
                expected = expected.replace(second=0, microsecond=0)
            elif "." not in full.group():
                expected = expected.replace(microsecond=0)
            return claimed == expected

        date_match = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?", value)
        clock = re.search(r"(?<!\d)(\d{1,2})[:：](\d{2})(?::(\d{2}))?", value)
        has_seconds = bool(clock and clock.group(3))
        if clock:
            hour, minute, second = int(clock.group(1)), int(clock.group(2)), int(clock.group(3) or 0)
        else:
            clock = re.search(r"([零〇一二两三四五六七八九十\d]+)[点时](?:([零〇一二两三四五六七八九十\d]+)分?|半)?", value)
            if clock:
                parsed_hour = parse_chinese_number(clock.group(1))
                parsed_minute = parse_chinese_number(clock.group(2)) if clock.group(2) else (30 if "半" in clock.group() else 0)
                if parsed_hour is None or parsed_minute is None:
                    return None
                hour, minute, second = int(parsed_hour), int(parsed_minute), 0
        if display_zone is not None:
            if expected.tzinfo is None:
                return None
            expected = expected.astimezone(display_zone)
        if date_match:
            try:
                date = datetime(*(int(part) for part in date_match.groups())).date()
            except ValueError:
                return False
            if date != expected.date():
                return False
        if not clock:
            return True if date_match else None
        prefix = value[:clock.start()]
        if re.search(r"下午|晚上|晚间|夜间", prefix) and hour < 12:
            hour += 12
        elif "中午" in prefix and 1 <= hour <= 6:
            hour += 12
        elif re.search(r"凌晨|午夜", prefix) and hour == 12:
            hour = 0
        if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
            return False
        return (hour, minute) == (expected.hour, expected.minute) and (not has_seconds or second == expected.second)

    @staticmethod
    def _ground_time_windows(reply: str, visible: dict, task_state: dict) -> str:
        """Validate both endpoints of explicit windows, retaining follow-up prose."""
        pattern = re.compile(
            r"(?:当前|目前)?(?:作业时间|任务时间|执行时间|时间窗口|(?<![\w])时间)\s*[：:为是]\s*"
            r"(?P<start>[^。！？!?\n]+?)\s*(?:至|到|～|~|—)\s*(?P<end>[^。！？!?\n]+)"
        )
        positions = [index for index, char in enumerate(reply) if char not in "*_`"]
        plain = "".join(reply[index] for index in positions)
        replacements = []
        for match in pattern.finditer(plain):
            prefix = plain[max(plain.rfind(mark, 0, match.start()) for mark in "。！？!?\n") + 1:match.start()]
            if re.search(r"请|是否|能否|例如|比如|示例|如果|假如|假设|建议|推荐", prefix):
                continue
            if match.end() < len(plain) and plain[match.end()] in "？?":
                continue
            end = match["end"]
            followup = re.search(r"[，,；;]\s*((?:请|建议|推荐|需要|还需|仍需|下一步|接下来).*)", end)
            end_value = end[:followup.start()] if followup else end
            for key, value in (("start_time", match["start"]), ("end_time", end_value)):
                saved = visible.get(key, task_state.get(key))
                if saved is None or WriteReplyGrounder._time_claim_matches(saved, value) is False:
                    start, stop = positions[match.start()], positions[match.end() - 1] + 1
                    while start and reply[start - 1] in "*_`":
                        start -= 1
                    while stop < len(reply) and reply[stop] in "*_`":
                        stop += 1
                    replacements.append((start, stop, followup.group(1) if followup else ""))
                    break
        for start, stop, replacement in reversed(replacements):
            reply = reply[:start] + replacement + reply[stop:]
        return reply

    @staticmethod
    def _remove_unsupported_now_gloss(reply: str) -> str:
        """A saved timestamp alone cannot prove an assertion that it equals now."""
        reply = re.sub(
            r"((?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?|\d{1,2}:\d{2}(?::\d{2})?)"
            r"[*_`]*\s*)[（(]\s*(?:即|也就是|也即)(?:当前时间|现在|此刻)\s*[）)]",
            r"\1", reply,
        )
        return re.sub(
            r"[（(]\s*(?:(?:即|也就是|也即)\s*)?(?:当前时间|现在|此刻)\s*(?:为|是|[：:])?\s*"
            r"\d{1,2}[:：]\d{2}(?::\d{2})?[^（）()\n]*[）)]", "", reply,
        )

    @staticmethod
    def _repair_missing_field_block(reply: str, missing_fields: list[dict] | None,
                                    visible: dict, task_state: dict) -> str:
        """Repair broken collection lists using actual missing slots, keeping useful questions.

        Ordinary prose and intact model questions remain untouched. A list is
        rebuilt only when its count/numbering is broken, repeats a field, or
        asks again for a field that is already saved and no longer missing.
        """
        if missing_fields is None:
            return reply
        missing = {item["key"]: item for item in missing_fields
                   if isinstance(item, dict) and item.get("key") and item.get("type") not in ("auto", "fixed")}
        aliases = {alias: key for key, names in WriteReplyGrounder._FACT_FIELD_ALIASES.items() for alias in names}
        aliases.update({label: key for key, label in FIELD_LABELS.items()})
        aliases.update({str(item.get("label") or key): key for key, item in missing.items()})
        aliases.update({"载荷": "payload", "水深": "water_depth"})
        labels = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
        heading = re.compile(r"(?:还需|仍需|需要).{0,15}(?:以下|下列)\s*(\d+)\s*个.{0,8}(?:参数|字段|信息)")
        numbered = re.compile(r"^\s*(\d+)[.)、]\s*(.+)$")
        field = re.compile(rf"^(?:{labels})(?=\s*[：:（(]|$)")
        lines = reply.splitlines()
        result = []
        i = 0
        while i < len(lines):
            title = heading.search(re.sub(r"[*_`]", "", lines[i]))
            if not title:
                result.append(lines[i])
                i += 1
                continue
            j, entries = i + 1, []
            while j < len(lines):
                if not lines[j].strip():
                    j += 1
                    continue
                plain_line = re.sub(r"[*_`]", "", lines[j])
                item = numbered.match(plain_line)
                name = field.match(item[2]) if item else None
                if item and name:
                    raw_item = numbered.match(lines[j])
                    body = raw_item[2] if raw_item else f"**{item[2]}**"
                    entries.append((int(item[1]), aliases.get(name[0]), body))
                    j += 1
                    continue
                # A field's description/options can span unindented lines.
                # Stop at an explicit section/closing prompt, not at the first
                # explanatory sentence, which would leave later items behind.
                boundary = re.match(
                    r"\s*(?:#{1,6}\s|[|]|✅|⚠️|当前已保存|仍需补充|任务尚未发布|"
                    r"请(?:补充|提供|确认)(?:以上|上述)|请回复|请逐项|请核对|您可以|随后|接下来)",
                    plain_line,
                )
                if entries and not boundary and not heading.search(plain_line):
                    number, key, body = entries[-1]
                    entries[-1] = (number, key, body + "\n" + lines[j])
                    j += 1
                    continue
                break
            keys = [key for _, key, _ in entries if key]
            saved = {**task_state, **visible}
            invalid = (
                [number for number, _, _ in entries] != list(range(1, int(title[1]) + 1))
                or len(keys) != len(set(keys))
                or any(key not in missing and saved.get(key) not in (None, [], "") for key in keys)
                or not missing
            )
            if not invalid:
                result.extend(lines[i:j])
                i = j
                continue
            if missing:
                selected = list(missing.items())[:3]
                result.append(f"接下来，还需要您补充以下 {len(selected)} 个参数：")
                originals = {key: body for _, key, body in entries if key in missing}
                for number, (key, item) in enumerate(selected, 1):
                    label = item.get("label") or FIELD_LABELS.get(key, key)
                    body = originals.get(key, f"**{label}**：请提供{label}。")
                    result.append(f"{number}. {body}")
            i = j
        return "\n".join(result)

    @staticmethod
    def _ground_field_assertions(reply: str, visible: dict, task_state: dict) -> str:
        """Keep only explicit field-value claims supported by committed facts.

        Parse coordinate values as a whole so their comma is never mistaken for
        a boundary before independent follow-up guidance.
        """
        aliases = {alias: key for key, names in WriteReplyGrounder._FACT_FIELD_ALIASES.items() for alias in names}
        label = "(?:" + "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True)) + ")"
        done = r"已(?:经|成功)?(?:为您|为你|自动)?"
        verb = r"(?:记录|写入|设置|设定|保存|更新|修改|调整|选择|选定|确认|关联|绑定|配置|指定|预设)"
        head = re.compile(
            rf"(?:(?:{done})?(?:将|把)?{label}\s*(?:已)?从[^。！？!?\n]{{1,120}}?(?:改|调整|更新|设置)(?:为|成|到)"
            rf"|{done}{verb}(?:了)?\s*{label}|{done}(?:将|把)\s*{label}\s*{verb}"
            rf"|{label}\s*{verb}(?:完成|成功|好了)"
            rf"|{label}\s*{done}{verb}|(?:当前|目前|现有|已保存的?|已记录的?)\s*{label}(?:仍)?"
            rf"|{label}\s*(?:[：:]|(?:仍|当前|目前)?[为是]))\s*(?:为|是|至|到|[：:])?\s*"
        )
        label_pattern = re.compile(label)
        nonassertion = r"请(?!(?:您|你)?(?:注意|知悉))|建议|推荐|可选|候选|例如|比如|示例|假如|如果|假设|是否|能否|尚未|还未|没有|不要|不是|并非|未曾|不能|无法|另一|其他"
        pending = r"(?:当前|目前|仍)?(?:请|需要|还需|仍需|待|未|尚未|还未|暂无|无|建议|推荐|可选|候选|提供|补充|确认|选择)"

        def compact(value: object) -> str:
            return re.sub(r"[\s*_`“”‘’\"']", "", str(value)).lower()

        def supported(key: str, value: str) -> bool:
            saved = visible.get(key, task_state.get(key))
            if key in {"start_time", "end_time"}:
                # Missing values retain the established missing-time scrubber;
                # relative wording without a reference clock is not guessed.
                return saved is None or WriteReplyGrounder._time_claim_matches(saved, value) is not False
            if saved is None or saved == "":
                return False
            candidate = re.split(r"[（(]", value, maxsplit=1)[0].rstrip("，,；;。 ")
            if compact(candidate) == compact(coord_parser.format_slot_display_value(key, saved)):
                return True
            if key in coord_parser.COORD_FIELDS:
                parsed = coord_parser.parse_coord_value(value)
                expected = coord_parser.parse_coord_value(saved)
                return parsed is not None and parsed == expected
            if key in {"equipment_unit_id", "wellhead_id", "support_vessel"}:
                return compact(candidate) == compact(saved)
            candidate = re.split(r"[，,]", candidate, maxsplit=1)[0]
            # A shorter display name may omit a generic suffix, but cannot add
            # a contradictory value to the saved name.
            candidate = compact(candidate)
            if len(candidate) < 2:
                return False
            pattern = re.escape(candidate)
            if re.match(r"[\d.]", candidate):
                pattern = r"(?<![\d.])" + pattern
            if re.search(r"[\d.]$", candidate):
                pattern += r"(?![\d.])"
            return re.search(pattern, compact(saved)) is not None

        def ground_sentence(match: re.Match) -> str:
            original = match.group(0)
            positions = [index for index, char in enumerate(original) if char not in "*_`"]
            plain = "".join(original[index] for index in positions)
            assertions = list(head.finditer(plain))
            removals = []
            for index, assertion in enumerate(assertions):
                key = aliases[label_pattern.search(assertion.group()).group()]
                end = assertions[index + 1].start() if index + 1 < len(assertions) else len(plain)
                tail = plain[assertion.end():end]
                guidance = re.search(r"[，,；;]\s*(?:请|建议|推荐|需要|还需|仍需|下一步|接下来|同时|另外|例如|比如)", tail)
                if guidance:
                    end = assertion.end() + guidance.start()
                value = plain[assertion.end():end].strip()
                clause_start = max(plain.rfind(mark, 0, assertion.start()) for mark in "，,；;") + 1
                prefix = plain[clause_start:assertion.start()]
                sentence_prefix = plain[:assertion.start()]
                if (not value or re.search(nonassertion, prefix)
                        or re.search(r"例如|比如|示例|假如|如果|假设", sentence_prefix)
                        or re.match(pending, value)
                        or re.match(r"[（(]?\s*(?:示例|例如|比如)[：:]?", value)
                        or re.search(r"(?:的话|情况下)(?:[，,].*)?[。！？!?]?$", value)
                        or (key not in {"start_time", "end_time"} and re.search(r"时(?:[，,].*)?[。！？!?]?$", value))
                        or (key in {"start_time", "end_time"} and re.match(r"必须|应当|应该|需要|须|早于|晚于|不早于|不晚于", value))
                        or re.search(r"[？?]|吗[。！？!?]?$", value)):
                    continue
                negated = re.match(r"(?:不是|并非|不为|不等于)\s*(.*)", value)
                if negated:
                    contrast = re.split(r"[，,]\s*而是\s*", negated.group(1), maxsplit=1)
                    if not supported(key, contrast[0]) and (len(contrast) == 1 or supported(key, contrast[1])):
                        continue
                elif supported(key, value):
                    continue
                # Include the boundary comma; the adjacent factual clause or
                # follow-up request retains its own formatting and punctuation.
                while end < len(plain) and plain[end] in "，,；; ":
                    end += 1
                start_offset = positions[assertion.start()]
                end_offset = positions[end] if end < len(positions) else len(original)
                while start_offset and original[start_offset - 1] in "*_`":
                    start_offset -= 1
                removals.append((start_offset, end_offset))
            terminal = re.search(r"[。！？!?]$", original)
            for start, end in reversed(removals):
                original = original[:start] + original[end:]
            if removals:
                original = re.sub(r"[，,；;]+$", "", original.rstrip())
                original = re.sub(r"^\s*(?:\d+[.)、]|[-*+])\s*$", "", original)
                if original and terminal and not re.search(r"[。！？!?]$", original):
                    original += terminal.group(0)
            return original

        return re.sub(r"[^。！？!?\n]+[。！？!?]?", ground_sentence, reply)

    @staticmethod
    def _ground_payload_assertions(reply: str, visible: dict, task_state: dict) -> str:
        """Check configuration and edit claims against the final optional list.

        Final state can disprove an edit without reconstructing its history:
        added items must be present, and replaced items must no longer remain.
        Onboard equipment is never added to this saved-list evidence.
        """
        saved = visible.get("payload", task_state.get("payload", []))
        if not isinstance(saved, (list, tuple)):
            return reply

        def compact(value: str) -> str:
            return re.sub(r"[\s*_`“”‘’\"']", "", value).lower()

        expected = {compact(str(item)) for item in saved if str(item).strip()}
        names = r"(?:携带工具|携带载荷|工具载荷|载荷(?:配置)?)"
        done = r"已(?:经|成功)?(?:为您|为你|自动)?"
        replacement = re.compile(
            rf"(?:{done})?(?:将|把)?{names}\s*(?:{done})?(?:从|由)"
            rf"(?P<old>.+?)(?:替换|更换|换|改|调整)(?:为|成|到)"
        )
        addition = re.compile(
            rf"(?:{done}(?:新增|增加|添加|加装)(?:了)?\s*{names}"
            rf"|{names}\s*{done}(?:新增|增加|添加|加装)(?:了)?)\s*[：:]?\s*"
        )
        configuration = re.compile(
            rf"(?:(?:{done})?(?:将|把){names}(?:设置|设定|更新|修改|调整)(?:为|成)"
            rf"|{done}(?:设置|设定|更新|修改|调整)(?:了)?{names}\s*(?:为|成|[：:])?"
            rf"|(?:当前|目前|已保存的?|已选的?)?{names}\s*"
            rf"(?:{done}(?:设置|设定|更新|修改|调整)(?:为|成)|"
            rf"(?P<includes>包括|包含)|(?:仅|只)?(?:保留|剩下)|[：:]|为|是))\s*"
        )
        nonassertion = re.compile(
            r"请(?!(?:您|你)?(?:注意|知悉))|建议|推荐|可选|候选|例如|比如|示例|如果|假如|假设|"
            r"是否|能否|尚未|还未|未曾|没有|不要|不是|并非|不能|无法"
        )
        other_labels = "|".join(re.escape(alias) for aliases in WriteReplyGrounder._FACT_FIELD_ALIASES.values() for alias in aliases)
        boundary = re.compile(
            rf"[，,；;]\s*(?=请|建议|推荐|需要|还需|仍需|下一步|接下来|同时|另外|例如|比如|"
            rf"(?:当前|目前)?(?:{other_labels}))"
        )
        expected_pattern = re.compile("|".join(re.escape(item) for item in sorted(expected, key=len, reverse=True))) if expected else None

        def listed(value: str) -> tuple[set[str], bool]:
            value = compact(value).strip("。；;[]【】")
            found = set(expected_pattern.findall(value)) if expected_pattern else set()
            remainder = expected_pattern.sub("", value) if expected_pattern else value
            remainder = re.sub(r"[、，,；;]|以及|和|与|及", "", remainder)
            return found, not remainder and bool(found)

        def old_items_remain(value: str) -> bool:
            value = compact(value).strip("。；;[]【】")
            separator = r"[、，,；;]|以及|和|与|及"
            return any(re.search(rf"(?:^|{separator}){re.escape(item)}(?=$|{separator})", value)
                       for item in expected)

        def ground_block(match: re.Match) -> str:
            items = re.sub(r"(?m)^[ \t]*[-*+]\s+", "", match["items"])
            found, complete = listed("、".join(items.splitlines()))
            return match.group() if complete and found == expected else ""

        reply = re.sub(
            rf"(?m)^[ \t]*(?:\*\*)?(?:当前|目前|已保存的?|已选的?)?{names}(?:\*\*)?"
            rf"\s*[：:][ \t]*(?:\*\*)?[ \t]*\n"
            rf"(?P<items>(?:[ \t]*[-*+]\s+[^\n]+(?:\n|$))+)",
            ground_block, reply,
        )

        def ground_sentence(match: re.Match) -> str:
            original = match.group()
            positions = [index for index, char in enumerate(original) if char not in "*_`"]
            plain = "".join(original[index] for index in positions)
            for pattern, kind in ((replacement, "replace"), (addition, "add"), (configuration, "configuration")):
                claim = pattern.search(plain)
                if not claim:
                    continue
                if (nonassertion.search(plain[:claim.start()])
                        or re.search(r"[？?]|吗[。！？!?]?$", plain)):
                    return original
                end = len(plain)
                followup = boundary.search(plain, claim.end())
                if followup:
                    end = followup.start()
                value = plain[claim.end():end].strip()
                if not value or re.match(r"(?:尚未|还未|暂无|未|无|待|请|需要|建议|推荐|可选|候选|例如|比如)", value):
                    return original
                # State headings can carry a completed action after the colon.
                value = re.sub(rf"^{done}(?:设置|设定|更新|修改|调整)(?:为|成)\s*", "", value)
                items, complete = listed(value)
                supported = complete and (
                    kind == "add"
                    or (kind == "configuration" and (claim.group("includes") or items == expected))
                    or (kind == "replace" and not old_items_remain(claim.group("old")))
                )
                if supported:
                    return original
                start = positions[claim.start()]
                stop = positions[end] if end < len(positions) else len(original)
                while start and original[start - 1] in "*_`":
                    start -= 1
                if followup:
                    stop = positions[followup.end()] if followup.end() < len(positions) else len(original)
                return original[:start] + original[stop:]
            return original

        return re.sub(r"[^。！？!?\n]+[。！？!?]?", ground_sentence, reply)

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
    def _scrub_uncommitted_vessel_assertions(reply: str) -> str:
        """Drop vessel state claims by clause, keeping guidance and its formatting."""
        vessel_names = r"(?:母船支持|支持船|母船|作业船|支援船)"
        actions = r"(?:关联|选择|选定|配置|设置|绑定|指定|预设)"
        nonassertion = (
            r"请(?!(?:您|你)?(?:注意|知悉))|是否|能否|尚未|还未|未曾|未能|没有|暂无|暂未|无法|不能|不要|不应|"
            r"待|将要|需要|仍需|还需|可选|可以|建议|推荐|例如|比如|示例|如果|若|"
            r"(?:未|不)(?:被|成功)?$"
        )

        def scrub_clause(match: re.Match) -> str:
            original = match.group(0)
            text = re.sub(r"[*_`]", "", original).strip()
            if not re.search(vessel_names, text):
                return original
            # Choosing a vessel is also a common request, pending state, or
            # question. A bare action verb is not proof of a completed write.
            if text.endswith(("？", "?")):
                return original
            completed = re.search(
                rf"已(?:经|成功)?(?:(?:为您|为你|自动|成功|完成))*"
                rf"(?:(?:将|把){vessel_names}(?:编号|配置)?)?{actions}"
                rf"|{actions}(?:完成|成功|好了)",
                text,
            )
            # Only a request/negation before the claim can qualify it. A tail
            # such as "（待确认）" or "满足作业需要" cannot undo "已配置".
            if completed:
                return original if re.search(nonassertion, text[:completed.start()]) else ""
            state_value = re.search(
                rf"{vessel_names}(?:编号|配置)?\s*(?:[：:]|为|是)\s*(\S.*?)"
                r"[，,；;。！？!?]?$",
                text,
            )
            if state_value:
                prefix = text[:state_value.start(1)]
                value = state_value.group(1)
                guidance_value = re.match(
                    r"(?:当前|目前|暂时|仍然|仍)?"
                    r"(?:请|尚未|还未|暂无|暂未|未|无|不|需|待|可选|候选|"
                    r"提供|补充|确认|确定|选择|建议|预计|计划)",
                    value,
                )
                if not re.search(nonassertion, prefix) and not guidance_value:
                    return ""
            return original

        cleaned = re.sub(r"[^，,；;。！？!?]+[，,；;。！？!?]?", scrub_clause, reply)
        if cleaned == reply:
            return reply
        # If the last assertion was removed, use the sentence's original
        # terminal punctuation for the surviving guidance, not a dangling comma.
        terminal = re.search(r"[。！？!?]$", reply)
        cleaned = re.sub(r"[，,；;]+$", "", cleaned.rstrip())
        if cleaned and terminal and not re.search(r"[。！？!?]$", cleaned):
            cleaned += terminal.group(0)
        return cleaned

    @staticmethod
    def _scrub_uncommitted_start_time_assertions(reply: str) -> str:
        """Remove concrete start claims when no start time has been committed."""
        names = r"(?:开始|起始|启动)时间|start_?time"
        temporal_value = r"\d{1,2}:\d{2}|[零〇一二两三四五六七八九十百半\d.]+\s*(?:个\s*)?(?:小时|分钟|天|秒|点|时)"
        removed_start = False

        def scrub_clause(match: re.Match) -> str:
            nonlocal removed_start
            original = match.group(0)
            text = re.sub(r"[*_`]", "", original)
            completed = re.search(
                r"已(?:经|成功)?(?:为您|为你|自动)?.{0,12}"
                r"(?:记录|写入|更新|修改|调整|设置|设定)", text,
            )
            named = re.search(names, text)
            scheduled = re.search(r"(?:任务|作业|执行|计划).{0,30}(?:开始|启动)", text)
            concrete = re.search(temporal_value, text)
            assertion = (named and completed) or (concrete and (named or scheduled))
            if assertion:
                boundary = completed.start() if completed else (named or scheduled).start()
                prefix = text[:boundary]
                if re.search(
                    r"请|是否|何时|几点|什么时候|尚未|还未|暂未|未能|不能|无法|建议|推荐|希望|打算"
                    r"|示例|例如|比如|如果|假如|假设", prefix,
                ):
                    return original
                if not completed and re.search(
                    r"请|建议|推荐|希望|打算|[？?]|是否|何时|几点|什么时候|必须|应当|应该|需要|尚未|还未|未记录"
                    r"|示例|例如|比如|如果|假如|假设", text,
                ):
                    return original
                removed_start = True
                return ""
            # A duration stated as part of the unsupported schedule is not a
            # committed time window either. Independent follow-up requests stay.
            if removed_start and re.match(r"\s*(?:持续|时长)", text) and concrete:
                return ""
            if removed_start and re.search(r"请确认(?:该|此)(?:开始)?时间", text):
                return ""
            return original

        cleaned = re.sub(r"[^，,；;。！？!?]+[，,；;。！？!?]?", scrub_clause, reply)
        if cleaned == reply:
            return reply
        terminal = re.search(r"[。！？!?]$", reply)
        cleaned = re.sub(r"[，,；;]+$", "", cleaned.rstrip())
        if cleaned and terminal and not re.search(r"[。！？!?]$", cleaned):
            cleaned += terminal.group(0)
        return cleaned

    @staticmethod
    def _scrub_uncommitted_slot_assertions(reply: str, visible: dict, task_state: dict) -> str:
        """Remove unsupported state assertions, preserving questions and explanations.

        A field mention or a clock value alone is not a claim that it was saved.
        In particular, missing-field questions and committed start times must
        survive when the end time has not been committed in this turn.
        """
        check_start = "start_time" not in visible and not task_state.get("start_time")
        check_end = "end_time" not in visible and not task_state.get("end_time")
        check_payload = "payload" not in visible
        check_location = "oilfield_name" not in visible and not task_state.get("oilfield_name")
        check_vessel = "support_vessel" not in visible
        if not check_start and not check_end and not check_payload and not check_location and not check_vessel:
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

        # Oilfield names are special: the linker may retain raw/pending
        # metadata while the schema rejects the user-facing ``oilfield_name``.
        # In that state the model often echoes the requested oilfield as if it
        # were already the task location (for example, ``作业区域：流花11-1
        # 油田``). Keep the rejection/explanation prose, but remove only the
        # state-style assertion so the receipt remains the source of truth.
        if "oilfield_name" not in visible and not task_state.get("oilfield_name"):
            def scrub_uncommitted_oilfield_assertions(match: re.Match) -> str:
                line = match.group(0)
                plain = re.sub(r"[*_`]", "", line).strip()
                value = re.split(r"[：:]|\s+(?:为|是)\s+", plain, maxsplit=1)[-1].strip()
                if not re.search(r"(?:油田|气田)", value):
                    return line
                # Guidance such as ``油田名称：请提供坐标`` is useful and has
                # no concrete state claim; only concrete values are removed.
                if re.match(r"(?:请|需要|需|待|未|无法|不能|暂不|尚未|请选择|明确)", value):
                    return line
                return ""

            reply = re.sub(
                r"(?im)^[ \t]*(?:[-*+]\s+|\d+[.)、]\s+)?"
                r"(?:\*\*)?(?:当前|目前|任务|作业)?(?:作业区域|作业地点|油田名称)"
                r"(?:\*\*)?[ \t]*(?:(?:为|是)[ \t]*|[：:])"
                r"[^，。！？!?\n]*?(?:油田|气田)(?:（[^，。！？!?\n]*）)?[。！？!?]?$",
                scrub_uncommitted_oilfield_assertions,
                reply,
            )
            reply = re.sub(
                r"(?<![\u4e00-\u9fff])(?:当前|目前|任务|作业)?(?:作业区域|作业地点|油田名称)"
                r"\s*(?:(?:为|是)\s*|[：:]\s*)"
                r"(?!(?:请|需要|需|待|未|无法|不能|暂不|尚未|请选择|明确))"
                r"[^，。！？!?\n]*(?:油田|气田)"
                r"(?:（[^，。！？!?\n]*）)?",
                "",
                reply,
            )

        def scrub_sentence(match: re.Match) -> str:
            original = match.group(0)
            if check_start:
                original = WriteReplyGrounder._scrub_uncommitted_start_time_assertions(original)
            text = re.sub(r"[*_`]", "", original)
            clauses = re.split(r"[，,；;]", text)

            def guidance_about(names: str) -> bool:
                if re.search(r"示例|例如|比如", text):
                    return True
                named_clauses = [clause for clause in clauses if re.search(names, clause)]
                return bool(named_clauses) and all(re.search(
                    r"(?:请|需要|还需|仍需|待).{0,12}(?:提供|补充|确认|确定|选择|指定|设置|输入|参考|核对|查看|修改)"
                    r"|是否|何时|几点|什么时候|是指|指的是|表示|定义"
                    r"|(?:必须|应当|应该|需要|须|需).{0,8}(?:早于|晚于|大于|小于|一致|匹配)"
                    rf"|(?:建议|推荐|可选|候选).{{0,12}}(?:{names})"
                    rf"|(?:{names})(?:配置)?\s*[：:]?\s*(?:建议|推荐|可选|候选)",
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
                pending_payload = not task_state.get("payload") and all(
                    re.search(r"(?:载荷|携带工具)(?:配置)?\s*[：:]?\s*"
                              r"(?:尚未|还未|暂未|暂无|未选择|未配置|未安装)", clause)
                    for clause in clauses if re.search(r"载荷|携带工具", clause)
                )
                states_payload = bool(re.search(
                    r"(?:当前|目前|已保存|已选|携带).{0,8}(?:载荷|工具)"
                    r"|(?:载荷|携带工具)(?:配置)?\s*(?:[：:]|为|是|包括|包含|仍|依旧|依然|保持|当前|目前)",
                    text,
                ))
                if (completed_mutation_about(r"载荷|携带工具")
                        or (states_payload and not pending_payload and not guidance_about(r"载荷|携带工具"))):
                    return ""
            if check_vessel and re.search(r"支持船|母船|母船支持|作业船|支援船", text):
                return WriteReplyGrounder._scrub_uncommitted_vessel_assertions(original)
            return original

        return re.sub(r"[^。！？!?\n]+[。！？!?]?", scrub_sentence, reply)

    @staticmethod
    def _ground_blocked_publish_instructions(reply: str, *, hard: bool,
                                             include_guidance: bool = True) -> str:
        """Replace bypass instructions with the required action, retaining list numbering."""
        next_step = (
            "请先修正上述硬约束问题，再继续校验；当前不能确认发布。"
            if hard else
            "如接受这些软警告，请先明确回复“忽略软警告”，待系统重新校验后再确认发布。"
        )
        guidance_emitted = False
        warning_choice_rows: set[int] = set()

        def correct_instruction(match: re.Match) -> str:
            nonlocal guidance_emitted
            original = match.group(0)
            text = re.sub(r"[*_`]", "", original)
            # The receipt owns the choice to acknowledge a soft warning or
            # modify the task. A model paraphrase is a duplicate even when it
            # says "您希望忽略上述软性警告" instead of the literal command.
            warning_choice = re.search(
                r"(?:[您你](?:希望|想要?|愿意|是否)|是否(?:希望|愿意|选择)?|要不要|请(?:[您你])?(?:选择|决定))"
                r"[^。！？!?]{0,60}(?:忽略|接受|豁免)[“\"']?\s*(?:上述|这些|此|该|本次|当前|所有)?"
                r"软(?:性)?(?:约束)?警告", text,
            )
            if (warning_choice and re.search(r"[？?]|还是|或者|或修改", text)
                    and not re.search(r"请勿|(?<!要)不要|不得|不能|不应|不可", warning_choice.group(0))):
                warning_choice_rows.add(reply.count("\n", 0, match.start()))
                return ""
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
                r"(?:(?:现)?已(?:经)?(?:进入|转入)|(?:任务|流程|系统)[^，,；;。]{0,8}(?:进入|转入))"
                r"(?:最终)?(?:发布确认|确认发布|confirming)", text,
            )
            if false_phase:
                conditional = text[:false_phase.start()]
                if (re.search(r"尚未|还未|未曾|未能|不能|无法|不会|不应|不可|将|会|待", false_phase.group(0))
                        or re.search(r"待|如果|假如|若|完成后|处理后", conditional)):
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
            warning_action = re.search(
                r"(?:请|可|回复|输入|发送|点击|选择)[^。！？!?]{0,30}(?:忽略|接受)[“\"']?(?:这些|上述)?软警告",
                text,
            )
            if warning_action and re.search(r"请勿|不要|不得|不能|不应|不可", warning_action.group(0)):
                warning_action = None
            if not (false_publication or false_acknowledgement or false_phase or direct_action or warning_action):
                return original
            if not include_guidance or guidance_emitted:
                return ""
            guidance_emitted = True
            prefix = re.match(r"(\s*(?:\d+[.)、]\s*|[-*+]\s+))", original)
            return (prefix.group(1) if prefix else "") + next_step

        grounded = re.sub(r"[^。！？!?\n]+[。！？!?]?(?:[*_`]+)?", correct_instruction, reply)
        if warning_choice_rows:
            # Remove only the choice block's dedicated heading and note, not
            # task facts or independent explanations surrounding the question.
            lines = grounded.splitlines()
            source_lines = reply.splitlines()
            remove_rows = set()
            for row in warning_choice_rows:
                previous, following = row - 1, row + 1
                while previous >= 0 and not source_lines[previous].strip():
                    previous -= 1
                while following < len(source_lines) and not source_lines[following].strip():
                    following += 1
                title = re.sub(r"[*_`]", "", source_lines[previous]).strip() if previous >= 0 else ""
                note = re.sub(r"[*_`]", "", source_lines[following]).strip() if following < len(source_lines) else ""
                if re.fullmatch(
                    r"(?:⚠️?\s*)?(?:请)?(?:明确|确认|选择)(?:[您你]的?)?"
                    r"(?:操作意向|处理意向|处理方式|下一步操作)\s*[：:]?", title,
                ):
                    remove_rows.add(previous)
                if re.fullmatch(
                    r"[（(]注[：:][^（）()]*软(?:性)?(?:约束)?警告[^（）()]*"
                    r"(?:发布|继续)[^（）()]*[）)]\s*[。.]?", note,
                ):
                    remove_rows.add(following)
            grounded = "\n".join(line for index, line in enumerate(lines) if index not in remove_rows)
        return grounded

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
        payload_configuration: dict | None = None,
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
                text = WriteReplyGrounder._ground_blocked_publish_instructions(
                    text, hard=has_hard, include_guidance=False,
                )
            if text and text not in unresolved:
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
        retained_keys = ["task_type", "wellhead_id", "equipment_type", "payload", "start_time", "end_time", "support_vessel"]
        retained_keys.extend(key for key, aliases in WriteReplyGrounder._FACT_FIELD_ALIASES.items()
                             if any(alias in str(actual_model_reply) for alias in aliases))
        if "水深" in str(actual_model_reply):
            retained_keys.append("water_depth")
        for key in dict.fromkeys(retained_keys):
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
        # Structured receipts are rendered from committed state below. A model
        # may echo their headings with different values; keep its follow-up
        # sentences, but never let that echo replace the authoritative receipt.
        cleaned_reply = WriteReplyGrounder._remove_model_receipts(cleaned_reply)
        cleaned_reply = WriteReplyGrounder._remove_unsupported_now_gloss(cleaned_reply)
        cleaned_reply = WriteReplyGrounder._ground_payload_classification(cleaned_reply, payload_configuration)
        cleaned_reply = WriteReplyGrounder._ground_water_depth_assertions(cleaned_reply, visible, task_state or {})
        cleaned_reply = WriteReplyGrounder._ground_time_windows(cleaned_reply, visible, task_state or {})
        cleaned_reply = WriteReplyGrounder._ground_field_assertions(cleaned_reply, visible, task_state or {})
        cleaned_reply = WriteReplyGrounder._ground_payload_assertions(cleaned_reply, visible, task_state or {})
        scrubbed = WriteReplyGrounder._scrub_uncommitted_slot_assertions(
            cleaned_reply,
            visible, task_state or {},
        )
        scrubbed = ground_telemetry_reply(scrubbed, context, task_state=task_state)

        blocked_hard = has_hard or str(context.get("type", "")).startswith("hard")
        blocked_soft = has_soft or str(context.get("type", "")).startswith("soft")
        if blocked_hard or blocked_soft:
            scrubbed = WriteReplyGrounder._ground_blocked_publish_instructions(scrubbed, hard=blocked_hard)
            # The authoritative receipt below owns the gate status. Keep it
            # out of echoed model receipts as well as the actionable step.
            if blocked_soft and not blocked_hard:
                scrubbed = re.sub(r"任务尚未发布，仍有软警告待处理[。.]", "", scrubbed)

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
        if scrubbed != str(actual_model_reply).strip():
            scrubbed = re.sub(r"(?m)^[ \t]*(?:\d+[.)、]|[-*+])?[ \t*_`。！？!?，,；;]*$", "", scrubbed)
        scrubbed = re.sub(r"[（\(]\s*[）\)]", "", scrubbed)
        scrubbed = re.sub(r"^[）\)]\s*", "", scrubbed)
        scrubbed = re.sub(r"\s*[（\(]$", "", scrubbed)
        if scrubbed.count("）") > scrubbed.count("（"):
            scrubbed = re.sub(r"）([。，\s]*)$", r"\1", scrubbed)
        if scrubbed.count(")") > scrubbed.count("("):
            scrubbed = re.sub(r"\)([。，\s]*)$", r"\1", scrubbed)

        scrubbed = re.sub(r"(?m)^[，。；\s]+", "", scrubbed)
        scrubbed = re.sub(r"[，。；\s]+$", "。", scrubbed)
        scrubbed = re.sub(r"([。！？])\1+", r"\1", scrubbed)
        scrubbed = scrubbed.strip()
        scrubbed = WriteReplyGrounder._repair_missing_field_block(
            scrubbed, missing_fields, visible, task_state or {},
        )
        scrubbed = re.sub(r'“([^“”"\n]+)"', r'“\1”', scrubbed)

        # A correction in model prose already carries the actionable step;
        # keep only the factual gate status in the appended receipt.
        if blocked_hard and "请先修正上述硬约束问题，再继续校验；当前不能确认发布。" in scrubbed:
            parts = ["任务尚未发布。" if part == "任务尚未发布，请先修正上述硬约束问题。" else part
                     for part in parts]
        elif blocked_soft and "如接受这些软警告，请先明确回复“忽略软警告”，待系统重新校验后再确认发布。" in scrubbed:
            parts = ["任务尚未发布，仍有软警告待处理。请查看右侧看板。" if part.startswith("任务尚未发布，仍有软警告待处理。") else part
                     for part in parts]

        # A shared heading is not evidence that values or missing fields match.
        deduped_parts = [part for part in parts if part not in scrubbed]

        final_receipt = "\n".join(deduped_parts)
        if not scrubbed:
            return receipt

        # 如果清洗后 scrubbed 只是单纯重复了 "本轮未写入任务状态"，则直接返回 receipt
        if scrubbed in ("本轮未写入任务状态。", "本轮未写入任务状态"):
            return receipt

        combined = f"{scrubbed}\n{final_receipt}".strip()
        combined = re.sub(r"([。！？])\1+", r"\1", combined)
        return combined
