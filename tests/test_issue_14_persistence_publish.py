"""
tests/test_issue_14_persistence_publish.py
针对 Issue #14 批次四的定向单元测试：
1. 发布产物保存完整校验证据 (status_ref, state_version, validation_version, violations)
2. 未来任务在 TaskIntent 中标记 runtime_validation
"""

import json
from pathlib import Path
import pytest
from src.llm_client import LLMClient
from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.task_intent_builder import TaskIntentBuilder
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


def test_publish_saves_validation_traceability(dm, tmp_path, monkeypatch):
    """测试最终发布的 TaskIntent 包含完整校验证据。"""
    task_dir = tmp_path / "task_intents"
    task_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.task_intent_builder.get_task_dir", lambda create=True: task_dir)

    now_str = get_current_datetime().strftime("%Y-%m-%d %H:%M:%S")
    task_state = {
        "task_id": "PI-20260810-001",
        "internal_id": "88888888-8888-4888-8888-888888888888",
        "intent_id": "TI20260810001",
        "task_type_key": "pipeline_inspection",
        "equipment_unit_id": "OBSROV--001",
        "equipment_type": "observation_rov",
        "water_depth": 300,
        "support_vessel": "海洋石油681",
        "oilfield_name": "东方1-1油田",
        "start_time": now_str,
        "end_time": "2099-01-01 18:00:00",
    }
    built_json = dict(task_state)

    dm.kb.state_info.set_status("OBSROV-001", {"current_velocity": 0.2, "turbidity": 3})
    val_res = dm.validator.validate_task(task_state)
    assert val_res.overall_status == "valid"

    builder = TaskIntentBuilder(dm.kb)
    prepared = builder.prepare(
        task_state=task_state,
        built_json=built_json,
        mode="standard",
        task_type_key="pipeline_inspection",
        intent_id="TI20260810001",
        validation_result=val_res,
        validation_acknowledgements=[],
    )

    staging_path = builder.create_staging(prepared)
    builder.publish_staging(staging_path, prepared)

    # 读取最终发布在磁盘上的 JSON 文件
    final_file = task_dir / f"task_intent_{prepared['intent_id']}.json"
    assert final_file.exists()
    published_json = json.loads(final_file.read_text(encoding="utf-8"))

    assert "conditions" in published_json
    cond = published_json["conditions"]
    assert "validation" in cond
    val_info = cond["validation"]
    assert val_info["overall_status"] == "valid"
    assert val_info["status_ref"] == "OBSROV-001"
    assert "state_version" in val_info
    assert "validation_version" in val_info
    assert "validation_fingerprint" in val_info
    assert val_info["violations"] == []


def test_future_task_runtime_validation_flag(dm):
    """测试未来任务在 TaskIntent 中标记 runtime_validation.required 为 True。"""
    task_state = {
        "task_id": "PI-20260810-002",
        "internal_id": "99999999-9999-4999-9999-999999999999",
        "intent_id": "TI20260810002",
        "task_type_key": "pipeline_inspection",
        "equipment_unit_id": "OBSROV--001",
        "start_time": "2099-01-01 10:00:00",
        "end_time": "2099-01-01 18:00:00",
    }
    built_json = dict(task_state)

    val_res = dm.validator.validate_task(task_state)
    assert val_res.overall_status == "pending_runtime_validation"

    builder = TaskIntentBuilder(dm.kb)
    prepared = builder.prepare(
        task_state=task_state,
        built_json=built_json,
        mode="standard",
        task_type_key="pipeline_inspection",
        intent_id="TI20260810002",
        validation_result=val_res,
    )

    cond = prepared["conditions"]
    assert cond["runtime_validation"]["required"] is True
    assert cond["runtime_validation"]["status"] == "pending_runtime_validation"
