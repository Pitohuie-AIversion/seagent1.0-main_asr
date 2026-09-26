"""Preserve useful guidance while replacing unsupported slot assertions with receipts."""

import pytest

from src.handlers.write_reply_grounder import WriteReplyGrounder


def ground(reply, *, updates=None, state=None, missing=None):
    return WriteReplyGrounder.ground_write_reply(
        reply,
        accepted_updates=updates if updates is not None else {"water_depth": 300},
        unresolved_inputs=[],
        task_state=state or {},
        missing_fields=missing,
    )


def test_real_task_followup_keeps_first_numbered_question_and_committed_start():
    reply = (
        "已记录以下参数：\n- **开始时间**：2026-09-27 08:00\n"
        "接下来，还需要您确认以下 3 个关键参数：\n"
        "1. **任务结束时间**：请提供结束时间，例如明天12:00。\n"
        "2. **管缆类型**：请确认是海底油气管道、电力电缆还是光纤通信缆。\n"
        "3. **起始点经纬度**：请提供任务起点坐标。"
    )
    result = ground(reply, updates={"start_time": "2026-09-27T08:00:00", "water_depth": 300},
                    missing=[{"key": "end_time", "label": "任务结束时间"}])
    assert reply in result
    assert "✅ 已记录" in result


@pytest.mark.parametrize("prose", [
    "结束时间必须晚于开始时间。",
    "时间使用24小时制，00:00表示当天零点，23:59表示当天最后一分钟。",
    "例如从08:00开始、持续2小时，结束时间应为10:00；这只是示例，尚未写入。",
    "1. **结束时间**：需要您补充任务完成的具体时刻。",
    "您希望几点结束？是否选择12:00作为任务结束时间？",
])
def test_time_questions_examples_and_rules_survive_without_end_time_commit(prose):
    assert prose in ground(prose)


@pytest.mark.parametrize("claim", [
    "结束时间已写入13:00。",
    "已将结束时间调整为13:00。",
    "当前结束时间仍为13:00。",
    "- **结束时间**：2026-09-27 13:00。",
    "作业时间：09:00至13:00。",
])
def test_uncommitted_end_time_claims_are_still_removed(claim):
    result = ground("感谢补充水深信息。\n" + claim + "\n请确认支持船编号。",
                    state={"end_time": "2026-09-27T12:00:00"})
    assert "13:00" not in result
    assert "12:00" in result
    assert "感谢补充水深信息" in result
    assert "请确认支持船编号" in result


def test_payload_guidance_does_not_discard_the_entire_conversation():
    prose = "水深信息已收到。载荷选择请参考右侧卡片。\n请提供支持船编号，以便继续规划。"
    result = ground(prose, state={"payload": ["双目水下成像系统"]})
    assert prose in result
    assert "当前已保存：携带工具：双目水下成像系统" in result


def test_stale_payload_claim_is_removed_without_losing_independent_explanation():
    result = ground(
        "水深已修改为300米。\n当前载荷仍为浑水水下成像系统。\n"
        "携带载荷：浑水水下成像系统\n请提供支持船编号，以便继续规划。",
        state={"payload": ["双目水下成像系统"]},
    )
    assert "浑水水下成像系统" not in result
    assert "双目水下成像系统" in result
    assert "水深已修改为300米" in result
    assert "请提供支持船编号，以便继续规划" in result


def test_payload_state_block_does_not_leave_stale_list_items():
    result = ground(
        "水深已修改为300米。\n\n当前载荷配置：\n- 浑水水下成像系统\n- 机械扫描声呐\n\n"
        "接下来请确认支持船编号。",
        state={"payload": ["双目水下成像系统"]},
    )
    assert "浑水水下成像系统" not in result
    assert "机械扫描声呐" not in result
    assert "接下来请确认支持船编号" in result


def test_non_payload_explanation_remains_when_false_payload_mutation_is_scrubbed():
    result = ground("已将载荷更新为浑水水下成像系统。\n载荷选择请参考右侧卡片。\n水深信息已收到。",
                    state={"payload": ["双目水下成像系统"]})
    assert "浑水水下成像系统" not in result
    assert "载荷选择请参考右侧卡片" in result
    assert "水深信息已收到" in result


@pytest.mark.parametrize("severity", ["soft", "hard"])
@pytest.mark.parametrize("instruction", [
    "1. **忽略上述警告**，直接确认发布此任务？",
    "1. 请直接回复“确认发布”或提出您的修改意见。",
    "1. 任务已发布。",
    "1. 可以立即发布任务。",
    "1. 请直接发布。",
])
def test_blocked_task_corrects_publish_instructions_without_breaking_list(severity, instruction):
    result = WriteReplyGrounder.ground_write_reply(
        instruction + "\n2. 修改相关参数后再确认。",
        accepted_updates={"water_depth": 300}, unresolved_inputs=[], missing_fields=[],
        constraint_context={"type": severity, "violations": [{"severity": severity, "message": "需处理的约束"}]},
    )
    assert "1. " in result and "2. 修改相关参数后再确认" in result
    assert "直接确认发布" not in result and "请直接回复“确认发布”" not in result
    assert "任务已发布" not in result and "立即发布" not in result
    if severity == "hard":
        assert "请先修正上述硬约束问题" in result
        assert "忽略软警告" not in result
    else:
        assert "请先明确回复“忽略软警告”" in result
    assert "任务尚未发布" in result


def test_blocked_task_keeps_existing_no_publish_guidance():
    guidance = "请勿直接确认发布任务。请先提供任务结束时间。"
    result = WriteReplyGrounder.ground_write_reply(
        guidance, accepted_updates={"water_depth": 300}, unresolved_inputs=[],
        constraint_context={"type": "hard"},
    )
    assert guidance in result


@pytest.mark.parametrize("prose", [
    "已记录水深300米，载荷选择请参考右侧卡片。",
    "已记录开始时间08:00，请补充结束时间。",
    "作业时间：明天08:00开始。",
])
def test_committed_update_does_not_turn_other_guidance_into_false_claim(prose):
    result = ground(prose, updates={"water_depth": 300, "start_time": "2026-09-27T08:00:00"},
                    state={"payload": ["双目水下成像系统"]})
    assert prose in result


@pytest.mark.parametrize("prose, stale", [
    ("请补充支持船编号，当前载荷仍为浑水水下成像系统。", "浑水水下成像系统"),
    ("请补充水深，结束时间为13:00。", "13:00"),
    ("请确认结束时间，当前结束时间为13:00。", "13:00"),
])
def test_unrelated_question_cannot_hide_unsupported_slot_assertion(prose, stale):
    result = ground(prose, state={"payload": ["双目水下成像系统"], "end_time": "2026-09-27T12:00:00"})
    assert stale not in result
    assert "双目水下成像系统" in result and "12:00" in result


@pytest.mark.parametrize("as_unresolved", [False, True])
def test_blocked_warning_cannot_claim_acceptance_or_entering_confirmation(as_unresolved):
    false_state = "用户已确认接受软性约束警告（未来任务环境与遥测延后校验），无需进一步字段更新，任务流程进入发布确认阶段。"
    result = WriteReplyGrounder.ground_write_reply(
        "" if as_unresolved else false_state,
        accepted_updates={},
        unresolved_inputs=[false_state, "设备型号需要确认"] if as_unresolved else [],
        task_state={"water_depth": 300}, missing_fields=[],
        constraint_context={"type": "soft", "violations": [{"severity": "soft", "code": "C032"}]},
    )
    assert "已确认接受" not in result
    assert "进入发布确认阶段" not in result
    assert "忽略软警告" in result and "任务尚未发布" in result
    if as_unresolved:
        assert "设备型号需要确认" in result


REAL_BLOCKED_ACKNOWLEDGEMENTS = [
    '收到，已记录您确认**忽略“未来任务环境与遥测延后校验提示”软警告**的决定。',
    '- **警告处理**：已接受忽略，系统将不再阻塞此流程。',
    '- **任务阶段**：所有必填字段已收集完毕，且软警告已解除。任务现已进入**最终发布确认阶段**。',
    '是否正式**发布**该管缆巡检任务？',
]


@pytest.mark.parametrize('prose', REAL_BLOCKED_ACKNOWLEDGEMENTS)
@pytest.mark.parametrize('as_unresolved', [False, True])
def test_real_named_warning_claims_cannot_advance_blocked_task(prose, as_unresolved):
    result = WriteReplyGrounder.ground_write_reply(
        '' if as_unresolved else prose,
        accepted_updates={}, unresolved_inputs=[prose] if as_unresolved else [],
        task_state={'water_depth': 250}, missing_fields=[],
        constraint_context={'type': 'soft', 'violations': [{'severity': 'soft', 'code': 'C032'}]},
    )
    for false_claim in ('已记录您确认', '已接受忽略', '不再阻塞', '软警告已解除', '最终发布确认阶段', '是否正式'):
        assert false_claim not in result
    assert '任务尚未发布' in result and '忽略软警告' in result


@pytest.mark.parametrize('prose', REAL_BLOCKED_ACKNOWLEDGEMENTS[:3])
def test_accepted_warning_claims_survive_when_constraints_are_resolved(prose):
    result = WriteReplyGrounder.ground_write_reply(
        prose, accepted_updates={}, unresolved_inputs=[], task_state={'water_depth': 250},
        missing_fields=[], constraint_context={'type': 'none', 'violations': []},
    )
    assert prose in result
    assert '仍有软警告待处理' not in result


def test_blocked_warning_retains_negative_and_future_guidance():
    prose = '软警告尚未解除，任务尚未进入最终发布确认阶段。待您明确确认忽略软警告后，系统会重新校验。'
    result = WriteReplyGrounder.ground_write_reply(
        prose, accepted_updates={}, unresolved_inputs=[], task_state={'water_depth': 250},
        constraint_context={'type': 'soft'},
    )
    assert prose in result


WRITE_WELCOME_TEMPLATE = (
    '您好，SEAgent 水下多智能体任务决策系统已就绪。\n\n'
    '系统提供以下两类核心交互能力，并将根据您的输入自动识别需求并进入相应处理流程：\n\n'
    '1. **知识与状态查询**\n用于查询机器人能力与设备参数。\n\n'
    '2. **任务创建与准入**\n根据作业需求收集任务目标、时间、位置等关键信息。\n\n'
    '请直接描述您的作业需求，或提出需要查询的问题。'
)


@pytest.mark.parametrize('suffix', ['', '\n请补充任务结束时间，以便继续规划。'])
def test_write_response_discards_full_generic_welcome_but_retains_followup(suffix):
    result = ground(WRITE_WELCOME_TEMPLATE + suffix, state={'start_time': '2026-09-27T08:00:00'})
    assert '核心交互能力' not in result and '知识与状态查询' not in result
    assert '系统已就绪' not in result
    assert '✅ 已记录：水深（米）：300 米' in result
    assert '08:00' in result
    if suffix:
        assert suffix.strip() in result


def test_write_response_keeps_short_greeting_and_task_specific_list():
    prose = '您好，系统已就绪。\n1. 请提供任务结束时间。\n2. 请提供支持船编号。'
    assert prose in ground(prose)
