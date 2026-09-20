"""
src/temporal_parser.py — 时间关系抽取、区间推导与相对时间物化引擎

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
from zoneinfo import ZoneInfo

from .duration_parser import (
    is_keep_duration_expression, parse_duration_evidence, parse_duration_spec,
)
from src.extraction.model_profile import ModelRole, _is_unsupported_role_keyword_error
from .relative_time_parser import parse_time_range


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
        if parse_duration_evidence(text).success or is_keep_duration_expression(text):
            return True
        return bool(re.search(
            r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百千万点半]+)\s*(?:个)?\s*"
            r"(?:周|星期|刻钟)", str(text or ""),
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
            value = getattr(value, "value")

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
        return bool(
            re.search(
                r"今天|今晚|今早|明天|明晚|明早|后天|大后天|昨天|前天|次日|"
                r"\d{4}[年/-]|\d{1,2}[月/-]\d{1,2}|[一二三四五六七八九十]{1,3}月|"
                r"周[一二三四五六日天12345670]|星期[一二三四五六日天12345670]|"
                r"[一二两三四五六七八九十0-9]+天[后前]|[一二两三四五六七八九十0-9]+周[后前]|"
                r"月底|月末|月初|年底|年末|年初",
                text,
            )
        )

    @staticmethod
    def mentions_end_time_keep(text: str) -> bool:
        """探测文本是否明确提及结束时间保持不变"""
        if not text:
            return False
        return bool(re.search(r"(?:结束|终止|截止|完工|收工)\s*时间\s*(?:保持)?不变", text))

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
        adjustments = {}
        for match in re.finditer(
            r"(?P<target>开始时间|起始时间|结束时间|终止时间|截止时间|持续时间|持续时长|时长)"
            r"\s*(?:再|又|继续|比原来|比原本)?\s*"
            r"(?P<action>增加|减少|加长|缩短|延长|加上|减去|推迟|延后|提前|推后|后移|前移|加|减)"
            r"(?:了)?\s*(?P<amount>[^，,。；;\n]+)", str(text or ""),
        ):
            if re.match(r"(?:到|至|为)", match["amount"]):
                continue
            spec = parse_duration_spec(match[0])
            if spec.state.value != "delta" or spec.delta_seconds is None:
                continue
            target = match["target"]
            key = ("start_time" if target in ("开始时间", "起始时间") else
                   "end_time" if target in ("结束时间", "终止时间", "截止时间") else "duration")
            adjustments[key] = (match[0], spec.delta_seconds)
        return adjustments

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
    def materialize_time_relation(
        candidates: list,
        relation: object,
        current_state: dict,
        allowed_keys: set[str],
        user_message: str = "",
    ) -> tuple[list, list[str]]:
        """Resolve grounded S/D/E edits once, then validate the resulting interval."""
        if "end_time" not in allowed_keys:
            return candidates, []

        import math
        from .simulated_time import get_current_datetime

        # Work on copies: a rejected adjustment must not mutate caller candidates.
        candidates = [dict(item) if isinstance(item, dict) else item for item in candidates]
        previous_start = TemporalParser.parse_state_datetime(current_state.get("start_time"))
        previous_end = TemporalParser.parse_state_datetime(current_state.get("end_time"))
        adjustments = TemporalParser.extract_time_adjustments(user_message)
        keep_duration = is_keep_duration_expression(user_message)
        keep_start = bool(re.search(r"(?:开始|起始)时间\s*(?:保持)?不变", user_message))
        keep_end = TemporalParser.mentions_end_time_keep(user_message)

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

        if user_message and not TemporalParser.has_explicit_duration(user_message):
            relation = None
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
            has_explicit_duration_value = any(
                re.match(r"\s*(?:任务)?(?:持续时间|持续时长|时长|持续)", clause)
                and parse_duration_spec(clause).state.value == "explicit"
                for clause in re.split(r"[，,。；;\n]", user_message)
            )
            if not has_explicit_duration_value:
                duration_text = None

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
                )]
        elif "start_time" in endpoint_adjustments and not keep_end and "end_time" not in endpoint_adjustments:
            # Inferred endpoints may be based on the same erroneous model ISO.
            # A clock/date that the user explicitly gave remains a constraint.
            def explicit_end(item):
                raw = str(item.get("raw_value") or "")
                return raw and raw in user_message and (
                    TemporalParser.has_date_semantics(raw)
                    or re.search(r"[0-9一二两三四五六七八九十]+(?:点|时|[:：])", raw)
                )
            candidates = [item for item in candidates if not (
                isinstance(item, dict) and item.get("canonical_key") == "end_time"
                and not explicit_end(item)
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
            return candidates, []

        base_dt = get_current_datetime().replace(microsecond=0)
        start_text = TemporalParser.select_time_range_start_text(start_cand, current_state, user_message, base_dt)
        end_text = TemporalParser.select_time_range_end_text(end_cand, user_message, has_relation)
        if start_cand and start_cand.get("resolution_method") == "time_endpoint_shifted":
            start_text = start_cand["normalized_value"]
        if end_cand and end_cand.get("resolution_method") in ("time_endpoint_shifted", "end_time_unchanged"):
            end_text = end_cand["normalized_value"]
        range_result = parse_time_range(start_text, duration_text, end_text, base_dt=base_dt,
            previous_start=previous_start, previous_end=previous_end)
        if not range_result.success:
            if endpoint_adjustments or "duration" in adjustments:
                candidates = without_times(candidates)
            elif range_result.error_code == "TIME_RANGE_CONFLICT":
                candidates = [item for item in candidates if not (
                    isinstance(item, dict) and item.get("canonical_key") == "end_time"
                )]
            if range_result.error_code == "START_TIME_REQUIRED":
                return candidates, [f"{raw_text or duration_text or '持续时长'}：缺少开始时间，无法计算结束时间。"]
            if range_result.error_code == "INVALID_DURATION":
                return candidates, ["持续时长必须为正数且置信度合法，未写入结束时间。"]
            return candidates, [range_result.error_message] if range_result.error_message else []

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
        return candidates, []
