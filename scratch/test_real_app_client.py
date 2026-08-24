import uuid
from web_backend import app

def test_app_client_formatting():
    client = app.test_client()
    session_id = f"test_client_fmt_{uuid.uuid4().hex[:8]}"
    print(f"=== Flask App Test Client 真实大模型响应格式化测试 (session_id={session_id}) ===")

    msg = "我想安排一次管缆巡检任务，起始点经纬度{\"lat\":19.8,\"lon\":113.2}，结束点经纬度{\"lat\":19.9,\"lon\":113.6}，水深130米，管缆类型为海底油气管道，开始时间2026-08-24T17:02:05，结束时间2026-08-24T19:02:11"

    res = client.post("/api/chat", json={
        "message": msg,
        "session_id": session_id,
        "mode": "task"
    })

    print("HTTP Status Code:", res.status_code)
    data = res.get_json()
    if res.status_code != 200:
        print("Response Error Data:", data)
        raise RuntimeError(f"API chat returned HTTP {res.status_code}")

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

    print("\n--- 断言校验 ---")
    assert '{"lat":19.8,"lon":113.2}' not in reply, f"回复中不应包含原始 JSON，实际为:\n{reply}"
    assert '{"lat":19.9,"lon":113.6}' not in reply, f"回复中不应包含原始 JSON，实际为:\n{reply}"
    print("✅ 回复中无原始 JSON 坐标字符串！")

    assert "北纬 19.8 度，东经 113.2 度" in reply, f"回复中应包含自然语言坐标 '北纬 19.8 度，东经 113.2 度'，实际为:\n{reply}"
    assert "北纬 19.9 度，东经 113.6 度" in reply, f"回复中应包含自然语言坐标 '北纬 19.9 度，东经 113.6 度'，实际为:\n{reply}"
    print("✅ 回复中包含自然语言包装的起止坐标！")

    sp_slot = slot_dict.get("start_point", {})
    ep_slot = slot_dict.get("end_point", {})
    wd_slot = slot_dict.get("water_depth", {})

    assert sp_slot.get("display_value") == "北纬 19.8 度，东经 113.2 度", f"start_point display_value 错误: {sp_slot.get('display_value')}"
    assert ep_slot.get("display_value") == "北纬 19.9 度，东经 113.6 度", f"end_point display_value 错误: {ep_slot.get('display_value')}"
    assert wd_slot.get("display_value") == "130 米", f"water_depth display_value 错误: {wd_slot.get('display_value')}"
    print("✅ ui_state 字段 display_value 全部正确包装！")

    print("\n🎉 真实大模型 API 端到端验证成功！")

if __name__ == "__main__":
    test_app_client_formatting()
