"""Natural wording from the real burial journey must update saved time slots."""

import pytest

from tests.test_time_relation_protocol import candidate, extract, relation, times


MESSAGE = "把开工时间推迟半小时，作业时长仍保持两小时，其他参数不变。"
STATE = {"start_time": "2026-09-27T09:00:00", "end_time": "2026-09-27T11:00:00"}


@pytest.mark.parametrize("model_relation", [
    None,
    relation("SET", "duration", "作业时长仍保持两小时", 7200),
    relation("ADD", "end_time", "推迟半小时", 1800),
    relation("ADD", "start_time", "推迟半小时", 1800, keep=True),
])
def test_opening_time_alias_overrides_wrong_end_candidate_and_relation(model_relation):
    result = extract(MESSAGE,
        [candidate("end_time", "推迟半小时", STATE["end_time"])],
        model_relation, STATE,
    )
    assert times(result) == {"start_time": "2026-09-27T09:30:00", "end_time": "2026-09-27T11:30:00"}


def test_named_duration_after_start_shift_is_used_instead_of_old_or_model_duration():
    result = extract(MESSAGE, rel=relation("ADD", "end_time", "推迟半小时", 1800),
                     state={**STATE, "end_time": "2026-09-27T12:00:00"})
    assert times(result) == {"start_time": "2026-09-27T09:30:00", "end_time": "2026-09-27T11:30:00"}


def test_opening_time_keep_does_not_mean_keep_duration():
    result = extract("开工时间保持不变，作业时长增加半小时。", rel=relation("ADD", "duration"), state=STATE)
    assert times(result).get("start_time", STATE["start_time"]) == STATE["start_time"]
    assert times(result)["end_time"] == "2026-09-27T11:30:00"


@pytest.mark.parametrize("alias", ["完工时间", "收工时间"])
def test_finishing_time_alias_changes_end_only(alias):
    result = extract(f"{alias}提前半小时，开工时间保持不变。",
                     rel=relation("ADD", "start_time"), state=STATE)
    assert times(result).get("start_time", STATE["start_time"]) == STATE["start_time"]
    assert times(result)["end_time"] == "2026-09-27T10:30:00"


@pytest.mark.parametrize("patch_v2,norm_v2", [(False, False), (True, False), (True, True)])
def test_real_burial_shift_commits_both_slots_in_supported_pipelines(monkeypatch, tmp_path, patch_v2, norm_v2):
    from src.dialogue_manager import DialogueManager
    from src.knowledge_retriever import KnowledgeBase
    from tests.interaction_plan_support import ScriptedLLM, make_plan
    from tests.test_time_relation_protocol import extraction

    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch_v2)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm_v2)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="收到。")
    manager = DialogueManager(llm=llm, kb=KnowledgeBase())
    manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_burial", "normal"))
    slots = manager.slot_store.clone_slots()
    for key, value in {**STATE, "task_type_key": "pipeline_burial", "task_type": "管缆埋设", "water_depth": 100}.items():
        slots[key].value, slots[key].status = value, "valid"
    manager.slot_store.commit_transaction(slots, [], request_id="seed-time-shift")
    manager._rebuild_cache()
    manager.phase = "collecting"
    llm.queue_extraction(extraction(
        [candidate("end_time", "推迟半小时", STATE["end_time"])],
        relation("SET", "duration", "作业时长仍保持两小时", 7200),
    ))
    reply = manager.process(MESSAGE)
    assert manager.task_state["start_time"] == "2026-09-27T09:30:00"
    assert manager.task_state["end_time"] == "2026-09-27T11:30:00"
    assert manager.task_state["water_depth"] == 100
    assert "09:30" in reply and "11:30" in reply


@pytest.mark.parametrize('message,start,end', [
    ('创建管缆埋设任务，埋设电力电缆，明天上午9点到11点，水深100米。', '09:00:00', '11:00:00'),
    ('明天下午2点到5点，水深100米。', '14:00:00', '17:00:00'),
    ('明天上午9点到12点，水深100米。', '09:00:00', '12:00:00'),
])
def test_explicit_time_range_survives_model_omitting_both_times(message, start, end, monkeypatch):
    from datetime import datetime
    monkeypatch.setattr('src.temporal.simulated_time.get_current_datetime', lambda: datetime(2026, 9, 27, 1, 0))
    from src.temporal.temporal_parser import TemporalParser
    candidates, unresolved = TemporalParser.materialize_time_relation([], None, {}, {'start_time', 'end_time'}, user_message=message)
    actual = times({'slot_candidates': candidates, 'unresolved': unresolved})
    assert actual['start_time'] == '2026-09-28T' + start
    assert actual['end_time'] == '2026-09-28T' + end


@pytest.mark.parametrize('message', [
    '如果天气允许，明天上午9点到11点。',
    '不要安排明天上午9点到11点。',
    '明天上午9点到11点可以吗？',
    '明天上午9点到11点，后天上午9点到11点。',
])
def test_range_recovery_does_not_guess_conditional_or_multiple_times(message):
    from src.temporal.temporal_parser import TemporalParser
    candidates, unresolved = TemporalParser.materialize_time_relation([], None, {}, {'start_time', 'end_time'}, user_message=message)
    assert not candidates and not unresolved
