#!/usr/bin/env python3
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

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

    forged = {"forged": True, "secret": "replacement_claim"}

    with tempfile.TemporaryDirectory() as tmp_dir:
        task_dir = Path(tmp_dir) / "task"
        task_dir.mkdir(parents=True, exist_ok=True)

        with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            st = builder.create_staging(intent)

            real_rename = os.rename

            def race_claim_replace(src, dst):
                real_rename(src, dst)
                if ".claimed_" in str(dst):
                    with open(dst, "w", encoding="utf-8") as f:
                        json.dump(forged, f)

            with patch("os.rename", side_effect=race_claim_replace):
                pub_name = builder.publish_staging(st, intent)

            claims = list(task_dir.glob(".claimed_*"))
            assert len(claims) > 0
            with open(claims[0], "r", encoding="utf-8") as f:
                data = json.load(f)
            assert data.get("secret") == "replacement_claim"

    print("[PROBE 1 SUCCESS] Claim cleanup replacement file preserved safely.")

if __name__ == "__main__":
    run_probe()
