"""Real-model overflow must preserve authoritative input and fail transactionally."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import src.llm_client as llm_module
from src.exceptions import ContextBudgetError
from src.llm_client import LLMClient


class CharTokenizer:
    def apply_chat_template(self, messages, **kwargs):
        return json.dumps(messages, ensure_ascii=False)

    def encode(self, text, **kwargs):
        return list(text)


def make_client(monkeypatch, limit=1200):
    monkeypatch.setattr(llm_module, "SamplingParams", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(llm_module, "is_model_profiles_v2_enabled", lambda: False)
    engine = SimpleNamespace(
        llm_engine=SimpleNamespace(model_config=SimpleNamespace(max_model_len=limit)),
        generate=Mock(return_value=[SimpleNamespace(outputs=[SimpleNamespace(text="回答")])]),
    )
    return LLMClient(engine, CharTokenizer())


def rendered(client):
    return client.llm.generate.call_args.args[0][0]


def test_old_turns_removed_without_changing_current_input_or_system(monkeypatch):
    client = make_client(monkeypatch, 500)
    messages = [
        {"role": "system", "content": "硬约束不能忽略"},
        {"role": "user", "content": "旧问题" * 100},
        {"role": "assistant", "content": "旧回复" * 100},
        {"role": "user", "content": "请解释这项警告，不要发布"},
    ]
    original = copy.deepcopy(messages)
    assert client.generate_text(messages, max_tokens=100) == "回答"
    assert json.loads(rendered(client)) == [messages[0], messages[-1]]
    assert len(rendered(client)) + 100 <= 500
    assert messages == original


def test_complete_relevant_knowledge_record_preserved_and_partial_declared(monkeypatch):
    client = make_client(monkeypatch)
    relevant = {"id": "C032", "name": "未来任务环境与遥测延后校验", "message": "运行前复核，不能推断机器人损坏"}
    evidence = {"found": True, "results": [
        {"category": "catalog", "robots": [{"name": str(i), "brief": "无关设备介绍" * 300} for i in range(3)]},
        {"category": "constraints", "rules": [relevant]},
    ]}
    prefix = "必须遵守硬约束。\n【知识库强类型检索证据】\n"
    suffix = "\n全部规则保持。当前任务事实：禁止发布。"
    messages = [
        {"role": "system", "content": prefix + json.dumps(evidence, ensure_ascii=False, indent=2) + suffix},
        {"role": "user", "content": "未来任务环境与遥测延后校验是什么意思？机器人坏了？不要发布。"},
    ]
    original = copy.deepcopy(messages)
    client.generate_text(messages, max_tokens=100)
    result = json.loads(rendered(client))
    assert result[0]["content"].startswith(prefix)
    assert result[0]["content"].endswith(suffix)
    compressed = json.loads(result[0]["content"][len(prefix):-len(suffix)])
    assert compressed["results"] == [{"category": "constraints", "rules": [relevant]}]
    assert compressed["context_budget"]["partial"] is True
    assert compressed["context_budget"]["omitted_records"] == 3
    assert result[-1] == messages[-1]
    assert messages == original
    assert len(rendered(client)) + 100 <= 1200


def test_mandatory_context_overflow_never_generates_or_slices_json(monkeypatch):
    client = make_client(monkeypatch, 400)
    messages = [
        {"role": "system", "content": "硬约束" * 100},
        {"role": "user", "content": json.dumps({"must_keep": "用户原文" * 100}, ensure_ascii=False)},
    ]
    before = copy.deepcopy(messages)
    with pytest.raises(ContextBudgetError, match="超过模型可处理长度"):
        client.generate_text(messages, max_tokens=100)
    assert messages == before
    client.llm.generate.assert_not_called()


def test_small_prompt_is_unchanged_and_output_budget_reserved(monkeypatch):
    client = make_client(monkeypatch, 300)
    messages = [{"role": "system", "content": "规则"}, {"role": "user", "content": "问题"}]
    client.generate_text(messages, max_tokens=100)
    assert json.loads(rendered(client)) == messages
    assert client.llm.generate.call_args.args[1].max_tokens == 100
    with pytest.raises(ContextBudgetError):
        client.generate_text(messages, max_tokens=290)


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
@pytest.mark.parametrize("phase", ["collecting", "blocked_soft"])
def test_overflow_error_rolls_back_and_allows_later_request(monkeypatch, endpoint, phase):
    import web_backend
    import src.web.state as state
    from src.dialogue_manager import DialogueManager
    from src.slots.slot_store import Slot

    sid = f"context-budget-{phase}-{endpoint}"
    manager = DialogueManager(session_id=sid)
    manager.phase = phase
    manager.conversation_history = [{"role": "user", "content": "原始任务"}]
    before = manager.export_snapshot()
    monkeypatch.setitem(state._sessions_manager, sid, manager)
    monkeypatch.setitem(web_backend.app.config, "TESTING", True)
    monkeypatch.setitem(web_backend.app.config, "SEAGENT_API_TOKENS", [])

    def overflow(*args, **kwargs):
        manager.slot_store.slots["start_time"] = Slot("start_time", value="2030-01-01T10:00:00", status="valid")
        manager.phase = "blocked_soft"
        manager._soft_whitelist.add("C032")
        manager.dialogue_mode = "knowledge_qa"
        manager.conversation_history.append({"role": "user", "content": "失败的本轮"})
        raise ContextBudgetError("当前问题与必要上下文超过模型可处理长度，请缩小查询范围。")

    monkeypatch.setattr(manager, "_process_internal", overflow)
    with web_backend.app.test_client() as client:
        response = client.post(endpoint, json={"session_id": sid, "request_id": "budget-request", "message": "解释警告"})
        if endpoint.endswith("/stream"):
            body = response.get_data(as_text=True)
            assert "event: error" in body
            assert "event: result" not in body
            payload = json.loads(body.split("data: ", 1)[1].strip())
        else:
            assert response.status_code == 503
            payload = response.get_json()
        assert payload["error"] == "ContextBudgetError"
        assert payload["code"] == 503
        assert payload["retryable"] is False
        assert payload["request_id"] == "budget-request"
        assert "槽位校验失败" not in payload["msg"]
        assert manager.export_snapshot() == before
        assert not manager._soft_whitelist
        monkeypatch.setattr(manager, "_process_internal", lambda *a, **kw: "可继续")
        next_response = client.post("/api/chat", json={"session_id": sid, "message": "简短问题"})
        assert next_response.status_code == 200
        assert next_response.get_json()["reply"] == "可继续"


def test_router_does_not_disguise_budget_failure_as_successful_clarification():
    from src.session.intent_router import IntentRouter
    llm = SimpleNamespace(classify_interaction=Mock(side_effect=ContextBudgetError("必要上下文超限")))
    with pytest.raises(ContextBudgetError, match="必要上下文超限"):
        IntentRouter(llm).route("解释水下机器人作业规则", [], {})
