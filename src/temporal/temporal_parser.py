"""
src/temporal/temporal_parser.py — 时间关系抽取、区间推导与相对时间物化引擎

职责：
1. 相对时间、口语化日期与时序语义探测 (has_date_semantics, looks_like_iso_datetime)；
2. 任务持续时长与增量变动抽取 (extract_temporal_relation, extract_duration_delta_text)；
3. 基于时间范围解析器与时长算术物化 end_time 与 start_time 偏移 (materialize_time_relation)；
4. 快照时间与候选时间灵活解析 (parse_state_datetime)。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from src.extraction.assertion_scope import task_edits_are_paused
from src.extraction.model_profile import ModelRole, _is_unsupported_role_keyword_error

from .duration_parser import (
    is_keep_duration_expression,
    parse_duration_evidence,
    parse_duration_spec,
)
from .relative_time_parser import parse_time_range

_DATE_SEMANTICS = re.compile(
    r"今天|今晚|今早|明天|明晚|明早|后天|大后天|昨天|前天|次日|"
    r"\d{4}[年/-]|\d{1,2}[月/-]\d{1,2}|[一二三四五六七八九十]{1,3}月|"
    r"周[一二三四五六日天12345670]|星期[一二三四五六日天12345670]|"
    r"[一二两三四五六七八九十0-9]+天[后前]|[一二两三四五六七八九十0-9]+周[后前]|"
    r"月底|月末|月初|年底|年末|年初"
)


_PRESENT_INSTRUCTION = re.compile(r"\s*(?:(?:请|任务|作业)\s*)*(?:现在|目前|当前|本次|这次|本轮)")
_PAST_CONTEXT = re.compile(r"\s*(?:此前|之前|昨天|昨日|前天|曾经|上次|上一轮|过去|历史|原先)")
_CONDITIONAL_CONTEXT = re.compile(r"如果|假如|假设|要是|倘若|除非|若|只要|只有|仅当")
_TIME_SUBJECTS = (re.compile(r"开始|起始|开工"), re.compile(r"结束|终止|截止|完工|收工"), re.compile(r"持续|时长"))
_DEPENDENT_QUESTION = re.compile(
    r"(?:这样|这个(?:修改|设置|开始时间|结束时间|持续时间|持续时长|时长|时间)?|这么)?"
    r"(?:可以|行吗|是否|能否|可否|合适|安全|会怎样|会怎么样|好吗|对吗)"
)
_DEPENDENT_QUESTION_TAIL = re.compile(r"[？?]|吗|呢|是否|能否|可否|会怎样|会怎么样")
_TIME_SUBJECT_FOLLOWING = re.compile(
    r"水深|机器人|设备|ROV|AUV|支持船|母船|船只|井口|管缆|电缆|起点|终点|"
    r"坐标|经纬度|载荷|工具", re.IGNORECASE
)
_WITHDRAWAL = re.compile(
    r"不要|暂不|暂时不|不必|无需|不需要|不能|禁止|不允许|"
    r"别(?:把|将|再|修改|改|调整|设置|动|推迟|提前|延长|缩短)|勿"
)
_CANCELLATION = re.compile(r"取消|撤销")
_QUESTION_ONLY = re.compile(r"(?<!不)(?:只|仅)(?:是|想|要)?(?:讨论|咨询|了解|询问|问问|假设|举例)")
_QUESTION_GENERAL = re.compile(r"是否|能否|会不会")
_QUESTION_END = re.compile(r"[？?]|(?:吗|么)\s*$|会怎样|会怎么样|会有什么")

_END_TIME_KEEP_RE = re.compile(r"(?:结束|终止|截止|完工|收工)\s*时间\s*(?:保持)?不变")
_START_TIME_KEEP_RE = re.compile(r"(?:开始|起始|开工)时间\s*(?:保持)?不变")

_RANGE_DATE = r"(?:大后天|后天|明天|今天|\d{4}-\d{1,2}-\d{1,2})"
_RANGE_PART = r"(?:凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜间)"
_RANGE_NUMBER = r"[0-9零一二两三四五六七八九十]{1,3}"
_RANGE_CLOCK = rf"(?:{_RANGE_NUMBER}点(?:半|{_RANGE_NUMBER}分?)?|\d{{1,2}}[:：]\d{{2}})"
_DATED_CLOCK_RANGE = re.compile(
    rf"(?P<start>{_RANGE_DATE}\s*{_RANGE_PART}?\s*{_RANGE_CLOCK})\s*(?:到|至)\s*"
    rf"(?P<end>{_RANGE_DATE}?\s*{_RANGE_PART}?\s*{_RANGE_CLOCK})(?=$|[，,。；;！!？?\s])"
)


def parse_relative_datetime(*args: Any, **kwargs: Any) -> Any:
    """动态感知外部针对 src.extraction.extractor.parse_relative_datetime 的 monkey-patch"""
    import sys
    ext_mod = sys.modules.get("src.extraction.extractor")
    if ext_mod is not None:
        prd = getattr(ext_mod, "parse_relative_datetime", None)
        if prd is not None and callable(prd):
            if prd is not parse_relative_datetime:
                return prd(*args, **kwargs)
    from .relative_time_parser import parse_relative_datetime as _prd
    return _prd(*args, **kwargs)


class TemporalParser:
    """时间关系抽取、区间推导与相对时间物化引擎"""

    def __init__(self, llm: Any = None):
        self.llm = llm

    @staticmethod
    def has_explicit_duration(text: str) -> bool:
        """Use the shared duration parser, retaining model-converted week/quarter units."""
        from .relative_time_parser import RELATIVE_OFFSET_PATTERN

        # Elapsed offsets name a point, not an execution duration. Keep a
        # separately stated duration, including adjacent unpunctuated fields.
        if TemporalParser.explicit_duration_relation(text) is not None:
            return True
        duration_text = re.sub(RELATIVE_OFFSET_PATTERN, " ", str(text or ""))
        if parse_duration_evidence(duration_text).success or is_keep_duration_expression(duration_text):
            return True
        return bool(re.search(
            r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百千万点半]+)\s*(?:个)?\s*"
            r"(?:周|星期|刻钟)", duration_text,
        ))

    # ──────────────────────────────────────────────────────────────────────────
    # 静态工具方法
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def parse_state_datetime(value: object) -> datetime | None:
        """灵活解析快照或状态中的时间对象（兼容字符串、字典、Slot 对象与 UTC 'Z' 后缀）"""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, dict):
            value = value.get("value") or value.get("normalized_value")
            if value is None:
                return None
        elif hasattr(value, "value") and not isinstance(value, (str, bytes)):
            value = value.value

        text = str(value).strip()
        if not text or text.lower() in ("none", "null"):
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    @staticmethod
    def looks_like_iso_datetime(value: object) -> bool:
        """快速判断字符串是否具备 ISO-8601 前缀格式"""
        if not isinstance(value, str):
            return False
        return bool(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value.strip()))

    @staticmethod
    def has_date_semantics(text: str) -> bool:
        """探测文本是否含有显式日期、相对日或星期语义"""
        if not text:
            return False
        return bool(_DATE_SEMANTICS.search(text))

    @staticmethod
    def mentions_end_time_keep(text: str) -> bool:
        """探测文本是否明确提及结束时间保持不变"""
        if not text:
            return False
        return bool(_END_TIME_KEEP_RE.search(text))

    @staticmethod
    def extract_duration_delta_text(text: str) -> str | None:
        """从用户口语中提取增量时长修饰文本（如'时长再延长1小时'）"""
        adjustment = TemporalParser.extract_time_adjustments(text).get("duration")
        return adjustment[0] if adjustment else None

    @staticmethod
    def extract_time_adjustments(text: str) -> dict[str, tuple[str, float]]:
        """Ground explicit target/direction/amount in the current user turn.

        Calendar edits ("延长到明天下午1点") are endpoints, not duration deltas.
        Amount parsing is shared with the duration engine, including filler words.
        """
        return TemporalParser._time_adjustment_evidence(text)[0]

    @staticmethod
    def _time_context_modifiers(text: str, start: int, end: int) -> tuple[bool, bool]:
        """History crosses commas until a present instruction; questions end it."""
        sentence_start = max((text.rfind(mark, 0, start) for mark in "。；;！!？?\n"), default=-1) + 1
        historical = conditional = False
        for context in re.split(r"[，,]", text[sentence_start:end]):
            if _PRESENT_INSTRUCTION.match(context):
                historical = False
            if _PAST_CONTEXT.match(context):
                historical = True
            conditional |= bool(_CONDITIONAL_CONTEXT.search(context))
        return historical, conditional

    @staticmethod
    def _nonassertive_time_context(
        text: str, start: int, end: int, *, allow_withdrawal: bool = False,
    ) -> bool:
        """Bind history and dependent questions to the time statement they qualify."""
        boundaries = "，,。；;！!？?\n"
        clause_start = max((text.rfind(mark, 0, start) for mark in boundaries), default=-1) + 1
        clause_end = min((pos for mark in boundaries
                          if (pos := text.find(mark, end)) >= 0), default=len(text))
        clause = text[clause_start:clause_end]
        if text[clause_end:clause_end + 1] in {"？", "?"}:
            clause += text[clause_end]
        historical, conditional = TemporalParser._time_context_modifiers(text, start, clause_end)
        dependent_question = False
        if text[clause_end:clause_end + 1] in {"，", ","}:
            following = re.match(r"[^，,。；;！!？?\n]*(?:[？?])?", text[clause_end + 1:])[0].strip()
            # "可以吗" qualifies this edit; "可以把水深改为300米吗"
            # is an independent question with its own named subject.
            has_subject = _TIME_SUBJECT_FOLLOWING.search(following)
            current_subjects = {index for index, pattern in enumerate(_TIME_SUBJECTS) if pattern.search(clause)}
            following_subjects = {index for index, pattern in enumerate(_TIME_SUBJECTS) if pattern.search(following)}
            has_subject = has_subject or bool(following_subjects and not current_subjects & following_subjects)
            dependent_question = not has_subject and bool(
                _DEPENDENT_QUESTION.match(following)
                and _DEPENDENT_QUESTION_TAIL.search(following)
            )
        return bool(
            historical or conditional or dependent_question
            or _WITHDRAWAL.search(clause)
            or (not allow_withdrawal and _CANCELLATION.search(clause))
            or _QUESTION_ONLY.search(clause)
            or _QUESTION_GENERAL.search(clause)
            or _QUESTION_END.search(clause)
        )

    @staticmethod
    def _time_edits_are_paused(text: str) -> bool:
        """Whole-turn restrictions on time must survive clause recovery.

        Require the complete object: a restriction on water depth or a task
        identifier does not forbid an independently requested time edit.
        Endpoint-specific restrictions remain with the existing clause guards.
        """
        if task_edits_are_paused(text):
            return True
        prefix = r"(?:(?:请|本轮|这轮|这次|现在|目前|暂时|先|以上)\s*)*"
        target = r"(?:(?:任何|所有|全部|当前|现有)\s*)*(?:任务时间|起止时间|时间(?:参数|配置|字段)?)"
        refusal = r"(?:不要|别|不|无需|不必|不需要|禁止|不允许|不能|勿)\s*(?:再|先)?\s*"
        action = r"(?:修改|改动|调整|变更|更改|更新|设置|写入|改|动)"
        patterns = (
            prefix + refusal + action + r"\s*" + target + r"\s*(?:了|吧)?",
            prefix + refusal + r"(?:把|将)\s*" + target + r"\s*" + action + r"\s*(?:了|吧)?",
            prefix + target + r"\s*(?:先|暂时|都)?\s*" + refusal + action + r"\s*(?:了|吧)?",
            prefix + r"(?:我|我们)?(?:只|仅)(?:是|想|要)?(?:讨论|咨询|了解|询问|问问)\s*"
            + target + r"\s*(?:而已)?",
        )
        return any(re.fullmatch(pattern, clause.strip())
                   for clause in re.split(r"[，,。；;！!？?\n]+", str(text or ""))
                   for pattern in patterns if clause.strip())

    @staticmethod
    def _cancelled_time_targets(text: str) -> set[str]:
        """Distinguish withdrawing a field edit from rejecting one delta.

        A local '取消这个修改' refers to the preceding time statement in the
        same sentence. It must not cancel a later independent endpoint edit.
        """
        cancelled: set[str] = set()
        targets = {
            "start_time": r"开始|起始|开工",
            "end_time": r"结束|终止|截止|完工|收工",
            "duration": r"持续|时长",
        }
        text = str(text or "")
        for sentence in re.finditer(r"[^。；;！!？?\n]+", text):
            previous_targets: set[str] = set()
            for clause_match in re.finditer(r"[^，,]+", sentence[0]):
                clause = clause_match[0]
                start = sentence.start() + clause_match.start()
                end = sentence.start() + clause_match.end()
                if TemporalParser._nonassertive_time_context(
                    text, start, end, allow_withdrawal=True,
                ):
                    previous_targets = set()
                    continue
                # Bind withdrawals to task fields. A camera's start-time
                # correction must not withdraw an independent task start.
                task_field_clause = re.match(
                    r"\s*(?:(?:请|先|暂时|把|将|对|取消|撤销|任务|作业)\s*)*"
                    r"(?:开始|起始|开工|结束|终止|截止|完工|收工|持续|时长)", clause,
                )
                if task_field_clause:
                    mentioned = {key for key, pattern in targets.items() if re.search(pattern, clause)}
                else:
                    mentioned = set()
                    if _DATED_CLOCK_RANGE.search(clause):
                        mentioned.update({"start_time", "end_time"})
                    for statement in TemporalParser._explicit_time_statements(clause):
                        if statement["kind"] == "duration":
                            mentioned.add("duration")
                        elif statement["kind"] == "endpoint":
                            mentioned.add("start_time" if statement["label"] in (
                                "开始时间", "起始时间", "开工时间", "开始", "开工",
                            ) else "end_time")
                withdrawal = re.search(r"取消|撤销", clause)
                if withdrawal and not re.search(r"不要\s*(?:取消|撤销)|别\s*(?:取消|撤销)|[吗么]\s*$", clause):
                    if mentioned and not TemporalParser.has_explicit_duration(clause):
                        cancelled.update(mentioned)
                    elif not mentioned and re.fullmatch(
                        r"\s*(?:请|先|暂时)?(?:取消|撤销)(?:这个|该|上述|刚才(?:的)?)?(?:修改|调整|改动|变更)\s*", clause,
                    ):
                        cancelled.update(previous_targets)
                elif not withdrawal:
                    previous_targets = mentioned
        return cancelled

    @staticmethod
    def _time_adjustment_evidence(text: str) -> tuple[dict[str, tuple[str, float]], set[str]]:
        adjustments = {}
        ignored_targets = set()
        rejected_deltas: dict[str, set[float]] = {}
        text = str(text or "")
        for match in re.finditer(
            r"(?P<target>开始时间|起始时间|开工时间|结束时间|终止时间|截止时间|完工时间|收工时间|持续时间|持续时长|时长)"
            r"\s*(?:再|又|继续|比原来|比原本)?\s*"
            r"(?P<action>增加|减少|加长|缩短|延长|加上|减去|推迟|延后|提前|推后|后移|前移|加|减)"
            r"(?:了)?\s*(?P<amount>[^，,。；;！!？?\n]+)", text,
        ):
            if re.match(r"(?:到|至|为)", match["amount"]):
                continue
            # The target is already grounded above; parse only the action and
            # amount so aliases such as 开工时间 cannot invalidate the duration.
            spec = parse_duration_spec(match["action"] + match["amount"])
            if spec.state.value != "delta" or spec.delta_seconds is None:
                continue
            target = match["target"]
            key = ("start_time" if target in ("开始时间", "起始时间", "开工时间") else
                   "end_time" if target in ("结束时间", "终止时间", "截止时间", "完工时间", "收工时间") else "duration")
            if TemporalParser._nonassertive_time_context(text, match.start(), match.end()):
                ignored_targets.add(key)
                clause_start = max(text.rfind(mark, 0, match.start()) for mark in "，,。；;！!？?\n") + 1
                # Repeating yesterday's change or asking about it does not
                # revoke a later affirmative instruction for the same amount.
                if (not any(TemporalParser._time_context_modifiers(text, match.start(), match.end()))
                        and re.search(r"不要|别|取消|撤销|暂不|不能|禁止|勿", text[clause_start:match.end()])):
                    rejected_deltas.setdefault(key, set()).add(spec.delta_seconds)
                continue
            adjustments[key] = (match[0], spec.delta_seconds)
        for key, (_, delta) in list(adjustments.items()):
            if delta in rejected_deltas.get(key, set()):
                adjustments.pop(key)
        ignored_targets.difference_update(adjustments)
        return adjustments, ignored_targets

    @staticmethod
    def _explicit_time_candidate(candidate: dict, user_message: str) -> bool:
        raw = str(candidate.get("raw_value") or "")
        if not raw or not (TemporalParser.has_date_semantics(raw)
                           or re.search(r"[0-9一二两三四五六七八九十]+(?:点|时|[:：])", raw)):
            return False
        return any(not TemporalParser._nonassertive_time_context(
            user_message, match.start(), match.end(),
        ) for match in re.finditer(re.escape(raw), user_message))

    @staticmethod
    def select_time_range_start_text(
        start_cand: dict | None,
        current_state: dict,
        user_message: str,
        base_dt: datetime,
    ) -> object:
        """选取用于计算时间区间的开始时间输入文本"""
        if start_cand is None:
            return current_state.get("start_time")

        raw = str(start_cand.get("raw_value") or "").strip()
        normalized = start_cand.get("normalized_value")
        if re.search(r"(?:小时|钟头|分钟|分|秒)[^，,。；;\n]*后", raw):
            # Model-normalized ISO values may contain the old truncated-offset
            # result. Recompute from the full relative evidence before deriving
            # an end; an invalid phrase must reach range validation unchanged.
            return parse_relative_datetime(raw, base_dt, full_user_message=raw) or raw
        if raw and (
            TemporalParser.has_date_semantics(raw)
            or TemporalParser.has_date_semantics(user_message)
            or re.search(r"现在|此刻|当前|立即|马上|立刻|即刻", raw)
        ):
            rel_iso = parse_relative_datetime(raw, base_dt, full_user_message=user_message)
            if rel_iso:
                return rel_iso

        if TemporalParser.looks_like_iso_datetime(normalized):
            return normalized
        return raw or normalized

    @staticmethod
    def select_time_range_end_text(
        end_cand: dict | None,
        user_message: str,
        has_relation: bool,
    ) -> object:
        """选取用于计算时间区间的结束时间输入文本"""
        if end_cand is None:
            return None

        raw = str(end_cand.get("raw_value") or "").strip()
        normalized = end_cand.get("normalized_value")
        if has_relation and raw and raw not in user_message:
            return None
        if (has_relation and parse_duration_evidence(raw).success
                and not TemporalParser._explicit_time_candidate(end_cand, user_message)):
            # A model-derived end may quote the duration itself as evidence.
            # It is not an independent endpoint constraint; derive it from S+D.
            return None
        return raw or normalized

    # ──────────────────────────────────────────────────────────────────────────
    # 核心抽取与物化业务方法
    # ──────────────────────────────────────────────────────────────────────────

    def extract_temporal_relation(
        self,
        user_message: str,
        current_state: dict,
        llm: Any = None,
    ) -> dict | None:
        """调用 LLM 从用户消息中提取时长关系结构体 (has_duration, target, action, duration_seconds)"""
        active_llm = llm or self.llm
        if not active_llm:
            return None
        extractor = getattr(active_llm, "extract_temporal_relation", None)
        if not callable(extractor):
            return None
        messages = [
            {
                "role": "system",
                "content": (
                    "你是时间关系抽取器，判断最新输入是否给出任务持续时长或时长增量变动。"
                    "必须输出 has_duration、target、action、keep_existing_duration、duration_seconds、raw_text、confidence。"
                    "存在时长时 has_duration=true，将时长换算为正数秒（换算参考：半小时=1800，一个半小时=5400，2.5小时=9000，45分钟=2700）。"
                    "正确判断修饰目标对象 target：修饰开始时间输出 'start_time'；修饰持续时间输出 'duration'；修饰结束时间输出 'end_time'。"
                    "正确识别动作标记 action：表达增加、延长、再加、多干、推迟、比原来加时输出 'ADD'；表达提前、缩短、减少时输出 'SUB'；表达持续、设定为、总共时输出 'SET'。"
                    "只要求保持持续时间时 action='KEEP'、target='duration'、duration_seconds=null。"
                    "明确要求持续时间保持不变时 keep_existing_duration=true；开始或结束时间不变不等于保持持续时间。"
                    "不存在时长时 has_duration=false，其余可空字段为 null。只输出 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "latest_user_message": user_message,
                        "current_start_time": current_state.get("start_time"),
                        "current_end_time": current_state.get("end_time"),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            relation = extractor(
                messages,
                max_tokens=180,
                role=ModelRole.EXTRACTOR,
            )
        except TypeError as exc:
            if not _is_unsupported_role_keyword_error(exc):
                raise
            relation = extractor(messages, max_tokens=180)
        return relation if isinstance(relation, dict) else None

    @staticmethod
    def explicit_range_candidates(user_message: str, base_dt: datetime) -> list[dict]:
        """Recover one explicit dated clock range omitted by the extractor.

        This only supplies candidates on a WRITE path. Ambiguous, conditional,
        negated and multiple ranges remain with the ordinary clarification path.
        """
        matches = [
            match for match in _DATED_CLOCK_RANGE.finditer(user_message)
            if not TemporalParser._nonassertive_time_context(
                user_message, match.start(), match.end(),
            )
        ]
        if len(matches) != 1:
            return []
        match = matches[0]
        start, end = match['start'].strip(), match['end'].strip()
        result = parse_time_range(start, None, end, base_dt=base_dt)
        # An unqualified 1..11 o'clock endpoint can share the stated meridiem.
        # Noon parses without a prefix and must never become 上午十二点 (00:00).
        if not result.success and result.error_code == "INVALID_END_TIME" and not re.search(_RANGE_PART, end):
            meridiem = re.search(_RANGE_PART, start)
            if meridiem and not re.search(_RANGE_DATE, end):
                result = parse_time_range(start, None, meridiem[0] + end, base_dt=base_dt)
        if not result.success:
            return []
        return [
            {"canonical_key": key, "raw_key": key, "raw_value": raw,
             "normalized_value": value, "confidence": 1.0,
             "resolution_method": "explicit_time_range"}
            for key, raw, value in (
                ("start_time", start, result.start_time.iso_string),
                ("end_time", end, result.end_time.iso_string),
            )
        ]

    @staticmethod
    def _explicit_time_statements(user_message: str) -> list[dict]:
        """Read adjacent task endpoints and durations as separate statements.

        A statement must start a task clause or follow another complete time
        statement. This prevents equipment clocks and partial quantities from
        becoming task times, while preserving unpunctuated spoken requests.
        """
        from .relative_time_parser import DURATION_VALUE_PATTERN, RELATIVE_OFFSET_PATTERN

        date = r"(?:大后天|后天|明天|今天|\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2})"
        meridiem = r"(?:凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜间)"
        number = r"[0-9零一二两三四五六七八九十]{1,3}"
        clock = rf"(?:\d{{1,2}}[:：]\d{{2}}(?::\d{{2}})?|{number}点(?:半|{number}分?)?)"
        value = rf"(?:{date}\s*{meridiem}?\s*{clock}|{RELATIVE_OFFSET_PATTERN}|现在|此刻|当前|立即|马上|立刻|即刻)"
        label = r"(?:开始时间|起始时间|开工时间|结束时间|终止时间|截止时间|完工时间|收工时间)"
        connector = r"\s*(?:修改为|设置为|调整为|改成|改为|延长到|推迟到|提前到|为|是|[:：=])?\s*"
        event = r"(?:开始|开工|结束|完工|收工)"
        endpoint = rf"(?:{label}{connector}{value}(?:\s*{event})?|(?:在|于|从)?\s*{value}\s*(?:再)?{event})"
        duration_label = r"(?:持续时间|持续时长|时长|持续)"
        # Requiring a complete following statement lets the duration matcher
        # backtrack: 持续4小时30分钟后结束 means a four-hour duration and
        # an endpoint 30 minutes from now, not a 4h30m duration with lost end.
        boundary = rf"(?=$|\s|[？?]|{endpoint}|{duration_label})"
        endpoint_pattern = re.compile(
            rf"(?:(?P<field_label>{label}){connector}(?P<field_value>{value})(?:\s*(?P<field_event>{event}))?"
            rf"|(?:在|于|从)?\s*(?P<event_value>{value})\s*(?:再)?(?P<event_label>{event})){boundary}"
        )
        duration_pattern = re.compile(
            rf"{duration_label}\s*(?:为|是|[:：=])?\s*(?P<value>{DURATION_VALUE_PATTERN}){boundary}"
        )
        endpoint_hint = re.compile(
            rf"(?:(?P<field_label>{label}){connector}(?=[\d零〇一二两三四五六七八九十半负+\-]|明天|今天|后天|大后天|现在)"
            rf"|(?:在|于|从)?\s*(?=[\d零〇一二两三四五六七八九十半负+\-])"
            rf"[^，,。；;]*?后\s*(?P<event_label>{event}))"
        )
        statements = []
        for clause in re.finditer(r"[^，,。；;！!？?\n]+", user_message):
            text = clause[0]
            prefix = re.match(
                rf"\s*(?:(?:请|把|将|任务|作业|"
                rf"(?:现在|目前|当前|本次|这次|本轮)(?=\s*(?:{label}|{duration_label}|任务|作业|把|将)))\s*)*",
                text,
            )
            position = prefix.end()
            while position < len(text):
                match = endpoint_pattern.match(text, position)
                kind = "endpoint"
                if match is None:
                    match = duration_pattern.match(text, position)
                    kind = "duration"
                if match is None:
                    hint = endpoint_hint.match(text, position)
                    if hint and not TemporalParser._nonassertive_time_context(
                        user_message, clause.start() + position, clause.end(),
                    ):
                        statements.append({
                            "kind": "invalid_endpoint",
                            "label": hint["field_label"] or hint["event_label"],
                        })
                    break
                start, end = clause.start() + match.start(), clause.start() + match.end()
                if not TemporalParser._nonassertive_time_context(user_message, start, end):
                    groups = match.groupdict()
                    if groups.get("field_event"):
                        starts = groups["field_label"] in ("开始时间", "起始时间", "开工时间")
                        if starts != (groups["field_event"] in ("开始", "开工")):
                            kind = "invalid_endpoint"
                    statements.append({
                        "kind": kind,
                        "label": groups.get("field_label") or groups.get("event_label"),
                        "value": groups.get("field_value") or groups.get("event_value") or groups.get("value"),
                        "raw_text": match[0], "start": start, "end": end,
                    })
                position = match.end()
                whitespace = re.match(r"\s*", text[position:])
                position += whitespace.end()
        return statements

    @staticmethod
    def explicit_field_time_candidates(user_message: str, base_dt: datetime) -> list[dict]:
        """Recover complete, unique task endpoints omitted by the model."""
        return TemporalParser._explicit_field_time_evidence(user_message, base_dt)[0]

    @staticmethod
    def _explicit_field_time_evidence(user_message: str, base_dt: datetime) -> tuple[list[dict], set[str]]:
        from .relative_time_parser import parse_relative_datetime_detail

        by_key: dict[str, list[dict]] = {}
        rejected_keys: set[str] = set()
        for statement in TemporalParser._explicit_time_statements(user_message):
            if statement["kind"] not in ("endpoint", "invalid_endpoint"):
                continue
            key = "start_time" if statement["label"] in ("开始时间", "起始时间", "开工时间", "开始", "开工") else "end_time"
            if statement["kind"] == "invalid_endpoint":
                rejected_keys.add(key)
                continue
            raw = statement["value"].strip()
            result = parse_relative_datetime_detail(raw, base_dt, full_user_message=raw)
            # A numeric HH:MM clock is a 24-hour field value. The general
            # spoken-time parser also flags 09:00 as missing a meridiem.
            clock_is_numeric = bool(re.search(r"\d{1,2}[:：]\d{2}(?::\d{2})?$", raw))
            unsafe_ambiguities = any(
                ambiguity.code.value != "MERIDIEM_UNSPECIFIED" or not clock_is_numeric
                for ambiguity in result.ambiguities
            )
            if not result.success or unsafe_ambiguities:
                rejected_keys.add(key)
                continue
            by_key.setdefault(key, []).append({
                "canonical_key": key, "raw_key": statement["label"], "raw_value": raw,
                "normalized_value": result.iso_string, "confidence": 1.0,
                "resolution_method": "explicit_time_field",
            })
        rejected_keys.update(key for key, items in by_key.items() if len(items) != 1)
        return [items[0] for key, items in by_key.items() if key not in rejected_keys], rejected_keys

    @staticmethod
    def explicit_duration_relation(user_message: str) -> dict | None:
        """Keep the complete affirmative duration clause when the model truncates it."""
        relations = []
        for statement in TemporalParser._explicit_time_statements(user_message):
            if statement["kind"] != "duration":
                continue
            raw = statement["raw_text"]
            spec = parse_duration_spec(statement["value"])
            if spec.state.value == "explicit":
                relations.append({"has_duration": True, "target": "duration", "action": "SET",
                                  "raw_text": raw, "duration_seconds": spec.total_seconds, "confidence": 1.0})
        return relations[0] if len(relations) == 1 else None

    @staticmethod
    def materialize_time_relation(
        candidates: list,
        relation: object,
        current_state: dict,
        allowed_keys: set[str],
        user_message: str = "",
    ) -> tuple[list, list[str]]:
        """Resolve grounded S/D/E edits once, then validate the resulting interval."""
        # Apply the full-turn veto before recovering omitted fields or trusting
        # a model relation, including schemas which expose only start_time.
        if TemporalParser._time_edits_are_paused(user_message):
            return [item for item in candidates if not (
                isinstance(item, dict) and item.get("canonical_key") in ("start_time", "end_time")
            )], []
        if "end_time" not in allowed_keys:
            return candidates, []

        import math

        from .relative_time_parser import RELATIVE_OFFSET_PATTERN
        from .simulated_time import get_current_datetime

        cancelled_targets = TemporalParser._cancelled_time_targets(user_message)
        adjustments, ignored_adjustments = TemporalParser._time_adjustment_evidence(user_message)
        adjustments = {key: value for key, value in adjustments.items() if key not in cancelled_targets}
        if isinstance(relation, dict) and str(relation.get("target") or "duration").lower() in cancelled_targets:
            relation = None

        # Work on copies: a rejected adjustment must not mutate caller candidates.
        candidates = [dict(item) if isinstance(item, dict) else item for item in candidates]
        # A model can return an absolute clock quoted only in a rejected or
        # discussed clause; do not let that bypass the deterministic guards.
        def only_nonassertive_time_evidence(item):
            if not isinstance(item, dict) or item.get("canonical_key") not in ("start_time", "end_time"):
                return False
            raw = str(item.get("raw_value") or "")
            occurrences = list(re.finditer(re.escape(raw), user_message)) if raw else []
            return bool(occurrences) and all(TemporalParser._nonassertive_time_context(
                user_message, match.start(), match.end(),
            ) for match in occurrences)

        candidates = [item for item in candidates if not only_nonassertive_time_evidence(item)]
        explicit_fields, rejected_time_keys = TemporalParser._explicit_field_time_evidence(
            user_message, get_current_datetime().replace(microsecond=0),
        )
        explicit_fields = [item for item in explicit_fields if item["canonical_key"] not in cancelled_targets]
        rejected_time_keys.difference_update(cancelled_targets)
        explicit_keys = {item["canonical_key"] for item in explicit_fields}
        withdrawn_keys = cancelled_targets & {"start_time", "end_time"}
        if cancelled_targets & {"start_time", "duration"} and "end_time" not in explicit_keys | adjustments.keys():
            # An inferred end belongs to the withdrawn start/duration edit;
            # an independently specified end remains an authorized update.
            withdrawn_keys.add("end_time")
        def ungrounded_relative_candidate(item):
            if not isinstance(item, dict) or item.get("canonical_key") not in ("start_time", "end_time"):
                return False
            if item.get("canonical_key") in explicit_keys:
                return False
            raw = str(item.get("raw_value") or "")
            if not re.search(r"(?:小时|钟头|分钟|分|秒)[^，,。；;\n]*后", raw):
                return False
            from .relative_time_parser import parse_relative_datetime_detail

            clauses = [match[0] for match in re.finditer(r"[^，,。；;！!\n]+", user_message)
                       if raw in match[0]]
            if not clauses:
                return True
            for clause in clauses:
                parsed = parse_relative_datetime_detail(clause, get_current_datetime(), full_user_message=clause)
                if not parsed.success or parsed.has_ambiguities:
                    return True
                # A bare answer such as 半小时后 may use the model's field
                # binding. An explicitly named event must have task evidence.
                if re.search(r"开始|起始|开工|结束|终止|截止|完工|收工|启动", clause):
                    return True
                # Single-field follow-ups can return before range arithmetic.
                # Ground them now so a model ISO cannot override the offset.
                item["normalized_value"] = parsed.iso_string
                item["resolution_method"] = "relative_date_parsed"
            return False

        candidates = [item for item in candidates if not (
            isinstance(item, dict) and item.get("canonical_key") in (explicit_keys | rejected_time_keys | withdrawn_keys)
        ) and not ungrounded_relative_candidate(item)] + explicit_fields
        duration_statements = [statement for statement in TemporalParser._explicit_time_statements(user_message)
                               if statement["kind"] == "duration" and "duration" not in cancelled_targets]
        if rejected_time_keys or len(duration_statements) > 1:
            # A duplicated or ambiguous endpoint cannot be replaced by a model
            # guess, nor can a duration re-derive it from an old saved endpoint.
            return [item for item in candidates if not (
                isinstance(item, dict) and item.get("canonical_key") in ("start_time", "end_time")
            )], ["任务时间存在歧义或重复声明，请明确唯一的开始时间、结束时间和持续时长。"]
        if not any(isinstance(item, dict) and item.get("canonical_key") in ("start_time", "end_time") for item in candidates):
            candidates.extend(
                item for item in TemporalParser.explicit_range_candidates(
                    user_message, get_current_datetime().replace(microsecond=0),
                ) if item["canonical_key"] not in withdrawn_keys
            )
        previous_start = TemporalParser.parse_state_datetime(current_state.get("start_time"))
        previous_end = TemporalParser.parse_state_datetime(current_state.get("end_time"))
        asserted_clauses = [
            match[0] for match in re.finditer(r"[^，,。；;！!？?\n]+", user_message)
            if not TemporalParser._nonassertive_time_context(user_message, match.start(), match.end())
        ]
        asserted_text = "，".join(asserted_clauses)
        keep_duration = "duration" not in cancelled_targets and is_keep_duration_expression(asserted_text)
        keep_start = "start_time" not in cancelled_targets and bool(_START_TIME_KEEP_RE.search(asserted_text))
        keep_end = ("end_time" in cancelled_targets and previous_end is not None) or (
            "end_time" not in cancelled_targets and TemporalParser.mentions_end_time_keep(asserted_text)
        )

        def without_cancelled(items):
            return [item for item in items if not (
                isinstance(item, dict) and item.get("canonical_key") in cancelled_targets
            )]

        def without_times(items):
            return [item for item in items if not (
                isinstance(item, dict) and item.get("canonical_key") in ("start_time", "end_time")
            )]

        def replace_point(key, value, raw, method):
            nonlocal candidates
            original = next((item for item in reversed(candidates) if isinstance(item, dict)
                             and item.get("canonical_key") == key), {})
            candidates = [item for item in candidates if not (
                isinstance(item, dict) and item.get("canonical_key") == key
            )]
            candidates.append({**original, "canonical_key": key, "raw_key": key,
                "raw_value": raw, "normalized_value": value.isoformat(timespec="seconds"),
                "confidence": original.get("confidence", 1.0), "resolution_method": method})

        if ignored_adjustments:
            ignored_keys = ignored_adjustments & {"start_time", "end_time"}
            # A rejected start/duration shift may have a model-inferred end.
            if ignored_adjustments & {"start_time", "duration"}:
                ignored_keys.add("end_time")
            ignored_keys -= adjustments.keys()
            ignored_keys -= explicit_keys
            if "duration" in adjustments:
                ignored_keys.discard("end_time")
            candidates = [item for item in candidates if not (
                isinstance(item, dict) and item.get("canonical_key") in ignored_keys
                and not TemporalParser._explicit_time_candidate(item, user_message)
            )]
            if isinstance(relation, dict) and (
                not adjustments or str(relation.get("target") or "duration").lower() in ignored_adjustments
            ):
                relation = None

        # SET relations need the same authorization guard as endpoint deltas.
        # Models may turn a rejected/hypothetical duration into an inferred end;
        # retain an independently stated clock while discarding that inference.
        duration_clauses = [match for match in re.finditer(r"[^，,。；;！!？?\n]+", user_message)
                            if TemporalParser.has_explicit_duration(match[0])]
        rejected_duration_clauses = [match for match in duration_clauses
                                    if TemporalParser._nonassertive_time_context(
                                        user_message, match.start(), match.end())]
        if rejected_duration_clauses:
            only_rejected_duration = len(duration_clauses) == len(rejected_duration_clauses)

            def only_in_rejected_duration(raw):
                occurrences = list(re.finditer(re.escape(str(raw)), user_message)) if raw else []
                return bool(occurrences) and all(
                    any(clause.start() <= occurrence.start() and occurrence.end() <= clause.end()
                        for clause in rejected_duration_clauses)
                    for occurrence in occurrences
                )

            candidates = [item for item in candidates if not (
                isinstance(item, dict) and item.get("canonical_key") in ("start_time", "end_time")
                and (only_rejected_duration or only_in_rejected_duration(item.get("raw_value")))
                and not TemporalParser._explicit_time_candidate(item, user_message)
            )]
            if isinstance(relation, dict) and (
                only_rejected_duration or only_in_rejected_duration(relation.get("raw_text"))
            ):
                relation = None

        if user_message and not TemporalParser.has_explicit_duration(user_message):
            relation = None
        explicit_duration = (TemporalParser.explicit_duration_relation(user_message)
                             if "duration" not in cancelled_targets else None)
        if explicit_duration is not None:
            relation = explicit_duration
        rel = relation if isinstance(relation, dict) and relation.get("has_duration") is not False else {}
        action = str(rel.get("action") or "SET").upper()
        target = str(rel.get("target") or "duration").lower()
        raw_text = str(rel.get("raw_text") or "").strip()
        try:
            confidence = float(rel.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        amount = rel.get("duration_seconds")
        detail = parse_duration_evidence(raw_text)
        raw_spec = parse_duration_spec(raw_text)
        if detail.success:
            amount = detail.total_seconds
        if amount is not None and (not isinstance(amount, (int, float)) or not math.isfinite(amount)):
            return without_times(candidates), ["持续时长必须为有限数值，未写入结束时间。"]
        if raw_spec.state.value == "delta" and action == "SET":
            action = "SUB" if raw_spec.delta_seconds < 0 else "ADD"
        model_increment = action in ("ADD", "SUB")
        duration_text = None
        if rel:
            if rel.get("keep_existing_duration") or action == "KEEP" or raw_spec.state.value == "keep":
                duration_text = "持续时间不变"
            elif isinstance(amount, (int, float)) and amount > 0:
                duration_text = f"{amount}秒"
                if model_increment:
                    duration_text = ("减少" if action == "SUB" else "增加") + duration_text
            elif raw_text and raw_text not in ("持续时长", "持续时间", "时长"):
                duration_text = raw_text

        # Explicit user target/direction takes priority over a model's ISO or
        # mislabeled relation. Missing explicit syntax can still use the typed
        # relation (e.g. other phrasing supported by the semantic model).
        endpoint_adjustments = {k: v for k, v in adjustments.items() if k != "duration"}
        if model_increment and target in ("start_time", "end_time") and amount and not adjustments:
            endpoint_adjustments[target] = (raw_text, -amount if action == "SUB" else amount)
        if (keep_start and "start_time" in endpoint_adjustments) or (keep_end and "end_time" in endpoint_adjustments):
            return without_times(candidates), ["时间修改与保持不变的要求冲突，未写入时间。"]
        if keep_duration and "duration" in adjustments:
            return without_times(candidates), ["持续时间修改与保持不变的要求冲突，未写入时间。"]

        for key, (raw, delta) in endpoint_adjustments.items():
            previous = previous_start if key == "start_time" else previous_end
            if previous is None:
                return without_times(candidates), ["缺少原有时间，无法按增量调整时间。"]
            replace_point(key, previous + timedelta(seconds=delta), raw, "time_endpoint_shifted")

        # Endpoint deltas are not changes to duration. Do not feed the same
        # half-hour into both endpoint shifting and duration arithmetic.
        if endpoint_adjustments and "duration" not in adjustments:
            duration_text = None
            for clause in re.split(r"[，,。；;\n]", user_message):
                if not re.match(r"\s*(?:任务|作业)?(?:持续时间|持续时长|时长|持续)", clause):
                    continue
                explicit_duration = parse_duration_spec(clause)
                if explicit_duration.state.value == "explicit":
                    # "开工推迟半小时，作业时长仍保持两小时" contains
                    # two amounts. The half-hour is solely the endpoint shift.
                    duration_text = f"{explicit_duration.total_seconds}秒"
                    raw_text = clause.strip()
                    target, model_increment = "duration", False
                    break

        if keep_start:
            if previous_start is None:
                return without_times(candidates), ["缺少原有开始时间，无法保持不变。"]
            candidates = [item for item in candidates if not (
                isinstance(item, dict) and item.get("canonical_key") == "start_time"
            )]
        if keep_end:
            if previous_end is None:
                return without_times(candidates), ["缺少原有结束时间，无法保持不变。"]
            replace_point("end_time", previous_end, "结束时间不变", "end_time_unchanged")
            if target == "start_time":
                duration_text = None

        if keep_duration:
            duration_text = "持续时间不变"
        if "duration" in adjustments:
            raw_text, delta = adjustments["duration"]
            duration_text = ("增加" if delta > 0 else "减少") + f"{abs(delta)}秒"
            # A candidate that calls the duration amount an endpoint must not
            # override the explicit duration edit. Keep an explicit fixed end
            # as a real additional constraint and let the engine check it.
            if not keep_end:
                candidates = [item for item in candidates if not (
                    isinstance(item, dict) and item.get("canonical_key") == "end_time"
                    and not TemporalParser._explicit_time_candidate(item, user_message)
                )]
        elif (not keep_end and "end_time" not in endpoint_adjustments
              and ("start_time" in endpoint_adjustments or (
                  duration_text is None and any(
                      isinstance(item, dict) and item.get("canonical_key") == "start_time"
                      and re.search(RELATIVE_OFFSET_PATTERN, str(item.get("raw_value") or ""))
                      for item in candidates
                  )
              ))):
            # Inferred endpoints may be based on the same erroneous model ISO.
            # A clock/date that the user explicitly gave remains a constraint.
            candidates = [item for item in candidates if not (
                isinstance(item, dict) and item.get("canonical_key") == "end_time"
                and "end_time" not in explicit_keys
                and not TemporalParser._explicit_time_candidate(item, user_message)
            )]

        start_cand = next((item for item in reversed(candidates) if isinstance(item, dict)
                           and item.get("canonical_key") == "start_time"), None)
        end_cand = next((item for item in reversed(candidates) if isinstance(item, dict)
                         and item.get("canonical_key") == "end_time"), None)
        if duration_text is None and start_cand is not None and end_cand is None:
            if previous_start and previous_end:
                duration_text = "持续时间不变"
                raw_text = raw_text or "保持原持续时长"
        has_relation = bool(duration_text)
        if not has_relation and not endpoint_adjustments and not keep_end and not (start_cand and end_cand):
            return without_cancelled(candidates), []

        base_dt = get_current_datetime().replace(microsecond=0)
        start_text = TemporalParser.select_time_range_start_text(start_cand, current_state, user_message, base_dt)
        end_text = TemporalParser.select_time_range_end_text(end_cand, user_message, has_relation)
        if start_cand and start_cand.get("resolution_method") == "time_endpoint_shifted":
            start_text = start_cand["normalized_value"]
        if end_cand and end_cand.get("resolution_method") in ("time_endpoint_shifted", "end_time_unchanged", "explicit_time_field"):
            end_text = end_cand["normalized_value"]
        if has_relation and end_cand is not None and end_text is None:
            # Discard unsupported model arithmetic even if S+D later fails
            # (for example because the start is still missing).
            candidates = [item for item in candidates if not (
                isinstance(item, dict) and item.get("canonical_key") == "end_time"
            )]
            end_cand = None
        range_result = parse_time_range(start_text, duration_text, end_text, base_dt=base_dt,
            previous_start=previous_start, previous_end=previous_end)
        if (
            not range_result.success
            and range_result.error_code in {"INVALID_END_TIME", "END_NOT_AFTER_START"}
            and not has_relation and not endpoint_adjustments and not keep_end
            and start_cand is not None and end_cand is not None
            and all(TemporalParser.looks_like_iso_datetime(item.get("normalized_value"))
                    for item in (start_cand, end_cand))
        ):
            # Only repair a raw endpoint that lost shared context or contains
            # the whole spoken range. A model ISO alone cannot override an
            # explicit contradictory endpoint (e.g. 10:00 -> 09:00).
            from .relative_time_parser import parse_relative_datetime_detail

            raw_start = str(start_cand.get("raw_value") or "").strip()
            raw_end = str(end_cand.get("raw_value") or "").strip()
            # The model may expand 十二点 into raw 明天上午十二点, which
            # is absent from the user sentence and parses as midnight. Anchor
            # the real range to its stated start and read the actual endpoint.
            spoken_range = re.search(
                re.escape(raw_start) + r"\s*(?:到|至)\s*([^，,。；;\n]+)",
                user_message,
            ) if raw_start else None
            range_tail = re.search(r"(?:到|至)\s*([^到至]+)$", raw_end)
            endpoint = (spoken_range.group(1).strip() if spoken_range else
                        range_tail.group(1).strip() if range_tail else raw_end)
            if spoken_range or (raw_end and raw_end in user_message and (
                range_tail or not TemporalParser.has_date_semantics(endpoint)
            )):
                meridiem_pattern = r"凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|夜间"
                shared_meridiem = re.search(meridiem_pattern, raw_start)
                evidence = parse_relative_datetime_detail(
                    endpoint, base_dt, full_user_message=user_message,
                )
                # Inherit meridiem only for an ambiguous clock (1..11).
                # Bare noon already resolves to 12:00; adding 上午 makes it 00:00.
                if (evidence.has_ambiguities and not re.search(meridiem_pattern, endpoint)
                        and shared_meridiem and raw_start in user_message):
                    evidence = parse_relative_datetime_detail(
                        shared_meridiem.group(0) + endpoint, base_dt,
                        full_user_message=user_message,
                    )
                if (evidence.success and not evidence.has_ambiguities
                        and evidence.target_local_datetime == TemporalParser.parse_state_datetime(
                            end_cand["normalized_value"])):
                    normalized_range = parse_time_range(
                        start_text, None, evidence.iso_string, base_dt=base_dt,
                    )
                    if normalized_range.success:
                        range_result = normalized_range
        if not range_result.success:
            if endpoint_adjustments or "duration" in adjustments or cancelled_targets:
                candidates = without_times(candidates)
            elif range_result.error_code == "TIME_RANGE_CONFLICT":
                candidates = [item for item in candidates if not (
                    isinstance(item, dict) and item.get("canonical_key") == "end_time"
                )]
            if range_result.error_code == "START_TIME_REQUIRED":
                return without_cancelled(candidates), [f"{raw_text or duration_text or '持续时长'}：缺少开始时间，无法计算结束时间。"]
            if range_result.error_code == "INVALID_DURATION":
                return without_cancelled(candidates), ["持续时长必须为正数且置信度合法，未写入结束时间。"]
            return without_cancelled(candidates), [range_result.error_message] if range_result.error_message else []

        if start_cand is not None:
            original = start_cand.get("normalized_value")
            start_cand["normalized_value"] = range_result.start_time.iso_string
            if range_result.start_time.parse_method != "absolute_iso" or original != range_result.start_time.iso_string:
                start_cand["resolution_method"] = "relative_date_parsed"
        if end_cand is not None and not has_relation:
            end_cand["normalized_value"] = range_result.end_time.iso_string
            if range_result.end_time.parse_method == "range_cross_midnight":
                end_cand["resolution_method"] = "cross_day_auto_corrected"
            elif range_result.end_time.parse_method != "absolute_iso":
                end_cand["resolution_method"] = "relative_date_parsed"
        else:
            candidates = [item for item in candidates if not (
                isinstance(item, dict) and item.get("canonical_key") == "end_time"
            )]
            candidates.append({"raw_key": "持续时长", "canonical_key": "end_time",
                "raw_value": raw_text or duration_text or "持续时长",
                "normalized_value": range_result.end_time.iso_string, "confidence": confidence,
                "resolution_method": "duration_incremental_arithmetic" if model_increment and target == "duration"
                    else "duration_arithmetic"})
        return without_cancelled(candidates), []
