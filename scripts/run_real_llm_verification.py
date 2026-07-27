"""
scripts/run_real_llm_verification.py - LLM 6-Turn Verification Script

Supports Real LLM (Qwen3.5-9B) when weights are present, and falls back to Scheme B
(Mock Verification with [SKIPPED_REAL_MODEL] notice) when in offline mock mode.
"""

import json
import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient


def run_verification():
    print("🚀 Initializing LLMClient...")
    kb = KnowledgeBase()
    llm = LLMClient()
    dm = DialogueManager(llm, kb)

    is_real_model = (getattr(llm, "llm", None) is not None) and (getattr(llm, "tok", None) is not None)

    if not is_real_model:
        print("[SKIPPED_REAL_MODEL] Real model weights (Qwen3.5-9B) are not loaded in offline environment.")
        print("ℹ️ Running in Mock Verification mode...")
    else:
        print(f"✅ Real LLM (Qwen3.5-9B) loaded successfully on device: {getattr(llm, 'device', 'cuda')}")

    turns = [
        "你好。",
        "机器人可以使用哪些工具？",
        "500米级机器人有哪些？",
        "创建一个管缆巡检任务，水深300米。",
        "当前任务进行到哪一步？",
        "帮我处理一下。",
    ]

    print("\n" + "=" * 80)
    mode_str = "Real LLM (Qwen3.5-9B)" if is_real_model else "Mock Verification"
    print(f"📋 6-Turn Acceptance Verification Report ({mode_str})")
    print("=" * 80 + "\n")

    for idx, user_input in enumerate(turns, 1):
        extractor_calls = 0
        real_extract = dm.extractor.extract_updates
        last_route = None
        real_route = dm.intent_router.route

        def spy_extract(*args, **kwargs):
            nonlocal extractor_calls
            extractor_calls += 1
            return real_extract(*args, **kwargs)

        def spy_route(*args, **kwargs):
            nonlocal last_route
            r = real_route(*args, **kwargs)
            last_route = r
            return r

        dm.extractor.extract_updates = spy_extract
        dm.intent_router.route = spy_route

        v_before = dm.slot_store.version
        snap_before = dm.slot_store.export_snapshot()

        reply = dm.process(user_input, request_id=f"req_t{idx}")

        dm.extractor.extract_updates = real_extract
        dm.intent_router.route = real_route

        v_after = dm.slot_store.version
        snap_after = dm.slot_store.export_snapshot()
        slot_modified = (v_before != v_after or snap_before != snap_after)

        print(f"--- [Turn {idx}] --------------------------------------------------")
        print(f"原始输入      : {user_input}")
        print(f"路由结果      : {last_route.intent if last_route else 'N/A'}")
        print(f"Confidence    : {last_route.confidence:.2f}" if last_route else "Confidence    : N/A")
        print(f"Source        : {last_route.source if last_route else 'N/A'}")
        print(f"ShouldUpdate  : {last_route.should_update_slots if last_route else 'N/A'}")
        print(f"Extractor调用 : {extractor_calls} 次")
        print(f"SlotStore版本 : v{v_before} -> v{v_after} (修改={slot_modified})")
        print(f"已收集槽位    : {json.dumps(dm._last_built_json, ensure_ascii=False)}")
        print(f"LLM回复       : {reply}")
        print()

        # 核心断言
        if idx in (1, 2, 3, 5, 6):
            assert not slot_modified, f"Turn {idx} unexpected slot modification!"
            assert extractor_calls == 0, f"Turn {idx} unexpected extractor call!"
        if idx == 2:
            assert any(kw in reply for kw in ["摄像系统", "探测仪", "工具", "抓手", "负荷"]), "Turn 2 reply missing tools!"
        if idx == 3:
            assert any(kw in reply for kw in ["500", "观察级", "设备", "机器人"]), "Turn 3 reply missing 500m devices!"
            assert "water_depth" not in dm.slot_store.slots or dm.slot_store.slots["water_depth"].value is None
        if idx == 4:
            assert slot_modified, "Turn 4 should modify slot_store!"
            assert dm.slot_store.slots.get("water_depth") and float(dm.slot_store.slots["water_depth"].value) == 300.0
        if idx == 5:
            assert last_route and last_route.intent == "TASK_STATUS"
            assert "collecting" in reply or "阶段" in reply or "字段" in reply
        if idx == 6:
            assert last_route and last_route.intent in ("CLARIFICATION", "UNKNOWN")
            assert "理解" in reply or "澄清" in reply or "不确定" in reply or "新建" in reply

    if is_real_model:
        print("🎉 Real LLM (Qwen3.5-9B) 6-Turn Verification PASSED 100%!")
    else:
        print("🎉 Mock LLM 6-Turn Verification Completed Successfully!")


if __name__ == "__main__":
    run_verification()
