"""Follow-up regressions for saved times, transitions, denials, and cleanup."""

import pytest

from src.handlers.write_reply_grounder import WriteReplyGrounder


def ground(prose, *, state=None, updates=None):
    return WriteReplyGrounder.ground_write_reply(
        prose, accepted_updates=updates or {"water_depth": 300},
        task_state=state or {}, unresolved_inputs=[],
    )


@pytest.mark.parametrize("updated", [False, True])
@pytest.mark.parametrize("key,label,saved,wrong", [
    ("start_time", "开始时间", "2026-10-20T09:00:00", "10:00"),
    ("end_time", "结束时间", "2026-10-20T13:00:00", "14:00"),
])
@pytest.mark.parametrize("template", ["{label}已记录为{wrong}", "当前{label}为{wrong}"])
def test_saved_time_values_are_checked_even_when_the_slot_exists(updated, key, label, saved, wrong, template):
    prose = template.format(label=label, wrong=wrong) + "，请提供起点。"
    result = ground(prose, updates={key: saved} if updated else None,
                    state={} if updated else {key: saved})
    assert wrong not in result
    assert saved in result
    assert "请提供起点。" in result


@pytest.mark.parametrize("saved,prose", [
    ("2026-10-20T09:00:00", "当前开始时间为09:00。"),
    ("2026-10-20T09:00:30", "当前开始时间为09:00。"),
    ("2026-10-20T09:00:00", "开始时间已记录为九点。"),
    ("2026-10-20T21:30:00", "开始时间已记录为晚上九点半。"),
    ("2026-10-20T09:00:00", "开始时间已记录为2026年10月20日09:00。"),
    ("2026-01-02T09:00:00", "开始时间已记录为2026-1-2 9:00。"),
    ("2026-10-20T09:00:00+08:00", "开始时间已记录为2026-10-20T01:00:00Z。"),
    ("2026-10-20T01:00:00+08:00", "开始时间已记录为2026-10-19T17:00:00Z。"),
    ("2026-10-20T09:00:00+08:00", "开始时间已记录为2026-10-20 01:00 UTC。"),
    ("2026-10-20T09:00:00+08:00", "当前开始时间为01:00Z。"),
    ("2026-10-20T01:00:00+00:00", "当前开始时间为09:00+08:00。"),
    ("2026-10-20T01:00:00+00:00", "当前开始时间为北京时间09:00。"),
    ("2026-10-20T09:00:00", "开始时间已记录为2026-10-20T01:00:00Z。"),
])
def test_equivalent_times_and_unspecified_source_timezone_are_not_reinterpreted(saved, prose):
    assert prose in ground(prose, state={"start_time": saved})


@pytest.mark.parametrize("saved,claimed,equivalent", [
    ("2026-10-20T13:00:00", "中午一点", True),
    ("2026-10-20T13:00:00", "中午1:00", True),
    ("2026-10-20T11:00:00", "中午十一点", True),
    ("2026-10-20T12:00:00", "中午十二点", True),
    ("2026-10-20T00:00:00", "午夜十二点", True),
    ("2026-10-20T01:00:00", "午夜一点", True),
    ("2026-10-20T01:00:00", "中午一点", False),
    ("2026-10-20T23:00:00", "中午十一点", False),
    ("2026-10-20T00:00:00", "中午十二点", False),
    ("2026-10-20T12:00:00", "午夜十二点", False),
    ("2026-10-20T13:00:00", "午夜一点", False),
])
def test_noon_and_midnight_qualifiers_keep_equivalent_clocks_and_reject_wrong_ones(saved, claimed, equivalent):
    prose = f"开始时间已记录为{claimed}。"
    assert (prose in ground(prose, state={"start_time": saved})) is equivalent


@pytest.mark.parametrize("prose", [
    "开始时间已记录为2026-10-21T09:00:00。",
    "当前开始时间为2026年10月21日09:00。",
    "当前开始时间为2026-10-20T02:00:00Z。",
    "开始时间已记录为十点。",
])
def test_wrong_date_or_timezone_instant_cannot_survive_on_a_matching_clock(prose):
    assert prose not in ground(prose, state={"start_time": "2026-10-20T09:00:00+08:00"})


@pytest.mark.parametrize("prose", [
    "开始时间是否已记录为10:00？",
    "请将开始时间调整为10:00。",
    "例如，开始时间为10:00时需要核对窗口。",
    "开始时间必须早于结束时间。",
    "当前开始时间必须早于12:00。",
    "结束时间应当晚于开始时间。",
    "如果开始时间为10:00，结束时间需要顺延。",
])
def test_time_questions_examples_and_relational_explanations_survive(prose):
    assert prose in ground(prose, state={"start_time": "2026-10-20T09:00:00", "end_time": "2026-10-20T13:00:00"})


def test_relative_time_does_not_sample_a_new_clock_or_infer_a_reference_from_saved_start(monkeypatch):
    def unexpected_clock():
        raise AssertionError("reply grounding must not sample now")
    monkeypatch.setattr("src.temporal.simulated_time.get_current_datetime", unexpected_clock)
    prose = "目前任务计划于一小时后开始。当前开始时间为09:00。"
    assert prose in ground(prose, state={"start_time": "2026-10-20T09:00:00"})


@pytest.mark.parametrize("prose,keep", [
    ("作业时间：09:00至13:00，请提供起点。", True),
    ("作业时间：09:00至14:00，请提供起点。", False),
    ("时间窗口：10:00到13:00，请提供起点。", False),
    ("例如，作业时间：09:00至14:00。", True),
    ("作业时间：09:00至14:00？", True),
])
def test_time_windows_check_both_saved_endpoints_and_preserve_guidance(prose, keep):
    result = ground(prose, state={"start_time": "2026-10-20T09:00:00", "end_time": "2026-10-20T13:00:00"})
    assert (prose in result) is keep
    if "请提供起点" in prose:
        assert "请提供起点。" in result


@pytest.mark.parametrize("key,saved,prose,target", [
    ("cable_type", "电力电缆", "管缆类型已从电力电缆改为光纤通信缆。", "光纤通信缆"),
    ("equipment_type", "轻型工作级深海机器人150HP", "已将设备型号从轻型工作级深海机器人150HP调整为通用工作级深海机器人250HP。", "250HP"),
    ("start_time", "2026-10-20T09:00:00", "开始时间已从09:00改为10:00。", "10:00"),
    ("end_time", "2026-10-20T13:00:00", "已将结束时间从13:00调整到14:00。", "14:00"),
])
def test_before_after_claims_validate_the_new_value_not_the_old_one(key, saved, prose, target):
    result = ground(prose + "请提供起点。", state={key: saved})
    assert target not in result
    assert "请提供起点。" in result


@pytest.mark.parametrize("key,saved,prose", [
    ("cable_type", "电力电缆", "管缆类型已从光纤通信缆改为电力电缆。"),
    ("start_time", "2026-10-20T10:00:00", "开始时间已从09:00改为10:00。"),
    ("end_time", "2026-10-20T14:00:00", "已将结束时间从13:00调整到14:00。"),
    ("water_depth", 330, "已将水深从320米调整为330米。"),
])
def test_supported_before_after_explanations_keep_the_original_value_reference(key, saved, prose):
    assert prose in ground(prose, state={key: saved}, updates={key: saved})


@pytest.mark.parametrize("prose", [
    "当前管缆类型不是光纤通信缆，而是电力电缆。",
    "当前管缆类型并非光纤通信缆。",
    "当前设备型号并非通用工作级深海机器人250HP。",
])
def test_truthful_negated_field_explanations_survive(prose):
    assert prose in ground(prose, state={"cable_type": "电力电缆", "equipment_type": "轻型工作级深海机器人150HP"})


@pytest.mark.parametrize("prose", [
    "当前管缆类型不是电力电缆，而是光纤通信缆。",
    "当前管缆类型不是光纤通信缆，而是海底油气管道。",
    "当前管缆类型并非电力电缆。",
])
def test_negation_cannot_hide_a_contradictory_current_value(prose):
    assert prose not in ground(prose, state={"cable_type": "电力电缆"})


@pytest.mark.parametrize("prose", [
    "1. **水深**：**5000**米。\n2. 请提供起点。",
    "- **水深**：**5000**米。\n请提供起点。",
    "1. **开始时间**：**10:00**。\n2. 请提供起点。",
])
def test_removed_assertions_do_not_leave_empty_list_or_markdown_fragments(prose):
    result = ground(prose, updates={"cable_type": "电力电缆"}, state={"start_time": "2026-10-20T09:00:00"})
    assert "5000" not in result and "10:00" not in result
    assert "**" not in result and "1." not in result
    assert "请提供起点。" in result
