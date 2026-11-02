import pytest

from src.handlers.knowledge_reply_grounder import ground_payload_requirement_reply


@pytest.mark.parametrize("unsupported", [
    "只要具备机械臂及操作能力，即可满足任务准入。",
    "利用机载设备即可满足准入条件。",
    "只要机器人具备巡检能力，则任务可发布。",
    "只要选用工作级机器人，即可直接执行阀门插拔任务。",
    "本系统的准入校验逻辑是‘能力匹配’而非‘工具清单’。",
])
def test_configuration_does_not_grant_unconditional_admission(unsupported):
    answer = "电液机械臂是可选载荷。\n" + unsupported + "\n通用工作级机器人自带多功能液压机械臂。"
    result = ground_payload_requirement_reply(answer)
    assert unsupported not in result
    assert "电液机械臂是可选载荷。" in result
    assert "自带多功能液压机械臂" in result
    assert "硬约束校验通过" in result and "明确确认" in result
    assert "执行协议支持" in result


@pytest.mark.parametrize("explanation", [
    "不能认为只要具备机械臂即可满足准入条件。",
    "能力匹配不代表即可发布任务。",
    "是否意味着即可发布任务？",
    "完成全部校验和发布确认后，即可发布任务计划。",
    "设备具备巡检能力，不需要强制另带侧扫声呐。",
])
def test_correct_limitations_and_configuration_facts_are_preserved(explanation):
    assert explanation in ground_payload_requirement_reply(explanation)


def test_read_only_answer_does_not_solicit_a_new_task():
    result = ground_payload_requirement_reply("无需另带。请描述您的作业需求，系统将创建任务。")
    assert "无需另带。" in result
    assert "请描述" not in result
    assert "本轮未创建、修改或发布任务" in result
