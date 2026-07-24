#!/usr/bin/env python3
import copy
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.exceptions import IntentIdConflict
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

    old_final_content = copy.deepcopy(intent)
    old_final_content["priority"] = 1
    forged_staging = {"forged": True, "secret": "replacement_staging"}

    with tempfile.TemporaryDirectory() as tmp_dir:
        task_dir = Path(tmp_dir) / "task"
        task_dir.mkdir(parents=True, exist_ok=True)
        final_file = task_dir / "task_intent_TI2026063001.json"
        with open(final_file, "w", encoding="utf-8") as f:
            json.dump(old_final_content, f)

        staging_file = task_dir / f"task_intent_TI2026063001.staging_{os.getpid()}_5678_abcd1234"
        with open(staging_file, "w", encoding="utf-8") as f:
            json.dump(intent, f)

        with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(forged_staging, f)

            try:
                builder.publish_staging(staging_file, intent)
                assert False, "Should have raised IntentIdConflict"
            except IntentIdConflict:
                pass

        with open(final_file, "r", encoding="utf-8") as f:
            assert json.load(f)["priority"] == 1

        assert staging_file.exists()
        with open(staging_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data.get("secret") == "replacement_staging"

    print("[PROBE 3 SUCCESS] Final exists staging replacement file preserved safely.")

if __name__ == "__main__":
    run_probe()
