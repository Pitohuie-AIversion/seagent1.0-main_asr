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

from .duration_parser import parse_duration_spec
from .model_profile import ModelRole, _is_unsupported_role_keyword_error
from .relative_time_parser import parse_time_range


def parse_relative_datetime(*args: Any, **kwargs: Any) -> Any:
    """动态感知外部针对 src.extractor.parse_relative_datetime 的 monkey-patch"""
    import sys
    ext_mod = sys.modules.get("src.extractor")
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
        if not text:
            return None
        matches = re.finditer(
            r"(?:任务)?(?:持续时间|持续时长|时长)\s*(?:再)?(?:增加|减少|加长|缩短|延长|加上|减去|加|减)(?:了)?\s*"
            r"(?:[0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百千万亿点半]+)\s*"
            r"(?:个)?\s*(?:天|日|小时|钟头|h|hr|hours?|hrs?|分钟|分|min|mins?|minutes?|秒钟|秒|secs?|seconds?)",
            text,
            re.IGNORECASE,
        )
        found = [m.group(0).strip() for m in matches]
        if not found:
            return None
        candidate = found[-1]
        spec = parse_duration_spec(candidate)
        return candidate if spec.state.value == "delta" else None

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
                    "必须输出 has_duration、target、action、duration_seconds、raw_text、confidence。"
                    "存在时长时 has_duration=true，将时长换算为正数秒（换算参考：半小时=1800，一个半小时=5400，2.5小时=9000，45分钟=2700）。"
                    "正确判断修饰目标对象 target：修饰开始时间输出 'start_time'；修饰持续时间输出 'duration'；修饰结束时间输出 'end_time'。"
                    "正确识别动作标记 action：表达增加、延长、再加、多干、推迟、比原来加时输出 'ADD'；表达提前、缩短、减少时输出 'SUB'；表达持续、设定为、总共时输出 'SET'。"
                    "不存在时长时 has_duration=false，其余可空字段为 null。只输出 JSON。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "latest_user_message": user_message,
                        "current_start_time": current_state.get("start_time"),
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
        """利用统一时间区间解析器推导或校验 end_time，处理时长算术与跨日时间校正"""
        if "end_time" not in allowed_keys:
            return candidates, []

        start_cand = None
        end_cand = None
        for item in reversed(candidates):
            if isinstance(item, dict):
                if item.get("canonical_key") == "start_time" and start_cand is None:
                    start_cand = item
                elif item.get("canonical_key") == "end_time" and end_cand is None:
                    end_cand = item

        raw_text = None
        confidence = 1.0
        duration_text = None

        if isinstance(relation, dict) and relation.get("has_duration") is not False and relation != {}:
            raw_text = str(relation.get("raw_text") or "").strip()
            try:
                confidence = float(relation.get("confidence", 1.0))
            except (TypeError, ValueError):
                confidence = 1.0
            import math
            dur_sec = relation.get("duration_seconds")
            from .duration_parser import parse_duration_with_detail
            raw_dur_detail = parse_duration_with_detail(raw_text) if raw_text else None
            if raw_dur_detail and raw_dur_detail.success and raw_dur_detail.total_seconds:
                duration_text = raw_text
            elif dur_sec is not None and isinstance(dur_sec, (int, float)) and math.isfinite(dur_sec) and dur_sec > 0:
                duration_text = f"{dur_sec}秒"
            elif dur_sec is not None and isinstance(dur_sec, (int, float)) and not math.isfinite(dur_sec):
                return candidates, ["持续时长必须为有限数值，未写入结束时间。"]
            elif raw_text and raw_text not in ("持续时长", "持续时间", "时长"):
                duration_text = raw_text
            elif relation.get("keep_existing_duration"):
                duration_text = "持续时间不变"

        user_duration_delta = TemporalParser.extract_duration_delta_text(user_message)
        if user_duration_delta:
            duration_text = user_duration_delta
            raw_text = user_duration_delta
            end_cand = None

        end_unchanged = (
            end_cand is None
            and TemporalParser.mentions_end_time_keep(user_message)
            and current_state.get("end_time")
        )

        if duration_text is None and start_cand is not None and end_cand is None and not end_unchanged:
            if current_state.get("start_time") and current_state.get("end_time"):
                duration_text = "持续时间不变"
                raw_text = raw_text or "保持原持续时长"

        has_relation = bool(duration_text)
        has_end_candidate = end_cand is not None
        has_start_candidate = start_cand is not None
        if not has_relation and not end_unchanged and not (has_start_candidate and has_end_candidate):
            return candidates, []

        from .simulated_time import get_current_datetime
        base_dt = get_current_datetime()

        start_text = TemporalParser.select_time_range_start_text(
            start_cand,
            current_state,
            user_message,
            base_dt,
        )
        end_text = TemporalParser.select_time_range_end_text(
            end_cand,
            user_message,
            has_relation,
        )
        if end_unchanged:
            end_text = current_state.get("end_time")
        previous_start = TemporalParser.parse_state_datetime(current_state.get("start_time"))
        previous_end = TemporalParser.parse_state_datetime(current_state.get("end_time"))

        range_result = parse_time_range(
            start_text,
            duration_text,
            end_text,
            base_dt=base_dt,
            previous_start=previous_start,
            previous_end=previous_end,
        )

        if not range_result.success:
            if range_result.error_code == "START_TIME_REQUIRED":
                label = raw_text or duration_text or "持续时长"
                return candidates, [f"{label}：缺少开始时间，无法计算结束时间。"]
            if range_result.error_code == "INVALID_DURATION":
                return candidates, ["持续时长必须为正数且置信度合法，未写入结束时间。"]
            if range_result.error_message:
                return candidates, [range_result.error_message]
            return candidates, []

        updated_candidates = list(candidates)
        if start_cand is not None and range_result.start_time.iso_string:
            original_start_norm = str(start_cand.get("normalized_value") or "").strip()
            start_cand["normalized_value"] = range_result.start_time.iso_string
            if (
                range_result.start_time.parse_method != "absolute_iso"
                or original_start_norm != range_result.start_time.iso_string
            ):
                start_cand["resolution_method"] = "relative_date_parsed"

        if range_result.end_time.iso_string:
            if end_cand is not None and not has_relation:
                end_cand["normalized_value"] = range_result.end_time.iso_string
                if range_result.end_time.parse_method == "range_cross_midnight":
                    end_cand["resolution_method"] = "cross_day_auto_corrected"
                elif range_result.end_time.parse_method != "absolute_iso":
                    end_cand["resolution_method"] = "relative_date_parsed"
            elif end_unchanged:
                updated_candidates.append(
                    {
                        "raw_key": "结束时间",
                        "canonical_key": "end_time",
                        "raw_value": "结束时间不变",
                        "normalized_value": range_result.end_time.iso_string,
                        "confidence": confidence,
                        "resolution_method": "end_time_unchanged",
                    }
                )
            else:
                target = str(relation.get("target") or "duration").lower() if isinstance(relation, dict) else "duration"
                action = str(relation.get("action") or "SET").upper() if isinstance(relation, dict) else "SET"

                final_end_iso = range_result.end_time.iso_string
                res_method = "duration_arithmetic"

                dur_sec = relation.get("duration_seconds") if isinstance(relation, dict) else None
                if dur_sec is None and range_result.duration:
                    dur_sec = range_result.duration.total_seconds

                if action in ("ADD", "SUB") and dur_sec:
                    from datetime import timedelta
                    delta_sec = float(dur_sec) if action == "ADD" else -float(dur_sec)
                    if target in ("duration", "end_time") and previous_end:
                        derived_dt = previous_end + timedelta(seconds=delta_sec)
                        final_end_iso = derived_dt.strftime("%Y-%m-%dT%H:%M:%S")
                        res_method = "duration_incremental_arithmetic"
                    elif target == "start_time" and previous_start:
                        derived_start = previous_start + timedelta(seconds=delta_sec)
                        for item in updated_candidates:
                            if isinstance(item, dict) and item.get("canonical_key") == "start_time":
                                item["normalized_value"] = derived_start.strftime("%Y-%m-%dT%H:%M:%S")
                                item["resolution_method"] = "start_time_shifted"
                        if previous_end:
                            derived_dt = previous_end + timedelta(seconds=delta_sec)
                            final_end_iso = derived_dt.strftime("%Y-%m-%dT%H:%M:%S")
                            res_method = "start_and_end_shifted"

                updated_candidates = [
                    item for item in updated_candidates
                    if not (isinstance(item, dict) and item.get("canonical_key") == "end_time")
                ]
                updated_candidates.append(
                    {
                        "raw_key": "持续时长",
                        "canonical_key": "end_time",
                        "raw_value": raw_text or duration_text or "持续时长",
                        "normalized_value": final_end_iso,
                        "confidence": confidence,
                        "resolution_method": res_method,
                    }
                )
        return updated_candidates, []
