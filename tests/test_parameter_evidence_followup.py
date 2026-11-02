"""Historical values and other-field qualifiers must keep their local scope."""
import copy

import pytest

from src.dialogue_manager import DialogueManager
from src.extraction.extractor import ParameterExtractor
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate


FIRST = {"lat": 20.1, "lon": 115.2}
CURRENT = {"lat": 20.2, "lon": 115.3}
TRACKED = ("water_depth", "start_point", "end_point", "support_vessel")


@pytest.fixture(params=[(False, False), (True, False), (True, True)],
                ids=["legacy", "patch-v2", "norm-v2"])
def dialogue(request, monkeypatch, tmp_path):
    patch, norm = request.param
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="收到。")
    manager = DialogueManager(llm, KnowledgeBase())
    manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_inspection", "normal"))
    slots = manager.slot_store.clone_slots()
    values = {
        "task_type_key": "pipeline_inspection", "task_type": "管缆巡检",
        "intent_id": "TI2026102001", "water_depth": 300,
        "start_point": {"lat": 20.0, "lon": 115.0},
        "end_point": {"lat": 20.3, "lon": 115.5}, "support_vessel": "海洋石油681",
    }
    for key, value in values.items():
        slots[key].value = copy.deepcopy(value)
        slots[key].status = "valid"
        slots[key].raw_value = f"先前确认的{key}"
        slots[key].confidence = 0.98
        slots[key].source = "user_input"
    manager.slot_store.commit_transaction(slots, [], request_id="seed-parameter-evidence")
    manager._rebuild_cache()
    manager.phase = "collecting"
    return manager, llm


def assert_turn(dialogue, message, updates, mentioned_fields, model_has_wrong_values):
    manager, llm = dialogue
    before = {key: manager.slot_store.slots[key].to_dict() for key in TRACKED}
    wrong = {"water_depth": 999, "start_point": {"lat": 22.0, "lon": 117.0}}
    candidates = [slot_candidate(key, wrong[key]) for key in mentioned_fields] if model_has_wrong_values else []
    llm.queue_extraction(extraction_result(*candidates))

    manager.process(message)

    # Exercise the real extractor/normalizer/transaction path; only the model
    # response is scripted so omission and incorrect proposals are reproducible.
    assert isinstance(manager.extractor, ParameterExtractor)
    assert len(llm.extract_calls) == 1
    for key in TRACKED:
        if key in updates:
            assert manager.task_state[key] == updates[key]
            assert manager.slot_store.slots[key].status == "valid"
        else:
            assert manager.slot_store.slots[key].to_dict() == before[key]
    restored = DialogueManager(ScriptedLLM(), manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert restored.task_state == manager.task_state
    for key in TRACKED:
        assert restored.slot_store.slots[key].to_dict() == manager.slot_store.slots[key].to_dict()


@pytest.mark.parametrize("message,updates,mentioned_fields", [
    ("昨天，水深320米。", {}, ("water_depth",)),
    ("昨天，水深320米，现在水深改为350米。",
     {"water_depth": 350}, ("water_depth",)),
    ("之前起点北纬20.1度东经115.2度，现在起点改为北纬20.2度东经115.3度。",
     {"start_point": CURRENT}, ("start_point",)),
    ("昨天，起点北纬20.1度东经115.2度，水深320米。",
     {}, ("start_point", "water_depth")),
    ("此前，水深320米，起点北纬20.1度东经115.2度，现在水深改为350米，起点改为北纬20.2度东经115.3度。",
     {"water_depth": 350, "start_point": CURRENT}, ("start_point", "water_depth")),
    ("昨天，水深改为320米，起点改为北纬20.1度东经115.2度。",
     {}, ("start_point", "water_depth")),
    ("不要把水深改为320米，水深改为350米，不要把起点改为北纬20.1度东经115.2度，起点改为北纬20.2度东经115.3度。",
     {"water_depth": 350, "start_point": CURRENT}, ("start_point", "water_depth")),
    ("昨天，水深不变，现在水深改为350米。",
     {"water_depth": 350}, ("water_depth",)),
    ("昨天，水深不变。",
     {}, ("water_depth",)),
    ("昨天，水深320米，取消这个修改，现在水深改为350米。",
     {"water_depth": 350}, ("water_depth",)),
    ("昨天，起点改为北纬20.1度东经115.2度，取消这个修改。现在起点改为北纬20.2度东经115.3度。",
     {"start_point": CURRENT}, ("start_point",)),
    ("昨天，坐标不变。现在起点改为北纬20.2度东经115.3度。",
     {"start_point": CURRENT}, ("start_point",)),
    ("昨天，坐标不变。",
     {}, ("start_point",)),
    ("昨天在流花11-1油田，水深改为320米，起点改为北纬20.1度东经115.2度。",
     {"water_depth": 320, "start_point": FIRST}, ("start_point", "water_depth")),
    ("昨天水深320米，起点改为北纬20.1度东经115.2度。",
     {"start_point": FIRST}, ("start_point", "water_depth")),
    ("昨天起点北纬20.1度东经115.2度，水深改为350米。",
     {"water_depth": 350}, ("start_point", "water_depth")),
    ("水深不变，现在水深改为350米。",
     {}, ("water_depth",)),
    ("起点改为北纬20.1度东经115.2度，取消这个修改。现在起点改为北纬20.2度东经115.3度。",
     {}, ("start_point",)),
])
@pytest.mark.parametrize("model_has_wrong_values", [False, True])
def test_history_and_explicit_current_corrections(
    dialogue, message, updates, mentioned_fields, model_has_wrong_values,
):
    assert_turn(dialogue, message, updates, mentioned_fields, model_has_wrong_values)


@pytest.mark.parametrize("message,updates", [
    ("起点改为北纬20.1度东经115.2度，水深320米，可以吗？", {"start_point": FIRST}),
    ("起点改为北纬20.1度东经115.2度，水深320米，取消这个修改。", {"start_point": FIRST}),
    ("起点改为北纬20.1度东经115.2度，水深320米，如果天气允许。", {"start_point": FIRST}),
    ("起点改为北纬20.1度东经115.2度，可以吗？水深改为350米。", {"water_depth": 350}),
    ("起点改为北纬20.1度东经115.2度，取消这个修改，水深改为350米。", {"water_depth": 350}),
    ("起点改为北纬20.1度东经115.2度，如果天气允许，水深改为350米。", {"start_point": FIRST}),
    ("起点改为北纬20.1度东经115.2度，如果天气允许。水深改为350米。", {"water_depth": 350}),
])
@pytest.mark.parametrize("model_has_wrong_values", [False, True])
def test_coordinate_followups_bind_to_the_nearest_field(
    dialogue, message, updates, model_has_wrong_values,
):
    assert_turn(dialogue, message, updates, ("start_point", "water_depth"), model_has_wrong_values)


@pytest.mark.parametrize("message,mentioned_fields", [
    ("若需要，水深改为320米。", ("water_depth",)),
    ("若需要，起点改为北纬20.1度东经115.2度。", ("start_point",)),
])
@pytest.mark.parametrize("model_has_wrong_values", [False, True])
def test_bare_ruo_condition_does_not_write(
    dialogue, message, mentioned_fields, model_has_wrong_values,
):
    assert_turn(dialogue, message, {}, mentioned_fields, model_has_wrong_values)
