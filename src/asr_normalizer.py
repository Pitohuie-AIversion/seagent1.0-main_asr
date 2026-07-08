"""
asr_normalizer.py - Context-aware rule-based ASR terminology correction.

This module corrects explicitly matched professional terminology aliases using local context rules.
It does not infer task type, task state, urgency, or rewrite user intent.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TermRule:
    target: str
    aliases: list[str]
    context_words: list[str]
    category: str
    base_score: int = 3
    threshold: int = 4


@dataclass(frozen=True)
class TermCandidate:
    source: str
    target: str
    category: str
    start: int
    end: int
    score: int
    evidence: list[str]


ACTION_WORDS = frozenset({"插入", "拔出", "确认", "下发", "开始", "结束", "巡检", "检查"})


TREE_TYPE_CONTEXT_WORDS = [
    "采油树",
    "采油树类型",
    "控制面板",
    "液压面板",
    "电液控制面板",
    "井口",
    "井",
    "阀门",
    "主阀",
    "翼阀",
    "节流阀",
    "ROV",
    "工作级ROV",
    "机械臂",
    "扭矩工具",
    "FMC",
    "类型",
    "插拔",
    "插入",
    "拔出",
]


COORDINATE_CONTEXT_WORDS = [
    "北纬",
    "纬度",
    "经纬度",
    "坐标",
    "起始点",
    "结束点",
    "油田坐标",
    "油田位置",
    "管缆位置",
    "度",
]


TERM_RULES: tuple[TermRule, ...] = (
    # 采油树不再区分立式/卧式，停用类型名称的ASR转写纠错。
    # TermRule(
    #     target="卧式",
    #     aliases=["卧室", "卧试", "或式", "卧事"],
    #     context_words=TREE_TYPE_CONTEXT_WORDS,
    #     category="tree_type",
    # ),
    # TermRule(
    #     target="立式",
    #     aliases=["历史", "力士", "例式", "利式", "粒式", "立事"],
    #     context_words=TREE_TYPE_CONTEXT_WORDS,
    #     category="tree_type",
    # ),
    # TermRule(
    #     target="卧式采油树",
    #     aliases=["卧室采油树", "卧试采油树"],
    #     context_words=TREE_TYPE_CONTEXT_WORDS,
    #     category="tree",
    # ),
    TermRule(
        target="采油树",
        aliases=["采油数", "采油书", "采油术","柴油数","柴油树"],
        context_words=[
            "控制面板",
            "液压面板",
            "电液控制面板",
            "井口",
            "井",
            "阀门",
            "主阀",
            "翼阀",
            "节流阀",
            "ROV",
            "工作级ROV",
            "机械臂",
            "扭矩工具",
            "FMC",
            "插拔",
            "插入",
            "拔出",
        ],
        category="tree",
    ),
    TermRule(
        target="翼阀",
        aliases=["逆阀", "一阀", "义阀"],
        context_words=["目标阀门", "主阀", "阀门", "采油树", "控制面板", "井口", "ROV", "机械臂"],
        category="valve",
    ),
    TermRule(
        target="陵水",
        aliases=["临水", "灵水", "令水","邻水"],
        context_words=["油田", "气田", "区块", "井区", "南海", "作业区", "陵水"],
        category="oilfield",
    ),
    TermRule(
        target="流花",
        aliases=["硫化", "流化", "六花"],
        context_words=["油田", "气田", "区块", "井区", "南海", "作业区"],
        category="oilfield",
    ),
    TermRule(
        target="管缆",
        aliases=["管蓝", "管栏","馆缆","观览","馆览"],
        context_words=["巡检", "管线", "管道", "起始点", "结束点", "坐标", "ROV", "AUV", "观察级ROV","巡检"],
        category="pipeline",
    ),
    TermRule(
        target="声呐",
        aliases=["声纳"],
        context_words=["巡检", "管线", "管道", "坐标", "ROV", "AUV", "观察级ROV", "高清摄像", "多波束", "侧扫"],
        category="pipeline",
    ),
    TermRule(
        target="东经",
        aliases=["东京", "东京都"],
        context_words=COORDINATE_CONTEXT_WORDS,
        category="coordinate_direction",
        threshold=7,
    ),
)


def normalize_terminology(text: str, window_size: int = 16) -> dict:
    """Correct ASR terminology aliases with local context scoring."""
    if not text:
        return {
            "text": text,
            "corrected_text": text,
            "normalization_changed": False,
            "replacements": [],
            "warnings": [],
        }

    candidates = _collect_candidates(text, max(window_size, 0))
    selected, warnings = _select_candidates(candidates)
    corrected_text = _apply_replacements(text, selected)

    return {
        "text": text,
        "corrected_text": corrected_text,
        "normalization_changed": corrected_text != text,
        "replacements": [
            {
                "source": item.source,
                "target": item.target,
                "category": item.category,
                "start": item.start,
                "end": item.end,
                "score": item.score,
                "evidence": item.evidence,
            }
            for item in selected
        ],
        "warnings": warnings,
    }


def _collect_candidates(text: str, window_size: int) -> list[TermCandidate]:
    candidates: list[TermCandidate] = []
    for rule in TERM_RULES:
        for alias in rule.aliases:
            if alias == rule.target or alias in ACTION_WORDS:
                continue
            start = 0
            while True:
                index = text.find(alias, start)
                if index < 0:
                    break
                end = index + len(alias)
                score, evidence = _score_candidate(text, index, end, alias, rule, window_size)
                if score >= rule.threshold:
                    candidates.append(
                        TermCandidate(
                            source=alias,
                            target=rule.target,
                            category=rule.category,
                            start=index,
                            end=end,
                            score=score,
                            evidence=evidence,
                        )
                    )
                start = index + 1
    return candidates


def _score_candidate(
    text: str,
    start: int,
    end: int,
    alias: str,
    rule: TermRule,
    window_size: int,
) -> tuple[int, list[str]]:
    score = rule.base_score
    evidence = [f"命中候选词“{alias}”"]
    window_start = max(0, start - window_size)
    window_end = min(len(text), end + window_size)
    window = text[window_start:window_end]

    for word in rule.context_words:
        if word and word in window:
            score += 2
            evidence.append(f"命中上下文“{word}”")
    return score, evidence


def _select_candidates(candidates: list[TermCandidate]) -> tuple[list[TermCandidate], list[str]]:
    warnings: list[str] = []
    if not candidates:
        return [], warnings

    best_by_span: dict[tuple[int, int, str], list[TermCandidate]] = {}
    for item in candidates:
        best_by_span.setdefault((item.start, item.end, item.source), []).append(item)

    resolved: list[TermCandidate] = []
    for group in best_by_span.values():
        ordered = sorted(group, key=lambda item: item.score, reverse=True)
        if len(ordered) > 1 and ordered[0].score - ordered[1].score < 2:
            top_targets = "、".join({item.target for item in ordered if item.score == ordered[0].score})
            warnings.append(f"候选冲突：{ordered[0].source} -> {top_targets}")
            continue
        resolved.append(ordered[0])

    selected: list[TermCandidate] = []
    for item in sorted(resolved, key=lambda item: (item.start, -(item.end - item.start), -item.score)):
        if any(_overlaps(item, existing) for existing in selected):
            warnings.append(f"跳过重叠候选：{item.source} -> {item.target}")
            continue
        selected.append(item)
    return sorted(selected, key=lambda item: item.start), warnings


def _overlaps(left: TermCandidate, right: TermCandidate) -> bool:
    return left.start < right.end and right.start < left.end


def _apply_replacements(text: str, replacements: list[TermCandidate]) -> str:
    if not replacements:
        return text

    parts: list[str] = []
    cursor = 0
    for item in replacements:
        parts.append(text[cursor:item.start])
        parts.append(item.target)
        cursor = item.end
    parts.append(text[cursor:])
    return "".join(parts)
