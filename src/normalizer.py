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
        """按字段定义规范化本轮候选值，无法规范化时保留原值供后续校验。"""
        normalized_updates = dict(updates)
        temp_state = dict(current_state)

        for field_def in field_definitions:
            key = field_def["key"]
            if key not in updates or updates[key] in (None, ""):
                continue

            allowed = allowed_values_resolver(field_def, temp_state)
            normalized = self.normalize(
                updates[key],
                allowed,
                field_def["type"],
            )
            if normalized is None:
                continue

            normalized_updates[key] = normalized
            temp_state[key] = normalized

        return normalized_updates

    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _match_key(value: str) -> str:
        text = unicodedata.normalize("NFKC", str(value or ""))
        text = re.sub(r"[\s\u3000]+", "", text)
        return text.casefold()

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
            return None

        result = []
        for item in items:
            mapped = self._normalize_string(item, allowed)
            # 列表不能静默丢弃非法元素，否则会产生“只录入了一部分”的假成功。
            if mapped is None:
                return None
            if mapped not in result:
                result.append(mapped)

        return result if result else None
