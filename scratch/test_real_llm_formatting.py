import requests
import json
import uuid

BASE_URL = "http://127.0.0.1:8890/api/chat"

def test_real_llm_formatting():
    session_id = f"real_llm_fmt_{uuid.uuid4().hex[:8]}"
    print(f"=== 真实大模型响应与 UI State 格式化验证 (session_id={session_id}) ===")

    msg = "安排管缆巡检，起始点经纬度{\"lat\":19.8,\"lon\":113.2}，结束点经纬度{\"lat\":19.9,\"lon\":113.6}，水深130米，管缆类型为海底油气管道，开始时间2026-08-24T17:02:05，结束时间2026-08-24T19:02:11"
    
    payload = {
        "message": msg,
        "session_id": session_id,
        "mode": "task"
    }

    res = requests.post(BASE_URL, json=payload, timeout=60)
    print("HTTP Status Code:", res.status_code)
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"

    data = res.json()
    reply = data.get("reply", "")
    ui_state = data.get("ui_state", {})
    slots = ui_state.get("slots", [])

    print("\n--- 真实大模型回复文本 ---")
    print(reply)

    print("\n--- ui_state.slots 字段列表 ---")
    slot_dict = {}
    for s in slots:
        k = s.get("key")
        val = s.get("value")
        disp = s.get("display_value")
        slot_dict[k] = s
        print(f"Slot key={k}: value={val}, display_value={disp}")

    print("\n--- 断言检查 ---")

    # 1. 检查回复文本中不含原始 JSON
    assert '{"lat":19.8,"lon":113.2}' not in reply, "回复中仍包含原始坐标 JSON {'lat':19.8,'lon':113.2}"
    assert '{"lat":19.9,"lon":113.6}' not in reply, "回复中仍包含原始坐标 JSON {'lat':19.9,'lon':113.6}"
    print("✅ 回复文本中已剔除原始 JSON 坐标！")

    # 2. 检查回复文本中包含自然语言坐标包装
    assert "北纬 19.8 度，东经 113.2 度" in reply, "回复中未出现自然语言格式 '北纬 19.8 度，东经 113.2 度'"
    assert "北纬 19.9 度，东经 113.6 度" in reply, "回复中未出现自然语言格式 '北纬 19.9 度，东经 113.6 度'"
    print("✅ 回复文本成功包含自然语言坐标包装！")

    # 3. 检查 ui_state 中的 display_value
    sp_slot = slot_dict.get("start_point", {})
    ep_slot = slot_dict.get("end_point", {})
    wd_slot = slot_dict.get("water_depth", {})

    assert sp_slot.get("display_value") == "北纬 19.8 度，东经 113.2 度", f"start_point display_value 异常: {sp_slot.get('display_value')}"
    assert ep_slot.get("display_value") == "北纬 19.9 度，东经 113.6 度", f"end_point display_value 异常: {ep_slot.get('display_value')}"
    assert wd_slot.get("display_value") == "130 米", f"water_depth display_value 异常: {wd_slot.get('display_value')}"
    print("✅ ui_state 中的 start_point/end_point/water_depth display_value 验证全部通过！")

    print("\n🎉 真实大模型及后端 API 自然语言格式化验证全部 Success!")

if __name__ == "__main__":
    test_real_llm_formatting()
