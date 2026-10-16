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


def test_uncommitted_support_vessel_claim_is_removed_but_question_survives():
    prose = (
        "当前已为您预设以下设备配置：\n"
        "* **作业设备**：通用工作级深海机器人 250HP\n"
        "* **母船支持**：已关联可用支持船（海洋石油681 / DSV-Oceanic）\n"
        "请提供支持船编号，以便继续规划。"
    )
    result = ground(prose, state={"equipment_type": "通用工作级深海机器人 250HP"},
                    missing=[{"key": "support_vessel", "label": "支持船编号"}])
    assert "已关联可用支持船" not in result
    assert "请提供支持船编号，以便继续规划" in result


def test_support_vessel_choices_remain_when_no_vessel_is_committed():
    prose = "可选支持船包括海洋石油681和 DSV-Oceanic，请确认支持船编号。"
    result = ground(prose, state={"equipment_type": "通用工作级深海机器人 250HP"},
                    missing=[{"key": "support_vessel", "label": "支持船编号"}])
    assert prose in result


def test_uncommitted_support_vessel_claim_is_removed_inside_followup_sentence():
    prose = "母船支持：已关联可用支持船（海洋石油681），请确认支持船编号。"
    result = ground(prose, state={"equipment_type": "通用工作级深海机器人 250HP"},
                    missing=[{"key": "support_vessel", "label": "支持船编号"}])
    assert "已关联可用支持船" not in result
    assert "请确认支持船编号" in result


@pytest.mark.parametrize("prose", [
    "请选择支持船。",
    "尚未选择支持船。",
    "支持船尚未选择。",
    "请先配置支持船，再补充任务结束时间。",
    "尚未关联支持船，请先选择支持船编号。",
    "支持船是否已选择？",
    "请不要选择支持船海洋石油681。",
    "支持船可选择海洋石油681。",
    "支持船： 请选择海洋石油681。",
    "1. **支持船**：请选择海洋石油681。",
    "支持船：尚未指定。",
    "支持船：需要您提供编号。",
    "支持船并未关联成功。",
    "支持船：暂无。",
    "支持船：未关联。",
    "请确认支持船是否已完成配置。",
    "支持船尚未完成配置。",
    "支持船：当前尚未配置。",
])
def test_support_vessel_requests_negations_and_choices_preserve_prose(prose):
    assert prose in ground(prose, missing=[{"key": "support_vessel", "label": "支持船编号"}])


@pytest.mark.parametrize("claim", [
    "母船支持：已关联可用支持船（海洋石油681）",
    "已经为您选择支持船海洋石油681",
    "已将支持船设置为海洋石油681",
    "支持船已设置为海洋石油681",
    "支持船：海洋石油681",
    "已关联支持船海洋石油681（待确认）",
    "支持船：海洋石油681（待确认）",
    "已为您配置支持船海洋石油681以满足作业需要",
    "支持船已配置为海洋石油681（可选其他支持船）",
    "支持船已完成配置",
])
@pytest.mark.parametrize("claim_first", [True, False])
def test_support_vessel_scrub_removes_only_assertion_clause(claim, claim_first):
    guidance = "请补充**支持船编号**"
    prose = f"{claim}，{guidance}。" if claim_first else f"{guidance}，{claim}。"
    result = ground(prose, missing=[{"key": "support_vessel", "label": "支持船编号"}])
    assert claim not in result
    assert guidance + "。" in result
    assert "，。" not in result


@pytest.mark.parametrize("prose", [
    "推荐支持船为海洋石油681。",
    "建议支持船：海洋石油681。",
    "支持船：建议选择海洋石油681。",
])
def test_support_vessel_recommendations_survive_without_becoming_saved_state(prose):
    assert prose in ground(prose, missing=[{"key": "support_vessel", "label": "支持船编号"}])


@pytest.mark.parametrize("notice", ["请注意", "请知悉", "请您注意"])
def test_notice_prefix_does_not_authorize_uncommitted_vessel_claim(notice):
    result = ground(f"{notice}：支持船已自动关联海洋石油681。请补充支持船编号。")
    assert "已自动关联海洋石油681" not in result
    assert "请补充支持船编号。" in result


def test_rejected_vessel_change_reports_retained_vessel_and_keeps_guidance():
    result = ground(
        "支持船已设置为DSV-Oceanic。请核对支持船编号。",
        state={"support_vessel": "海洋石油681"},
    )
    assert "DSV-Oceanic" not in result
    assert "当前已保存：支持船编号：海洋石油681" in result
    assert "请核对支持船编号。" in result


@pytest.mark.parametrize("claim", [
    "当前携带工具：高清水下摄像机。",
    "当前载荷配置：\n- 高清水下摄像机\n- 前视声呐",
])
@pytest.mark.parametrize("state", [{}, {"payload": []}])
def test_uncommitted_payload_assertion_is_removed_even_without_saved_payload(claim, state):
    result = ground(claim + "\n请提供携带工具。", state=state)
    assert "高清水下摄像机" not in result
    assert "前视声呐" not in result
    assert "请提供携带工具。" in result


@pytest.mark.parametrize("prose", [
    "携带工具：尚未选择。",
    "当前载荷：暂无。",
    "推荐携带工具：高清水下摄像机。",
    "载荷：建议选择高清水下摄像机。",
])
def test_empty_payload_keeps_pending_status_and_recommendations(prose):
    assert prose in ground(prose)


def test_model_receipt_cannot_replace_committed_values_or_hide_followup():
    result = ground("✅ 已记录：水深（米）：5000 米。请补充支持船编号。")
    assert "5000" not in result
    assert "✅ 已记录：水深（米）：300 米。" in result
    assert "请补充支持船编号。" in result


@pytest.mark.parametrize("prose", [
    "已记录水深5000米。",
    "已设置水深为5000米。",
    "已将水深设置为5000米。",
    "水深已更新为5000米。",
    "当前水深为5000米。",
    "当前**水深**：**5000**米。",
    "请补充支持船编号，当前水深为5000米。",
])
@pytest.mark.parametrize("updated", [False, True], ids=["retained-depth", "updated-depth"])
def test_explicit_depth_assertion_uses_committed_or_retained_value(prose, updated):
    updates = {"water_depth": 300} if updated else {"support_vessel": "海洋石油681"}
    state = {"water_depth": 300}
    result = ground(prose + "\n请核对任务信息。", updates=updates, state=state)
    assert "5000" not in result
    assert "300" in result
    assert "请核对任务信息。" in result
    if "请补充支持船编号" in prose:
        assert "请补充支持船编号" in result
    assert state == {"water_depth": 300}
    assert updates == ({"water_depth": 300} if updated else {"support_vessel": "海洋石油681"})


@pytest.mark.parametrize("prose", [
    "当前水深是5000米吗？",
    "请确认是否已记录水深5000米。",
    "例如，当前水深为5000米时需要另选设备。",
    "假如已设置水深为5000米，应重新校验。",
    "建议将水深设置为5000米。",
    "尚未记录水深5000米。",
    "当前水深并非5000米。",
    "已记录水深300米；该机器人最大作业水深为5000米。",
    "机器人当前水深为5000米，任务水深另行填写。",
    "已记录水深300.0米。",
    "当前水深为0.3公里。",
])
def test_depth_grounding_preserves_questions_examples_and_other_facts(prose):
    assert prose in ground(prose, state={"water_depth": 300})


@pytest.mark.parametrize("prose", [
    "水深调整为330米（原320米）。",
    "将水深从320米改为330米。",
    "已将水深设置为330米（原320米）。",
])
def test_depth_grounding_preserves_correct_before_after_explanation(prose):
    assert prose in ground(prose, updates={"water_depth": 330}, state={"water_depth": 330})


def test_depth_grounding_does_not_guess_without_a_committed_value():
    prose = "当前水深为5000米。"
    assert prose in ground(prose, updates={"support_vessel": "海洋石油681"})


@pytest.mark.parametrize("receipt", [
    "**✅ 已记录**：水深（米）：5000 米。",
    "**✅ 已记录：**水深（米）：5000 米。",
    "### ✅ 已记录：水深（米）：5000 米。",
    "- **✅ 已记录**：水深（米）：5000 米。",
    "✅ 已记录：\n- 水深（米）：5000 米。",
    "**✅ 已记录：**\n\n- **水深（米）**：5000 米。",
    "### 当前已保存：\n1. **水深（米）**：5000 米。",
])
def test_markdown_model_receipt_cannot_retain_wrong_values(receipt):
    result = ground(receipt + "\n- 请补充支持船编号。\n载荷选择请参考右侧卡片。")
    assert "5000" not in result
    assert "✅ 已记录：水深（米）：300 米。" in result
    assert "- 请补充支持船编号。" in result
    assert "载荷选择请参考右侧卡片。" in result


def test_bold_receipt_keeps_followup_in_same_line():
    result = ground("**✅ 已记录：水深（米）：5000 米。**请补充支持船编号。")
    assert "5000" not in result
    assert "请补充支持船编号。" in result


def test_receipt_block_preserves_field_specific_request():
    guidance = "- **结束时间**：请提供希望的完成时刻，避免与后续作业冲突。"
    result = ground("**仍需补充：**\n" + guidance,
                    missing=[{"key": "end_time", "label": "结束时间"}])
    assert guidance in result


def test_model_missing_receipt_cannot_hide_other_required_fields():
    result = ground(
        "仍需补充：支持船编号。",
        missing=[{"key": "end_time", "label": "结束时间"},
                 {"key": "payload", "label": "携带工具"}],
    )
    assert "仍需补充：结束时间、携带工具。" in result


def test_identical_model_receipt_is_only_displayed_once():
    receipt = "✅ 已记录：水深（米）：300 米。"
    result = ground(receipt + "\n请补充支持船编号。")
    assert result.count(receipt) == 1
    assert "请补充支持船编号。" in result


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


@pytest.mark.parametrize('claim', ['您的任务参数已收集完整。', '所有信息已收集完成。'])
def test_missing_fields_do_not_claim_complete_collection(claim):
    result = ground(claim + '\n请补充任务开始时间。', missing=[{'key': 'start_time', 'label': '任务开始时间'}])
    assert claim not in result
    assert '请补充任务开始时间。' in result
    assert '仍需补充：任务开始时间' in result


def test_uncommitted_oilfield_state_assertion_is_removed_but_rejection_guidance_survives():
    reply = (
        "关于‘流花11-1油田’，当前系统暂不支持通过油田名称自动映射坐标。\n"
        "当前任务状态如下：\n"
        "- 任务类型：管缆埋设\n"
        "- 作业区域：流花11-1油田（尚未转换为精确坐标）\n"
        "请补充起始点和结束点坐标。"
    )
    result = WriteReplyGrounder.ground_write_reply(
        reply,
        accepted_updates={},
        unresolved_inputs=["当前任务类型未包含油田槽位，无法通过油田名称映射。"],
        task_state={"task_type": "管缆埋设"},
        missing_fields=[{"key": "start_point", "label": "起始点经纬度"}],
    )
    assert "暂不支持通过油田名称自动映射坐标" in result
    assert "作业区域：流花11-1油田" not in result
    assert "当前已保存：任务类型：管缆埋设" in result


def test_uncommitted_inline_oilfield_state_assertion_is_removed():
    result = WriteReplyGrounder.ground_write_reply(
        "当前作业区域为流花11-1油田。请补充精确坐标。",
        accepted_updates={},
        unresolved_inputs=[],
        task_state={"task_type": "管缆埋设"},
        missing_fields=[{"key": "start_point", "label": "起始点经纬度"}],
    )
    assert "当前作业区域为流花11-1油田" not in result
    assert "\n，请" not in result
    assert "请补充精确坐标" in result


def test_uncommitted_inline_oilfield_scrub_preserves_followup_guidance():
    result = WriteReplyGrounder.ground_write_reply(
        "当前作业区域为流花11-1油田，请补充精确坐标。",
        accepted_updates={},
        unresolved_inputs=[],
        task_state={"task_type": "管缆埋设"},
        missing_fields=[{"key": "start_point", "label": "起始点经纬度"}],
    )
    assert "当前作业区域为流花11-1油田" not in result
    assert "请补充精确坐标" in result


def test_write_response_discards_misplaced_off_topic_reject_template():
    """在任务编辑与写入响应中，模型若误吐出离题拒识模板，必须被彻底剥离清洗。"""
    from src.extraction.prompts import OFF_TOPIC_REJECT_TEMPLATE
    misplaced_reply = (
        OFF_TOPIC_REJECT_TEMPLATE + "\n\n"
        "任务参数已更新：\n- **管缆类型**：电力电缆\n"
        "接下来，还需要您确认以下 2 个关键参数：\n"
        "1. **作业区域**：请提供油田坐标。\n"
        "2. **执行机器人**：开拓者号未在设备库登记，请选择合规型号。"
    )
    result = ground(
        misplaced_reply,
        updates={"cable_type": "电力电缆", "water_depth": 120.0},
        missing=[{"key": "oilfield_coordinates", "label": "油田坐标"}],
    )
    assert "抱歉，本系统专注于" not in result
    assert "请描述您的水下作业需求" not in result
    assert "电力电缆" in result
    assert "油田坐标" in result
    assert "开拓者号未在设备库登记" in result
    assert "✅ 已记录" in result
