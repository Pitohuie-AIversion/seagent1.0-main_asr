from datetime import timedelta
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouteResult
from src.knowledge_retriever import KnowledgeBase
from src.simulated_time import get_current_datetime


class DummyLLM:
    """避免测试加载真实 vLLM，只提供 DialogueManager 需要的最小接口。"""

    def chat(self, messages, temperature=0.7, max_tokens=None):
        return "测试回复"

    def generate(self, messages, temperature=0.7, max_tokens=None):
        return "null"

    def filter_reply(self, reply):
        return reply


class DeterministicIntentRouter:
    """本用例只验证写槽链路，意图路由固定为 WRITE。"""

    def route(
        self,
        user_message,
        conversation_history=None,
        task_state=None,
        phase="collecting",
        expected_slots=None,
    ):
        return IntentRouteResult(
            interaction_type="WRITE",
            confidence=1.0,
            reason="deterministic slot-write test",
        )


class DeterministicPipelineExtractor:
    """按用户给定的完整案例返回标准槽位候选，不越过 extractor 的职责边界。"""

    def __init__(self, kb):
        self.kb = kb
        self.start_time = get_current_datetime().replace(microsecond=0)
        self.end_time = self.start_time + timedelta(hours=5)
        self.calls = []

    @staticmethod
    def _candidate(key, value, raw):
        return {
            "canonical_key": key,
            "normalized_value": value,
            "raw_value": raw,
            "confidence": 1.0,
        }

    def extract_updates(
        self,
        user_message,
        current_state,
        task_type_key=None,
        task_type_map=None,
        required=None,
        ROV2type=None,
        conversation_history=None,
    ):
        self.calls.append(
            {
                "user_message": user_message,
                "task_type_key": task_type_key,
                "current_state": dict(current_state or {}),
            }
        )
        candidates = []
        raw = user_message

        if "管缆巡检" in raw:
            candidates.extend(
                [
                    self._candidate("task_type", "管缆巡检", raw),
                    self._candidate("task_type_key", "pipeline_inspection", raw),
                ]
            )
        if "开始时间现在" in raw:
            candidates.append(
                self._candidate(
                    "start_time",
                    self.start_time.strftime("%Y-%m-%dT%H:%M:%S"),
                    raw,
                )
            )
        if "结束时间五小时后" in raw:
            candidates.append(
                self._candidate(
                    "end_time",
                    self.end_time.strftime("%Y-%m-%dT%H:%M:%S"),
                    raw,
                )
            )
        if "海底油气管道" in raw:
            candidates.append(self._candidate("cable_type", "海底油气管道", raw))
        if "起始点" in raw:
            candidates.append(self._candidate("start_point", {"lat": 16.8, "lon": 113.5}, raw))
        if "结束点" in raw:
            candidates.append(self._candidate("end_point", {"lat": 19.0, "lon": 113.8}, raw))
        if "水深300米" in raw:
            candidates.append(self._candidate("water_depth", "300米", raw))
        if "轻型工作级" in raw:
            candidates.append(self._candidate("equipment_family", "轻型工作级深海机器人", raw))
        if "第一个型号" in raw:
            variants = self.kb.get_task_allowed_robot_variants(
                "pipeline_inspection",
                current_state.get("equipment_family"),
            )
            candidates.append(self._candidate("equipment_type", variants[0]["full_name"], raw))
        if "第一个编号" in raw:
            variant_name = current_state.get("equipment_type")
            robot = self.kb.get_rov_for_task(variant_name, "pipeline_inspection")
            candidates.append(self._candidate("equipment_unit_id", robot["unit_ids"][0], raw))
        if "工具全部携带" in raw:
            candidates.append(self._candidate("payload", "全部", raw))
        if "母船使用681" in raw:
            candidates.append(self._candidate("support_vessel", "海洋石油681", raw))

        return {"slot_candidates": candidates, "unresolved": []}


class PipelineInspectionSlotWriteFlowTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.manager = DialogueManager(DummyLLM(), self.kb)
        self.manager.intent_router = DeterministicIntentRouter()
        self.manager.extractor = DeterministicPipelineExtractor(self.kb)

    def _process_and_assert(self, message, expected_updates):
        self.manager.process(message, request_id="pipeline_slot_write_test")
        state = self.manager.slot_store.get_task_state()
        built = self.manager._last_built_json

        for key, expected in expected_updates.items():
            self.assertEqual(
                state.get(key),
                expected,
                f"{message!r} 后槽位 {key!r} 未按预期写入，当前状态为 {state}",
            )
            if key in built:
                self.assertEqual(
                    built.get(key),
                    expected,
                    f"{message!r} 后输出 JSON 字段 {key!r} 未按预期同步",
                )

    def test_pipeline_inspection_case_writes_each_input_incrementally(self):
        expected_robot = self.kb.get_rov_for_task("轻型工作级深海机器人", "pipeline_inspection")
        expected_equipment_type = expected_robot["full_name"]
        expected_equipment_unit_id = expected_robot["unit_ids"][0]
        expected_payload = expected_robot["supported_payloads"]
        expected_start_time = self.manager.extractor.start_time.strftime("%Y-%m-%dT%H:%M:%S")
        expected_end_time = self.manager.extractor.end_time.strftime("%Y-%m-%dT%H:%M:%S")

        steps = [
            ("我想做管缆巡检", {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}),
            ("开始时间现在", {"start_time": expected_start_time}),
            ("结束时间五小时后", {"end_time": expected_end_time}),
            ("管缆类型海底油气管道", {"cable_type": "海底油气管道"}),
            ("起始点(16.8,113.5)", {"start_point": {"lat": 16.8, "lon": 113.5}}),
            ("结束点(19.0,113.8)", {"end_point": {"lat": 19.0, "lon": 113.8}}),
            ("水深300米", {"water_depth": 300.0}),
            ("使用轻型工作级", {"equipment_family": "轻型工作级深海机器人"}),
            ("使用第一个型号", {"equipment_type": expected_equipment_type}),
            ("使用第一个编号", {"equipment_unit_id": expected_equipment_unit_id}),
            ("工具全部携带", {"payload": expected_payload}),
            ("母船使用681", {"support_vessel": "海洋石油681"}),
        ]

        for message, expected_updates in steps:
            with self.subTest(message=message):
                self._process_and_assert(message, expected_updates)

        final_json = self.manager._last_built_json
        for key in (
            "task_id",
            "task_type",
            "start_time",
            "end_time",
            "cable_type",
            "start_point",
            "end_point",
            "water_depth",
            "equipment_family",
            "equipment_type",
            "equipment_unit_id",
            "payload",
            "support_vessel",
        ):
            self.assertIn(key, final_json)
        self.assertEqual(self.manager._last_missing, [])

    def test_task_type_only_first_turn_does_not_extract_same_message_twice(self):
        self.manager.process("我想做管缆巡检", request_id="pipeline_slot_write_test")

        self.assertEqual(
            len(self.manager.extractor.calls),
            1,
            "首轮只包含任务类型时，不应将同一句话再次送入 Stage 2 抽取。",
        )
        self.assertEqual(self.manager.extractor.calls[0]["task_type_key"], None)


if __name__ == "__main__":
    unittest.main()
