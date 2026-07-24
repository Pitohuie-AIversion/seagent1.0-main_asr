#!/usr/bin/env python3
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.exceptions import TaskPersistenceError
from src.knowledge_retriever import KnowledgeBase
from src.task_intent_builder import TaskIntentBuilder

def run_probe():
    kb = KnowledgeBase()
    builder = TaskIntentBuilder(kb)
    intent = {
        "intent_id": "TI2026063001",
        "task_type": "pipeline_inspection",
        "priority": 7,
        "time": {"start": "2026-06-30T10:00:00+08:00", "end": "2026-06-30T12:00:00+08:00"},
        "location": {"oilfield": "南海一号", "water_depth_m": 300.0},
        "task": {
            "type": "pipeline_inspection",
            "details": {
                "pipeline_type": "subsea_oil_gas",
                "start_point": {"latitude": 20.0, "longitude": 110.0},
                "end_point": {"latitude": 20.1, "longitude": 110.1},
            },
        },
        "equipment": {
            "robot_type": "observation_rov",
            "payload": ["camera_hd"],
            "support_vessel": {"name": "海洋石油201"},
        },
        "conditions": {"max_current_speed_knots": 2.0, "sea_state_level": 3},
    }

    forged = {"forged": True, "secret": "replacement_temp"}

    with tempfile.TemporaryDirectory() as tmp_dir:
        task_dir = Path(tmp_dir) / "task"
        task_dir.mkdir(parents=True, exist_ok=True)

        with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            st = builder.create_staging(intent)

            def hook_commit_fail_and_replace_temp(temp_file, final_file):
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(forged, f)
                raise OSError("Disk failure during commit")

            try:
                with patch("src.task_intent_builder._atomic_commit_noreplace", side_effect=hook_commit_fail_and_replace_temp):
                    builder.publish_staging(st, intent)
                assert False, "Should have raised TaskPersistenceError"
            except TaskPersistenceError:
                pass

            tmps = list(task_dir.glob(".tmp_publish_*"))
            assert len(tmps) > 0
            with open(tmps[0], "r", encoding="utf-8") as f:
                data = json.load(f)
            assert data.get("secret") == "replacement_temp"

    print("[PROBE 2 SUCCESS] Temp rollback replacement file preserved safely.")

if __name__ == "__main__":
    run_probe()
