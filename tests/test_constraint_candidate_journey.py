"""Task constraints must guide choices without erasing explicit selections."""

import copy
from types import SimpleNamespace

import pytest

from src.dispatch.catalog_resolver import CatalogResolver
from src.handlers.equipment_collapse import EquipmentCollapseHandler
from src.knowledge_retriever import KnowledgeBase, RobotSelectionDataError
from src.slots.slot_store import Slot
from src.dialogue_manager import DialogueManager
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate


TASK = "pipeline_inspection"
AUV = "autonomous_underwater_vehicle_324cc"


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase()


def variants(domain):
    return {
        variant["variant_id"]: variant
        for category in domain["classes"]
        for family in category["families"]
        for variant in family["variants"]
    }


def slots_for(**values):
    return {
        key: Slot(key, value=value, status="valid", source="user_input")
        for key, value in {"task_type_key": TASK, **values}.items()
    }


@pytest.mark.parametrize("depth,expected_count", [
    (None, 3), (600, 3), (600.01, 1), (800, 1), (1000, 1), (1000.01, 0),
])
def test_depth_filters_choices_with_inclusive_hardware_boundary(kb, depth, expected_count):
    state = {} if depth is None else {"water_depth": depth}
    before = copy.deepcopy(state)
    domain = kb.get_feasible_robot_selection_domain(TASK, state)
    assert len(variants(domain)) == expected_count
    assert state == before
    if expected_count == 1:
        assert set(variants(domain)) == {AUV}
    if depth is not None and depth > 600:
        assert len(domain["rejected_variants"]) == 3 - expected_count
        assert all(item["reasons"] for item in domain["rejected_variants"])


@pytest.mark.parametrize("depth", [True, 0, -1, float("nan"), float("inf"), "unknown"])
def test_invalid_known_depth_cannot_offer_untested_candidates(kb, depth):
    with pytest.raises(RobotSelectionDataError):
        kb.get_feasible_robot_selection_domain(TASK, {"water_depth": depth})


@pytest.mark.parametrize("ref,expected", [
    ("robot_family_full_names", ["水下无人自主航行器"]),
    ("robot_variant_full_names", ["水下无人自主航行器 324CC"]),
])
def test_display_catalog_and_allowed_choices_use_the_same_constraints(kb, ref, expected):
    resolver = CatalogResolver(kb)
    field = {"allowed_values_ref": ref, "type": "string"}
    state = {"task_type_key": TASK, "water_depth": 800}
    assert resolver.resolve_allowed(field, TASK, state) == expected
    assert [
        row["canonical_value"] for row in resolver.resolve_candidate_catalog(field, TASK, state)
    ] == expected
    # Relaxing a condition must not reuse the narrowed candidate list.
    assert len(resolver.resolve_allowed(field, TASK, {"water_depth": 300})) == 3


def test_incompatible_known_variant_has_no_selectable_units(kb):
    resolver = CatalogResolver(kb)
    state = {
        "water_depth": 800,
        "equipment_type": "观察级深海机器人 75HP",
        "equipment_family": "观察级深海机器人",
    }
    field = {"allowed_values_ref": "robot_unit_ids", "type": "string"}
    assert resolver.resolve_allowed(field, TASK, state) == []
    assert resolver.resolve_candidate_catalog(field, TASK, state) == []


@pytest.mark.parametrize("source", ["onboard_payloads", "supported_payloads"])
def test_confirmed_payload_filters_variants_and_tracks_installation(kb, source):
    isolated = KnowledgeBase()
    hard_params = isolated.robot_fleet["model_variants"][AUV]["hard_params"]
    hard_params[source] = ["审查专用测量工具"]
    domain = isolated.get_feasible_robot_selection_domain(
        TASK, {"payload": ["审查专用测量工具"]},
    )
    chosen = variants(domain)
    assert set(chosen) == {AUV}
    assert chosen[AUV]["requires_installation"] == (
        ["审查专用测量工具"] if source == "supported_payloads" else []
    )
    assert len(domain["rejected_variants"]) == 2


def test_known_depth_automatically_selects_only_feasible_family(kb):
    slots = slots_for(water_depth=800)
    collapse = EquipmentCollapseHandler(SimpleNamespace(kb=kb))
    collapse.auto_collapse_robot_cascade(slots)
    assert slots["equipment_family"].value == "水下无人自主航行器"
    assert slots["equipment_type"].value == "水下无人自主航行器 324CC"
    assert slots["equipment_type"].source == "auto"
    slots["water_depth"].value = 300
    collapse.auto_collapse_robot_cascade(slots)
    assert slots["equipment_type"].status == "missing"
    assert slots["equipment_family"].status == "missing"


def test_explicit_incompatible_choice_remains_available_for_specific_diagnosis(kb):
    slots = slots_for(
        water_depth=800, equipment_class="observation_rov",
        equipment_family="观察级深海机器人",
        equipment_type="观察级深海机器人 75HP", equipment_unit_id="OBSROV-75-001",
    )
    expected = copy.deepcopy(slots)
    EquipmentCollapseHandler(SimpleNamespace(kb=kb)).auto_collapse_robot_cascade(slots)
    for key, old in expected.items():
        assert slots[key].to_dict() == old.to_dict()
    # The static registry remains usable for explicit input and error reporting.
    assert kb._resolve_robot_variant_exact("观察级深海机器人 75HP") is not None


def test_zero_candidates_recovers_when_user_corrects_depth(kb):
    slots = slots_for(water_depth=1200)
    collapse = EquipmentCollapseHandler(SimpleNamespace(kb=kb))
    collapse.auto_collapse_robot_cascade(slots)
    assert slots["equipment_class"].status == "invalid"
    slots["water_depth"].value = 800
    collapse.auto_collapse_robot_cascade(slots)
    assert slots["equipment_type"].value == "水下无人自主航行器 324CC"
    assert slots["equipment_type"].status == "valid"


@pytest.mark.parametrize("patch,norm", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("model_has_choice", [False, True])
def test_explicit_robot_outside_suggestions_survives_full_dialogue(
    kb, monkeypatch, patch, norm, model_has_choice,
):
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="收到。")
    manager = DialogueManager(llm, kb)
    manager.slot_store.init_task_slots(manager.builder.get_schema(TASK, "normal"))
    slots = manager.slot_store.clone_slots()
    for key, value in {
        "task_type_key": TASK, "task_type": "管缆巡检", "water_depth": 800,
    }.items():
        slots[key] = Slot(key, value=value, status="valid", source="user_input")
    manager.slot_store.commit_transaction(slots, [], request_id="seed-deep-task")
    manager._rebuild_cache()
    manager.phase = "collecting"
    llm.queue_extraction(extraction_result(*(
        [slot_candidate("equipment_type", "观察级深海机器人 75HP")]
        if model_has_choice else []
    )))
    manager.process("机器人型号使用观察级深海机器人75HP。")
    assert manager.task_state["water_depth"] == 800
    assert manager.task_state["equipment_type"] == "观察级深海机器人 75HP"
    assert manager.slot_store.slots["equipment_type"].status == "valid"


@pytest.mark.parametrize("patch,norm", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("selection", ["automatic", "explicit-model", "explicit-unit"])
def test_later_payload_edit_can_reconsider_only_automatic_robot_selection(
    kb, monkeypatch, patch, norm, selection,
):
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="收到。")
    manager = DialogueManager(llm, kb)
    manager.slot_store.init_task_slots(manager.builder.get_schema(TASK, "normal"))
    slots = manager.slot_store.clone_slots()
    for key, value in {"task_type_key": TASK, "task_type": "管缆巡检"}.items():
        slots[key] = Slot(key, value=value, status="valid")
    manager.slot_store.commit_transaction(slots, [], request_id="seed-payload-selection")
    manager._rebuild_cache()
    manager.phase = "collecting"

    llm.queue_extraction(extraction_result(slot_candidate("payload", ["高清水下摄像机"])))
    manager.process("载荷选择高清水下摄像机")
    assert manager.task_state["equipment_type"] == "观察级深海机器人 75HP"
    assert manager.slot_store.slots["equipment_type"].source == "auto"
    if selection != "automatic":
        key = "equipment_unit_id" if selection == "explicit-unit" else "equipment_type"
        selected = manager.task_state[key]
        llm.queue_extraction(extraction_result(slot_candidate(key, selected)))
        manager.process(f"{'单机编号' if selection == 'explicit-unit' else '机器人型号'}使用{selected}")
        assert manager.slot_store.slots[key].source != "auto"

    before = copy.deepcopy(manager.task_state["payload"])
    llm.queue_extraction(extraction_result(slot_candidate("payload", ["成像声呐"])))
    manager.process("载荷再加成像声呐")
    if selection == "automatic":
        assert manager.task_state["payload"] == ["高清水下摄像机", "成像声呐"]
        assert manager.slot_store.slots["payload"].status == "valid"
    else:
        assert manager.task_state["payload"] == before
        assert manager.task_state[key] == selected
        assert manager.slot_store.slots[key].source != "auto"
