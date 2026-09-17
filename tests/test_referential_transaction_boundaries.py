"""References preserve the original intent and use normal task transactions."""
from copy import deepcopy

import pytest

from src.dialogue_manager import DialogueManager
from src.slot_store import Slot
from tests.interaction_plan_support import ScriptedLLM, make_plan
from tests.test_slot_consistency import seed_complete_valid_pipeline_task


def make_manager(operation="WRITE"):
    return DialogueManager(llm=ScriptedLLM(
        default_plan=make_plan(operation, query_intent="ENVIRONMENT_QUERY" if operation == "READ" else None),
        default_reply="已处理本轮请求。",
    ))


def select_task(dm, key):
    dm.slot_store.init_task_slots(dm.builder.get_schema(key, dm.mode))
    slots, unresolved, version = dm.slot_store.snapshot()
    slots["task_type_key"] = Slot("task_type_key", value=key, status="valid")
    dm.slot_store.commit_transaction(slots, unresolved, expected_version=version)
    dm._rebuild_cache()


def business_state(dm):
    return deepcopy((dm.slot_store.export_snapshot(), dm.task_state,
                     dm._last_built_json, dm._last_missing, dm.phase, dm.final_result))


@pytest.mark.parametrize("message", [
    "如果选这个油田，水深多少？",
    "用这个机器人有什么限制？",
    "带这个工具是否适合管缆巡检？",
    "开始这个任务需要哪些参数？",
])
@pytest.mark.parametrize("phase", ["collecting", "confirming", "blocked_soft", "blocked_hard"])
def test_read_references_preserve_whole_turn_business_state(message, phase):
    dm = make_manager("READ")
    dm.phase = phase
    dm._last_discussed_oilfield = "流花11-1油田"
    dm._last_discussed_robot = "金牛座"
    dm._last_discussed_payload = "高清水下摄像机"
    dm._last_discussed_task_type = "pipeline_inspection"
    before = business_state(dm)

    dm.process(message)

    assert business_state(dm) == before
    assert message in dm.llm.classify_calls[0][-1]["content"]
    assert not dm.llm.extract_calls
    assert dm.conversation_history[-2]["content"] == message


def test_read_does_not_confirm_pending_oilfield():
    dm = make_manager("READ")
    dm.slot_store.slots["pending_oilfield_name"] = Slot(
        "pending_oilfield_name", value="流花", status="valid")
    dm.slot_store.slots["pending_oilfield_candidates"] = Slot(
        "pending_oilfield_candidates", value=[{"name": "流花11-1油田", "id": "lh"}],
        value_type="list", status="valid")
    before = business_state(dm)

    dm.process("确认这个油田的水深是多少？")

    assert business_state(dm) == before


def test_published_task_read_reference_preserves_done_and_published_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    dm = make_manager("READ")
    seed_complete_valid_pipeline_task(dm, dm.kb)
    dm.process("确认发布")
    if dm.phase == "blocked_soft":
        dm.process("忽略警告")
        dm.process("确认发布")
    assert dm.phase == "done"
    dm.ros2_dispatch = {"state": "SENT", "task_id": 0x80001, "retry_allowed": False}
    restored = make_manager("READ")
    saved = dm.export_snapshot()
    restored.load_snapshot(saved)
    assert restored.ros2_dispatch == dm.ros2_dispatch
    saved["ros2_dispatch"]["state"] = "FAILED"
    assert restored.ros2_dispatch["state"] == "SENT"
    dm._last_discussed_oilfield = "流花11-1油田"
    before = business_state(dm)

    dm.process("如果选这个油田，水深多少？")

    assert business_state(dm) == before


def test_done_write_reference_cannot_restart_or_modify_archived_task():
    dm = make_manager()
    dm.phase = "done"
    dm._last_discussed_oilfield = "流花11-1油田"
    before = business_state(dm)

    reply = dm.process("就去这个油田")

    assert business_state(dm) == before
    assert "无法就地修改" in reply


def test_reference_task_creation_commits_ids_and_preserves_original_text():
    dm = make_manager()
    dm._last_discussed_task_type = "pipeline_inspection"
    original_version = dm.slot_store.version

    dm.process("那开始这个任务")

    assert dm.task_state["task_type_key"] == "pipeline_inspection"
    assert dm.task_state["internal_id"]
    assert dm.slot_store.version > original_version
    assert dm.task_state == dm.slot_store.get_task_state()
    assert dm.conversation_history[-2]["content"] == "那开始这个任务"
    assert "那开始这个任务" in dm.llm.classify_calls[0][-1]["content"]


def test_oilfield_reference_uses_schema_and_entity_linking():
    dm = make_manager()
    select_task(dm, "tree_valve_operation")
    dm._last_discussed_oilfield = "流花11-1油田"
    version = dm.slot_store.version

    dm.process("就去这个油田")

    assert dm.task_state["oilfield_name"] == "流花11-1油田"
    assert dm.task_state["oilfield_coordinates"]
    assert dm.task_state == dm.slot_store.get_task_state()
    assert dm.slot_store.version > version


def test_oilfield_reference_is_rejected_for_coordinate_only_task():
    dm = make_manager()
    select_task(dm, "pipeline_inspection")
    dm._last_discussed_oilfield = "流花11-1油田"

    reply = dm.process("就去这个油田")

    assert dm.task_state.get("oilfield_name") is None
    assert dm.task_state.get("start_point") is None
    assert "未包含油田槽位" in reply


def test_taskless_reference_is_pending_then_validated_for_selected_task():
    dm = make_manager()
    dm._last_discussed_oilfield = "流花11-1油田"

    reply = dm.process("就去这个油田")

    assert dm.slot_store.get_task_state().get("oilfield_name") is None
    assert dm._pending_referential_candidates
    assert "暂存" in reply
    dm._last_discussed_task_type = "tree_valve_operation"
    dm.process("那开始这个任务")

    assert dm.task_state["task_type_key"] == "tree_valve_operation"
    assert dm.task_state["oilfield_name"] == "流花11-1油田"
    assert not dm._pending_referential_candidates


def test_robot_reference_uses_canonical_cascade_fields():
    dm = make_manager()
    select_task(dm, "pipeline_inspection")
    dm._last_discussed_robot = "观察级深海机器人 75HP"

    dm.process("就用这个机器人")

    assert dm.task_state["equipment_type"] == "观察级深海机器人 75HP"
    assert dm.task_state["equipment_family"]
    assert "robot_family" not in dm.task_state
    assert "specific_robot_id" not in dm.task_state


def test_payload_reference_is_validated_in_payload_schema_field():
    dm = make_manager()
    seed_complete_valid_pipeline_task(dm, dm.kb)
    payload_field = next(field for field in dm.builder.get_schema("pipeline_inspection", dm.mode)
                         if field["key"] == "payload")
    allowed = dm.builder.resolve_allowed_values(payload_field, "pipeline_inspection", dm.task_state)
    previous_payloads = list(dm.task_state["payload"])
    selected = next((item for item in allowed if item not in previous_payloads), allowed[0])
    dm._last_discussed_payload = selected

    dm.process("就带这个工具")

    assert selected in dm.task_state["payload"]
    assert set(previous_payloads).issubset(dm.task_state["payload"])
    assert "onboard_payloads" not in dm.task_state
    assert dm.task_state == dm.slot_store.get_task_state()


def test_reset_and_restore_clear_all_reference_context():
    for reset in (True, False):
        dm = make_manager()
        dm._last_discussed_task_type = "pipeline_inspection"
        dm._last_discussed_robot = "金牛座"
        dm._last_discussed_oilfield = "流花11-1油田"
        dm._last_discussed_payload = "高清水下摄像机"
        dm._last_visible_catalog_items = [{"type": "task_type", "key": "pipeline_inspection"}]
        dm._pending_referential_candidates = [{"canonical_key": "oilfield_name", "normalized_value": "流花11-1油田"}]
        if reset:
            dm.reset()
        else:
            dm.load_snapshot(make_manager().export_snapshot())
        assert not dm._last_visible_catalog_items
        assert not dm._pending_referential_candidates
        assert all(getattr(dm, "_last_discussed_" + entity) is None
                   for entity in ("task_type", "robot", "oilfield", "payload"))
        dm.process("就去这个油田")
        assert dm.slot_store.get_task_state().get("oilfield_name") is None


def test_dispatch_and_pending_publish_snapshot_defaults_and_reset():
    dm = make_manager()
    dm.phase = "confirming"
    dm._pending_published_intent = {"intent_id": "TI2026091701", "task": {"type": "pipeline_inspection"}}
    snapshot = dm.export_snapshot()
    restored = make_manager()
    restored.load_snapshot(snapshot)
    assert restored._pending_published_intent == dm._pending_published_intent
    snapshot["_pending_published_intent"]["task"]["type"] = "changed"
    assert restored._pending_published_intent["task"]["type"] == "pipeline_inspection"
    restored.ros2_dispatch = {"state": "SENT"}
    legacy = make_manager().export_snapshot()
    legacy.pop("ros2_dispatch")
    legacy.pop("_pending_published_intent")
    restored.load_snapshot(legacy)
    assert restored.ros2_dispatch is None
    assert restored._pending_published_intent is None
    restored.ros2_dispatch = {"state": "FAILED"}
    restored._pending_published_intent = {"intent_id": "TI2026091701"}
    restored.reset()
    assert restored.ros2_dispatch is None and restored._pending_published_intent is None


def test_invalid_snapshot_dispatch_metadata_rejected_atomically():
    dm = make_manager()
    before = dm.export_snapshot()
    for field, value in (("ros2_dispatch", "SENT"), ("_pending_published_intent", {"intent_id": "x"})):
        bad = deepcopy(before)
        bad[field] = value
        with pytest.raises(ValueError):
            dm.load_snapshot(bad)
        assert dm.export_snapshot() == before


def test_legacy_published_snapshot_exposes_reconciliation_without_send_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    dm = make_manager()
    seed_complete_valid_pipeline_task(dm, dm.kb)
    dm.process("确认发布")
    if dm.phase == "blocked_soft":
        dm.process("忽略警告")
        dm.process("确认发布")
    assert dm.phase == "done"
    snapshot = dm.export_snapshot()
    snapshot.pop("ros2_dispatch")
    restored = make_manager()

    restored.load_snapshot(snapshot)

    assert restored.phase == "done"
    assert restored.ros2_dispatch["state"] == "UNKNOWN"
    assert restored.ros2_dispatch["reason"] == "legacy_history"
    assert restored.ros2_dispatch["retry_allowed"] is True
    restored.reset()
    assert restored.ros2_dispatch is None
