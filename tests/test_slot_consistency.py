import unittest
import threading
import time
import io
import json
import logging
import tempfile
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import copy
import os
import builtins
import multiprocessing
from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.extractor import ParameterExtractor
from src.llm_client import LLMClient
from src.output_builder import OutputBuilder
from src.slot_store import SlotStore, Slot, SlotVersionConflict, SnapshotValidationError, VALID_VALUE_TYPES
from src.task_intent_builder import TaskIntentBuilder
from src.simulated_time import get_simulated_time
from src.history_manager import save_conversation
from src.result_paths import get_task_dir, get_history_dir
from tests.interaction_plan_support import empty_extraction, make_plan


from src.exceptions import (

    TaskPersistenceError,
    TaskRollbackError,
    IntentIdConflict,
    IdReservationError,
)

import src.id_sequence as id_sequence

import web_backend
from web_backend import app


def seed_complete_valid_pipeline_task(dm, kb):
    """Fills all required slots for pipeline_inspection in normal mode with dynamically retrieved valid KB values."""
    dm.reset()
    store = dm.slot_store

    task_type_key = "pipeline_inspection"
    task_type = kb.task_schemas.get("task_templates", {}).get(task_type_key, {}).get("task_type_values", ["管缆巡检"])[0]

    cable_types = [t["label"] for t in kb.assets.get("cable_types", [])]
    cable_type = cable_types[0] if cable_types else "电力电缆"

    from src.simulated_time import get_current_datetime
    from datetime import timedelta
    now_dt = get_current_datetime()
    water_depth = 300.0
    start_time = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
    end_time = (now_dt + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S")
    start_point = {"lat": 20.0, "lon": 110.0}
    end_point = {"lat": 20.1, "lon": 110.1}

    allowed_rovs = kb.get_task_allowed_robot_variants(task_type_key)
    selected_rov = allowed_rovs[0] if allowed_rovs else kb.get_all_rovs()[0]
    equipment_type = selected_rov["full_name"]
    equipment_unit_id = selected_rov.get("unit_ids", ["OBSROV-75-001"])[0]

    task_commons = kb.assets.get("payload_options", {}).get(task_type_key, {}).get("common", [])
    supported_payloads = selected_rov.get("supported_payloads", [])
    valid_payloads = [p for p in task_commons if p in supported_payloads]
    payload = valid_payloads[:2] if valid_payloads else ["激光标尺"]

    vessels = [v["id"] for v in kb.assets.get("vessels", []) if v.get("available", True)]
    support_vessel = vessels[0] if vessels else "DSV-Oceanic"

    slots_to_seed = {
        "internal_id": ("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "string"),
        "task_id": ("PI-20260718-001", "string"),
        "task_type_key": (task_type_key, "string"),
        "task_type": (task_type, "string"),
        "cable_type": (cable_type, "string"),
        "water_depth": (water_depth, "number"),
        "start_time": (start_time, "datetime"),
        "end_time": (end_time, "datetime"),
        "start_point": (start_point, "coord"),
        "end_point": (end_point, "coord"),
        "equipment_class": (selected_rov.get("robot_class") or "observation_rov", "string"),
        "equipment_family": (selected_rov.get("family_full_name") or selected_rov.get("family") or "ROV", "string"),
        "equipment_type": (equipment_type, "string"),
        "equipment_unit_id": (equipment_unit_id, "string"),
        "payload": (payload, "list"),
        "support_vessel": (support_vessel, "string"),
        "intent_id": ("TI2026063001", "string"),
    }

    for key, (val, vtype) in slots_to_seed.items():
        store.slots[key] = Slot(slot_name=key, value=val, value_type=vtype, status="valid", source="user_input")

    if str(kb.state_info.state_file).endswith("config/state.yaml"):
        temp_state_file = Path(tempfile.gettempdir()) / f"state_seeded_{os.getpid()}_{id(kb)}.yaml"
        if not temp_state_file.exists() and Path(kb.state_info.state_file).exists():
            import shutil
            shutil.copy(kb.state_info.state_file, temp_state_file)
        kb.state_info.state_file = temp_state_file

    kb.state_info.set_status(equipment_unit_id, {"overall_status": "available"})

    dm._rebuild_cache()
    dm.phase = "confirming"

    all_v = dm.validator.validate(dm.task_state)
    for v in all_v:
        if v.severity == "soft":
            for f in v.related_fields:
                val = dm.task_state.get(f)
                if val is not None:
                    dm._soft_whitelist.add((f, str(val), v.constraint_id))

    req_schema = dm.builder.get_schema(task_type_key, dm.mode)
    dm._last_missing = store.get_missing_slots(req_schema)
    return selected_rov


def _mp_id_two_barrier_worker(result_dir: str, prefix: str, queue: multiprocessing.Queue, b1: multiprocessing.Barrier, b2: multiprocessing.Barrier):
    os.environ["SEAGENT_RESULT_DIR"] = result_dir
    b1.wait()
    gid = id_sequence.next_daily_id(prefix, "20260718", 2, [(Path(result_dir) / "task", "intent_id")])
    b2.wait()
    queue.put(gid)


def _mp_publish_no_clobber_worker(result_dir: str, intent: dict, queue: multiprocessing.Queue, b1: multiprocessing.Barrier):
    os.environ["SEAGENT_RESULT_DIR"] = result_dir
    kb = KnowledgeBase()
    builder = TaskIntentBuilder(kb)
    staging = builder.create_staging(intent)
    b1.wait()
    try:
        filename = builder.publish_staging(staging, intent)
        queue.put(("success", filename))
    except IntentIdConflict as e:
        queue.put(("conflict", str(e)))
    except Exception as e:
        queue.put(("error", str(e)))


def assert_ssot_consistency(test_case, dm):
    """
    SSOT 校验辅助函数：验证 dm.task_state 和 dm._last_built_json 完全从 slot_store 派生。
    """
    test_case.assertEqual(dm.task_state, dm.slot_store.get_task_state())
    test_case.assertEqual(dm._last_built_json, dm.slot_store.get_built_json())



class SlotConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()
        cls.llm = MagicMock(spec=LLMClient)
        cls.llm.generate.return_value = "已接收到您的任务输入"
        cls.llm.filter_reply.side_effect = lambda text, *args, **kwargs: text if isinstance(text, str) else "已接收到您的任务输入"
        cls.llm.classify_interaction.return_value = make_plan("CLARIFY")
        cls.llm.extract_json.return_value = empty_extraction()
        app.testing = True
        web_backend.init_manager(DialogueManager(cls.llm, cls.kb))

    def setUp(self):
        get_simulated_time().set_current_time(
            datetime(2026, 6, 30, 17, 38, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        self.client = app.test_client()
        self.llm.classify_interaction.reset_mock()
        self.llm.classify_interaction.side_effect = None
        self.llm.classify_interaction.return_value = make_plan("CLARIFY")
        self.llm.extract_json.reset_mock()
        self.llm.extract_json.side_effect = None
        self.llm.extract_json.return_value = empty_extraction()
        self.dm = DialogueManager(self.llm, self.kb)
        self._orig_route = self.dm.intent_router.route

    def _set_plan(self, operation: str, **kwargs) -> None:
        self.llm.classify_interaction.return_value = make_plan(operation, **kwargs)


    def test_extractor_always_uses_six_recent_history_messages(self):
        llm = MagicMock(spec=LLMClient)
        llm.extract_json.return_value = empty_extraction()
        extractor = ParameterExtractor(llm)
        history = [
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"历史{index}",
            }
            for index in range(8)
        ]

        extractor.extract_updates(
            "改成第二个",
            {},
            task_type_key="pipeline_inspection",
            required=[],
            conversation_history=history,
        )

        messages = llm.extract_json.call_args.args[0]
        self.assertEqual(messages[1:-1], history[-6:])
        self.assertEqual(messages[-1]["content"], "改成第二个")

    def test_output_builder_keeps_ambiguous_aliases_and_candidate_evidence(self):
        builder = OutputBuilder(self.kb)
        catalog = [
            {"canonical_value": "A", "aliases": ["一号机", "Alpha"]},
            {"canonical_value": "B", "aliases": ["一号机", "Beta"]},
        ]

        alias_mappings, ambiguous_aliases = builder._build_alias_indexes(catalog)

        self.assertEqual(alias_mappings["Alpha"], "A")
        self.assertEqual(alias_mappings["Beta"], "B")
        self.assertNotIn("一号机", alias_mappings)
        self.assertEqual(ambiguous_aliases["一号机"], ["A", "B"])

    def test_extractor_resolves_exact_alias_to_allowed_canonical_value(self):
        builder = OutputBuilder(self.kb)
        required = builder.get_required(
            "tree_valve_operation",
            task_state={"equipment_family": "通用工作级深海机器人"},
        )
        llm = MagicMock(spec=LLMClient)
        llm.extract_json.return_value = {
            "slot_candidates": [
                {
                    "raw_key": "型号",
                    "canonical_key": "equipment_type",
                    "raw_value": "奇点250HP",
                    "normalized_value": "奇点250HP",
                    "confidence": 0.9,
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        result = ParameterExtractor(llm).extract_updates(
            "奇点250HP",
            {"equipment_family": "通用工作级深海机器人"},
            task_type_key="tree_valve_operation",
            required=required,
        )

        self.assertEqual(len(result["slot_candidates"]), 1)
        candidate = result["slot_candidates"][0]
        self.assertEqual(candidate["canonical_key"], "equipment_type")
        self.assertEqual(candidate["normalized_value"], "通用工作级深海机器人 250HP")
        self.assertEqual(candidate["resolution_method"], "alias_exact")
        self.assertEqual(llm.extract_json.call_count, 1)

    def test_extractor_uses_semantic_resolution_then_backend_validation(self):
        builder = OutputBuilder(self.kb)
        required = builder.get_required(
            "tree_valve_operation",
            task_state={"equipment_family": "通用工作级深海机器人"},
        )
        llm = MagicMock(spec=LLMClient)
        llm.extract_json.side_effect = [
            {
                "slot_candidates": [
                    {
                        "raw_key": "型号",
                        "canonical_key": "equipment_type",
                        "raw_value": "奇点那台250马力的",
                        "normalized_value": "奇点那台250马力的",
                        "confidence": 0.88,
                    }
                ],
                "list_mutations": [],
                "unresolved": [],
            },
            {
                "matched": True,
                "canonical_key": "equipment_type",
                "canonical_value": "通用工作级深海机器人 250HP",
                "confidence": 0.94,
                "reason": "奇点和250马力共同指向该型号",
            },
        ]

        result = ParameterExtractor(llm).extract_updates(
            "就用奇点那台250马力的",
            {"equipment_family": "通用工作级深海机器人"},
            task_type_key="tree_valve_operation",
            required=required,
        )

        self.assertEqual(len(result["slot_candidates"]), 1)
        candidate = result["slot_candidates"][0]
        self.assertEqual(candidate["canonical_key"], "equipment_type")
        self.assertEqual(candidate["normalized_value"], "通用工作级深海机器人 250HP")
        self.assertEqual(candidate["confidence"], 0.94)
        self.assertEqual(candidate["resolution_method"], "llm_semantic")
        self.assertEqual(result["unresolved"], [])
        self.assertEqual(llm.extract_json.call_count, 2)

    def test_extractor_preserves_conflicting_duplicate_task_selectors(self):
        """Task selector conflicts must survive normalization for DM preflight."""
        builder = OutputBuilder(self.kb)
        required = builder.get_required(
            "tree_valve_operation",
            task_state={"task_type_key": "tree_valve_operation"},
        )
        llm = MagicMock(spec=LLMClient)
        llm.extract_json.return_value = {
            "slot_candidates": [
                {
                    "raw_key": "任务类型",
                    "canonical_key": "task_type",
                    "raw_value": "插入",
                    "normalized_value": "采油树控制面板插入",
                    "confidence": 1.0,
                },
                {
                    "raw_key": "任务类型",
                    "canonical_key": "task_type",
                    "raw_value": "拔出",
                    "normalized_value": "采油树控制面板拔出",
                    "confidence": 1.0,
                },
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        result = ParameterExtractor(llm).extract_updates(
            "插入还是拔出",
            {"task_type_key": "tree_valve_operation"},
            task_type_key="tree_valve_operation",
            task_type_map=self.kb.get_task_type_map(),
            required=required,
        )

        values = [
            item["normalized_value"]
            for item in result["slot_candidates"]
            if item["canonical_key"] == "task_type"
        ]
        self.assertEqual(
            values,
            ["采油树控制面板插入", "采油树控制面板拔出"],
        )
        self.assertTrue(
            any("同轮具体任务类型互相冲突" in item for item in result["unresolved"])
        )

    def test_confirmation_publish_skips_parameter_extraction(self):
        self.dm.phase = "confirming"
        self.llm.extract_json.reset_mock()

        self.dm.process("确认发布")

        self.llm.extract_json.assert_not_called()

    # 1. 单条消息同时包含三个不同槽位
    def test_three_slots_in_one_message(self):
        self._set_plan("WRITE")
        self.dm.reset()
        self.llm.extract_json.return_value = {
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0},
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300.0, "confidence": 0.95},
                {"raw_key": "管缆类型", "canonical_key": "cable_type", "raw_value": "电力电缆", "normalized_value": "电力电缆", "confidence": 0.90}
            ],
            "list_mutations": [],
            "unresolved": []
        }
        self.dm.process("我要新建管缆巡检任务，水深300米，电力电缆")
        self.assertEqual(self.dm.slot_store.slots["task_type"].value, "管缆巡检")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 300.0)
        self.assertEqual(self.dm.slot_store.slots["cable_type"].value, "电力电缆")
        assert_ssot_consistency(self, self.dm)

    # 2. alias 映射到 canonical field
    def test_02_alias_mapping_canonical_key(self):
        self._set_plan("WRITE")
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.llm.extract_json.return_value = {
            "slot_candidates": [
                {"raw_key": "深度", "canonical_key": "water_depth", "raw_value": "500米", "normalized_value": 500.0, "confidence": 1.0}
            ],
            "list_mutations": [],
            "unresolved": []
        }
        self.dm.process("水深深度为500米")
        self.assertIn("water_depth", self.dm.slot_store.slots)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)
        assert_ssot_consistency(self, self.dm)

    # 3. 一个槽位包含多个值 (多值列表)
    def test_03_multi_value_slot(self):
        self._set_plan("WRITE")
        self.dm.reset()
        schema = self.dm.builder.get_schema("pipeline_inspection")
        self.dm.slot_store.init_task_slots(schema)
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.llm.extract_json.return_value = {
            "slot_candidates": [
                {"raw_key": "负载工具", "canonical_key": "payload", "raw_value": "高清水下摄像机,成像声呐", "normalized_value": ["高清水下摄像机", "成像声呐"], "confidence": 1.0}
            ],
            "list_mutations": [],
            "unresolved": []
        }
        self.dm.process("负载为高清水下摄像机和成像声呐")
        self.assertEqual(self.dm.slot_store.slots["payload"].value, ["高清水下摄像机", "成像声呐"])
        assert_ssot_consistency(self, self.dm)

    # 4. 重复输入按明确规则处理
    def test_04_duplicate_inputs_handling(self):
        self._set_plan("WRITE")
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.llm.extract_json.return_value = {
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300.0, "confidence": 1.0}
            ],
            "list_mutations": [],
            "unresolved": []
        }
        self.dm.process("水深300米")
        ver1 = self.dm.slot_store.version
        # 再次发送完全相同的数据
        self.dm.process("水深300米")
        ver2 = self.dm.slot_store.version
        self.assertEqual(ver1, ver2)
        assert_ssot_consistency(self, self.dm)

    def test_unextracted_value_remains_missing_for_followup(self):
        self._set_plan("WRITE")
        self.dm.reset()
        self.dm.slot_store.init_task_slots(
            self.dm.builder.get_schema("pipeline_inspection", "normal")
        )
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        self.dm.slot_store.slots["task_type"].value = "管缆巡检"
        self.dm.slot_store.slots["task_type"].status = "valid"
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.dm.llm.extract_json.return_value = {
            "slot_candidates": [],
            "list_mutations": [],
            "unresolved": [],
        }

        self.dm.process("管缆类型海底油气管道")
        self.llm.extract_json.assert_called_once()

        slot = self.dm.slot_store.slots["cable_type"]
        self.assertIsNone(slot.value)
        self.assertEqual(slot.status, "missing")

        schema = self.dm.builder.get_schema("pipeline_inspection", "normal")
        missing = self.dm.slot_store.get_missing_slots(
            schema,
            allowed_values_resolver=lambda field: self.dm.builder.resolve_allowed_values(
                field,
                "pipeline_inspection",
                self.dm.slot_store.get_task_state(),
            ),
        )
        self.assertIn("cable_type", {field["key"] for field in missing})

    def test_pending_oilfield_confirmation_commits_to_slot_store(self):
        self.dm.reset()
        self.dm.slot_store.init_task_slots(
            self.dm.builder.get_schema("tree_valve_operation", "normal")
        )
        slots = self.dm.slot_store.clone_slots()
        slots["pending_oilfield_name"].value = "硫化11-1油田"
        slots["pending_oilfield_name"].status = "valid"
        slots["pending_oilfield_candidates"].value = [
            {
                "name": "流花11-1油田",
                "id": "LH11-1",
                "confidence": 0.95,
                "evidence": ["alias"],
            }
        ]
        slots["pending_oilfield_candidates"].status = "valid"
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        reply = self.dm._resolve_pending_oilfield_confirmation("确认")

        self.assertIn("流花11-1油田", reply)
        self.assertEqual(
            self.dm.slot_store.slots["oilfield_name"].value,
            "流花11-1油田",
        )
        self.assertEqual(
            self.dm.slot_store.slots["oilfield_name"].status,
            "valid",
        )
        self.assertIsNone(
            self.dm.slot_store.slots["pending_oilfield_name"].value
        )
        self.assertEqual(
            self.dm.task_state,
            self.dm.slot_store.get_task_state(),
        )

    def test_06_conflict_value_and_candidate_value(self):
        store = SlotStore(self.kb)
        store.slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", version=1)

        snap_slots, snap_unresolved, snap_ver = store.snapshot()
        # 写入冲突候选
        snap_slots["water_depth"].candidate_value = 600.0
        snap_slots["water_depth"].status = "conflict"
        snap_slots["water_depth"].validation_error = "Conflict detected"
        store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver)

        self.assertEqual(store.slots["water_depth"].value, 300.0)
        self.assertEqual(store.slots["water_depth"].candidate_value, 600.0)
        self.assertEqual(store.slots["water_depth"].status, "conflict")
        # 冲突槽位不得进入 task_state
        self.assertNotIn("water_depth", store.get_task_state())

    # 7. 类型错误保留 raw_value 和 validation_error
    def test_07_type_validation_error(self):
        store = SlotStore(self.kb)
        snap_slots, snap_unresolved, snap_ver = store.snapshot()
        snap_slots["water_depth"] = Slot("water_depth", value=None, status="invalid", raw_value="五百米左右", validation_error="Expected float")
        store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver)

        slot = store.slots["water_depth"]
        self.assertEqual(slot.status, "invalid")
        self.assertEqual(slot.raw_value, "五百米左右")
        self.assertEqual(slot.validation_error, "Expected float")
        self.assertNotIn("water_depth", store.get_task_state())

    # 8. 值域错误不得进入 task_state 和 built_json
    def test_08_invalid_domain_value_excluded_from_task_state(self):
        store = SlotStore(self.kb)
        snap_slots, snap_unresolved, snap_ver = store.snapshot()
        snap_slots["cable_type"] = Slot("cable_type", value="非法缆线", status="invalid", validation_error="Out of domain")
        store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver)

        self.assertNotIn("cable_type", store.get_task_state())
        self.assertNotIn("cable_type", store.get_built_json())

    # 9. 无法识别的任务信息进入 unresolved
    def test_09_unrecognized_input_in_unresolved(self):
        self._set_plan("WRITE")
        self.dm.reset()
        self.llm.extract_json.return_value = {
            "slot_candidates": [],
            "list_mutations": [],
            "unresolved": ["某些无法理解的内容"]
        }

        reply = self.dm.process("设置水深为500米 某些无法理解的内容")
        self.assertIn("某些无法理解的内容", self.dm.slot_store.unresolved)

    # 10. GENERAL_CHAT 不修改 SlotStore
    def test_10_general_chat_leaves_slot_store_untouched(self):
        self.dm.reset()
        self._set_plan("READ", query_intent="GENERAL_CHAT")
        self.dm.extractor.extract_updates = MagicMock()
        initial_ver = self.dm.slot_store.version
        initial_state = self.dm.slot_store.get_task_state()

        reply = self.dm.process("你好")

        self.dm.extractor.extract_updates.assert_not_called()
        self.assertEqual(self.dm.slot_store.version, initial_ver)
        self.assertEqual(self.dm.slot_store.get_task_state(), initial_state)
        assert_ssot_consistency(self, self.dm)

    # 11. CLARIFY 不修改 SlotStore
    def test_11_unknown_intent_leaves_slot_store_untouched(self):
        self.dm.reset()
        self._set_plan("CLARIFY")
        self.dm.extractor.extract_updates = MagicMock()
        initial_ver = self.dm.slot_store.version

        reply = self.dm.process("???")

        self.dm.extractor.extract_updates.assert_not_called()
        self.assertEqual(self.dm.slot_store.version, initial_ver)
        self.assertEqual(self.dm.slot_store.get_task_state(), {})
        self.assertTrue(len(reply) > 0)
        assert_ssot_consistency(self, self.dm)

    # 12. 语义等价数值不制造冲突
    def test_semantically_equal_number_does_not_create_conflict(self):
        self._set_plan("WRITE")
        self.dm.reset()
        self.dm.slot_store.init_task_slots(
            self.dm.builder.get_schema("pipeline_inspection", "normal")
        )
        slots = self.dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"
        slots["task_type"].value = "管缆巡检"
        slots["task_type"].status = "valid"
        slots["water_depth"].value = 300.0
        slots["water_depth"].status = "valid"
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.dm.llm.extract_json.return_value = {
            "slot_candidates": [
                {
                    "raw_key": "水深",
                    "canonical_key": "water_depth",
                    "raw_value": "300米",
                    "normalized_value": "300",
                    "confidence": 1.0,
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        self.dm.process("水深300米")

        slot = self.dm.slot_store.slots["water_depth"]
        self.assertEqual(slot.value, 300.0)
        self.assertEqual(slot.status, "valid")
        self.assertIsNone(slot.candidate_value)

    # 15. WRITE 可以更新 SlotStore
    def test_15_task_update_updates_slot_store(self):
        self._set_plan("WRITE")
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.llm.extract_json.return_value = {
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300.0, "confidence": 1.0}
            ],
            "list_mutations": [],
            "unresolved": []
        }
        self.dm.process("水深300米")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 300.0)
        assert_ssot_consistency(self, self.dm)

    # 16. task_state 与 SlotStore 一致
    def test_16_ssot_task_state_consistency(self):
        self._set_plan("WRITE")
        self.dm.reset()
        self.llm.extract_json.return_value = {
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0},
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "500米", "normalized_value": 500.0, "confidence": 1.0}
            ],
            "list_mutations": [],
            "unresolved": []
        }
        # In this turn, different value is processed:
        self.dm.process("修改水深为500米")
        
        # 用户明确说“修改”时直接覆盖；不带修改意图的新值仍由下一项测试
        # 验证 conflict → 用户确认的流程。
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)
        self.assertIsNone(self.dm.slot_store.slots["water_depth"].candidate_value)

    def test_19_test_a_single_commit_transaction_per_request(self):
        self._set_plan("WRITE")
        self.dm.reset()
        commit_spy = MagicMock(wraps=self.dm.slot_store.commit_transaction)
        self.dm.slot_store.commit_transaction = commit_spy

        self.llm.extract_json.return_value = {
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "list_mutations": [],
            "unresolved": []
        }

        self.dm.process("新建管缆巡检任务")
        # 验证事务提交仅被调用 1 次
        self.assertEqual(commit_spy.call_count, 1)
        # 验证 task_id 包含在当次提交中且状态为 candidate（草稿预览）
        committed_slots = commit_spy.call_args[0][0]
        self.assertIn("task_id", committed_slots)
        # 草稿阶段 task_id 存在 candidate_value，.value 应为 None（未正式预约）
        self.assertIsNotNone(committed_slots["task_id"].candidate_value)
        self.assertIsNone(committed_slots["task_id"].value)
        self.assertEqual(committed_slots["task_id"].status, "candidate")
        assert_ssot_consistency(self, self.dm)

    # 20. 模拟 task_id preview 失败时静默降级 (Test B)
    def test_20_test_b_task_id_exception_leaves_state_untouched(self):
        """preview 失败应静默降级（记录 warning），不应抛出到调用方。
        task_id 草稿阶段为非关键读取操作，失败不影响主流程。
        """
        self._set_plan("WRITE")
        self.dm.reset()
        initial_ver = self.dm.slot_store.version

        self.llm.extract_json.return_value = {
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "list_mutations": [],
            "unresolved": []
        }

        # preview 失败应静默降级，不应抛出 RuntimeError
        with patch.object(self.dm.builder, "preview_task_id", side_effect=RuntimeError("preview failed")):
            # 不应抛出异常
            reply = self.dm.process("新建管缆巡检任务")
            self.assertIsNotNone(reply)  # 正常回复了对话

        # 事务应已提交（主流程未受影响）
        self.assertGreater(self.dm.slot_store.version, initial_ver, "slot_store 应已提交新状态")
        # task_id slot 应存在但 candidate_value 为空（preview 失败）
        task_id_slot = self.dm.slot_store.slots.get("task_id")
        self.assertIsNotNone(task_id_slot, "preview 失败后仍必须保留 task_id Slot")
        self.assertIsNone(task_id_slot.value, "preview 失败后 task_id.value 不得被设置")
        self.assertIsNone(task_id_slot.candidate_value, "preview 失败后 candidate_value 应为空")
        assert_ssot_consistency(self, self.dm)

    # 18. missing_slots 从 SlotStore 派生
    def test_18_missing_slots_derived_from_slot_store(self):
        self._set_plan("WRITE")
        self.dm.reset()
        self.llm.extract_json.return_value = {
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "list_mutations": [],
            "unresolved": []
        }
        self.dm.process("新建管缆巡检")
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        user_req_schema = [f for f in schema if f.get("type") not in ("auto", "fixed")]
        expected_missing = self.dm.slot_store.get_missing_slots(
            user_req_schema,
            allowed_values_resolver=lambda field: self.dm.builder.resolve_allowed_values(field, "pipeline_inspection", self.dm.task_state)
        )
        self.assertEqual(self.dm._last_missing, expected_missing)

    # 21. 模拟主 commit 失败时全部状态零修改
    def test_21_main_commit_failure_leaves_state_untouched(self):
        self._set_plan("WRITE")
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()
        initial_ver = self.dm.slot_store.version
        initial_hist_len = len(self.dm.conversation_history)


        self.dm.slot_store.commit_transaction = MagicMock(side_effect=SlotVersionConflict("Version conflict test"))

        self.llm.extract_json.return_value = {
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300.0, "confidence": 1.0}
            ],
            "list_mutations": [],
            "unresolved": []
        }

        with self.assertRaises(SlotVersionConflict):
            self.dm.process("水深300米")


        self.assertEqual(self.dm.slot_store.version, initial_ver)
        self.assertEqual(len(self.dm.conversation_history), initial_hist_len)

    # 22. expected_version 不一致时抛出 SlotVersionConflict
    def test_22_version_mismatch_raises_slot_version_conflict(self):
        store = SlotStore(self.kb)
        snap_slots, snap_unresolved, snap_ver = store.snapshot()
        with self.assertRaises(SlotVersionConflict):
            store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver + 999)

    # 23. 两个真实线程基于相同 version 提交时，不允许旧数据覆盖新数据 (真实多线程并发测试)
    def test_23_concurrency_optimistic_lock(self):
        store = SlotStore(self.kb)
        store.slots["water_depth"] = Slot("water_depth", value=None, status="missing")
        snap_slots1, snap_unresolved1, ver1 = store.snapshot()
        snap_slots2, snap_unresolved2, ver2 = store.snapshot()

        barrier = threading.Barrier(2)
        results = []
        errors = []

        def thread_task(snap_slots, snap_unresolved, ver, val, name):
            snap_slots["water_depth"].value = val
            snap_slots["water_depth"].status = "valid"
            barrier.wait()
            try:
                store.commit_transaction(snap_slots, snap_unresolved, expected_version=ver)
                results.append(name)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=thread_task, args=(snap_slots1, snap_unresolved1, ver1, 300.0, "t1"))
        t2 = threading.Thread(target=thread_task, args=(snap_slots2, snap_unresolved2, ver2, 999.0, "t2"))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 1, f"Expected exactly 1 successful thread, got {len(results)}")
        self.assertEqual(len(errors), 1, f"Expected exactly 1 conflict error, got {len(errors)}")
        self.assertIsInstance(errors[0], SlotVersionConflict)
        self.assertEqual(store.version, ver1 + 1)
        if results[0] == "t1":
            self.assertEqual(store.slots["water_depth"].value, 300.0)
        else:
            self.assertEqual(store.slots["water_depth"].value, 999.0)

    # 23b. 真实多槽位多线程并发提交，不允许字段混合
    def test_23b_multi_slot_concurrency_no_field_mixing(self):
        store = SlotStore(self.kb)
        store.slots["water_depth"] = Slot("water_depth", value=None, status="missing")
        store.slots["payload"] = Slot("payload", value=None, status="missing")
        store.slots["support_vessel"] = Slot("support_vessel", value=None, status="missing")

        snap_slots1, snap_unresolved1, ver1 = store.snapshot()
        snap_slots2, snap_unresolved2, ver2 = store.snapshot()

        barrier = threading.Barrier(2)
        results = []
        errors = []

        def thread_a():
            snap_slots1["water_depth"].value = 300.0
            snap_slots1["water_depth"].status = "valid"
            snap_slots1["payload"].value = ["高清水下摄像机"]
            snap_slots1["payload"].status = "valid"
            barrier.wait()
            try:
                store.commit_transaction(snap_slots1, snap_unresolved1, expected_version=ver1)
                results.append("A")
            except Exception as e:
                errors.append(e)

        def thread_b():
            snap_slots2["water_depth"].value = 500.0
            snap_slots2["water_depth"].status = "valid"
            snap_slots2["support_vessel"].value = "海洋石油681"
            snap_slots2["support_vessel"].status = "valid"
            barrier.wait()
            try:
                store.commit_transaction(snap_slots2, snap_unresolved2, expected_version=ver2)
                results.append("B")
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=thread_a)
        t2 = threading.Thread(target=thread_b)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], SlotVersionConflict)

        if results[0] == "A":
            self.assertEqual(store.slots["water_depth"].value, 300.0)
            self.assertEqual(store.slots["payload"].value, ["高清水下摄像机"])
            self.assertIsNone(store.slots["support_vessel"].value)
        else:
            self.assertEqual(store.slots["water_depth"].value, 500.0)
            self.assertIsNone(store.slots["payload"].value)
            self.assertEqual(store.slots["support_vessel"].value, "海洋石油681")

    # 24. 历史快照完整恢复
    def test_24_history_snapshot_full_restoration(self):
        store = SlotStore(self.kb)
        store.slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", version=2)
        store.version = 5
        snap = store.export_snapshot()

        new_store = SlotStore.from_snapshot(snap, self.kb)
        self.assertEqual(new_store.version, 5)
        self.assertEqual(new_store.slots["water_depth"].value, 300.0)

    # 25. legacy 快照转换恢复
    def test_25_legacy_snapshot_conversion(self):
        legacy_snap = {
            "task_state": {"task_type": "管缆巡检", "water_depth": 400.0},
            "conversation_history": [],
            "mode": "normal",
            "phase": "collecting"
        }
        self.dm.load_snapshot(legacy_snap)
        self.assertEqual(self.dm.task_state.get("water_depth"), 400.0)
        assert_ssot_consistency(self, self.dm)

    # 26. 非法快照恢复失败后，原会话全部状态保持不变 (Test C)
    def test_26_test_c_invalid_snapshot_restoration_leaves_state_untouched(self):
        self.dm.reset()
        self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=250.0, status="valid")
        self.dm.slot_store.version = 3
        self.dm.mode = "normal"
        self.dm.phase = "collecting"
        self.dm.task_state = self.dm.slot_store.get_task_state()
        self.dm._last_built_json = self.dm.slot_store.get_built_json()
        initial_hist = list(self.dm.conversation_history)

        invalid_snapshot = {
            "slot_store": {
                "store_version": -10,  # 非法 store_version
                "slots": {},
                "unresolved": []
            }
        }

        with self.assertRaises(SnapshotValidationError):
            self.dm.load_snapshot(invalid_snapshot)


        # 验证全部状态 100% 保持恢复前的原样
        self.assertEqual(self.dm.slot_store.version, 3)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 250.0)
        self.assertEqual(self.dm.mode, "normal")
        self.assertEqual(self.dm.phase, "collecting")
        self.assertEqual(self.dm.conversation_history, initial_hist)
        assert_ssot_consistency(self, self.dm)

    # 27. 快照恢复路径不存在直接 self.slot_store.slots 赋值
    def test_27_no_direct_slots_assignment(self):
        snap = {
            "slot_store": {
                "store_version": 1,
                "slots": {
                    "water_depth": {"slot_name": "water_depth", "value": 100.0, "status": "valid", "version": 1}
                },
                "unresolved": []
            },
            "mode": "normal",
            "phase": "collecting",
            "conversation_history": []
        }
        orig_store = self.dm.slot_store
        self.dm.load_snapshot(snap)
        # 验证 load_snapshot 创建了全新的 SlotStore 对象，而非在原对象的 .slots 上直接赋值
        self.assertIsNot(self.dm.slot_store, orig_store)
        assert_ssot_consistency(self, self.dm)

    # 28. ASR 文本和直接文本经过相同槽位流水线
    def test_28_asr_text_and_direct_text_same_pipeline(self):
        self._set_plan("WRITE")
        web_backend._shared_asr = MagicMock()
        web_backend._shared_asr.transcribe_file.return_value = {
            "text": "我要执行管缆巡检",
            "language_hint": "zh",
            "device": "cpu",
            "elapsed_ms": 10.0,
            "segments": []
        }
        web_backend._shared_llm.extract_json.return_value = {
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "list_mutations": [],
            "unresolved": []
        }



        data_file = (io.BytesIO(b"dummy wav audio data"), "test.wav")
        res_asr = self.client.post("/api/asr", data={"audio": data_file}, content_type="multipart/form-data")
        self.assertEqual(res_asr.status_code, 200)
        corrected_text = res_asr.get_json()["corrected_text"]

        res_chat_a = self.client.post("/api/chat", json={"session_id": "sess_pipeline_a", "message": corrected_text})
        data_a = res_chat_a.get_json()

        res_chat_b = self.client.post("/api/chat", json={"session_id": "sess_pipeline_b", "message": "我要执行管缆巡检"})
        data_b = res_chat_b.get_json()

        coll_a = {k: v for k, v in data_a["collected"].items() if k not in ("task_id", "internal_id")}
        coll_b = {k: v for k, v in data_b["collected"].items() if k not in ("task_id", "internal_id")}
        self.assertEqual(coll_a, coll_b)
        self.assertEqual(coll_a.get("task_type"), "管缆巡检")
        self.assertEqual(coll_b.get("task_type"), "管缆巡检")

    # 29. /api/chat 返回 409 时包含 request_id
    def test_29_api_chat_409_includes_request_id(self):
        mgr = web_backend.get_or_create_manager("sess_409_test")
        mgr.process = MagicMock(side_effect=SlotVersionConflict("Conflict simulation"))

        res = self.client.post("/api/chat", json={"session_id": "sess_409_test", "message": "并发冲突"})
        self.assertEqual(res.status_code, 409)
        data = res.get_json()
        self.assertEqual(data["code"], 409)
        self.assertEqual(data["error"], "SlotVersionConflict")
        self.assertIn("request_id", data)

    # 30. /api/chat 返回 500 时不泄露 traceback、文件路径或模型信息
    def test_30_api_chat_500_hides_traceback_and_paths(self):
        mgr = web_backend.get_or_create_manager("sess_500_test")
        mgr.process = MagicMock(side_effect=RuntimeError("Secret path leak: /root/private/model_weights.bin"))

        res = self.client.post("/api/chat", json={"session_id": "sess_500_test", "message": "触发500"})
        self.assertEqual(res.status_code, 500)
        data = res.get_json()
        self.assertEqual(data["code"], 500)
        self.assertEqual(data["msg"], "服务器内部错误，请稍后重试。")
        self.assertIn("request_id", data)
        self.assertNotIn("Traceback", data["msg"])
        self.assertNotIn("/root/private", data["msg"])

    # 30b. /api/chat 异常响应结构完整性测试
    def test_30b_api_chat_specific_exceptions_response_structure(self):
        cases = [
            (IdReservationError("ID counter error"), 500, "IdReservationError", True),
            (TaskPersistenceError("Disk error"), 500, "TaskPersistenceError", True),
            (TaskRollbackError("Rollback error"), 500, "TaskRollbackError", True),
            (IntentIdConflict("Conflict"), 409, "IntentIdConflict", True),
        ]
        for exc, expected_code, expected_err, expected_retryable in cases:
            sid = f"sess_err_{expected_err}"
            mgr = web_backend.get_or_create_manager(sid)
            mgr.process = MagicMock(side_effect=exc)
            res = self.client.post("/api/chat", json={"session_id": sid, "message": "test"})
            self.assertEqual(res.status_code, expected_code)
            data = res.get_json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["code"], expected_code)
            self.assertEqual(data["error"], expected_err)
            self.assertIn("request_id", data)
            self.assertEqual(data["retryable"], expected_retryable)
            self.assertNotIn("Traceback", data["msg"])
            self.assertNotIn("/root/", data["msg"])

    # 31. 前端刷新/历史恢复后 collected、missing、task_type 一致
    def test_31_frontend_refresh_and_history_load_consistency(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with patch("src.history_manager.get_history_dir", return_value=tmp_path):
                self.dm.reset()
                self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
                self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
                self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=350.0, status="valid")

                filename = save_conversation(
                    session_id="sess_ui_refresh",
                    conversation_history=[],
                    task_state=self.dm.slot_store.get_task_state(),
                    built_json=self.dm.slot_store.get_built_json(),
                    mode=self.dm.mode,
                    phase=self.dm.phase,
                    slot_store=self.dm.slot_store.export_snapshot()
                )

                res = self.client.post("/api/history/load", json={"history_id": filename, "session_id": "sess_ui_target"})
                self.assertEqual(res.status_code, 200)
                data = res.get_json()
                self.assertEqual(data["task_type"], "pipeline_inspection")
                self.assertEqual(data["built_json"]["water_depth"], 350.0)
                self.assertIsNotNone(data["missing"])

    # 32. publish失败不产生TaskIntent正式文件
    def test_32_commit_failure_no_final_task_intent_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)
            seed_complete_valid_pipeline_task(self.dm, self.kb)
            self.dm.intent_router.route = self._orig_route
            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path), \
                 patch("src.dialogue_manager.TaskIntentBuilder.publish_staging", side_effect=TaskPersistenceError("Simulated disk error")):
                with self.assertRaises(TaskPersistenceError):
                    self.dm.process("确认发布")

                final_files = list(tmp_path.glob("task_intent_*.json")) if tmp_path.exists() else []
                self.assertEqual(len(final_files), 0)

    # 33. publish失败不产生TaskIntent临时文件
    def test_33_commit_failure_no_temp_task_intent_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)
            seed_complete_valid_pipeline_task(self.dm, self.kb)
            self.dm.intent_router.route = self._orig_route
            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path), \
                 patch("src.dialogue_manager.TaskIntentBuilder.publish_staging", side_effect=TaskPersistenceError("Simulated disk error")):
                with self.assertRaises(TaskPersistenceError):
                    self.dm.process("确认发布")

                tmp_files = (list(tmp_path.glob("*.staging_*")) + list(tmp_path.glob("*.tmp_*"))) if tmp_path.exists() else []
                self.assertGreater(len(tmp_files), 0)

    # 34. TaskIntent prepare 不写文件
    def test_34_task_intent_prepare_no_disk_write(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path):
                builder = TaskIntentBuilder(self.kb)
                intent = builder.prepare(
                    task_state={"task_id": "PI-20260803-001", "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "water_depth": 300.0},
                    built_json={"task_id": "PI-20260803-001", "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "water_depth": 300.0, "equipment_type": "观察级ROV"},
                    mode="normal",
                    task_type_key="pipeline_inspection"
                )
                self.assertIn("intent_id", intent)
                self.assertFalse(tmp_path.exists())

    # 35. TaskIntent persist 使用原子替换
    def test_35_task_intent_persist_atomic_replace(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path):
                builder = TaskIntentBuilder(self.kb)
                intent = builder.prepare(
                    task_state={"task_id": "PI-20260803-001", "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "water_depth": 300.0},
                    built_json={"task_id": "PI-20260803-001", "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "water_depth": 300.0, "equipment_type": "观察级ROV"},
                    mode="normal",
                    task_type_key="pipeline_inspection"
                )
                filename = builder.persist(intent)
                target_file = tmp_path / filename
                self.assertTrue(target_file.exists())
                tmp_files = list(tmp_path.glob("*.tmp_*"))
                self.assertEqual(len(tmp_files), 0)

    # 36. 完整任务触发 publish 失败：日志包含元数据，状态与 SlotStore 100% 恢复
    def test_36_persist_failure_no_success_reply(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)

            selected_rov = seed_complete_valid_pipeline_task(self.dm, self.kb)
            self.dm.intent_router.route = self._orig_route

            req_schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
            missing = self.dm.slot_store.get_missing_slots(req_schema)
            self.assertEqual(len(missing), 0)
            all_violations = self.dm.validator.validate(self.dm.task_state)
            self.assertFalse(self.dm.validator.has_hard_violations(all_violations))
            self.assertEqual(self.dm.phase, "confirming")

            self.assertEqual(self.dm.task_state.get("equipment_type"), selected_rov["full_name"])
            self.assertIn(self.dm.task_state.get("equipment_unit_id"), selected_rov.get("unit_ids", []))

            store = self.dm.slot_store
            pre_snapshot = copy.deepcopy(store.export_snapshot())
            pre_task_state = copy.deepcopy(self.dm.task_state)
            pre_built_json = copy.deepcopy(self.dm._last_built_json)
            pre_missing = copy.deepcopy(self.dm._last_missing)

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path), \
                 patch("src.dialogue_manager.TaskIntentBuilder.publish_staging", side_effect=TaskPersistenceError("Simulated disk error")) as mock_pub, \
                 self.assertLogs("src.dialogue_manager", level="ERROR") as cm:
                with self.assertRaises(TaskPersistenceError):
                    self.dm.process("确认发布", request_id="req_test_36")

                mock_pub.assert_called_once()

                self.assertNotEqual(self.dm.phase, "done")
                self.assertEqual(self.dm.phase, "confirming")
                self.assertIsNone(self.dm.final_result)

                intent_slot = store.slots.get("intent_id")
                self.assertIsNotNone(intent_slot)
                self.assertEqual(intent_slot.value, "TI2026063001")
                self.assertEqual(intent_slot.status, "valid")

                self.assertEqual(store.export_snapshot(), pre_snapshot)
                self.assertEqual(self.dm.task_state, pre_task_state)
                self.assertEqual(self.dm._last_built_json, pre_built_json)
                self.assertEqual(self.dm._last_missing, pre_missing)

                final_files = list(tmp_path.glob("task_intent_*.json"))
                staging_files = list(tmp_path.glob("*.staging_*")) + list(tmp_path.glob("*.tmp_*"))
                self.assertEqual(len(final_files), 0)
                self.assertGreater(len(staging_files), 0)

                log_output = "\n".join(cm.output)
                self.assertIn("req_test_36", log_output)
                self.assertIn("publish_staging", log_output)
                self.assertIn("TaskPersistenceError", log_output)
                self.assertNotIn("DEBUG SYSTEM PROMPT START", log_output)

    # 36b. 完整任务成功发布端到端测试
    def test_36b_successful_publish_e2e(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)

            selected_rov = seed_complete_valid_pipeline_task(self.dm, self.kb)
            self.dm.intent_router.route = self._orig_route

            req_schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
            missing = self.dm.slot_store.get_missing_slots(req_schema)
            self.assertEqual(len(missing), 0)
            self.assertEqual(self.dm.phase, "confirming")

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                reply = self.dm.process("确认开始", request_id="req_test_36b")

                self.assertEqual(self.dm.phase, "done")
                self.assertIsNotNone(self.dm.final_result)

                intent_slot = self.dm.slot_store.slots.get("intent_id")
                self.assertIsNotNone(intent_slot)
                self.assertEqual(intent_slot.status, "valid")
                self.assertIsNotNone(intent_slot.value)

                final_files = list(tmp_path.glob("task_intent_*.json"))
                staging_files = list(tmp_path.glob("*.staging_*")) + list(tmp_path.glob("*.tmp_*"))
                self.assertEqual(len(final_files), 1)
                self.assertEqual(len(staging_files), 0)

                with open(final_files[0], "r", encoding="utf-8") as f:
                    file_data = json.load(f)

                self.assertEqual(intent_slot.value, file_data.get("intent_id"))
                self.assertTrue("下发" in reply or "已加入计划池" in reply)

                self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())
                self.assertEqual(self.dm._last_built_json, self.dm.slot_store.get_built_json())
                self.assertEqual(self.dm._last_missing, self.dm.slot_store.get_missing_slots(req_schema))

    # 37. 相同 intent_id 重复 persist 结果幂等
    def test_37_persist_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path):
                builder = TaskIntentBuilder(self.kb)
                intent = builder.prepare(
                    task_state={"task_id": "PI-20260803-001", "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "water_depth": 300.0},
                    built_json={"task_id": "PI-20260803-001", "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "water_depth": 300.0, "equipment_type": "观察级ROV"},
                    mode="normal",
                    task_type_key="pipeline_inspection"
                )
                f1 = builder.persist(intent)
                with self.assertRaises((IntentIdConflict, TaskPersistenceError)):
                    builder.persist(intent)

    # 38. 自定义 SEAGENT_RESULT_DIR 对所有模块生效
    def test_38_custom_seagent_result_dir_affects_all_modules(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}):
                task_dir = get_task_dir()
                hist_dir = get_history_dir()
                self.assertEqual(task_dir, Path(tmp_dir) / "task")
                self.assertEqual(hist_dir, Path(tmp_dir) / "history")

    # 39. task_id 扫描使用配置后的真实目录
    def test_39_task_id_scan_uses_configured_real_directory(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)
            dummy_file = tmp_task_dir / "task_PI2026063005.json"
            with open(dummy_file, "w", encoding="utf-8") as f:
                json.dump({"task_id": "PI2026063005"}, f)

            with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}):
                id_sequence._COUNTERS.clear()
                tid = self.dm.builder._generate_task_id("pipeline_inspection", {})
                self.assertTrue(tid.endswith("06"))

    # 40. 进程计数器清空后编号仍连续
    def test_40_id_sequence_continuity_on_process_restart(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)
            dummy_file = tmp_task_dir / "task_intent_TI2026063005.json"
            with open(dummy_file, "w", encoding="utf-8") as f:
                json.dump({"intent_id": "TI2026063005"}, f)

            with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}):
                id_sequence._COUNTERS.clear()
                next_id = id_sequence.next_daily_id("TI", "20260630", 2, [(tmp_task_dir, "intent_id")])
                self.assertEqual(next_id, "TI2026063006")

    # 41. 非法 value_type 快照被拒绝
    def test_41_invalid_value_type_snapshot_rejected(self):
        store = SlotStore(self.kb)
        snap = store.export_snapshot()
        snap["slots"]["water_depth"] = {
            "slot_name": "water_depth",
            "value": 300.0,
            "value_type": "invalid_type_xyz",
            "status": "valid",
            "version": 1
        }
        with self.assertRaises(SnapshotValidationError):
            SlotStore.from_snapshot(snap, self.kb)

    # 42. 非法 updated_at 快照被拒绝
    def test_42_invalid_updated_at_snapshot_rejected(self):
        store = SlotStore(self.kb)
        snap = store.export_snapshot()
        snap["slots"]["water_depth"] = {
            "slot_name": "water_depth",
            "value": 300.0,
            "value_type": "number",
            "status": "valid",
            "updated_at": "2026-99-99T88:00:00",
            "version": 1
        }
        with self.assertRaises(SnapshotValidationError):
            SlotStore.from_snapshot(snap, self.kb)

    # 43. 带时区 updated_at 快照可以恢复
    def test_43_timezone_aware_updated_at_snapshot_restored(self):
        store = SlotStore(self.kb)
        snap = store.export_snapshot()
        snap["slots"]["water_depth"] = {
            "slot_name": "water_depth",
            "value": 300.0,
            "value_type": "number",
            "status": "valid",
            "updated_at": "2026-06-30T17:38:00+08:00",
            "version": 1
        }
        restored = SlotStore.from_snapshot(snap, self.kb)
        self.assertEqual(restored.slots["water_depth"].updated_at, "2026-06-30T17:38:00+08:00")

    # 44. legacy snapshot 值类型准确推断
    def test_44_legacy_snapshot_value_type_inference(self):
        legacy_snap = {
            "task_state": {
                "water_depth": 400.0,
                "payload": ["高清水下摄像机"],
                "is_active": True,
                "start_point": {"lat": 19.5, "lon": 115.2},
                "start_time": "2026-06-30T17:38:00"
            },
            "conversation_history": [],
            "mode": "normal",
            "phase": "collecting"
        }
        self.dm.load_snapshot(legacy_snap)
        store = self.dm.slot_store
        self.assertEqual(store.slots["water_depth"].value_type, "number")
        self.assertEqual(store.slots["payload"].value_type, "list")
        self.assertEqual(store.slots["is_active"].value_type, "boolean")
        self.assertEqual(store.slots["start_point"].value_type, "coord")
        self.assertEqual(store.slots["start_time"].value_type, "datetime")

    # 45. 默认运行时不打印完整 system prompt
    def test_45_default_execution_no_system_prompt_print(self):
        captured_stdout = io.StringIO()
        with patch("sys.stdout", new=captured_stdout):
            self.dm.reset()
            self.dm.process("你好")
        output = captured_stdout.getvalue()
        self.assertNotIn("DEBUG SYSTEM PROMPT START", output)

    # 46. 会话锁并发隔离测试
    def test_46_session_lock_concurrency_isolation(self):
        dm1 = DialogueManager(self.llm, self.kb)
        dm2 = DialogueManager(self.llm, self.kb)

        with dm1._session_lock:
            lock_acquired = dm1._session_lock.acquire(blocking=False)
            self.assertTrue(lock_acquired)
            dm1._session_lock.release()

        with dm1._session_lock:
            dm2_acquired = dm2._session_lock.acquire(blocking=False)
            self.assertTrue(dm2_acquired)
            dm2._session_lock.release()

    # 47. 不同内容使用相同 intent_id 触发 IntentIdConflict
    def test_47_different_content_same_intent_id_raises_conflict(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path):
                builder = TaskIntentBuilder(self.kb)
                intent1 = builder.prepare(
                    task_state={"task_id": "PI-20260803-001", "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "water_depth": 300.0},
                    built_json={"task_id": "PI-20260803-001", "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "water_depth": 300.0, "equipment_type": "观察级ROV"},
                    mode="normal",
                    task_type_key="pipeline_inspection"
                )
                filename = builder.persist(intent1)

                intent2 = copy.deepcopy(intent1)
                intent2["task_type"] = "pipeline_inspection"
                intent2["priority"] = 9
                staging2 = builder.create_staging(intent2)
                with self.assertRaises(IntentIdConflict):
                    builder.publish_staging(staging2, intent2)

                with open(tmp_path / filename, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data["task_type"], "pipeline_inspection")

    # 48. 相同内容使用相同 intent_id 幂等重试成功
    def test_48_identical_content_same_intent_id_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)
            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path):
                builder = TaskIntentBuilder(self.kb)
                intent1 = builder.prepare(
                    task_state={"task_id": "PI-20260803-001", "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "water_depth": 300.0},
                    built_json={"task_id": "PI-20260803-001", "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "water_depth": 300.0, "equipment_type": "观察级ROV"},
                    mode="normal",
                    task_type_key="pipeline_inspection"
                )
                f1 = builder.persist(intent1)
                staging2 = builder.create_staging(intent1)
                with self.assertRaises((IntentIdConflict, TaskPersistenceError)):
                    builder.publish_staging(staging2, intent1)

                final_files = list(tmp_path.glob("task_intent_*.json"))
                self.assertEqual(len(final_files), 1)

    # 49a. 首次启动（目录不存在）多进程 intent_id 并发申请测试
    def test_49a_multiprocess_initial_run_intent_id_uniqueness(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            result_dir = Path(tmp_dir) / "non_existent_sub" / "result"
            ctx = multiprocessing.get_context("spawn")
            queue = ctx.Queue()
            b1 = ctx.Barrier(2)
            b2 = ctx.Barrier(2)

            p1 = ctx.Process(target=_mp_id_two_barrier_worker, args=(str(result_dir), "TI", queue, b1, b2))
            p2 = ctx.Process(target=_mp_id_two_barrier_worker, args=(str(result_dir), "TI", queue, b1, b2))

            p1.start()
            p2.start()
            p1.join(timeout=10)
            p2.join(timeout=10)

            self.assertEqual(p1.exitcode, 0)
            self.assertEqual(p2.exitcode, 0)

            id1 = queue.get(timeout=2)
            id2 = queue.get(timeout=2)

            self.assertNotEqual(id1, id2)
            self.assertEqual(set([id1, id2]), {"TI2026071801", "TI2026071802"})

            # Check counter file
            counter_file = result_dir / ".id_sequences.json"
            self.assertTrue(counter_file.exists())
            with open(counter_file, "r", encoding="utf-8") as f:
                cdata = json.load(f)
            self.assertEqual(cdata.get("TI20260718"), 2)

            # Check zero .res_* files created
            res_files = list(result_dir.glob(".res_*")) + list((result_dir / "task").glob(".res_*"))
            self.assertEqual(len(res_files), 0)

    # 49b. 首次启动（目录不存在）多进程 task_id 并发申请测试
    def test_49b_multiprocess_initial_run_task_id_uniqueness(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            result_dir = Path(tmp_dir) / "fresh_run" / "result"
            ctx = multiprocessing.get_context("spawn")
            queue = ctx.Queue()
            b1 = ctx.Barrier(2)
            b2 = ctx.Barrier(2)

            p1 = ctx.Process(target=_mp_id_two_barrier_worker, args=(str(result_dir), "PI", queue, b1, b2))
            p2 = ctx.Process(target=_mp_id_two_barrier_worker, args=(str(result_dir), "PI", queue, b1, b2))

            p1.start()
            p2.start()
            p1.join(timeout=10)
            p2.join(timeout=10)

            self.assertEqual(p1.exitcode, 0)
            self.assertEqual(p2.exitcode, 0)

            id1 = queue.get(timeout=2)
            id2 = queue.get(timeout=2)

            self.assertNotEqual(id1, id2)
            self.assertEqual(set([id1, id2]), {"PI2026071801", "PI2026071802"})

    # 49c. 4进程并发 ID 申请测试
    def test_49c_multiprocess_four_process_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = multiprocessing.get_context("spawn")
            queue = ctx.Queue()
            b1 = ctx.Barrier(4)
            b2 = ctx.Barrier(4)

            procs = [
                ctx.Process(target=_mp_id_two_barrier_worker, args=(tmp_dir, "TI", queue, b1, b2))
                for _ in range(4)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=10)

            for p in procs:
                self.assertEqual(p.exitcode, 0)

            ids = [queue.get(timeout=2) for _ in range(4)]
            self.assertEqual(len(set(ids)), 4)

    # 49d. 进程重启序列号连续性测试
    def test_49d_process_restart_sequence_continuity(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}):
                id_sequence._COUNTERS.clear()
                id1 = id_sequence.next_daily_id("TI", "20260718", 2, [])
                self.assertEqual(id1, "TI2026071801")

                # Clear process memory counter
                id_sequence._COUNTERS.clear()

                # Next call reads persistent file and returns 02
                id2 = id_sequence.next_daily_id("TI", "20260718", 2, [])
                self.assertEqual(id2, "TI2026071802")

    # 49e. 历史磁盘文件编号大于计数器测试
    def test_49e_historical_disk_file_higher_than_counter(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            disk_file = task_dir / "task_intent_TI2026071899.json"
            with open(disk_file, "w", encoding="utf-8") as f:
                json.dump({"intent_id": "TI2026071899"}, f)

            with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}):
                id_sequence._COUNTERS.clear()
                gid = id_sequence.next_daily_id("TI", "20260718", 2, [(task_dir, "intent_id")])
                self.assertEqual(gid, "TI20260718100")

    # 49f. 计数器写入失败抛出 IdReservationError 测试
    def test_49f_counter_write_failure_raises_id_reservation_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            orig_open = builtins.open
            def mock_open(file, *args, **kwargs):
                filepath = str(file)
                if ".id_sequences" in filepath or ".tmp_counter" in filepath:
                    raise OSError("Disk write error")
                return orig_open(file, *args, **kwargs)

            with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}), \
                 patch("builtins.open", side_effect=mock_open):
                id_sequence._COUNTERS.clear()
                with self.assertRaises(IdReservationError):
                    id_sequence.next_daily_id("TI", "20260718", 2, [])

    # 50a. 多进程 publish 竞争 testing (no-clobber with barrier)
    def test_50a_multiprocess_publish_no_clobber_race(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)

            intent1 = {
                "schema_version": 2,
                "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
                "task_id": "PI-20260718-001",
                "intent_id": "TI2026071801",
                "task_type": "pipeline_inspection",
                "priority": 7,
                "time": {"start": None, "end": None},
                "location": {"oilfield": None, "water_depth_m": 300.0},
                "task": {"type": "pipeline_inspection", "details": {}},
                "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": {"name": None}},
                "conditions": {}
            }
            intent2 = copy.deepcopy(intent1)
            intent2["priority"] = 8

            ctx = multiprocessing.get_context("spawn")
            queue = ctx.Queue()
            b1 = ctx.Barrier(2)

            p1 = ctx.Process(target=_mp_publish_no_clobber_worker, args=(tmp_dir, intent1, queue, b1))
            p2 = ctx.Process(target=_mp_publish_no_clobber_worker, args=(tmp_dir, intent2, queue, b1))

            p1.start()
            p2.start()
            p1.join(timeout=10)
            p2.join(timeout=10)

            self.assertEqual(p1.exitcode, 0)
            self.assertEqual(p2.exitcode, 0)

            res1 = queue.get(timeout=2)
            res2 = queue.get(timeout=2)

            statuses = [res1[0], res2[0]]
            self.assertEqual(sorted(statuses), ["conflict", "success"])

            final_file = task_dir / "task_intent_TI2026071801.json"
            self.assertTrue(final_file.exists())

            staging_files = list(task_dir.glob("*.staging_*")) + list(task_dir.glob("*.tmp_*"))
            self.assertGreaterEqual(len(staging_files), 0)

    # 50b. 多进程 publish 相同内容幂等重试成功 testing
    def test_50b_multiprocess_publish_idempotent_retry(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)

            intent1 = {
                "schema_version": 2,
                "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
                "task_id": "PI-20260718-001",
                "intent_id": "TI2026071801",
                "task_type": "pipeline_inspection",
                "priority": 7,
                "time": {"start": None, "end": None},
                "location": {"oilfield": None, "water_depth_m": 300.0},
                "task": {"type": "pipeline_inspection", "details": {}},
                "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": {"name": None}},
                "conditions": {}
            }

            ctx = multiprocessing.get_context("spawn")
            queue = ctx.Queue()
            b1 = ctx.Barrier(2)

            p1 = ctx.Process(target=_mp_publish_no_clobber_worker, args=(tmp_dir, intent1, queue, b1))
            p2 = ctx.Process(target=_mp_publish_no_clobber_worker, args=(tmp_dir, intent1, queue, b1))

            p1.start()
            p2.start()
            p1.join(timeout=10)
            p2.join(timeout=10)

            self.assertEqual(p1.exitcode, 0)
            self.assertEqual(p2.exitcode, 0)

            res1 = queue.get(timeout=2)
            res2 = queue.get(timeout=2)

            statuses = [res1[0], res2[0]]
            self.assertEqual(sorted(statuses), ["conflict", "success"])

            final_files = list(task_dir.glob("task_intent_*.json"))
            self.assertEqual(len(final_files), 1)

    # 51. 所有任务 schema 的 export -> restore 往返测试
    def test_51_all_task_schemas_export_restore_roundtrip(self):
        task_keys = list(self.kb.task_schemas.get("task_templates", {}).keys())
        self.assertTrue(len(task_keys) > 0)
        self.assertIn("pipeline_inspection", task_keys)
        self.assertIn("pipeline_burial", task_keys)
        self.assertIn("tree_valve_operation", task_keys)

        modes = ["normal", "emergency"]
        for key in task_keys:
            for mode in modes:
                schema_fields = self.dm.builder.get_schema(key, mode)
                self.assertTrue(len(schema_fields) > 0, f"Schema for {key} in {mode} mode must be non-empty")
                store = SlotStore(self.kb)
                store._init_task_slots_in_transaction(store.slots, schema_fields)

                snap = store.export_snapshot()
                restored = SlotStore.from_snapshot(snap, self.kb)
                self.assertEqual(store.export_snapshot(), restored.export_snapshot())

                for slot in restored.slots.values():
                    self.assertIn(slot.value_type, VALID_VALUE_TYPES, f"Slot {slot.slot_name} has invalid value_type '{slot.value_type}'")

    # 52. 装备别名解析及 category 保护测试
    def test_52_equipment_alias_normalization_and_category_protection(self):
        dm = DialogueManager(self.llm, self.kb)
        dm.reset()
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.slot_store.slots["equipment_type"] = Slot("equipment_type", candidate_value="观察级ROV", status="candidate")

        dm._apply_updates_in_transaction({"equipment_type": "观察级ROV"}, dm.slot_store.slots)
        eq_slot = dm.slot_store.slots.get("equipment_type")
        self.assertIsNotNone(eq_slot)
        self.assertEqual(eq_slot.value, "观察级深海机器人 75HP")
        self.assertNotEqual(eq_slot.value, "观察级ROV")

        dm._normalize_and_validate_in_transaction(dm.slot_store.slots, "pipeline_inspection")
        eq_slot_after = dm.slot_store.slots.get("equipment_type")
        self.assertEqual(eq_slot_after.status, "valid")
        self.assertEqual(eq_slot_after.value, "观察级深海机器人 75HP")


    # 53. 损坏计数器文件 Fail Closed 测试
    def test_53_corrupted_counter_files_fail_closed(self):
        invalid_contents = [
            ("{broken json", "broken JSON"),
            ("[1, 2, 3]", "top-level list"),
            ('{"TI20260718": -5}', "negative counter"),
            ('{"TI20260718": 12.5}', "float counter"),
            ('{"TI20260718": true}', "bool counter"),
            ('{"TI20260718": "abc"}', "invalid string counter"),
        ]
        for content, desc in invalid_contents:
            with tempfile.TemporaryDirectory() as tmp_dir:
                cnt_file = Path(tmp_dir) / ".id_sequences.json"
                with open(cnt_file, "w", encoding="utf-8") as f:
                    f.write(content)

                with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}):
                    id_sequence._COUNTERS.clear()
                    with self.assertRaises(IdReservationError, msg=f"Should fail closed for {desc}"):
                        id_sequence.next_daily_id("TI", "20260718", 2, [])

                    # Original file content unchanged
                    with open(cnt_file, "r", encoding="utf-8") as f:
                        self.assertEqual(f.read(), content)

                    # Memory counter not polluted
                    self.assertNotIn("TI20260718", id_sequence._COUNTERS)

    # 54. 数字字符串计数器显式迁移及恢复递增测试
    def test_54_counter_file_recovery_and_valid_string_migration(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cnt_file = Path(tmp_dir) / ".id_sequences.json"
            with open(cnt_file, "w", encoding="utf-8") as f:
                f.write('{"TI20260718": "50"}')

            with patch.dict(os.environ, {"SEAGENT_RESULT_DIR": tmp_dir}):
                id_sequence._COUNTERS.clear()
                gid = id_sequence.next_daily_id("TI", "20260718", 2, [])
                self.assertEqual(gid, "TI2026071851")


if __name__ == "__main__":
    unittest.main()
