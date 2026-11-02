"""Reply/state consistency regressions from the live exhibition rehearsal."""

import re

import pytest

from src.handlers.write_reply_grounder import WriteReplyGrounder


TIMES = {"start_time": "2026-10-21T15:15:54", "end_time": "2026-10-21T18:15:54"}
MISSING = [
    {"key": "end_point", "label": "结束点经纬度"},
    {"key": "payload", "label": "携带工具"},
    {"key": "support_vessel", "label": "支持船编号"},
]


def ground(reply, *, state=None, missing=None, constraints=None, unresolved=None, updates=None):
    return WriteReplyGrounder.ground_write_reply(
        reply, accepted_updates=updates or {"water_depth": 300}, task_state=state or TIMES,
        missing_fields=missing, unresolved_inputs=unresolved or [],
        constraint_context=constraints,
    )


@pytest.mark.parametrize("label", ["时间", "作业时间", "任务时间", "执行时间", "时间窗口"])
@pytest.mark.parametrize("markdown", [False, True])
def test_generic_and_markdown_time_window_cannot_contradict_saved_endpoints(label, markdown):
    label = f"**{label}**" if markdown else label
    result = ground(f"- {label}：2026-10-21T16:15:54 至 2026-10-21T19:15:54\n请提供任务终点坐标。")
    assert "16:15:54" not in result and "19:15:54" not in result
    assert TIMES["start_time"] in result and TIMES["end_time"] in result
    assert "请提供任务终点坐标。" in result
    assert not re.search(r"(?m)^\s*[-*]\s*$", result)


@pytest.mark.parametrize("reply", [
    "- **时间**：2026-10-21T15:15:54 至 2026-10-21T18:15:54",
    "例如，**时间**：2026-10-21T16:15:54 至 2026-10-21T19:15:54。",
    "**作业时间**：16:15:54 至 19:15:54？",
    "设备维护时间：16:15:54 至 19:15:54。",
])
def test_matching_windows_questions_examples_and_other_time_domains_remain(reply):
    assert reply in ground(reply)


@pytest.mark.parametrize("gloss", ["（即当前时间）", "(也就是现在)", "（也即此刻）"])
def test_absolute_timestamp_cannot_be_falsely_explained_as_now(gloss):
    result = ground(f"开始时间已记录为2026-10-21 15:15:54{gloss}。请提供持续时长。")
    assert gloss not in result
    assert "2026-10-21 15:15:54" in result
    assert "请提供持续时长。" in result


def test_relative_reference_explanation_is_preserved_without_resampling_now():
    prose = "开始时间已记录为2026-10-21 15:15:54（即当前时间1小时后）。"
    assert prose in ground(prose)


def test_live_dropped_payload_question_is_reconstructed_with_correct_numbering():
    prose = (
        "设备已选定，请继续补充任务信息。\n"
        "还需要您确认以下 3 个关键参数：\n"
        "1. **结束点经纬度**：请提供任务终点坐标（示例：北纬19.8度，东经113.5度）。\n"
        "3. **支持船编号**：请选择海洋石油681或DSV-Oceanic。\n"
        "您可以逐项提供。"
    )
    result = ground(prose, missing=MISSING)
    assert re.findall(r"(?m)^(\d+)\.", result) == ["1", "2", "3"]
    assert "2. **携带工具**：请提供携带工具。" in result
    assert "示例：北纬19.8度，东经113.5度" in result
    assert "请选择海洋石油681或DSV-Oceanic" in result
    assert "您可以逐项提供。" in result


def test_missing_question_repair_drops_saved_fields_and_repeated_questions():
    prose = (
        "还需要您确认以下 3 个关键参数：\n"
        "1. **开始时间**：请提供开始时间。\n"
        "2. **携带工具**：请提供携带工具。\n"
        "3. **携带工具**：请确认携带工具。"
    )
    result = ground(prose, missing=MISSING[:2])
    assert "请提供开始时间" not in result
    assert re.findall(r"(?m)^(\d+)\.", result) == ["1", "2"]
    assert "以下 2 个参数" in result
    assert result.count("**携带工具**") == 1


def test_intact_natural_followup_is_preserved_and_repair_requires_authoritative_missing():
    prose = "还需要您确认以下 2 个关键参数：\n1. **结束点经纬度**：请提供终点。\n2. **携带工具**：请确认检测工具。"
    assert prose in ground(prose, missing=MISSING)
    assert prose in ground(prose, missing=None)


def test_repair_uses_at_most_three_fields_and_ignores_system_fields():
    missing = [*MISSING, {"key": "intent_id", "type": "auto", "label": "内部编号"},
               {"key": "start_point", "label": "起始点经纬度"}]
    result = ground("还需要您确认以下 3 个关键参数：\n3. **携带工具**：请选择工具。", missing=missing)
    assert re.findall(r"(?m)^(\d+)\.", result) == ["1", "2", "3"]
    assert "内部编号" not in result


@pytest.mark.parametrize("severity", ["soft", "hard"])
def test_live_blocked_phase_claim_and_repeated_instructions_yield_one_correct_step(severity):
    prose = (
        '收到，已为您确认支持船为“海洋石油 681"。\n'
        "当前收集，系统进入发布确认阶段。请核对任务清单。\n"
        "**请直接回复“确认发布”。**\n"
        "请直接回复“确认发布”。"
    )
    result = ground(prose, state={**TIMES, "support_vessel": "海洋石油681"}, missing=[],
                    constraints={"type": severity},
                    updates={"support_vessel": "海洋石油681"},
                    unresolved=["系统进入发布确认阶段。", "设备型号需要确认"])
    assert "当前收集" not in result
    assert "进入发布确认阶段" not in result
    assert "请直接回复“确认发布”" not in result
    assert "任务尚未发布" in result and "请核对任务清单" in result
    assert "设备型号需要确认" in result
    assert '海洋石油 681”' in result
    if severity == "hard":
        assert result.count("请先修正上述硬约束问题") == 1
        assert "忽略软警告" not in result
    else:
        assert result.count("忽略软警告") == 1


def test_multiple_existing_warning_instructions_are_deduplicated():
    step = "如接受这些软警告，请先明确回复“忽略软警告”，待系统重新校验后再确认发布。"
    result = ground(f"*{step}**\n{step}", missing=[], constraints={"type": "soft"})
    assert result.count("忽略软警告") == 1
    assert "*" not in result
    assert not re.search(r"(?m)^\s*\*+\s*$", result)


def test_resolved_warning_phase_and_negative_guidance_are_not_rewritten():
    prose = "所有字段已收集完成，系统进入发布确认阶段。"
    assert prose in ground(prose, missing=[], constraints={"type": "none"})
    negative = "任务尚未进入最终发布确认阶段。请勿直接确认发布任务。"
    assert negative in ground(negative, missing=[], constraints={"type": "soft"})


@pytest.mark.parametrize("prose", [
    "待软警告处理完成后，系统进入发布确认阶段。",
    "系统将进入发布确认阶段。",
    "如果校验通过，系统进入发布确认阶段。",
])
def test_future_conditional_phase_explanations_are_preserved(prose):
    assert prose in ground(prose, missing=[], constraints={"type": "soft"})


@pytest.mark.parametrize("prose", ["请勿忽略软警告。", "请不要直接接受软警告。"])
def test_negative_warning_guidance_is_preserved(prose):
    assert prose in ground(prose, missing=[], constraints={"type": "soft"})


def test_complete_live_final_reply_keeps_task_summary_without_stale_health_or_false_phase():
    prose = (
        '收到，已为您确认支持船为“海洋石油 681"。\n'
        "当前收集，系统进入发布确认阶段。请核对以下任务清单与实时作业环境核验信息：\n"
        "### 📋 任务参数清单\n"
        "* **任务类型**：管缆巡检\n"
        "* **执行设备**：水下无人自主航行器 324CC (编号：AUV-324cc-001)\n"
        "* **作业时间**：2026-10-21 15:15:54 至 2026-10-21 18:15:54\n"
        "* **作业区域**：\n"
        "* 起点：北纬 19.9 度，东经 113.6 度\n"
        "* 终点：北纬 20.0 度，东经 113.7 度\n"
        "* **作业水深**：300.0 米\n"
        "* **支持船只**：海洋石油 681\n"
        "### 🤖 机器人实时遥测与作业环境核验看板\n"
        "| 项目 | 状态/数值 | 备注 |\n"
        "| :--- | :--- | :--- |\n"
        "| **选定机器人** | AUV-324cc-001 | 总体状态：可用 |\n"
        "| **海流流速** | 1.00 m/s | ⚠️ 高流速，仅支持基础航行 |\n"
        "| **水体浑浊度** | 8.00 | ⚠️ 中等，视觉感知下降 |\n"
        "| **障碍物密度** | 高 | ⚠️ 局部碰撞风险高，建议提高避障优先级 |\n"
        "| **母船支援** | 强 | 支持船可用 |\n"
        "| **推进器状态** | 正常 | |\n"
        "| **定深能力** | 正常 | |\n"
        "| **视觉系统** | 正常 | |\n"
        "| **声呐系统** | 正常 | |\n"
        "| **机械臂状态** | 正常 | |\n"
        "| **水声通信** | 正常 | |\n"
        "| **数据更新时间** | 2026-08-27T09:45:20 | ⚠️ **环境信息已过期** |\n"
        "⚠️ **系统软性警告提示**：\n"
        "当前作业环境数据最后更新于2026-08-27，已超出30分钟有效时间范围，无法反映实时海况与设备状态。\n"
        "*如接受这些软警告，请先明确回复“忽略软警告”，待系统重新校验后再确认发布。**\n"
        "如接受这些软警告，请先明确回复“忽略软警告”，待系统重新校验后再确认发布。\n"
    )
    state = {**TIMES, "task_type": "管缆巡检", "equipment_unit_id": "AUV-324cc-001",
             "equipment_type": "水下无人自主航行器 324CC", "water_depth": 300,
             "start_point": {"lat": 19.9, "lon": 113.6}, "end_point": {"lat": 20, "lon": 113.7},
             "payload": ["侧扫声呐"], "support_vessel": "海洋石油681"}
    result = ground(prose, state=state, missing=[], updates={"support_vessel": "海洋石油681"},
                    constraints={"type": "soft", "state_snapshot": {
                        "unit_id": "AUV-324cc-001", "updated_at": "2026-08-27T09:45:20+08:00",
                        "state": {"overall_status": "available", "sonar_status": "normal"},
                    }, "violations": [{"severity": "soft", "code": "C019"}]})
    assert "当前收集" not in result and "进入发布确认阶段" not in result
    assert "任务参数清单" in result and "侧扫声呐" in result
    assert '“海洋石油 681”' in result
    assert "总体状态：可用" not in result and "| **水声通信** | 正常" not in result
    assert "状态/数值" not in result
    assert "无法确认" in result and "任务尚未发布" in result
    assert result.count("忽略软警告") == 1
    assert "*如接受" not in result


def test_live_duration_reply_checks_absolute_result_after_relative_reference_clock():
    state = {"start_time": "2026-10-21T16:37:50", "end_time": "2026-10-21T19:37:50"}
    prose = (
        "任务开始时间已自动设定为当前时间（16:37）后 1 小时，即 **2026-10-21 17:37**。\n"
        "结合您输入的持续时长 3 小时，任务结束时间已确认为 **2026-10-21 19:37**。\n"
        "请提供起点坐标。"
    )
    result = ground(prose, state=state, updates={"end_time": state["end_time"]})
    assert "17:37" not in result
    assert "任务结束时间已确认为 **2026-10-21 19:37**" in result
    assert "16:37:50" in result and "请提供起点坐标" in result


@pytest.mark.parametrize("claim,keep", [
    ("当前时间（15:37）后1小时，即2026-10-21 16:37", True),
    ("当前时间（16:37）后1小时，即2026-10-21 17:37", False),
    ("一小时后，也就是2026-10-21 17:37", False),
    ("一小时后（即2026-10-21 17:37）", False),
    ("一小时后，对应的时间为2026-10-21 17:37", False),
    ("16:37，即2026-10-21 17:37", False),
    ("17:37，即2026-10-21 16:37", False),
    ("16:37，即2026-10-21 16:37", True),
    ("16:37（原定15:37）", True),
    ("16:37（原定15:37，即2026-10-21 15:37）", True),
    ("16:37，原定15:37，即2026-10-21 15:37", True),
    ("16:37（例如15:37这种格式）", True),
])
def test_time_equivalence_claims_are_all_checked_without_confusing_reference_or_old_clock(claim, keep):
    prose = f"任务开始时间已设定为{claim}。"
    result = ground(prose, state={"start_time": "2026-10-21T16:37:50"})
    assert (prose in result) is keep


def test_correct_task_window_does_not_claim_saved_start_is_current_time():
    prose = "- **作业时间**：2026-10-21 16:37 至 19:37（当前时间 16:37 起持续 3 小时）"
    result = ground(prose, state={"start_time": "2026-10-21T16:37:50", "end_time": "2026-10-21T19:37:50"})
    assert "当前时间 16:37" not in result
    assert "作业时间**：2026-10-21 16:37 至 19:37" in result


def test_bold_numbered_field_heading_does_not_gain_a_duplicate_question():
    prose = (
        "还需要您确认以下 1 个关键参数：\n"
        "**1. 支持船编号**\n"
        "请从以下选项中选择一个支持船只：\n"
        "* 海洋石油 681\n* DSV-Oceanic\n* 海洋石油 286\n* 海洋石油 708\n"
        "请回复您选定的支持船编号，随后我将为您生成任务确认单。"
    )
    result = ground(prose, missing=[MISSING[2]])
    assert prose in result
    assert "请提供支持船编号" not in result
    assert result.count("海洋石油 681") == 1


def test_repaired_markdown_numbering_preserves_multiline_choices_once():
    prose = (
        "还需要您确认以下 3 个关键参数：\n"
        "**1. 支持船编号**\n"
        "请从以下选项中选择：\n"
        "* 海洋石油681\n* DSV-Oceanic\n"
        "**3. 结束点经纬度**：请提供终点坐标。\n"
        "请逐项回复。"
    )
    result = ground(prose, missing=MISSING)
    assert re.findall(r"(?m)^(\d+)\.", result) == ["1", "2", "3"]
    assert result.count("请从以下选项中选择") == 1
    assert result.count("海洋石油681") == 1
    assert result.count("请提供终点坐标") == 1
    assert "**3. 结束点经纬度**" not in result
    assert "请逐项回复" in result


def test_previous_duplicate_bold_question_is_removed_with_its_choices_retained():
    prose = (
        "接下来，还需要您补充以下 1 个参数：\n"
        "1. **支持船编号**：请提供支持船编号。\n"
        "**1. 支持船编号**\n"
        "请从以下选项中选择一个支持船只：\n* 海洋石油681\n* DSV-Oceanic"
    )
    result = ground(prose, missing=[MISSING[2]])
    assert result.count("1. ") == 1
    assert result.count("海洋石油681") == 1


def test_live_multiline_collection_items_remain_intact_without_inserted_duplicates():
    prose = (
        "还需要您确认以下 3 个关键参数：\n"
        "1. **起始点经纬度**\n"
        "请提供巡检路线的起点坐标（示例：北纬19.8度，东经113.5度；纬度范围 -90 至 90，经度范围 -180 至 180）。\n"
        "2. **结束点经纬度**\n"
        "请提供巡检路线的终点坐标（示例：北纬19.8度，东经113.5度；纬度范围 -90 至 90，经度范围 -180 至 180）。\n"
        "3. **水深（米）**\n"
        "请明确作业目标区域的水深（单位：米）。\n"
        "请补充以上信息，以便系统继续为您完善任务配置。"
    )
    missing = [{"key": "start_point", "label": "起始点经纬度"},
               {"key": "end_point", "label": "结束点经纬度"},
               {"key": "water_depth", "label": "水深（米）"}]
    result = ground(prose, missing=missing, updates={"cable_type": "电力电缆"})
    assert prose in result
    assert re.findall(r"(?m)^(\d+)\.", result) == ["1", "2", "3"]
    assert result.count("请提供巡检路线的起点坐标") == 1
    assert result.count("请明确作业目标区域的水深") == 1


LIVE_WARNING_CHOICE_BLOCK = (
    "⚠️ **请明确您的操作意向：**\n"
    "您希望**忽略上述软性警告**继续推进任务（后续系统将在任务执行窗口期前再次核验实时状态），"
    "还是希望**修改相关参数**（如调整时间或位置以避开风险）？\n"
    "*(注：当前所有必填字段已收集，但需先处理此软警告确认后方可进入发布流程。)*"
)


def test_live_warning_choice_block_leaves_one_authoritative_instruction_without_future_recheck_promise():
    prose = (
        "支持船“海洋石油681”已确认录入任务。\n" + LIVE_WARNING_CHOICE_BLOCK + "\n"
        "机器人 AUV-324cc-001：遥测数据已过期，无法确认当前海况和设备健康，请刷新状态信息。\n"
        "- 最后更新时间：2026-08-27T09:45:20.554033+08:00\n"
        "- 状态版本号：1\n"
        "当前设备健康与海况：未知。历史快照不能作为当前正常、可用或安全的依据。\n"
        "已有遥测相关校验提示基于该历史快照，不能据此断言当前海况；校验与确认要求仍然有效。\n"
        "✅ 已记录：支持船编号：海洋石油681。\n"
        "当前已保存：任务类型：管缆巡检；开始时间：2026-10-21T17:04:02；结束时间：2026-10-21T20:04:02。\n"
        "任务尚未发布，仍有软警告待处理。请查看右侧看板后修改相关参数；如接受这些软警告，可明确回复“忽略软警告”后继续。"
    )
    state = {"task_type": "管缆巡检", "start_time": "2026-10-21T17:04:02", "end_time": "2026-10-21T20:04:02",
             "equipment_unit_id": "AUV-324cc-001", "support_vessel": "海洋石油681"}
    result = ground(prose, state=state, missing=[], updates={"support_vessel": "海洋石油681"},
                    constraints={"type": "soft", "runtime_validation_deferred": False,
                                 "violations": [{"severity": "soft", "code": "C019"}]})
    assert "操作意向" not in result and "还是希望" not in result
    assert "忽略上述软性警告" not in result
    assert "任务执行窗口期前再次核验" not in result
    assert "注：当前所有必填字段" not in result
    assert "支持船“海洋石油681”已确认录入任务。" in result
    assert "遥测数据已过期" in result and "任务尚未发布" in result
    assert len(re.findall(r"忽略(?:上述|这些)?软(?:性)?(?:约束)?警告", result)) == 1


@pytest.mark.parametrize("choice", [
    "您希望忽略上述软性警告继续，还是希望修改相关参数？",
    "您是否愿意接受这些软性约束警告，或者修改任务？",
    "是否忽略本次软约束警告继续任务？",
    "要不要接受当前软警告？",
    "请选择忽略该软警告或者修改参数。",
])
@pytest.mark.parametrize("severity", ["soft", "hard"])
def test_warning_choice_synonyms_cannot_duplicate_or_weaken_the_authoritative_gate(choice, severity):
    result = ground("任务信息已收到。\n" + choice + "\n请核对作业区域。", missing=[], constraints={"type": severity})
    assert choice not in result
    assert "任务信息已收到" in result and "请核对作业区域" in result
    assert "任务尚未发布" in result
    if severity == "hard":
        assert "忽略" not in result and "请先修正上述硬约束问题" in result
    else:
        assert result.count("忽略软警告") == 1


@pytest.mark.parametrize("prose", [
    "请不要选择忽略上述软性警告继续任务。",
    "软性警告是可以由用户明确确认接受的提示。",
    "请明确您的操作意向：\n请提供起点坐标。",
])
def test_warning_choice_cleanup_preserves_negative_guidance_definitions_and_unrelated_prompts(prose):
    assert prose in ground(prose, missing=[], constraints={"type": "soft"})


def test_warning_choice_cleanup_does_not_apply_when_warning_is_already_resolved():
    assert LIVE_WARNING_CHOICE_BLOCK in ground(LIVE_WARNING_CHOICE_BLOCK, missing=[], constraints={"type": "none"})


def test_warning_choice_cleanup_only_removes_its_adjacent_heading():
    unrelated = "请明确您的操作意向：\n请提供起点坐标。"
    result = ground(unrelated + "\n\n" + LIVE_WARNING_CHOICE_BLOCK, missing=[], constraints={"type": "soft"})
    assert unrelated in result
    assert result.count("操作意向") == 1
    assert "任务执行窗口期前再次核验" not in result
