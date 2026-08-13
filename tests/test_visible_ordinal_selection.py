"""跨轮编号选择必须来自用户实际可见的上一轮候选列表。"""

from __future__ import annotations

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.visible_selection_provenance import parse_ordinal_reference
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)


def _make_support_vessel_selection_dialogue(
    previous_assistant: str,
    *,
    raw_value: str = "第三个",
    normalized_value: str = "海洋石油286",
) -> DialogueManager:
    llm = ScriptedLLM(
        plans=[make_plan("WRITE", subject_type="task", relation="filled_fields")],
        extractions=[
            extraction_result(
                slot_candidate(
                    "support_vessel",
                    normalized_value,
                    raw_value=raw_value,
                )
            )
        ],
        replies=["已处理本轮选择。"],
    )
    dm = DialogueManager(llm, KnowledgeBase())
    schema = dm.builder.get_schema("pipeline_inspection", "normal")
    dm.slot_store.init_task_slots(schema)
    slots, unresolved, version = dm.slot_store.snapshot()
    slots["task_type"].value = "管缆巡检"
    slots["task_type"].status = "valid"
    slots["task_type_key"].value = "pipeline_inspection"
    slots["task_type_key"].status = "valid"
    dm.slot_store.commit_transaction(slots, unresolved, expected_version=version)
    dm.task_state = dm.slot_store.get_task_state()
    _, dm._last_missing = dm.builder.build(
        dm.task_state,
        "pipeline_inspection",
        "normal",
    )
    dm.conversation_history = [
        {"role": "user", "content": "当前任务有哪些支持船候选？"},
        {"role": "assistant", "content": previous_assistant},
    ]
    return dm


def test_hidden_allowed_value_order_cannot_authorize_third_option_write() -> None:
    dm = _make_support_vessel_selection_dialogue(
        "当前知识库没有可向您展示的支持船候选列表。"
    )

    reply = dm.process("那就选第三个。")
    slot = dm.slot_store.slots["support_vessel"]

    assert slot.value is None
    assert slot.status == "missing"
    assert "未写入" in reply
    assert "可见候选" in reply
    assert "无法验证所接受的推荐" not in reply


def test_visible_numbered_third_option_commits_same_canonical_value() -> None:
    dm = _make_support_vessel_selection_dialogue(
        "支持船候选如下：\n"
        "1. 海洋石油681\n"
        "2. DSV-Oceanic\n"
        "3. 海洋石油286\n"
        "4. 海洋石油708"
    )

    dm.process("那就选第三个。")
    slot = dm.slot_store.slots["support_vessel"]

    assert slot.value == "海洋石油286"
    assert slot.status == "valid"
    assert slot.raw_value == "第三个"
    assert slot.source == "assistant_option_selection"


def test_stale_numbered_list_cannot_authorize_ordinal_after_new_assistant_turn() -> None:
    dm = _make_support_vessel_selection_dialogue(
        "1. 海洋石油681\n2. DSV-Oceanic\n3. 海洋石油286\n4. 海洋石油708"
    )
    dm.conversation_history.extend(
        [
            {"role": "user", "content": "第三条船有什么特点？"},
            {"role": "assistant", "content": "我这里只做一般说明，没有重新列出候选。"},
        ]
    )

    dm.process("那就选第三个。")
    slot = dm.slot_store.slots["support_vessel"]

    assert slot.value is None
    assert slot.status == "missing"


def test_direct_named_enum_value_does_not_require_numbered_list_provenance() -> None:
    dm = _make_support_vessel_selection_dialogue(
        "当前没有列出候选。",
        raw_value="海洋石油286",
    )

    dm.process("支持船使用海洋石油286。")
    slot = dm.slot_store.slots["support_vessel"]

    assert slot.value == "海洋石油286"
    assert slot.status == "valid"
    assert slot.source == "user_input"


def test_nonordinal_final_confirmation_word_does_not_trigger_list_gate() -> None:
    assert parse_ordinal_reference("最后确认支持船使用海洋石油286") is None
    assert parse_ordinal_reference("执行流花11-1油田管缆巡检") is None
    assert parse_ordinal_reference("水深300米") is None
    assert parse_ordinal_reference("使用LROV-150-001") is None
    assert parse_ordinal_reference("最后一个") is not None
    assert parse_ordinal_reference("倒数第二艘") is not None
    assert parse_ordinal_reference("选3") is not None
