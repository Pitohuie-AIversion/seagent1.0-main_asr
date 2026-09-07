import requests
import json
import time

BASE_URL = "http://127.0.0.1:8890"

def run_fix_verification():
    session = requests.Session()
    
    def chat(msg, sid):
        print(f"\n--- [Session: {sid}] User --->: {msg}")
        t0 = time.time()
        try:
            res = session.post(f"{BASE_URL}/api/chat", json={"message": msg, "session_id": sid}, timeout=45)
            elapsed = round(time.time() - t0, 2)
            if res.status_code != 200:
                print(f"[HTTP {res.status_code}] {res.text}")
                return None
            data = res.json()
            reply = data.get("reply", "")
            ui_state = data.get("ui_state", {})
            phase = ui_state.get("dialogue_phase", "unknown")
            slots = ui_state.get("slots", {})
            print(f"<--- SEAgent (took {elapsed}s) [Phase: {phase}]:\n{reply}")
            if isinstance(slots, dict):
                filled_slots = {k: v.get("value") for k, v in slots.items() if isinstance(v, dict) and v.get("value") is not None}
                if filled_slots:
                    print(f"     [Filled Slots]: {filled_slots}")
            return data
        except Exception as e:
            print(f"[Error]: {e}")
            return None

    print("\n================== 验证 1: 设备代号问答（水深与功率） ==================")
    sid1 = f"verify_qa_{int(time.time())}"
    chat("请问天鹰座001的最大作业水深和功率是多少？", sid1)

    print("\n================== 验证 2: 通用工作级防误脱敏 ==================")
    sid2 = f"verify_general_{int(time.time())}"
    chat("让通用工作级001在流花11-1进行采油树控制面板插入作业，明天早上8点开始", sid2)

    print("\n================== 验证 3: 首轮包含代号的完整设备抽取 ==================")
    sid3 = f"verify_first_round_{int(time.time())}"
    chat("安排天鹰座001去陵水17-2进行管缆巡检", sid3)

if __name__ == "__main__":
    run_fix_verification()
