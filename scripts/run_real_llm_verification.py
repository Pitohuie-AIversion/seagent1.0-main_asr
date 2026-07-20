"""
scripts/run_real_llm_verification.py - Real LLM (Qwen3.5-9B) 6-Turn Verification Script

Executes 6 consecutive turns against real DialogueManager instance without OFFLINE_MOCK:
Turn 1: "你好。"
Turn 2: "机器人可以使用哪些工具？"
Turn 3: "500米级机器人有哪些？"
Turn 4: "创建一个管缆巡检任务，水深300米。"
Turn 5: "当前任务进行到哪一步？"
Turn 6: "帮我处理一下。"

Logs exact route, confidence, source, slot_store version, extractor calls, and response text.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient


def run_verification():
    print("🚀 Initializing Real LLMClient (Qwen3.5-9B on CUDA)...")
    kb = KnowledgeBase()
    llm = LLMClient()
    dm = DialogueManager(llm, kb)

    turns = [
        "你好。",
        "机器人可以使用哪些工具？",
        "500米级机器人有哪些？",
        "创建一个管缆巡检任务，水深300米。",
        "当前任务进行到哪一步？",
        "帮我处理一下。",
    ]

    print("\n" + "=" * 80)
    print("📋 Real LLM 6-Turn Acceptance Verification Report")
    print("=" * 80 + "\n")

    for idx, user_input in enumerate(turns, 1):
        extractor_calls = 0
        real_extract = dm.extractor.extract_updates

        def spy_extract(*args, **kwargs):
            nonlocal extractor_calls
            extractor_calls += 1
            return real_extract(*args, **kwargs)

        dm.extractor.extract_updates = spy_extract

        v_before = dm.slot_store.version
        snap_before = dm.slot_store.export_snapshot()

        route = dm.intent_router.route(user_input, dm.conversation_history, dm.task_state, dm.phase)

        reply = dm.process(user_input, request_id=f"req_real_t{idx}")

        # Restore original function after each turn
        dm.extractor.extract_updates = real_extract

        v_after = dm.slot_store.version
        snap_after = dm.slot_store.export_snapshot()

        slot_modified = (v_before != v_after or snap_before != snap_after)

        print(f"--- [Turn {idx}] --------------------------------------------------")
        print(f"原始输入      : {user_input}")
        print(f"路由结果      : {route.intent}")
        print(f"Confidence    : {route.confidence:.2f}")
        print(f"Source        : {route.source}")
        print(f"ShouldUpdate  : {route.should_update_slots}")
        print(f"Extractor调用 : {extractor_calls} 次")
        print(f"SlotStore版本 : v{v_before} -> v{v_after} (修改={slot_modified})")
        print(f"已收集槽位    : {json.dumps(dm._last_built_json, ensure_ascii=False)}")
        print(f"LLM回复       : {reply}")
        print()

        # 校验规则断言
        if idx in (1, 2, 3, 5, 6):
            assert not slot_modified, f"Turn {idx} unexpected slot modification!"
            assert extractor_calls == 0, f"Turn {idx} unexpected extractor call!"
        if idx in (2, 3):
            assert "water_depth" not in dm.slot_store.slots or dm.slot_store.slots["water_depth"].value is None
        if idx == 4:
            assert slot_modified, "Turn 4 should modify slot_store!"
            assert dm.slot_store.slots.get("water_depth") and float(dm.slot_store.slots["water_depth"].value) == 300.0
        if idx == 5:
            assert route.intent == "TASK_STATUS"
        if idx == 6:
            assert route.intent in ("CLARIFICATION", "UNKNOWN")

    print("🎉 All 6 Turns Real LLM Acceptance Verification PASSED 100%!")


if __name__ == "__main__":
    run_verification()
