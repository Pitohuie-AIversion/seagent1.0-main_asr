"""
tests/test_slot_collection_telemetry_deferral.py

验证任务约束与遥测校核的时序逻辑：
1. 在任务槽位收集中途（missing_slots 不为空），遥测软警告（如海流速度、浑浊度等）不阻断收集，系统维持 collecting 阶段提示补充缺失字段；
2. 当所有必填槽位收集完毕后，系统在发布前阶段（confirming / preview）自动触发机器人与环境遥测校核，呈现遥测摘要与软警告；
3. 用户在遥测校核显示后，可以进行确认修改、忽略警告或确认发布。
"""

import sys
import os
import tempfile
import uuid
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.simulated_time import get_current_datetime
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)


class TestSlotCollectionTelemetryDeferral(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="seagent_test_")
        os.environ["SEAGENT_RESULT_DIR"] = self.temp_dir
        self.kb = KnowledgeBase()

    def test_telemetry_check_deferred_until_slots_complete(self):
        """测试：槽位收集中途不报软警告，收集完毕后统一展示遥测与警告"""
        now = get_current_datetime()
        start_iso = "2026-08-24T09:00:00"
        end_iso = "2026-08-24T17:00:00"

        # Turn 1 extraction: 仅提供任务类型与油田
        turn1_ext = extraction_result(
            slot_candidate("task_type_key", "pipeline_burial"),
            slot_candidate("oilfield_name", "流花11-1油田"),
        )
        # Turn 2 extraction: 补全所有剩余必填槽位
        turn2_ext = extraction_result(
            slot_candidate("equipment_unit_id", "CRAWLER-1600-001"),
            slot_candidate("start_time", start_iso),
            slot_candidate("end_time", end_iso),
            slot_candidate("cable_type", "电缆"),
            slot_candidate("start_point", {"lat": 20.8, "lon": 115.7}),
            slot_candidate("end_point", {"lat": 20.82, "lon": 115.75}),
            slot_candidate("water_depth", 130.0),
            slot_candidate("payload", ["机械切割开沟模块", "TSS管缆跟踪系统"]),
            slot_candidate("support_vessel", "海洋石油681"),
        )
        # Turn 3 extraction: 空 (确认发布/忽略警告)
        turn3_ext = extraction_result()

        scripted_llm = ScriptedLLM(
            plans=[
                make_plan("WRITE"),
                make_plan("WRITE"),
                make_plan("WRITE"),
                make_plan("WRITE"),
            ],
            extractions=[
                turn1_ext, turn1_ext,  # Turn 1
                turn2_ext, turn2_ext,  # Turn 2
                turn3_ext, turn3_ext,  # Turn 3a
                turn3_ext, turn3_ext,  # Turn 3b
            ],
            replies=[
                "已记录任务类型与油田，请补充开始时间、结束时间、水深与作业设备。",
                "所有参数已收集完毕，已为您进行动态遥测与环境校核，请确认后发布。",
                "已忽略警告，准备发布。",
                "任务已成功确认发布。",
            ],
        )

        dm = DialogueManager(llm=scripted_llm, kb=self.kb, session_id=f"test_session_{uuid.uuid4()}")
        dm.reset()

        # Turn 1: 仅提供部分槽位 (缺少时间、水深、设备等)
        reply_1 = dm.process("在流花11-1油田执行管缆埋设作业")
        # 验证：缺少必填槽位时，维持 collecting 阶段，软警告不阻断
        self.assertEqual(dm.phase, "collecting")
        self.assertTrue(len(dm._last_missing) > 0)

        # Turn 2: 补全所有剩余必填槽位
        reply_2 = dm.process("使用CRAWLER-1600-001，水深130米，电缆，海洋石油681")
        # 验证：槽位全部收集完毕后，触发全量动态遥测校核，切至 blocked_soft 或 confirming 阶段
        self.assertIn(dm.phase, ("confirming", "blocked_soft"))
        self.assertEqual(len(dm._last_missing), 0)

        # Turn 3: 忽略警告与确认发布
        if dm.phase == "blocked_soft":
            reply_3a = dm.process("忽略警告")
        if dm.phase == "confirming":
            reply_3b = dm.process("确认发布")

        self.assertEqual(dm.phase, "done")
        self.assertIsNotNone(dm.get_final_result())


if __name__ == "__main__":
    unittest.main()
