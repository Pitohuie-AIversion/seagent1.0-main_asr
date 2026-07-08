"""
normalizer.py — 字段值规范化器
对有 allowed_values 约束的字段，将 LLM 提取的原始值映射到最近的合法选项。
优先精确匹配，模糊时调用 LLM 推理，无法映射返回 None（字段仍视为缺失）。
"""

import json
import re
from .llm_client import LLMClient


class FieldNormalizer:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def normalize(
        self,
        field_key: str,
        field_label: str,
        raw_value: str | list,
        allowed_values: list[str],
        field_type: str = "string",
    ) -> str | list | None:
        """
        将 raw_value 映射到 allowed_values 中的合法值。
        - string：返回单个合法值或 None
        - list：返回合法值列表（过滤掉无法映射的项），空列表返回 None
        """
        if not raw_value and raw_value != 0:
            return None

        if field_type == "list":
            return self._normalize_list(field_key, field_label, raw_value, allowed_values)
        else:
            return self._normalize_string(field_key, field_label, str(raw_value), allowed_values)

    # ──────────────────────────────────────────────────────────────────────────

    def _normalize_string(
        self, field_key: str, field_label: str, raw: str, allowed: list[str]
    ) -> str | None:
        # 1. 精确匹配（大小写不敏感）
        raw_lower = raw.strip().lower()
        for v in allowed:
            if v.lower() == raw_lower:
                return v

        # # 2. 子串包含匹配
        # for v in allowed:
        #     if raw_lower in v.lower() or v.lower() in raw_lower:
        #         return v

        # 3. LLM 推理映射
        return self._llm_map_single(field_label, raw, allowed)

    def _normalize_list(
        self, field_key: str, field_label: str, raw: str | list, allowed: list[str]
    ) -> list | None:
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

        result = []
        for item in items:
            mapped = self._normalize_string(field_key, field_label, item, allowed)
            if mapped and mapped not in result:
                result.append(mapped)

        return result if result else None

    def _llm_map_single(
        self, field_label: str, raw: str, allowed: list[str]
    ) -> str | None:
        allowed_json = json.dumps(allowed, ensure_ascii=False)
        system = (
            f"你是字段值映射助手。将用户输入映射到给定选项列表中最匹配的一项。\n"
            f"字段名：{field_label}\n"
            f"合法选项：{allowed_json}\n"
            f"规则：只返回选项列表中完全一致的字符串，不加引号、不加解释。"
            f"如果无法合理映射，返回单词 null。"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": f"用户输入：{raw}"},
        ]
        result = self.llm.generate(messages, temperature=0.0, max_tokens=50).strip()
        if result.lower() in ("null", "none", ""):
            return None
        # 验证返回值确实在允许列表中
        for v in allowed:
            if v == result or v.lower() == result.lower():
                return v
        return None
