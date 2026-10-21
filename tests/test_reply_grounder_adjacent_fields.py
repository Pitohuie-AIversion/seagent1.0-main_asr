"""Replies must describe the committed vessel, wellhead, and optional tools."""

import pytest

from src.handlers.write_reply_grounder import WriteReplyGrounder


CAMERA = "高清水下摄像机"
SONAR = "机械扫描声呐"


def ground(prose, *, updates=None, state=None, configuration=None):
    return WriteReplyGrounder.ground_write_reply(
        prose, accepted_updates=updates if updates is not None else {"water_depth": 300},
        task_state=state or {}, unresolved_inputs=[], payload_configuration=configuration,
    )


@pytest.mark.parametrize("prose,saved", [
    (f"已将携带工具从{CAMERA}替换为{SONAR}。", [CAMERA, SONAR]),
    (f"携带工具已从{CAMERA}换成{SONAR}。", [CAMERA, SONAR]),
    (f"当前携带工具：{CAMERA}、{SONAR}。", [SONAR]),
    (f"携带工具已更新为{SONAR}。", [CAMERA, SONAR]),
    (f"载荷配置：已更新为{CAMERA}。", [SONAR]),
    (f"携带工具只保留{SONAR}。", [CAMERA, SONAR]),
    (f"已新增载荷：{CAMERA}。", [SONAR]),
])
def test_committed_payload_does_not_license_a_conflicting_list_or_edit_claim(prose, saved):
    result = ground(prose + "请核对起点。", updates={"payload": saved}, state={"payload": saved})
    assert prose not in result
    assert "请核对起点。" in result
    assert "✅ 已记录：携带工具：" + "、".join(saved) in result


@pytest.mark.parametrize("prose,saved", [
    (f"已将携带工具从{CAMERA}替换为{SONAR}。", [SONAR]),
    (f"已将携带工具从{CAMERA}替换为{SONAR}。", [SONAR, "阀门扭矩工具"]),
    (f"当前携带工具：{CAMERA}、{SONAR}。", [CAMERA, SONAR]),
    (f"携带工具：{SONAR}，{CAMERA}。", [CAMERA, SONAR]),
    (f"携带工具已更新为{SONAR}。", [SONAR]),
    (f"载荷配置：已更新为{SONAR}。", [SONAR]),
    (f"携带工具只保留{SONAR}。", [SONAR]),
    (f"已新增载荷：{SONAR}。", [CAMERA, SONAR]),
    (f"携带工具已新增{SONAR}。", [CAMERA, SONAR]),
    (f"新增了{SONAR}。", [CAMERA, SONAR]),
    (f"当前携带工具包括{SONAR}。", [CAMERA, SONAR]),
    ("当前携带工具：履带与滑橇组件。", ["履带与滑橇组件"]),
    ("已将携带工具从多波束声呐系统替换为多波束声呐。", ["多波束声呐"]),
])
def test_true_configuration_replacement_and_incremental_claims_survive(prose, saved):
    assert prose in ground(prose, updates={"payload": saved}, state={"payload": saved})


@pytest.mark.parametrize("prose", [
    f"请将携带工具从{CAMERA}替换为{SONAR}。",
    f"如果携带工具从{CAMERA}换成{SONAR}，需要重新检查。",
    f"是否已新增载荷：{SONAR}？",
    f"例如，当前携带工具：{CAMERA}。",
    f"建议携带工具：{CAMERA}。",
    f"可选载荷：{CAMERA}。",
    f"候选携带工具：{CAMERA}。",
    "携带工具：尚未选择。",
])
def test_payload_questions_examples_and_pending_guidance_are_preserved(prose):
    assert prose in ground(prose, updates={"payload": [SONAR]}, state={"payload": [SONAR]})


def test_payload_cleanup_retains_independent_guidance_and_vessel_assertions():
    result = ground(
        f"当前携带工具：{CAMERA}，支持船为海洋石油681，请核对起点。",
        updates={"payload": [SONAR], "support_vessel": "海洋石油681"},
        state={"payload": [SONAR], "support_vessel": "海洋石油681"},
    )
    assert CAMERA not in result
    assert "支持船为海洋石油681，请核对起点。" in result


def test_onboard_catalogue_does_not_expand_saved_optional_payload_evidence():
    prose = f"当前携带工具：{CAMERA}、前视声呐系统。"
    result = ground(
        prose, updates={"payload": [CAMERA]}, state={"payload": [CAMERA]},
        configuration={"onboard_payloads": ["前视声呐系统"], "supported_payloads": [CAMERA]},
    )
    assert prose not in result
    assert "前视声呐系统" not in result


@pytest.mark.parametrize("saved,keep", [([CAMERA, SONAR], True), ([SONAR], False)])
def test_multiline_payload_claim_is_checked_as_one_complete_list(saved, keep):
    prose = f"**当前携带工具**：\n- {CAMERA}\n- {SONAR}\n"
    result = ground(prose + "请核对起点。", updates={"payload": saved}, state={"payload": saved})
    assert (prose in result) is keep
    assert "请核对起点。" in result
    if not keep:
        assert CAMERA not in result
        assert f"- {SONAR}" not in result


@pytest.mark.parametrize("prose", [
    "支持船已关联海洋石油286。",
    "已绑定支持船：海洋石油286。",
    "已将支持船指定为海洋石油286。",
    "支持船配置成功：海洋石油286。",
    "作业船已配置海洋石油286。",
])
def test_committed_vessel_cannot_preserve_another_vessels_success_claim(prose):
    result = ground(prose + "请核对起点。", updates={"support_vessel": "海洋石油681"})
    assert "海洋石油286" not in result
    assert "请核对起点。" in result
    assert "支持船编号：海洋石油681" in result


@pytest.mark.parametrize("prose", [
    "支持船已关联海洋石油681。",
    "已绑定支持船：海洋石油681。",
    "支持船配置成功：海洋石油681。",
    "是否已关联支持船海洋石油286？",
    "请关联支持船海洋石油286。",
    "例如，支持船已关联海洋石油286。",
    "支持船尚未关联海洋石油286。",
])
def test_true_vessel_claims_and_nonasserted_alternatives_survive(prose):
    assert prose in ground(prose, updates={"support_vessel": "海洋石油681"})


@pytest.mark.parametrize("key,saved,prose", [
    ("wellhead_id", "A04A", "当前井口编号为A04。"),
    ("wellhead_id", "XA04", "当前井口编号为A04。"),
    ("wellhead_id", "A04", "当前井口编号为A04，A05。"),
    ("wellhead_id", "A04", "当前井口编号为A04/A05。"),
    ("support_vessel", "海洋石油681", "当前支持船为海洋石油681，海洋石油286。"),
    ("equipment_unit_id", "LROV-150-001", "当前机器人编号为LROV-150-001，LROV-150-002。"),
])
@pytest.mark.parametrize("updated", [False, True])
def test_identifiers_must_match_whole_values_and_not_only_the_first_list_item(key, saved, prose, updated):
    result = ground(prose + "请核对起点。", updates={key: saved} if updated else None, state={key: saved})
    assert prose not in result
    assert "请核对起点。" in result
    assert saved in result


@pytest.mark.parametrize("prose", [
    "当前井口编号为A04。",
    "当前井口编号为A04（原A05）。",
    "井口编号已从A05改为A04。",
    "井口编号为A05吗？",
    "例如，井口编号为A05。",
])
def test_complete_identifiers_historical_notes_and_questions_survive(prose):
    assert prose in ground(prose, state={"wellhead_id": "A04"})
