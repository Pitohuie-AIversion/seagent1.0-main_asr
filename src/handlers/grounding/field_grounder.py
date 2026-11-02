"""
src/handlers/grounding/field_grounder.py - 槽位事实、时间窗口、水深与载荷断言对齐校验器
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from src.constants import FIELD_LABELS
from src.extraction import coord_parser
from src.temporal.duration_parser import parse_chinese_number

FACT_FIELD_ALIASES = {
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


def ground_payload_classification(reply: str, configuration: dict | None) -> str:
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
        guidance = re.search(r"[，,；;]((?:请|建议|推荐|如需|若需)[^。！？!?]*[。！？!?]?)$", original)
        return corrected + (guidance.group(1) if guidance else "")

    return re.sub(r"[^。！？!?\n]+[。！？!?]?", ground_sentence, reply)


def ground_water_depth_assertions(reply: str, visible: dict, task_state: dict) -> str:
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


def time_claim_matches(saved: object, value: str) -> bool | None:
    """Compare only stated date/clock components; never invent a 'now' base."""
    try:
        expected = saved if isinstance(saved, datetime) else datetime.fromisoformat(str(saved).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    historical = r"(?:原先|原来|原定|此前|之前|旧时间|调整前|修改前)"
    value = re.sub(rf"[（(]\s*{historical}[^（）()]*[）)]", "", value)
    value = re.split(rf"[，,]\s*{historical}", value, maxsplit=1)[0]
    equivalent_values = re.split(
        r"(?:[，,（(]\s*)?(?:也就是说|也就是|也即|即|换算(?:后)?为|折合为|对应(?:的时间)?为)\s*[：:]?\s*",
        value,
    )
    if len(equivalent_values) > 1:
        matches = [time_claim_matches(saved, item) for item in equivalent_values]
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


def ground_time_windows(reply: str, visible: dict, task_state: dict) -> str:
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
            if saved is None or time_claim_matches(saved, value) is False:
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


def remove_unsupported_now_gloss(reply: str) -> str:
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


def ground_field_assertions(reply: str, visible: dict, task_state: dict) -> str:
    """Keep only explicit field-value claims supported by committed facts."""
    aliases = {alias: key for key, names in FACT_FIELD_ALIASES.items() for alias in names}
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
            return saved is None or time_claim_matches(saved, value) is not False
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


def ground_payload_assertions(reply: str, visible: dict, task_state: dict) -> str:
    """Check configuration and edit claims against the final optional list."""
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
    other_labels = "|".join(re.escape(alias) for aliases in FACT_FIELD_ALIASES.values() for alias in aliases)
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


def ground_blocked_publish_instructions(reply: str, *, hard: bool,
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
