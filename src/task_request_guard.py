"""Deterministic pre-write analysis for compound task requests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


_REPLACEMENT_RE = re.compile(r"(?:改成|改为|换成|更换为|调整为|替换为)")


@dataclass(frozen=True)
class TaskRequestAnalysis:
    detected_task_types: tuple[str, ...] = ()
    unsupported_clauses: tuple[str, ...] = ()
    is_explicit_replacement: bool = False

    @property
    def should_block(self) -> bool:
        return len(self.detected_task_types) > 1 and not self.is_explicit_replacement

    def build_reply(self) -> str:
        parts: list[str] = []
        if len(self.detected_task_types) > 1:
            choices = "、".join(self.detected_task_types)
            parts.append(
                f"检测到您同时提出了多个水下任务：{choices}。"
                "一次只能创建一个任务，请选择当前要规划的任务。"
            )
        else:
            parts.append("请一次只提交一个受支持的水下任务。")
        return "".join(parts)


def analyze_task_request(
    user_message: str,
    supported_task_types: Iterable[str],
) -> TaskRequestAnalysis:
    """Analyze one WRITE message without mutating dialogue or task state."""
    text = str(user_message or "").strip()
    if not text:
        return TaskRequestAnalysis()

    alias_to_canonical = _build_task_aliases(supported_task_types)
    detected = _detect_task_types(text, alias_to_canonical)
    replacement = _is_explicit_replacement(text, alias_to_canonical)
    return TaskRequestAnalysis(
        detected_task_types=detected,
        is_explicit_replacement=replacement,
    )


def _build_task_aliases(supported_task_types: Iterable[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for raw_task_type in supported_task_types:
        canonical = str(raw_task_type or "").strip()
        if not canonical:
            continue
        aliases[canonical] = canonical
        simplified = canonical.replace("控制面板", "")
        if simplified != canonical:
            aliases[simplified] = canonical
    return aliases


def _detect_task_types(
    text: str,
    alias_to_canonical: dict[str, str],
) -> tuple[str, ...]:
    matches: list[tuple[int, int, str]] = []
    for alias, canonical in alias_to_canonical.items():
        for match in re.finditer(re.escape(alias), text):
            matches.append((match.start(), -len(alias), canonical))

    detected: list[str] = []
    for _, _, canonical in sorted(matches):
        if canonical not in detected:
            detected.append(canonical)
    return tuple(detected)


def _is_explicit_replacement(
    text: str,
    alias_to_canonical: dict[str, str],
) -> bool:
    for match in _REPLACEMENT_RE.finditer(text):
        before = _detect_task_types(text[: match.start()], alias_to_canonical)
        after = _detect_task_types(text[match.end() :], alias_to_canonical)
        if before and after and before[-1] != after[0]:
            return True
    return False
