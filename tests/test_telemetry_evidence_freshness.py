"""Expired telemetry must never become a current health claim in dialogue."""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.extraction.prompts import _format_state_snapshot_summary, build_responder_messages
from src.handlers.telemetry_status import TelemetryStatusHandler
from src.telemetry_evidence import assess_telemetry_freshness, ground_telemetry_reply
from src.state.runtime_checker import inspect_robot_availability


NOW = datetime(2026, 10, 21, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
OLD_STATE = {
    "overall_status": "available", "thruster_status": "normal",
    "current_velocity": 1.0, "obstacle_density": "high",
    "update_timestamp": "2026-08-27T09:45:20", "version": 3,
}
SNAPSHOT = {"unit_id": "AUV-324cc-001", "state_version": 3, "state": OLD_STATE}


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr("src.telemetry_evidence.get_current_datetime", lambda: NOW)
    monkeypatch.setattr("src.extraction.prompts.get_current_datetime", lambda: NOW)


def test_expired_snapshot_summary_does_not_present_historical_health_as_live():
    summary = _format_state_snapshot_summary(SNAPSHOT)
    assert "已过期" in summary
    assert "无法确认" in summary
    assert "2026-08-27T09:45:20" in summary
    assert "总体状态: 可用" not in summary
    assert "海流速度 1.0" not in summary
    assert "推进器 正常" not in summary


def test_complete_task_prompt_does_not_order_unconditional_healthy_conclusion():
    messages = build_responder_messages(
        task_state={}, built_json={}, missing_fields=[], mode="normal",
        phase="blocked_soft", knowledge_context="", constraint_context={
            "type": "soft", "state_snapshot": SNAPSHOT,
        }, conversation_history=[], latest_user_message="支持船使用海洋石油681",
        ROV2type={}, support_task=[], accepted_updates={},
    )
    system = messages[0]["content"]
    assert "设备健康度与水文指标正常，符合水下作业窗口要求" not in system
    assert "已过期" in system
    assert "海流速度 1.0" not in system


def test_environment_query_does_not_report_expired_values_as_current():
    handler = TelemetryStatusHandler(SimpleNamespace())
    reply = handler.build_environment_status_reply("AUV-324cc-001", OLD_STATE)
    assert "已过期" in reply
    assert "无法确认" in reply
    assert "机器人状态：可用" not in reply
    assert "海流流速：1.0" not in reply


@pytest.mark.parametrize("timestamp,status", [
    ("2026-10-21T16:00:00+08:00", "fresh"),
    ("2026-10-21T08:00:00Z", "fresh"),
    ("2026-10-21T15:30:00", "fresh"),
    ("2026-10-21T15:29:59", "stale"),
    ("2026-10-21T16:05:00+08:00", "fresh"),
    ("2026-10-21T16:05:01+08:00", "invalid"),
    ("not a timestamp", "invalid"),
    (None, "missing"),
])
def test_presentation_uses_runtime_timestamp_boundaries(timestamp, status):
    state = {"overall_status": "available", "updated_at": timestamp}
    evidence = assess_telemetry_freshness(state)
    availability = inspect_robot_availability("AUV-324cc-001", matched_unit={}, state=state, now_dt=NOW)
    assert evidence["status"] == status
    assert evidence["usable"] is availability["available"]


def test_fresh_unhealthy_snapshot_remains_unhealthy_not_hidden():
    snapshot = {"unit_id": "AUV-324cc-001", "state": {
        "overall_status": "unavailable", "thruster_status": "abnormal",
        "update_timestamp": NOW.isoformat(),
    }}
    summary = _format_state_snapshot_summary(snapshot)
    assert "unavailable" in summary
    assert "推进器 异常" in summary
    assert "已过期" not in summary
    assert NOW.isoformat() in summary


@pytest.mark.parametrize("deferred", [False, True])
def test_ungrounded_telemetry_reply_retains_task_receipt_and_conditional_advice(deferred):
    reply = (
        "已记录支持船：海洋石油681。\n\n"
        "### 机器人实时遥测与作业环境核验看板\n"
        "| 指标 | 状态 |\n| --- | --- |\n"
        "| 总体状态 | 可用 |\n| 海流流速 | 1.0 m/s |\n"
        "关键子系统：全部正常。\n"
        "如果海流过大，建议调整作业窗口。"
    )
    result = ground_telemetry_reply(reply, {
        "state_snapshot": SNAPSHOT, "runtime_validation_deferred": deferred,
    })
    assert "已记录支持船：海洋石油681" in result
    assert "如果海流过大，建议调整作业窗口" in result
    assert "全部正常" not in result
    assert "1.0 m/s" not in result
    assert "| 指标" not in result
    if deferred:
        assert "核验尚未进行" in result
        assert "2026-08-27" not in result
        assert "已过期" not in result
    else:
        assert "已过期" in result and "当前设备健康与海况：未知" in result


def test_direct_device_status_query_short_circuits_stale_model_health_claim(monkeypatch):
    from unittest.mock import Mock
    from src.dialogue_manager import DialogueManager
    from src.knowledge_retriever import KnowledgeBase
    from tests.interaction_plan_support import ScriptedLLM, make_plan

    llm = ScriptedLLM(plans=[make_plan("READ", query_intent="DEVICE_STATUS", source_policy="realtime_state")])
    manager = DialogueManager(llm, KnowledgeBase())
    manager.task_state = {"equipment_unit_id": "AUV-324cc-001"}
    monkeypatch.setattr(manager.kb, "get_robot_state_dict", Mock(return_value=OLD_STATE))
    chat = Mock(return_value="机器人当前正常可用。")
    monkeypatch.setattr(llm, "chat", chat)
    reply = manager.process("机器人现在状态如何？")
    assert "已过期" in reply
    assert "机器人当前正常可用" not in reply
    chat.assert_not_called()


def test_fresh_status_queries_preserve_actual_values():
    handler = TelemetryStatusHandler(SimpleNamespace())
    state = {**OLD_STATE, "update_timestamp": NOW.isoformat()}
    reply = handler.build_environment_status_reply("AUV-324cc-001", state)
    assert "海流流速：1.0 m/s" in reply
    assert "障碍物密度：高" in reply
    assert "机器人状态：可用" in reply


def test_collection_receipt_does_not_gain_unsolicited_telemetry_warning():
    reply = "已记录开始时间：2026-10-21 17:00。请提供持续时长。"
    for snapshot in ({}, SNAPSHOT):
        assert ground_telemetry_reply(reply, {"type": "none", "state_snapshot": snapshot}) == reply


def test_live_dialogue_telemetry_table_removed_as_whole_but_task_table_retained():
    reply = """已记录支持船：海洋石油681。
### 任务参数清单
| 参数 | 值 |
| --- | --- |
| 执行设备 | AUV-324cc-001 |
| 支持船只 | 海洋石油681 |
### 🤖 机器人实时遥测与作业环境核验看板
| 项目 | 状态/数值 | 备注 |
| :--- | :--- | :--- |
| **选定机器人** | AUV-324cc-001 | 总体状态：可用 |
| **海流流速** | 1.00 m/s | ⚠️ 高流速，仅支持基础航行 |
| **水体浑浊度** | 8.00 | ⚠️ 中等，视觉感知下降 |
| **障碍物密度** | 高 | ⚠️ 局部碰撞风险高，建议提高避障优先级 |
| **母船支援** | 强 | 支持船可用 |
| **推进器状态** | 正常 | |
| **定深能力** | 正常 | |
| **视觉系统** | 正常 | |
| **声呐系统** | 正常 | |
| **机械臂状态** | 正常 | |
| **水声通信** | 正常 | |
| **数据更新时间** | 2026-08-27T09:45:20 | 环境信息已过期 |
⚠️ **系统软性警告提示**：
当前区域存在障碍物密集、浑浊度中等及高流速等环境约束。
任务尚未发布，仍有软警告待处理。
"""
    result = ground_telemetry_reply(reply, {"type": "soft", "state_snapshot": SNAPSHOT})
    assert "| 执行设备 | AUV-324cc-001 |" in result
    assert "| 支持船只 | 海洋石油681 |" in result
    assert "| --- | --- |" in result
    assert "状态/数值" not in result
    assert "正常 |" not in result
    assert "支持船可用" not in result
    assert "当前区域存在" not in result
    assert "任务尚未发布" in result
    assert "已过期" in result


def test_dict_deferred_notice_does_not_present_expired_snapshot():
    reply = ground_telemetry_reply("任务参数已齐备。", {
        "state_snapshot": SNAPSHOT,
        "violations": [{"check_type": "future_task_runtime_notice"}],
    })
    assert "核验尚未进行" in reply
    assert "已过期" not in reply
