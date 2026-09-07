import requests
import json
import time

BASE_URL = "http://127.0.0.1:8890"

def run_fleet_interaction_tests():
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
            constraints = ui_state.get("constraints", [])
            print(f"<--- SEAgent (took {elapsed}s) [Phase: {phase}]:\n{reply}")
            if isinstance(slots, dict):
                filled_slots = {k: v.get("value") for k, v in slots.items() if isinstance(v, dict) and v.get("value") is not None}
                if filled_slots:
                    print(f"     [Filled Slots]: {filled_slots}")
            if constraints:
                print(f"     [Constraints]: {constraints}")
            return data
        except Exception as e:
            print(f"[Error]: {e}")
            return None

    # Test 1: 真实库内机器人标准全流程 (天鹰座001 + 陵水17-2 + 管缆巡检)
    print("\n================== TEST 1: 库内机器人标准巡检任务全流程 ==================")
    sid1 = f"test_fleet_std_{int(time.time())}"
    chat("安排天鹰座001去陵水17-2进行管缆巡检", sid1)
    chat("明天上午9点开始，明天下午5点结束", sid1)
    chat("巡检海底油气管道，携带高清水下摄像机和成像声呐，支持船用海洋石油681", sid1)
    chat("确认发布", sid1)

    # Test 2: 采油树控制面板操作 (通用工作级001 + 流花11-1)
    print("\n================== TEST 2: 采油树控制面板插入作业 ==================")
    sid2 = f"test_fleet_tree_{int(time.time())}"
    chat("让通用工作级001在流花11-1进行采油树控制面板插入作业，计划明天早上8点开始", sid2)

    # Test 3: 多轮修改机器人与地点
    print("\n================== TEST 3: 多轮修改机器人与作业地点 ==================")
    sid3 = f"test_fleet_modify_{int(time.time())}"
    chat("安排观察级001去崖城13-1进行管缆巡检", sid3)
    chat("把机器人换成天鹰座001，地点改为陵水17-2", sid3)

    # Test 4: 硬约束阻断与修复闭环 (观察级001 超水深 -> 强行确认阻断 -> 换通用工作级001修复)
    print("\n================== TEST 4: 水深硬约束阻断与修复闭环 ==================")
    sid4 = f"test_fleet_depth_hard_{int(time.time())}"
    chat("安排观察级001去水深2500米区域进行管缆巡检", sid4)
    chat("不管，立刻确认执行", sid4)
    chat("那把机器人换成通用工作级001，水深2500米", sid4)

    # Test 5: 取消任务与设备参数只读查询
    print("\n================== TEST 5: 任务取消与设备参数知识问答 ==================")
    sid5 = f"test_fleet_cancel_qa_{int(time.time())}"
    chat("安排天鹰座001去流花11-1", sid5)
    chat("算了，取消任务", sid5)
    chat("请问天鹰座001的最大作业水深和功率是多少？", sid5)

if __name__ == "__main__":
    run_fleet_interaction_tests()
