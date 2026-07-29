"""Deterministic pre-write analysis for compound task requests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


_CLAUSE_SPLIT_RE = re.compile(
    r"(?:[，,；;。]\s*)?"
    r"(?:另外(?:再)?|此外|同时|还要|再帮我|并且|以及)\s*"
)
_TASK_ACTION_CUES = (
    "帮我",
    "安排",
    "创建",
    "执行",
    "下达",
    "做个",
    "做一个",
    "制作",
    "获取",
    "去",
    "买",
)
_REPLACEMENT_RE = re.compile(r"(?:改成|改为|换成|更换为|调整为|替换为)")


@dataclass(frozen=True)
class TaskRequestAnalysis:
    detected_task_types: tuple[str, ...] = ()
    unsupported_clauses: tuple[str, ...] = ()
    is_explicit_replacement: bool = False

    @property
    def should_block(self) -> bool:
        if self.unsupported_clauses:
            return True
        return len(self.detected_task_types) > 1 and not self.is_explicit_replacement

    def build_reply(self) -> str:
        parts: list[str] = []
        if self.unsupported_clauses:
            unsupported = "、".join(f"“{clause}”" for clause in self.unsupported_clauses)
            parts.append(f"当前系统不支持以下域外任务：{unsupported}。")

        if len(self.detected_task_types) > 1:
            choices = "、".join(self.detected_task_types)
            parts.append(
                f"检测到您同时提出了多个水下任务：{choices}。"
                "一次只能创建一个任务，请选择当前要规划的任务。"
            )
        elif self.unsupported_clauses and self.detected_task_types:
            parts.append(
                f"当前可支持的水下任务是“{self.detected_task_types[0]}”。"
                "请仅提交一个受支持的任务后继续。"
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
    unsupported = _detect_unsupported_clauses(text, alias_to_canonical)
    return TaskRequestAnalysis(
        detected_task_types=detected,
        unsupported_clauses=unsupported,
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


def _detect_unsupported_clauses(
    text: str,
    alias_to_canonical: dict[str, str],
) -> tuple[str, ...]:
    clauses = [
        clause.strip(" \t\r\n，,；;。")
        for clause in _CLAUSE_SPLIT_RE.split(text)
    ]
    if len(clauses) <= 1:
        return ()

    unsupported: list[str] = []
    for clause in clauses:
        if not clause:
            continue
        if _detect_task_types(clause, alias_to_canonical):
            continue
        if not any(cue in clause for cue in _TASK_ACTION_CUES):
            continue
        if clause not in unsupported:
            unsupported.append(clause)
    return tuple(unsupported)
