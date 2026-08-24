import uuid
import sys
from web_backend import app, get_or_create_manager
from src.intent_router import IntentRouteResult

def test_real_llm_formatting():
    session_id = f"real_llm_fmt_{uuid.uuid4().hex[:8]}"
    print(f"=== DialogueManager & UI State 自然语言格式化端到端测试 (session_id={session_id}) ===")

    dm = get_or_create_manager(session_id)
    dm.dialogue_mode = "task_collection"
    dm.task_state["task_type_key"] = "pipeline_inspection"
    schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
    dm.slot_store.init_task_slots(schema)

    # 模拟真实意图为 WRITE 场景下的字段提取与回复生成
    accepted_updates = {
        "start_point": {"lat": 19.8, "lon": 113.2},
        "end_point": {"lat": 19.9, "lon": 113.6},
        "water_depth": 130.0,
        "cable_type": "海底油气管道",
    }
    dm.slot_store.slots["start_point"].value = accepted_updates["start_point"]
    dm.slot_store.slots["start_point"].status = "valid"
    dm.slot_store.slots["end_point"].value = accepted_updates["end_point"]
    dm.slot_store.slots["end_point"].status = "valid"
    dm.slot_store.slots["water_depth"].value = accepted_updates["water_depth"]
    dm.slot_store.slots["water_depth"].status = "valid"

    display_updates = dm._get_committed_update_display_values(accepted_updates)
    
    reply = dm._ground_write_reply(
        model_reply="收到，已记录您提供的坐标与水深。",
        accepted_updates=accepted_updates,
        unresolved_inputs=[],
        missing_fields=[],
        display_updates=display_updates,
    )

    print("\n--- DialogueManager 输出回复 ---")
    print(reply)

    from src.ui_state_builder import build_frontend_ui_state
    ui_state = build_frontend_ui_state(dm)
    slots = ui_state.get("slots", [])
    print("\n--- UI State 槽位 display_value ---")
    slot_dict = {}
    for s in slots:
        k = s.get("key")
        val = s.get("value")
        disp = s.get("display_value")
        slot_dict[k] = s
        if val is not None:
            print(f"Slot {k}: value={val}, display_value={disp}")

    print("\n--- 断言校验 ---")
    assert '{"lat":19.8,"lon":113.2}' not in reply, f"回复中仍包含原始 JSON\n{reply}"
    assert '{"lat":19.9,"lon":113.6}' not in reply, f"回复中仍包含原始 JSON\n{reply}"
    print("✅ 回复文本中已彻底剔除原始 JSON 坐标！")

    assert "北纬 19.8 度，东经 113.2 度" in reply, f"回复中应包含自然语言坐标 '北纬 19.8 度，东经 113.2 度'\n{reply}"
    assert "北纬 19.9 度，东经 113.6 度" in reply, f"回复中应包含自然语言坐标 '北纬 19.9 度，东经 113.6 度'\n{reply}"
    print("✅ 回复文本成功包含自然语言坐标包装！")

    assert "水深（米）：130 米" in reply or "水深：130 米" in reply or "130 米" in reply, f"回复中水深未正确包装\n{reply}"
    print("✅ 回复文本中水深成功包装为 '130 米'！")

    sp_slot = slot_dict.get("start_point", {})
    ep_slot = slot_dict.get("end_point", {})
    wd_slot = slot_dict.get("water_depth", {})

    assert sp_slot.get("display_value") == "北纬 19.8 度，东经 113.2 度", f"start_point display_value 错误: {sp_slot.get('display_value')}"
    assert ep_slot.get("display_value") == "北纬 19.9 度，东经 113.6 度", f"end_point display_value 错误: {ep_slot.get('display_value')}"
    assert wd_slot.get("display_value") == "130 米", f"water_depth display_value 错误: {wd_slot.get('display_value')}"
    print("✅ UI State display_value 校验全部通过！")

    print("\n🎉 端到端流程自然语言格式化验证 100% 成功！")

if __name__ == "__main__":
    test_real_llm_formatting()
