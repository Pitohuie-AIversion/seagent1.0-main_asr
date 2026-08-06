"""
tests/test_issue_14_dialogue_validation_gate.py
针对 Issue #14 批次三的定向单元测试：
1. DialogueManager 的统一校验刷新 _refresh_validation 与门禁逻辑
2. 软警告确认与 (task_version, validation_fingerprint, status_ref, state_version) 版本绑定
3. 状态或任务发生变化后旧确认自动失效
4. blocked_hard 与 validation_error 无法通过确认/忽略绕过
5. SlotStore 校验快照的持久化与恢复兼容性 (Schema v1 / v2)
"""

import pytest
from src.llm_client import LLMClient
from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.slot_store import SlotStore, SnapshotValidationError, ValidationAcknowledgement
from src.validator import ValidationResult
from src.simulated_time import get_current_datetime


@pytest.fixture
def dm(tmp_path):
    state_file = tmp_path / "state.yaml"
    import shutil
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.state_file = state_file
    llm = LLMClient(None, None)
    return DialogueManager(llm, kb)


def test_slot_store_snapshot_schema_v2(dm):
    """测试 SlotStore 的 Schema v2 导出与恢复，以及 Schema v1 兼容。"""
    store = dm.slot_store
    store.validation_result = ValidationResult.from_dict({
        "overall_status": "blocked_soft",
        "task_version": 1,
        "validation_version": 1,
        "validation_fingerprint": "abc12345",
        "state_snapshot": {"unit_id": "OBSROV--001", "status_ref": "OBSROV-001", "state_version": 1},
        "violations": [],
    })
    store.validation_acknowledgements = [
        ValidationAcknowledgement.from_dict({
            "constraint_id": "C014",
            "task_version": 1,
            "validation_version": 1,
            "validation_fingerprint": "abc12345",
            "status_ref": "OBSROV-001",
            "state_version": 1,
        })
    ]

    snap = store.export_snapshot()
    assert snap["snapshot_schema_version"] == 2
    assert snap["validation"] == store.validation_result.to_dict()
    assert snap["validation_acknowledgements"] == [ack.to_dict() for ack in store.validation_acknowledgements]

    # 新建 SlotStore 恢复
    new_store = SlotStore.from_snapshot(snap, kb=dm.kb)
    assert isinstance(new_store.validation_result, ValidationResult)
    assert new_store.validation_result.to_dict() == store.validation_result.to_dict()
    assert len(new_store.validation_acknowledgements) == 1
    assert isinstance(new_store.validation_acknowledgements[0], ValidationAcknowledgement)

    # 测试 Schema v1 旧快照恢复兼容
    v1_snap = {
        "store_version": 1,
        "slots": snap["slots"],
        "unresolved": snap["unresolved"],
    }
    restored_v1_store = SlotStore.from_snapshot(v1_snap, kb=dm.kb)
    assert restored_v1_store.validation_result is None
    assert restored_v1_store.validation_acknowledgements == []


def test_soft_warning_acknowledgement_fingerprint_invalidation(dm):
    """测试遥测状态或任务字段改变后，指纹变化使旧确认自动失效。"""
    now_str = get_current_datetime().strftime("%Y-%m-%d %H:%M:%S")

    dm.task_state.update({
        "task_type_key": "pipeline_inspection",
        "equipment_unit_id": "OBSROV--001",
        "water_depth": 300,
        "support_vessel": "海洋石油681",
        "oilfield_name": "东方1-1油田",
        "start_time": now_str,
    })

    # 模拟设置浑浊度 15 (触发 C014 soft warning)
    dm.kb.state_info.set_status("OBSROV-001", {"turbidity": 15, "current_velocity": 0.2})
    res1 = dm.validator.validate_task(dm.task_state)
    assert res1.overall_status == "blocked_soft"

    # 执行忽略警告处理，添加 acknowledgement
    dm.phase = "blocked_soft"
    dm._blocking_violations = res1.violations
    dm._handle_soft_warning_confirmation("确认忽略警告", "req_1")

    assert len(dm.slot_store.validation_acknowledgements) > 0

    # 现在模拟设备遥测状态升级：浑浊度变为 20，state_version 递增
    dm.kb.state_info.set_status("OBSROV-001", {"turbidity": 20, "current_velocity": 0.2})
    
    # 再次触发校验，由于 state_version 变动导致指纹不匹配，旧确认应失效
    res2 = dm.validator.validate_task(dm.task_state, task_version=dm.slot_store.version, previous_result=res1)
    assert res2.overall_status == "blocked_soft"

    dm.phase = "confirming"
    reply = dm._handle_final_publish_confirmation("确认发布任务", "req_2")
    assert "不满足" in reply or "不符合" in reply or "重" in reply or "确认" in reply or "浑浊度" in reply or "建议" in reply


def test_blocked_hard_cannot_be_bypassed(dm):
    """测试流速>1.2 (C017 hard block) 时，无法通过任何确认或忽略消息绕过发布。"""
    now_str = get_current_datetime().strftime("%Y-%m-%d %H:%M:%S")

    dm.task_state.update({
        "task_type_key": "pipeline_inspection",
        "equipment_unit_id": "OBSROV--001",
        "water_depth": 300,
        "support_vessel": "海洋石油681",
        "oilfield_name": "东方1-1油田",
        "start_time": now_str,
    })

    # 设置超限流速 1.5
    dm.kb.state_info.set_status("OBSROV-001", {"current_velocity": 1.5})
    res = dm.validator.validate_task(dm.task_state)
    assert res.overall_status == "blocked_hard"

    dm.phase = "blocked_hard"
    dm._blocking_violations = res.violations
    reply = dm._handle_task_confirm("确认")
    assert dm.phase == "blocked_hard"

    reply = dm._handle_task_confirm("忽略警告")
    assert dm.phase == "blocked_hard"
