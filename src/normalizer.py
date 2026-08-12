"""
normalizer.py — 字段值统一规范化器

职责：
1. 将模型或用户输入转换为 schema 规定的数据类型；
2. 将枚举字段收敛到 allowed_values 中的标准值；
3. 任何无法可靠规范化的值都返回 None，不让非标准值进入槽位。
"""

import json
import math
import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Callable

from .coord_parser import parse_coord_value


from dataclasses import dataclass

@dataclass(frozen=True)
class NormalizationFailure:
    field: str
    raw_value: Any
    error_code: str
    message: str


@dataclass(frozen=True)
class NormalizeUpdatesResult:
    normalized_updates: dict[str, Any]
    failures: dict[str, NormalizationFailure]


class FieldNormalizer:
    def normalize(
        self,
        raw_value: Any,
        allowed_values: list[Any] | None,
        field_type: str = "string",
    ) -> Any | None:
        """
        按字段 schema 将 raw_value 转为唯一标准表示。

        - number：float，最多两位小数，超出部分直接截断；
        - coord：{"lat": float, "lon": float}；
        - datetime：YYYY-MM-DDTHH:MM:SS；
        - string/tasktype：有候选集时只能返回候选集中的原值；
        - list：去重后的标准列表，任一元素无法映射则整体失败；
        - raw：只做 Unicode 与首尾空白清理，不改变业务内容。
        """
        if raw_value is None or raw_value == "":
            return None

        allowed = list(allowed_values or [])

        if field_type == "number":
            return self._normalize_number(raw_value)
        if field_type == "coord":
            return parse_coord_value(raw_value)
        if field_type == "datetime":
            return self._normalize_datetime(raw_value)
        if field_type == "list":
            return self._normalize_list(raw_value, allowed)
        if field_type in ("string", "tasktype"):
            return self._normalize_string(str(raw_value), allowed)
        if field_type == "raw":
            return self._normalize_text(raw_value)
        if field_type in ("auto", "fixed"):
            return raw_value

        return None

    @staticmethod
    def validate_field_constraints(
        value: Any,
        field_definition: dict[str, Any],
    ) -> tuple[str, str] | None:
        """校验 schema 声明的通用值域约束，不解释自然语言。"""
        if value is None or field_definition.get("type") != "number":
            return None

        key = str(field_definition.get("key") or "number")
        if "exclusive_minimum" in field_definition:
            threshold = field_definition["exclusive_minimum"]
            if value <= threshold:
                return "below_exclusive_minimum", f"{key} 必须大于 {threshold}"
        if "minimum" in field_definition:
            threshold = field_definition["minimum"]
            if value < threshold:
                return "below_minimum", f"{key} 不得小于 {threshold}"
        return None

    def normalize_updates_with_failures(
        self,
        updates: dict[str, Any],
        field_definitions: list[dict[str, Any]],
        current_state: dict[str, Any],
        allowed_values_resolver: Callable[
            [dict[str, Any], dict[str, Any]],
            list[Any] | None,
        ],
    ) -> NormalizeUpdatesResult:
        """
        按字段定义规范化本轮候选值。
        成功规范化的字段存入 normalized_updates；
        规范化失败的字段记入 failures 字典，不把非法 raw_value 混入 normalized_updates。
        """
        normalized_updates: dict[str, Any] = {}
        failures: dict[str, NormalizationFailure] = {}
        temp_state = dict(current_state)

        for field_def in field_definitions:
            key = field_def["key"]
            if key not in updates or updates[key] in (None, ""):
                continue

            raw_val = updates[key]
            allowed = allowed_values_resolver(field_def, temp_state)
            field_type = field_def.get("type", "string")

            normalized = self.normalize(
                raw_val,
                allowed,
                field_type,
            )
            constraint_failure = self.validate_field_constraints(
                normalized,
                field_def,
            )
            if normalized is None or constraint_failure is not None:
                error_code, message = constraint_failure or (
                    "normalization_failed",
                    f"无法将 '{raw_val}' 规范化为合法的 {field_type} 类型",
                )
                failures[key] = NormalizationFailure(
                    field=key,
                    raw_value=raw_val,
                    error_code=error_code,
                    message=message,
                )
            else:
                normalized_updates[key] = normalized
                temp_state[key] = normalized

        return NormalizeUpdatesResult(
            normalized_updates=normalized_updates,
            failures=failures,
        )

    def normalize_updates(
        self,
        updates: dict[str, Any],
        field_definitions: list[dict[str, Any]],
        current_state: dict[str, Any],
        allowed_values_resolver: Callable[
            [dict[str, Any], dict[str, Any]],
            list[Any] | None,
        ],
    ) -> dict[str, Any]:
        """兼容接口：仅返回成功规范化的更新字典。"""
        res = self.normalize_updates_with_failures(
            updates, field_definitions, current_state, allowed_values_resolver
        )
        return res.normalized_updates

    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def make_match_key(value: Any) -> str:
        text = unicodedata.normalize("NFKC", str(value or ""))
        text = re.sub(r"[\s\u3000]+", "", text)
        return text.casefold()

    @staticmethod
    def _match_key(value: str) -> str:
        return FieldNormalizer.make_match_key(value)

    @staticmethod
    def _normalize_text(value: Any) -> str | None:
        text = unicodedata.normalize("NFKC", str(value)).strip()
        return text or None

    @staticmethod
    def _normalize_number(raw: Any) -> float | None:
        """将数值统一为 float；超过两位小数时向零截断，不四舍五入。"""
        if isinstance(raw, bool):
            return None

        text = unicodedata.normalize("NFKC", str(raw)).strip()
        # 当前 number 字段主要是距离/水深，允许常见米制单位但不接受夹杂文本。
        match = re.fullmatch(
            r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))(?:\s*(?:m|米|公尺))?",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None

        try:
            value = Decimal(match.group(1))
            value = value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            result = float(value)
        except (InvalidOperation, ValueError, OverflowError):
            return None

        return result if math.isfinite(result) else None

    @staticmethod
    def _normalize_datetime(raw: Any) -> str | None:
        """接受常见 ISO 日期时间写法，统一到无时区、秒级格式。"""
        if isinstance(raw, datetime):
            parsed = raw
        elif isinstance(raw, str):
            text = unicodedata.normalize("NFKC", raw).strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
        else:
            return None

        # 任务时间目前采用本地模拟时间，不允许带时区值混入后产生隐式换算。
        if parsed.tzinfo is not None:
            return None
        return parsed.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")

    def _normalize_string(self, raw: str, allowed: list[str]) -> str | None:
        normalized_text = self._normalize_text(raw)
        if normalized_text is None:
            return None

        # 无候选约束的普通字符串，只进行确定性文本清理。
        if not allowed:
            return normalized_text

        # 1. 确定性格式归一匹配：忽略空白与英文大小写，返回 allowed 中的标准值。
        raw_key = self._match_key(normalized_text)
        for v in allowed:
            if self._match_key(v) == raw_key:
                return v

        # 不使用模型猜测标准值。无法确定性匹配时交由上层标记为无效。
        return None

    def _normalize_list(self, raw: str | list, allowed: list[str]) -> list | None:
        if isinstance(raw, str) and self._match_key(raw) in {
            "全选", "全部", "所有", "全部配置", "全配置"
        }:
            return list(allowed) if allowed else None

        # 将原始值统一为列表
        if isinstance(raw, str):
            # 尝试解析 JSON 数组，否则按常见分隔符拆分
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    items = [str(x) for x in parsed]
                else:
                    items = [raw]
            except Exception:
                items = re.split(r"[,，、\n]+", raw)
                items = [x.strip() for x in items if x.strip()]
        else:
            items = [str(x) for x in raw]

        if not items:
            return [] if isinstance(raw, list) else None

        result = []
        for item in items:
            mapped = self._normalize_string(item, allowed)
            # 列表不能静默丢弃非法元素，否则会产生“只录入了一部分”的假成功。
            if mapped is None:
                return None
            if mapped not in result:
                result.append(mapped)

        return result if result else None
