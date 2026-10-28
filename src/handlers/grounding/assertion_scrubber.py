"""
src/handlers/grounding/assertion_scrubber.py - 模型未提交断言、幻觉词及排版语法清洗器
"""

from __future__ import annotations

import re
from typing import Callable
from src.constants import FIELD_LABELS


def remove_model_receipts(reply: str) -> str:
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


def scrub_uncommitted_vessel_assertions(reply: str) -> str:
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


def scrub_uncommitted_start_time_assertions(reply: str) -> str:
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


def scrub_uncommitted_slot_assertions(reply: str, visible: dict, task_state: dict) -> str:
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
            original = scrub_uncommitted_start_time_assertions(original)
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
            return scrub_uncommitted_vessel_assertions(original)
        return original

    return re.sub(r"[^。！？!?\n]+[。！？!?]?", scrub_sentence, reply)


def remove_write_welcome_template(reply: str) -> str:
    """Discard the misplaced capability menu in a WRITE response only."""
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


def remove_write_off_topic_template(reply: str) -> str:
    """Discard any misplaced off-topic reject template in a WRITE response."""
    pattern = (
        r"抱歉[，,]?\s*本系统专注于[^\n。！？]*"
        r"(?:水下设备能力与状态查询|任务创建与准入校验|任务状态查询与发布管理)[^\n。！？]*"
        r"三类场景[。！？!?]?\s*"
        r"(?:请描述您的水下作业需求[^\n。！？]*[。！？!?]?)?"
    )
    cleaned = re.sub(pattern, "", reply).strip()
    cleaned = re.sub(r"请描述您的水下作业需求[，,]?或提出以上三类范围内的问题[。！？!?]?", "", cleaned).strip()
    return cleaned


def clean_hallucinations_and_syntax(
    scrubbed: str,
    *,
    actual_model_reply: str,
    visible: dict,
    task_state: dict | None,
    violations: list,
    labels: list[str],
    missing_fields: list[dict] | None,
    repair_missing_fn: Callable[..., str] | None = None,
) -> str:
    """清洗越权发布、未提交更新臆造词及标点语法缺陷。"""
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
    if repair_missing_fn:
        scrubbed = repair_missing_fn(
            scrubbed, missing_fields, visible, task_state or {},
        )
    scrubbed = re.sub(r'“([^“”"\n]+)"', r'“\1”', scrubbed)
    return scrubbed
