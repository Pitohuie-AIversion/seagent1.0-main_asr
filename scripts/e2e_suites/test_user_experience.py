import requests
import json
import time

BASE_URL = "http://127.0.0.1:8890"

def run_interaction_tests():
    session = requests.Session()
    
    def chat(msg, sid):
        print(f"\n--- [Session: {sid}] User --->: {msg}")
        t0 = time.time()
        try:
            res = session.post(f"{BASE_URL}/api/chat", json={"message": msg, "session_id": sid}, timeout=30)
            elapsed = round(time.time() - t0, 2)
            if res.status_code != 200:
                print(f"[HTTP {res.status_code}] {res.text}")
                return None
            data = res.json()
            reply = data.get("reply", "")
            phase = data.get("ui_state", {}).get("dialogue_phase", "unknown")
            slots = data.get("ui_state", {}).get("slots", {})
            constraints = data.get("ui_state", {}).get("constraints", {})
            print(f"<--- SEAgent (took {elapsed}s) [Phase: {phase}]: {reply}")
            if slots:
                filled_slots = {k: v.get("value") for k, v in slots.items() if v.get("value") is not None}
                print(f"     Filled Slots: {filled_slots}")
            if constraints:
                print(f"     Constraints: {constraints}")
            return data
        except Exception as e:
            print(f"[Error]: {e}")
            return None

    # Test Suite 1: 普通闲聊与领域问答
    print("\n================== TEST 1: 普通对话与知识问答 ==================")
    sid1 = f"test_chat_{int(time.time())}"
    chat("你好，你是谁？能帮我做什么？", sid1)
    chat("陵水17-2气田的主要设施有哪些？", sid1)
    chat("今天天气怎么样？", sid1)

    # Test Suite 2: 标准巡检任务创建与发布
    print("\n================== TEST 2: 标准巡检任务全流程 ==================")
    sid2 = f"test_task_standard_{int(time.time())}"
    chat("派海马1号去陵水17-2巡检采油树", sid2)
    chat("明天上午9点开始", sid2)
    chat("确认发布", sid2)

    # Test Suite 3: 任务中途修改
    print("\n================== TEST 3: 任务中途槽位修改 ==================")
    sid3 = f"test_task_modify_{int(time.time())}"
    chat("安排海龙3号在崖城13-1进行管线巡检，预计明天下午2点开始", sid3)
    chat("把地点改成陵水17-2，机器人换成海马1号", sid3)
    chat("确认", sid3)

    # Test Suite 4: 软约束警告及用户忽略警告
    print("\n================== TEST 4: 软约束与警告忽略 ==================")
    sid4 = f"test_task_soft_{int(time.time())}"
    # 模拟一个可能触发警告或者边界的指令
    chat("让海星6000去流花11-1执行作业", sid4)
    chat("明天早上8点", sid4)
    chat("忽略警告，继续发布", sid4)

    # Test Suite 5: 硬约束违规与阻断尝试
    print("\n================== TEST 5: 硬约束违规与绕过测试 ==================")
    sid5 = f"test_task_hard_{int(time.time())}"
    # 海马1号最大工作水深可能有限制，或者安排去一个超深度/冲突场景
    chat("让海马1号去水深4500米的区域巡检", sid5)
    chat("不管，立刻确认执行", sid5)

    # Test Suite 6: 中途取消/重置
    print("\n================== TEST 6: 任务取消与会话重置 ==================")
    sid6 = f"test_task_cancel_{int(time.time())}"
    chat("安排海龙3号去崖城13-1巡检", sid6)
    chat("算了，取消这个任务", sid6)
    chat("帮我查一下海马1号的最大下潜深度", sid6)

if __name__ == "__main__":
    run_interaction_tests()
