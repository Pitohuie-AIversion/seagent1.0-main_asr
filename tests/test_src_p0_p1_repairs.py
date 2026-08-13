"""Regression tests for the P0/P1 findings from the full src audit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.extractor import ParameterExtractor
from src.knowledge_retriever import KnowledgeBase
from src.state_info import RobotStateInfo
from src.task_intent_builder import TaskIntentBuilder, validate_task_intent
from src.validator import TaskValidator


class SourceAuditP0P1RegressionTest(unittest.TestCase):
    def test_unanchored_coordinate_pairs_keep_start_then_end_order(self):
        code = (
            "import json; "
            "from src.coord_parser import parse_coordinate_updates as parse; "
            "print(json.dumps(parse('19.8,113.5 20.8,114.5', "
            "{'start_point', 'end_point'}), sort_keys=True))"
        )
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = "5"
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        parsed = json.loads(completed.stdout)
        self.assertEqual({"lat": 19.8, "lon": 113.5}, parsed["start_point"])
        self.assertEqual({"lat": 20.8, "lon": 114.5}, parsed["end_point"])

    def test_non_finite_duration_is_rejected_without_raising(self):
        for duration in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(duration=duration):
                candidates, unresolved = ParameterExtractor._materialize_time_relation(
                    [],
                    {
                        "has_duration": True,
                        "duration_seconds": duration,
                        "confidence": 0.9,
                        "raw_text": "持续时长",
                    },
                    {"start_time": "2026-08-13T10:00:00"},
                    {"end_time"},
                )
                self.assertEqual([], candidates)
                self.assertTrue(unresolved)

    def test_runtime_availability_rejects_far_future_telemetry(self):
        now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        future = (now + timedelta(days=30)).isoformat()
        state_info = RobotStateInfo.__new__(RobotStateInfo)
        state_info._load_fleet = lambda: {
            "fleet_units": [{"unit_id": "U-1", "status_ref": "U-1"}]
        }
        state_info._snapshot_lock = lambda exclusive=False: nullcontext()
        state_info._load_state_unlocked = lambda: {
            "store_version": 1,
            "robots": {
                "U-1": {
                    "version": 1,
                    "updated_at": future,
                    "update_timestamp": future,
                    "overall_status": "available",
                    "is_online": True,
                    "is_busy": False,
                }
            },
        }

        with patch("src.state_info.get_current_datetime", return_value=now):
            result = state_info.check_runtime_availability("U-1")

        self.assertFalse(result["available"])
        self.assertEqual("INVALID_STATE_DATA", result["reason_code"])

    def test_validator_rejects_far_future_telemetry(self):
        now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        future = (now + timedelta(days=30)).isoformat()
        snapshot = {
            "unit_id": "OBSROV-75-001",
            "status_ref": "OBSROV-75-001",
            "state_version": 1,
            "store_version": 1,
            "updated_at": future,
            "state": {
                "overall_status": "available",
                "updated_at": future,
                "update_timestamp": future,
            },
        }
        kb = KnowledgeBase()
        validator = TaskValidator(kb)
        with (
            patch.object(kb, "get_unit_state_snapshot", return_value=snapshot),
            patch.object(validator, "_is_task_start_now", return_value=True),
            patch("src.validator.get_current_datetime", return_value=now),
        ):
            result = validator.validate_task(
                {
                    "task_type_key": "pipeline_inspection",
                    "equipment_unit_id": "OBSROV-75-001",
                },
                purpose="publish",
            )

        self.assertEqual("validation_error", result.overall_status)
        self.assertEqual("INVALID_STATE_DATA", result.error["code"])

    def test_malformed_task_intent_cannot_reach_final(self):
        malformed = {
            "schema_version": 2,
            "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "task_id": "PI-20260813-001",
            "intent_id": "TI2026081301",
            "task_type": "pipeline_inspection",
            "priority": 7,
            "time": {"start": [], "end": {"bad": True}},
            "location": {"oilfield": 123, "water_depth_m": -999},
            "task": {"type": "pipeline_inspection", "details": "not-a-dict"},
            "equipment": {
                "robot_type": "observation_rov",
                "payload": "not-a-list",
                "support_vessel": [],
            },
            "conditions": {},
        }
        kb = KnowledgeBase()
        self.assertFalse(validate_task_intent(malformed, kb.task_schemas))

        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir)
            builder = TaskIntentBuilder(kb)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                with self.assertRaises(Exception):
                    builder.persist(malformed)
            self.assertEqual([], list(task_dir.glob("task_intent_*.json")))


if __name__ == "__main__":
    unittest.main()
