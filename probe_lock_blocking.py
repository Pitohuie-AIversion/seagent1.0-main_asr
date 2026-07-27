#!/usr/bin/env python3
import multiprocessing as mp
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.knowledge_retriever import KnowledgeBase
from src.task_intent_builder import TaskIntentBuilder, TaskPublishLock

def _mp_lock_holder(tmp_dir_str, hold_event, ready_event):
    task_dir = Path(tmp_dir_str) / "task"
    with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
        lock = TaskPublishLock(task_dir)
        with lock:
            ready_event.set()
            hold_event.wait(timeout=5)

def _mp_contender_create(tmp_dir_str, intent, res_queue):
    kb = KnowledgeBase()
    builder = TaskIntentBuilder(kb)
    task_dir = Path(tmp_dir_str) / "task"
    with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
        try:
            st = builder.create_staging(intent)
            res_queue.put(("acquired", st.name))
        except Exception as e:
            res_queue.put(("error", type(e).__name__))

def run_probe():
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

    ctx = mp.get_context("spawn")
    with tempfile.TemporaryDirectory() as tmp_dir:
        task_dir = Path(tmp_dir) / "task"
        task_dir.mkdir(parents=True, exist_ok=True)

        res_q = ctx.Queue()
        ready_e = ctx.Event()
        hold_e = ctx.Event()

        p_holder = ctx.Process(target=_mp_lock_holder, args=(tmp_dir, hold_e, ready_e))
        p_holder.start()
        ready_e.wait(timeout=5)

        p_contender = ctx.Process(target=_mp_contender_create, args=(tmp_dir, intent, res_q))
        p_contender.start()

        p_contender.join(timeout=0.4)
        assert p_contender.is_alive(), "Process B must be blocked when Process A holds lock"

        hold_e.set()
        p_holder.join(timeout=5)
        p_contender.join(timeout=5)

        res = res_q.get(timeout=2)
        assert res[0] == "acquired"

    print("[PROBE 5 SUCCESS] Process B blocked by cross-process TaskPublishLock.")

if __name__ == "__main__":
    run_probe()
