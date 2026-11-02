"""Knowledge answers must see the same payload semantics as task validation."""

import json
from copy import deepcopy

import pytest

from src.knowledge_retriever import KnowledgeBase
from tests.test_llm_context_budget import make_client, rendered


@pytest.mark.parametrize("query_type", ["KNOWLEDGE_QA", "TOOL_QUERY"])
def test_requirement_query_includes_actual_onboard_and_optional_configuration(query_type):
    kb = KnowledgeBase()
    evidence = kb.execute_typed_query(
        query_type, "采油树阀门作业必须另带电液机械臂吗？",
        context={"subject_type": "system_rule"},
    )["payload_configuration_evidence"]
    robot = next(r for r in evidence["robot_configurations"]
                 if r["equipment_type"] == "通用工作级深海机器人 250HP")
    assert "多功能液压机械臂" in robot["onboard_payloads"]
    assert "电液机械臂" in robot["supported_payloads"]
    assert robot["capabilities"] == ["tree_operation"]
    assert evidence["task_required_capabilities"]["tree_valve_operation"]["required_capabilities"] == ["tree_operation"]
    assert "不表示必须携带" in evidence["selection_semantics"]["supported_payloads"]


def test_configuration_evidence_follows_changed_knowledge_base(monkeypatch):
    kb = KnowledgeBase()
    robots = deepcopy(kb.get_all_rovs())
    robots[0]["onboard_payloads"] = ["测试机载工具"]
    robots[0]["supported_payloads"] = ["测试选配工具"]
    monkeypatch.setattr(kb, "get_all_rovs", lambda: robots)
    evidence = kb.execute_typed_query("KNOWLEDGE_QA", "解释载荷选择规则", context={"subject_type": "system_rule"})
    actual = evidence["payload_configuration_evidence"]["robot_configurations"][0]
    assert actual["onboard_payloads"] == ["测试机载工具"]
    assert actual["supported_payloads"] == ["测试选配工具"]


def test_unrelated_knowledge_query_does_not_add_payload_context():
    evidence = KnowledgeBase().execute_typed_query(
        "KNOWLEDGE_QA", "硬约束能不能忽略？", context={"subject_type": "system_rule"},
    )
    assert "payload_configuration_evidence" not in evidence


def test_payload_configuration_survives_general_evidence_trimming(monkeypatch):
    evidence = KnowledgeBase().execute_typed_query(
        "KNOWLEDGE_QA", "是否必须加装机械臂？", context={"subject_type": "system_rule"},
    )
    expected = deepcopy(evidence["payload_configuration_evidence"])
    evidence["results"] = [{"category": "irrelevant", "entries": [{"text": "无关介绍" * 20000}]}]
    client = make_client(monkeypatch, limit=15000)
    prefix = "【知识库强类型检索证据】\n"
    client.generate_text([
        {"role": "system", "content": prefix + json.dumps(evidence, ensure_ascii=False)},
        {"role": "user", "content": "是否必须加装机械臂？"},
    ], max_tokens=100)
    sent = json.loads(rendered(client))
    compressed = json.loads(sent[0]["content"][len(prefix):])
    assert compressed["context_budget"]["partial"] is True
    assert compressed["payload_configuration_evidence"] == expected
    assert compressed["results"] == []


def test_specific_requirement_query_omits_irrelevant_catalogues_but_keeps_rules():
    evidence = KnowledgeBase().execute_typed_query(
        "KNOWLEDGE_QA",
        "管缆巡检必须携带侧扫声呐或成像声呐吗？采油树阀门作业必须另带电液机械臂吗？",
        context={"subject_type": "system_rule"},
    )
    assert evidence["query_mode"] == "payload_requirements"
    categories = {item["category"]: item for item in evidence["results"]}
    assert set(categories) == {"task_templates", "constraints_rules", "payload_catalog"}
    payload_field = categories["task_templates"]["templates"]["tree_valve_operation"]["payload_fields_by_mode"]["normal"][0]
    assert payload_field["allowed_values_ref"] == "payload_options.tree_valve_operation"
    rule = next(rule for rule in categories["constraints_rules"]["constraints"] if rule["id"] == "C002")
    assert rule["check_type"] == "robot_category"
    assert "robot_configurations" in evidence["payload_configuration_evidence"]


def test_comprehensive_explanation_keeps_environment_and_vessel_evidence():
    evidence = KnowledgeBase().execute_typed_query(
        "KNOWLEDGE_QA", "请解释DVL、海流、禁入区、载荷和支持船规则。",
        context={"subject_type": "system_rule"},
    )
    categories = {item["category"] for item in evidence["results"]}
    assert {"oil_fields", "forbidden_areas", "vessels"} <= categories
    assert "payload_configuration_evidence" in evidence
