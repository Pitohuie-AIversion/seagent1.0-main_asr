"""Regression coverage for backend findings B001–B003 and N001–N002."""

from datetime import datetime
import json
import re
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import patch

import pytest

from src.dialogue_manager import DialogueManager
from src.handlers.constraint_decision import ConstraintDecisionHandler
from src.handlers.slot_transaction import SlotTransactionManager
from src.knowledge_retriever import KnowledgeBase
from src.normalization_contract import NormalizationApplyPlan
from src.oilfield_linker import OilfieldEntityLinker
from src.output_builder import OutputBuilder
from src.slot_store import Slot, SlotStore
from src.task_intent_builder import TaskIntentBuilder
from src.task_patch import ListMutationPatch, TaskPatchValidationError, build_task_patch
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase()


def prepare_intent(kb, **kwargs):
    state = {
        "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "task_id": "PI-20990801-001",
        "intent_id": "TI2099080101",
        "equipment_type": "观察级ROV",
    }
    return TaskIntentBuilder(kb).prepare(
        task_state=state, built_json=kwargs.pop("built_json", state),
        mode="normal", task_type_key="pipeline_inspection", **kwargs,
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_validation_compatibility_copies_snapshot(kb, legacy):
    snapshot = {"status_ref": "robot:test", "values": {"depth": 300}}
    validation = {"overall_status": "valid", "state_snapshot": snapshot}
    value = SimpleNamespace(**validation) if legacy else validation
    intent = prepare_intent(kb, validation_result=value)
    result = intent["conditions"]["validation"]
    assert result["state_snapshot"] == snapshot
    snapshot["values"]["depth"] = 999
    assert result["state_snapshot"]["values"]["depth"] == 300


def test_dictionary_acknowledgements_are_copied(kb):
    ack = {"constraint_id": "C013", "evidence": {"accepted": True}}
    intent = prepare_intent(kb, validation_acknowledgements=[ack])
    ack["evidence"]["accepted"] = False
    assert intent["conditions"]["validation"]["acknowledged_constraints"][0]["evidence"]["accepted"]


@pytest.mark.parametrize("offset", ["-05:00", "+08:00", "Z", ""])
def test_intent_time_retains_instant_and_defaults_naive_to_beijing(kb, offset):
    start = "2099-08-01T08:00:00" + offset
    end = "2099-08-01T09:00:00" + offset
    intent = prepare_intent(kb, built_json={"start_time": start, "end_time": end})
    for key, original in (("start", start), ("end", end)):
        expected = original.replace("Z", "+00:00") if offset else original + "+08:00"
        assert datetime.fromisoformat(intent["time"][key]) == datetime.fromisoformat(expected)


def test_intent_normalization_preserves_subsecond_interval(kb):
    start = "2099-08-01T08:00:00.100000-05:00"
    end = "2099-08-01T08:00:00.200000-05:00"
    intent = prepare_intent(kb, built_json={"start_time": start, "end_time": end})
    assert intent["time"] == {"start": start, "end": end}


def oilfield_violations(kb, extras):
    manager = SimpleNamespace(
        task_state={"task_type_key": "tree_valve_operation", "oilfield_entity_id": "lingshui_17_2", **extras},
        mode="normal", builder=OutputBuilder(kb), kb=kb,
        oilfield_linker=OilfieldEntityLinker(kb.environment, kb.constraints),
    )
    return ConstraintDecisionHandler(manager)._merge_oilfield_context_violations([])


@pytest.mark.parametrize("extras", [
    {}, {"oilfield_coordinates": {"lat": 17.52, "lon": 110.15}},
    {"start_point": {"lat": 17.52, "lon": 110.15}}, {"water_depth": 100},
])
def test_uncollected_oilfield_context_is_not_a_hard_failure(kb, extras):
    assert oilfield_violations(kb, extras) == []


@pytest.mark.parametrize("extras", [
    {"oilfield_coordinates": None}, {"water_depth": None},
    {"water_depth": 100000},
])
def test_explicit_invalid_oilfield_context_stays_blocked(kb, extras):
    violations = oilfield_violations(kb, extras)
    assert violations
    assert all(v.severity == "hard" for v in violations)


def test_oilfield_mismatch_preserves_configured_constraint_severity(kb):
    violations = oilfield_violations(kb, {"oilfield_coordinates": {"lat": 0, "lon": 0}})
    configured = next(c for c in kb.get_constraints() if c["id"] == "C028")
    assert len(violations) == 1
    assert violations[0].constraint_id == "C028"
    assert violations[0].severity == configured["severity"]


def test_normalization_apply_plan_annotations_resolve():
    for method in (SlotTransactionManager.apply_normalized_plan_in_transaction,
                   SlotTransactionManager._apply_normalized_plan_in_transaction):
        assert get_type_hints(method)["plan"] is NormalizationApplyPlan


def mutation(operation, items=(), targets=()):
    return {
        "field": "payload", "operation": operation, "items": list(items),
        "target_items": list(targets), "raw_text": "scripted payload mutation",
        "confidence": 0.95, "source": "user_input",
    }


@pytest.mark.parametrize("items", [(), ("成像声呐",)])
def test_set_contract_accepts_complete_list_without_targets(items):
    result = ListMutationPatch(**{**mutation("set"), "items": items, "target_items": ()})
    assert result.items == items


def test_set_contract_rejects_targeted_semantics():
    with pytest.raises(TaskPatchValidationError):
        ListMutationPatch(**{**mutation("set"), "items": ("成像声呐",), "target_items": ("高清水下摄像机",)})


def seed_payload_manager(kb, payload):
    llm = ScriptedLLM(default_reply="已处理。")
    manager = DialogueManager(llm=llm, kb=kb)
    manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_inspection", manager.mode))
    slots = manager.slot_store.clone_slots()
    for key, value in (("task_type_key", "pipeline_inspection"), ("task_type", "管缆巡检")):
        slots[key] = Slot(key, value=value, status="valid")
    slots["payload"] = Slot("payload", value=list(payload), value_type="list", status="valid" if payload else "missing")
    manager.slot_store.commit_transaction(slots, [], request_id="seed-audit-payload")
    manager._rebuild_cache()
    manager.phase = "collecting"
    return manager, llm


@pytest.mark.parametrize("task_v2,norm_v2", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("initial,message,extraction,expected", [
    ([], "成像声呐", extraction_result(slot_candidate("payload", ["成像声呐"])), ["成像声呐"]),
    (["高清水下摄像机", "激光标尺"], "载荷改成成像声呐", extraction_result(slot_candidate("payload", ["成像声呐"])), ["成像声呐"]),
    (["高清水下摄像机", "激光标尺"], "把高清水下摄像机换成成像声呐", extraction_result(slot_candidate("payload", ["成像声呐"])), ["激光标尺", "成像声呐"]),
    (["高清水下摄像机", "激光标尺"], "执行预编排替换", extraction_result(list_mutations=[mutation("replace", ["成像声呐"], ["高清水下摄像机"])]), ["激光标尺", "成像声呐"]),
    (["高清水下摄像机"], "载荷改成不存在的工具", extraction_result(slot_candidate("payload", ["不存在的工具"])), ["高清水下摄像机"]),
])
def test_payload_candidate_and_mutation_full_runtime(kb, task_v2, norm_v2, initial, message, extraction, expected):
    manager, llm = seed_payload_manager(kb, initial)
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction)
    with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=task_v2), \
         patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=norm_v2):
        manager.process(message)
    assert manager.slot_store.slots["payload"].value == expected
    assert manager.task_state["payload"] == expected
    assert manager._last_built_json["payload"] == expected
    assert len(llm.extract_calls) == 1


def test_legacy_targeted_replace_without_targets_preserves_payload():
    slots = {"payload": Slot("payload", value=["机械手"], value_type="list", status="valid")}
    result = SlotStore().apply_list_mutation(
        slots, mutation("replace", ["摄像机"]), payload_catalog={},
        required_schema=[{"key": "payload", "type": "list", "allowed_values": ["机械手", "摄像机"]}],
    )
    assert not result["success"]
    assert slots["payload"].value == ["机械手"]


def test_extractor_payload_examples_follow_task_patch_contract(kb):
    manager, llm = seed_payload_manager(kb, [])
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction_result(slot_candidate("payload", ["成像声呐"])))
    manager.process("成像声呐")
    system_prompt = llm.extract_calls[0][0]["content"]
    examples = re.findall(r"list_mutations: (\[.*\])", system_prompt)
    operations = set()
    for example in examples:
        result = build_task_patch(extraction_result(list_mutations=json.loads(example)))
        operations.add(result.list_mutations[0].operation)
    assert operations == {"add", "remove", "clear", "set", "replace"}
