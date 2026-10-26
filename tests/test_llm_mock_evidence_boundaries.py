"""Offline replies must use the requested evidence section, not later JSON."""

import pytest

from src.extraction.prompts import (
    build_general_chat_messages,
    build_knowledge_responder_messages,
    build_status_responder_messages,
)
from src.llm_client import LLMClient


@pytest.mark.parametrize("question", ["你好", "你是谁"])
def test_general_chat_capability_facts_do_not_become_knowledge_results(question):
    client = LLMClient()
    assert client.is_mock
    messages = build_general_chat_messages([], question, {"child_tasks_supported": False})

    reply = client.chat(messages)

    assert "水下" in reply
    assert "当前知识库未提供" not in reply


def test_knowledge_reply_uses_its_own_section_despite_marker_mentions_and_later_facts():
    client = LLMClient()
    assert client.is_mock
    messages = build_knowledge_responder_messages(
        {"found": True, "query_type": "TOOL_QUERY", "results": [
            {"category": "all_supported_tools", "tools": ["侧扫声呐"]},
        ]}, [], "有哪些工具？", task_structure_evidence={"found": False},
    )

    assert "侧扫声呐" in client.chat(messages)


@pytest.mark.parametrize("found,expected", [(True, "collecting"), (False, "当前实时状态源尚未建立")])
def test_status_section_is_not_mistaken_for_knowledge_evidence(found, expected):
    client = LLMClient()
    assert client.is_mock
    messages = build_status_responder_messages(
        {"found": found, "query_type": "TASK_STATUS", "phase": "collecting"},
        [], "当前任务状态如何？",
    )

    assert expected in client.chat(messages)


@pytest.mark.parametrize("section", ["证据尚未提供", "{broken json}"])
def test_missing_or_invalid_evidence_does_not_consume_another_section(section):
    client = LLMClient()
    assert client.is_mock
    messages = [
        {"role": "system", "content": (
            "SEAgent 水下多智能体任务决策系统\n【知识库强类型检索证据】\n"
            + section + '\n【其他说明】\n{"found": false}'
        )},
        {"role": "user", "content": "你是谁"},
    ]

    assert "知识与状态查询" in client.chat(messages)
