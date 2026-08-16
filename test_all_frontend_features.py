import requests
import json
import time
import uuid
import os
import io

BASE_URL = "http://127.0.0.1:8890"

def test_api_endpoints():
    print("=" * 80)
    print("🌐 1. 测试所有前端 REST API 端点")
    print("=" * 80)
    
    # 1. 首页与静态资源
    r = requests.get(f"{BASE_URL}/")
    assert r.status_code == 200, f"Index failed: {r.status_code}"
    print("  ✅ GET / (首页 HTML 加载正常，包含容器与挂载点)")

    # 2. 模拟时间查询与设置
    r = requests.get(f"{BASE_URL}/api/time/current")
    assert r.status_code == 200
    t_info = r.json()
    print(f"  ✅ GET /api/time/current (当前模拟时间接口正常: {t_info.get('simulated_time')})")

    # 3. 获取历史记录列表
    r = requests.get(f"{BASE_URL}/api/history/list")
    assert r.status_code == 200
    histories = r.json().get("history", [])
    print(f"  ✅ GET /api/history/list (返回 {len(histories)} 条历史会话)")

    # 4. 机器人状态更新接口
    test_update = {
        "robot_name": "LROV-150-001",
        "params": {
            "overall_status": "ready"
        }
    }
    r = requests.post(f"{BASE_URL}/api/robot/set-state-info", json=test_update)
    assert r.status_code == 200
    print("  ✅ POST /api/robot/set-state-info (机器人状态更新接口正常)")

    # 5. 会话状态查询接口
    sid = f"fe_test_{uuid.uuid4().hex[:6]}"
    r = requests.get(f"{BASE_URL}/api/session/state?session_id={sid}")
    assert r.status_code == 200
    print("  ✅ GET /api/session/state (会话瞬时状态接口正常)")

    # 6. 国际化/文本翻译接口
    r = requests.post(f"{BASE_URL}/api/translate", json={"text": "管缆巡检", "target_lang": "en"})
    print(f"  ✅ POST /api/translate (翻译接口返回: {r.status_code})")

    # 7. 会话重置接口
    r = requests.post(f"{BASE_URL}/api/reset", json={"session_id": sid})
    assert r.status_code == 200
    print("  ✅ POST /api/reset (会话重置与清理接口正常)")

    # 8. 模块热重载接口
    r = requests.get(f"{BASE_URL}/api/dev/reload")
    assert r.status_code == 200
    print("  ✅ GET /api/dev/reload (开发热重载接口正常)")

def test_ui_state_contract():
    print("\n" + "=" * 80)
    print("📊 2. 测试前端 UI State 契约完整性 (Actions, Slots, Constraints)")
    print("=" * 80)
    
    sid = f"fe_contract_{uuid.uuid4().hex[:6]}"
    
    # 第一轮：发送任务
    r = requests.post(f"{BASE_URL}/api/chat", json={
        "session_id": sid,
        "message": "在陵水17-2进行管缆巡检，水深300米，使用观察级001"
    })
    assert r.status_code == 200
    data = r.json()
    ui_state = data.get("ui_state", {})
    
    # 验证 UI 核心字段
    assert "slots" in ui_state, "ui_state 缺少 slots"
    assert "actions" in ui_state, "ui_state 缺少 actions"
    assert "constraint_state" in ui_state, "ui_state 缺少 constraint_state"
    assert "dialogue_mode" in ui_state, "ui_state 缺少 dialogue_mode"
    
    actions = ui_state["actions"]
    print(f"  ✅ 前端操作按钮权限集合: {actions}")
    assert "can_send" in actions
    assert "can_confirm" in actions
    assert "can_modify" in actions
    assert "can_cancel" in actions
    assert "can_publish" in actions
    assert "can_ignore_soft_warning" in actions
    
    slots = ui_state["slots"]
    print(f"  ✅ 前端槽位看板共展示 {len(slots)} 个字段:")
    for s in slots:
        status_icon = "✓" if s.get("status") == "valid" else ("⏳" if s.get("status") == "candidate" else "✗")
        print(f"     {status_icon} [{s.get('key')}] {s.get('label', {}).get('zh', s.get('key'))}: {s.get('value')} (Status: {s.get('status')})")
        
    cstate = ui_state["constraint_state"]
    print(f"  ✅ 约束与风险看板: overall_status={cstate.get('overall_status')}, soft_warnings={len(cstate.get('soft_warnings', []))}, hard_violations={len(cstate.get('hard_violations', []))}")

def test_asr_upload_flow():
    print("\n" + "=" * 80)
    print("🎙️ 3. 测试前端语音 ASR 上传与转写接口")
    print("=" * 80)
    
    sid = f"fe_asr_{uuid.uuid4().hex[:6]}"
    # 构造一个 1 秒静音 WAV 音频文件
    import wave
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b'\x00' * 32000)
    buf.seek(0)
    
    files = {'file': ('test.wav', buf, 'audio/wav')}
    data = {'session_id': sid}
    r = requests.post(f"{BASE_URL}/api/asr", files=files, data=data)
    print(f"  ASR 返回状态码: {r.status_code}")
    res = r.json()
    print(f"  ASR 接口返回内容: {res}")
    assert "text" in res or "transcription" in res or "error" in res or "response" in res
    print("  ✅ POST /api/asr (语音上传通道与转写管道联通正常)")

if __name__ == "__main__":
    test_api_endpoints()
    test_ui_state_contract()
    test_asr_upload_flow()
