"""
test_issue_14_snapshot_restore_revalidation.py — 覆盖 Snapshot 恢复后对象反序列化与旧 Ack 失效机制
"""

import copy
import json
import pytest
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import SlotStore, Slot, ValidationAcknowledgement
from src.validator import TaskValidator, ValidationResult, Violation
from src.dialogue_manager import DialogueManager


def test_snapshot_restore_reconstructs_dataclasses(tmp_path):
    kb = KnowledgeBase()
    store = SlotStore(kb)
    store.init_task_slots([{"key": "equipment_unit_id", "label": "设备编号", "type": "string"}])
    store.slots["equipment_unit_id"].value = "OBSROV-001"
    store.slots["equipment_unit_id"].status = "valid"

    # 模拟构建 ValidationResult & Acknowledgement
    res = ValidationResult(
        overall_status="blocked_soft",
        validated_at="2026-08-06T10:00:00",
        task_version=1,
        validation_version=2,
        validation_fingerprint="fp12345",
        state_snapshot={"unit_id": "OBSROV-001", "status_ref": "OBSROV-001", "state_version": 81},
        violations=[Violation("C014", "流速偏高", "流速偏高", "soft")],
    )
    ack = ValidationAcknowledgement(
        constraint_id="C014",
        acknowledged_at="2026-08-06T10:00:05",
        task_version=1,
        validation_version=2,
        validation_fingerprint="fp12345",
        status_ref="OBSROV-001",
        state_version=81,
    )
    store.validation_result = res
    store.validation_acknowledgements = [ack]

    snap = store.export_snapshot()

    # 从 snapshot 恢复
    new_store = SlotStore(kb)
    new_store.restore_snapshot(snap)

    assert isinstance(new_store.validation_result, ValidationResult)
    assert new_store.validation_result.validation_fingerprint == "fp12345"
    assert len(new_store.validation_acknowledgements) == 1
    assert isinstance(new_store.validation_acknowledgements[0], ValidationAcknowledgement)
    assert new_store.validation_acknowledgements[0].constraint_id == "C014"


def test_whitelisting_invalidated_on_state_version_change(tmp_path):
    kb = KnowledgeBase()
    store = SlotStore(kb)

    res = ValidationResult(
        overall_status="blocked_soft",
        validated_at="2026-08-06T10:00:00",
        task_version=1,
        validation_version=2,
        validation_fingerprint="fp12345",
        state_snapshot={"unit_id": "OBSROV--001", "status_ref": "OBSROV-001", "state_version": 81},
        violations=[Violation("C014", "流速偏高", "流速偏高", "soft")],
    )
    ack = ValidationAcknowledgement(
        constraint_id="C014",
        acknowledged_at="2026-08-06T10:00:05",
        task_version=1,
        validation_version=2,
        validation_fingerprint="fp12345",
        status_ref="OBSROV-001",
        state_version=80, # 不匹配 81
    )
    store.validation_result = res
    store.validation_acknowledgements = [ack]

    dm = DialogueManager(kb=kb)
    dm.slot_store = store

    v = Violation("C014", "流速偏高", "流速偏高", "soft")
    assert dm._is_whitelisted(v) is False
