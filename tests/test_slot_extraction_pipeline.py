"""
tests/test_slot_extraction_pipeline.py - 槽位抽取与消歧管道单元测试
"""

import pytest
from unittest.mock import MagicMock, patch

from src.dialogue_manager import DialogueManager
from src.handlers import (
    DialogueContext,
    SlotFillingHandler,
    SlotExtractionPipeline,
    ExtractionPipelineResult,
)
from src.slot_store import Slot


class TestSlotExtractionPipeline:
    """验证 SlotExtractionPipeline 数据契约、抽取流程与消歧隔离性"""

    def test_pipeline_component_instantiation(self):
        dm = DialogueManager()
        assert hasattr(dm.slot_handler, "extraction_pipeline")
        assert isinstance(dm.slot_handler.extraction_pipeline, SlotExtractionPipeline)
        assert dm.slot_handler.extraction_pipeline.manager is dm
        assert dm.slot_handler.extraction_pipeline.slot_filling_handler is dm.slot_handler

    def test_pipeline_result_dataclass_contract(self):
        res = ExtractionPipelineResult()
        assert res.early_return_reply is None
        assert isinstance(res.extraction_res, dict)
        assert isinstance(res.stage2_updates, dict)
        assert isinstance(res.list_mutations, list)
        assert isinstance(res.proposed_pending_rov, list)
        assert res.payload_mutation_failed is False
        assert res.mutation_failure_result is None
        assert res.apply_plan is None
        assert res.effective_task_type_key is None
        assert isinstance(res.field_defs, list)

    def test_pipeline_run_normal_extraction(self):
        dm = DialogueManager()
        # 预设 task_type_key 为海底管道巡检
        new_slots, _, expected_version = dm.slot_store.snapshot()
        new_slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        new_slots["task_type"] = Slot("task_type", value="海底管道巡检", status="valid")

        ctx = DialogueContext(
            manager=dm,
            user_message="起点坐标设为东经120度北纬30度，作业水深150米",
            request_id="req_test_pipeline_01",
        )

        current_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
        turn_unresolved = []
        new_unresolved = []
        merged_updates = {}
        merged_updates_meta = {}

        mock_candidates = [
            {
                "canonical_key": "water_depth",
                "normalized_value": 150.0,
                "raw_value": "150米",
                "confidence": 1.0,
                "resolution_method": "model",
            }
        ]
        with patch.object(
            dm.extractor,
            "extract_updates",
            return_value={"slot_candidates": mock_candidates, "list_mutations": [], "unresolved": []},
        ):
            result = dm.slot_handler.extraction_pipeline.run_pipeline(
                ctx=ctx,
                task_type_key="pipeline_inspection",
                current_state=current_state,
                has_acknowledge_action=False,
                task_patch_v2_active=True,
                new_slots=new_slots,
                turn_unresolved=turn_unresolved,
                new_unresolved=new_unresolved,
                merged_updates=merged_updates,
                merged_updates_meta=merged_updates_meta,
                expected_version=expected_version,
            )

        assert isinstance(result, ExtractionPipelineResult)
        assert result.early_return_reply is None
        assert isinstance(result.field_defs, list)
        assert len(result.field_defs) > 0
        assert "water_depth" in result.stage2_updates or "water_depth" in merged_updates

    def test_pipeline_empty_candidates_triggers_guidance(self):
        dm = DialogueManager()
        new_slots, _, expected_version = dm.slot_store.snapshot()
        new_slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")

        ctx = DialogueContext(
            manager=dm,
            user_message="今天天气不错",
            request_id="req_test_pipeline_02",
        )

        current_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
        turn_unresolved = []
        new_unresolved = []
        merged_updates = {}
        merged_updates_meta = {}

        result = dm.slot_handler.extraction_pipeline.run_pipeline(
            ctx=ctx,
            task_type_key="pipeline_inspection",
            current_state=current_state,
            has_acknowledge_action=False,
            task_patch_v2_active=True,
            new_slots=new_slots,
            turn_unresolved=turn_unresolved,
            new_unresolved=new_unresolved,
            merged_updates=merged_updates,
            merged_updates_meta=merged_updates_meta,
            expected_version=expected_version,
        )

        assert isinstance(result, ExtractionPipelineResult)
        # 空候选且非确认应触发 early_return_reply 引导用户
        assert result.early_return_reply is not None
        assert isinstance(result.early_return_reply, str)

    def test_pipeline_task_type_conflict_early_return(self):
        dm = DialogueManager()
        new_slots, _, expected_version = dm.slot_store.snapshot()
        new_slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")

        ctx = DialogueContext(
            manager=dm,
            user_message="改为电缆巡检同时做设备检修",
            request_id="req_test_pipeline_03",
        )

        current_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
        turn_unresolved = []
        new_unresolved = []
        merged_updates = {}
        merged_updates_meta = {}

        # 模拟抽取结果中包含任务选择器冲突
        with patch.object(
            dm.extractor,
            "extract_updates",
            return_value={
                "slot_candidates": [],
                "unresolved": ["同轮具体任务类型互相冲突：pipeline_inspection 与 equipment_maintenance"],
            },
        ):
            result = dm.slot_handler.extraction_pipeline.run_pipeline(
                ctx=ctx,
                task_type_key="pipeline_inspection",
                current_state=current_state,
                has_acknowledge_action=False,
                task_patch_v2_active=False,
                new_slots=new_slots,
                turn_unresolved=turn_unresolved,
                new_unresolved=new_unresolved,
                merged_updates=merged_updates,
                merged_updates_meta=merged_updates_meta,
                expected_version=expected_version,
            )

            assert isinstance(result, ExtractionPipelineResult)
            assert result.early_return_reply is not None
            assert "冲突" in result.early_return_reply or "任务类型" in result.early_return_reply

    def test_pipeline_rov_description_resolution(self):
        dm = DialogueManager()
        new_slots, _, expected_version = dm.slot_store.snapshot()
        new_slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")

        ctx = DialogueContext(
            manager=dm,
            user_message="使用重型作业级ROV执行",
            request_id="req_test_pipeline_04",
        )

        current_state = {k: s.value for k, s in new_slots.items() if s.status == "valid" and s.value is not None}
        turn_unresolved = []
        new_unresolved = []
        merged_updates = {}
        merged_updates_meta = {}

        with patch.object(
            dm.extractor,
            "extract_updates",
            return_value={
                "slot_candidates": [
                    {
                        "canonical_key": "rov_description",
                        "normalized_value": "重型作业级ROV",
                        "raw_value": "重型作业级ROV",
                        "confidence": 1.0,
                    }
                ],
                "unresolved": [],
            },
        ):
            result = dm.slot_handler.extraction_pipeline.run_pipeline(
                ctx=ctx,
                task_type_key="pipeline_inspection",
                current_state=current_state,
                has_acknowledge_action=False,
                task_patch_v2_active=False,
                new_slots=new_slots,
                turn_unresolved=turn_unresolved,
                new_unresolved=new_unresolved,
                merged_updates=merged_updates,
                merged_updates_meta=merged_updates_meta,
                expected_version=expected_version,
            )

            assert isinstance(result, ExtractionPipelineResult)
            assert result.early_return_reply is None
            assert "rov_description" in result.stage2_updates
            assert isinstance(result.proposed_pending_rov, list)
