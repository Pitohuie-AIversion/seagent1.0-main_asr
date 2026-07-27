#!/usr/bin/env python3
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from tests.test_slot_consistency import seed_complete_valid_pipeline_task

class DummyLLM(LLMClient):
    def __init__(self):
        self.llm = None
    def chat(self, messages, temperature=0.7, max_tokens=800, **kwargs):
        return "回复"
    def generate(self, messages, temperature=0.7, max_tokens=800, **kwargs):
        return "回复"
    def filter_reply(self, text):
        return text

def run_probe():
    kb = KnowledgeBase()
    llm = DummyLLM()

    bad_finals = [
        {"intent_id": "TI2026063001"},
        {"intent_id": "TI2026063001", "task_type": "pipeline_inspection"},
    ]

    with tempfile.TemporaryDirectory() as tmp_dir:
        task_dir = Path(tmp_dir) / "task"
        task_dir.mkdir(parents=True, exist_ok=True)
        pub_file = task_dir / "task_intent_TI2026063001.json"

        for bad_final in bad_finals:
            with open(pub_file, "w", encoding="utf-8") as f:
                json.dump(bad_final, f)

            dm = DialogueManager(llm, kb)
            seed_complete_valid_pipeline_task(dm, kb)
            dm.slot_store.slots["intent_id"].value = "TI2026063001"
            dm.slot_store.slots["intent_id"].status = "valid"
            dm.task_state["intent_id"] = "TI2026063001"

            snap = {
                "phase": "done",
                "mode": "normal",
                "task_state": dm.task_state,
                "built_json": dm._last_built_json,
                "slot_store": dm.slot_store.export_snapshot(),
            }

            with patch("src.dialogue_manager.get_task_dir", return_value=task_dir), \
                 patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("src.result_paths.get_task_dir", return_value=task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                dm.load_snapshot(snap)

            assert dm.phase != "done", f"Malformed final structure {bad_final} must be rejected"

    print("[PROBE 4 SUCCESS] Incomplete final schema rejected by consumer.")

if __name__ == "__main__":
    run_probe()
