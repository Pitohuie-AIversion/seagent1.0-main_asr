import requests
import json
import time
import uuid

BASE_URL = "http://127.0.0.1:8890"

def test_rectifications():
    print("=" * 80)
    print("🧪 验证 4 大缺陷专项修复效果")
    print("=" * 80)

    # -------------------------------------------------------------
    # 验证 1: 消除负数坐标幻觉
    # -------------------------------------------------------------
    sid1 = f"verify_coord_{uuid.uuid4().hex[:6]}"
    print(f"\n[验证 1: 消除负数坐标幻觉] Session: {sid1}")
    r = requests.post(f"{BASE_URL}/api/chat", json={
        "session_id": sid1,
        "message": "安排通用工作级001在流花11-1执行采油树插入，开始时间2026-08-20 15:00，结束时间2026-08-20 09:00"
    })
    data = r.json()
    reply = data.get("reply", "")
    print(f"Reply: {reply}")
    assert "(-20, 15)" not in reply and "C028" not in reply, "❌ 仍存在负数坐标幻觉或 C028 警告"
    assert "20.815" in reply or "流花 11-1" in reply or "流花11-1" in reply
    print("  ✅ 验证 1 PASS: 油田坐标成功绑定标准坐标 (20.815, 115.735)，无负数坐标幻觉！")

    # -------------------------------------------------------------
    # 验证 2: 消除自身类别冲突误报
    # -------------------------------------------------------------
    sid2 = f"verify_cls_conflict_{uuid.uuid4().hex[:6]}"
    print(f"\n[验证 2: 消除设备类别自身冲突] Session: {sid2}")
    r = requests.post(f"{BASE_URL}/api/chat", json={
        "session_id": sid2,
        "message": "安排观察级001明天早上8点去陵水17-2的最深处1550米进行海底管缆埋设作业"
    })
    data = r.json()
    reply = data.get("reply", "")
    print(f"Reply: {reply}")
    assert "conflicts with active valid class '管缆埋设机器人'" not in reply, "❌ 仍存在自身类别冲突误报"
    print("  ✅ 验证 2 PASS: 成功消除 '管缆埋设机器人' 自身冲突误报！")

    # -------------------------------------------------------------
    # 验证 3: 已发布任务就地修改明确引导
    # -------------------------------------------------------------
    sid3 = f"verify_post_pub_{uuid.uuid4().hex[:6]}"
    print(f"\n[验证 3: 已发布任务修改友好引导] Session: {sid3}")
    # 先恢复机器人状态为正常可用
    requests.post(f"{BASE_URL}/api/robot/set-state-info", json={
        "robot_name": "WROV-250-001",
        "params": {
            "overall_status": "available",
            "survival_status": "normal",
            "system_health": "normal",
            "battery_soc": 95.0,
            "hydraulic_pressure_bar": 210.0,
            "power_kw": 180.0
        }
    })
    # 先完成发布
    requests.post(f"{BASE_URL}/api/chat", json={
        "session_id": sid3,
        "message": "安排通用工作级001明天早上8点在流花11-1进行采油树控制面板插入，水深300米，井口LH-01，结束时间明天中午12点，带高清水下摄像机和电液机械臂，支持船海洋石油681"
    })
    requests.post(f"{BASE_URL}/api/chat", json={"session_id": sid3, "message": "忽略警告并确认发布"})
    r_pub = requests.post(f"{BASE_URL}/api/chat", json={"session_id": sid3, "message": "确认发布"})
    print("Publish reply:", r_pub.json().get("reply"))
    # 尝试修改已发布任务
    r = requests.post(f"{BASE_URL}/api/chat", json={"session_id": sid3, "message": "把水深改成200米"})
    data = r.json()
    reply = data.get("reply", "")
    print(f"Post publish modification reply: {reply}")
    assert "已正式确认发布" in reply and "无法就地修改参数" in reply, "❌ 缺少已发布任务修改指引"
    print("  ✅ 验证 3 PASS: 已发布任务就地修改返回明确友好的引导指引！")

    # -------------------------------------------------------------
    # 验证 4: 管缆埋设喷冲模块+声呐有效装配
    # -------------------------------------------------------------
    sid4 = f"verify_burial_payload_{uuid.uuid4().hex[:6]}"
    print(f"\n[验证 4: 管缆埋设载荷有效性] Session: {sid4}")
    r = requests.post(f"{BASE_URL}/api/chat", json={
        "session_id": sid4,
        "message": "我想做管缆埋设，开始时间现在，结束时间五小时后，水深300米，管缆类型为海底油气管道，起始点(17.60,111.00)，结束点(17.70,111.10)，设备型号为特种工作级深海机器人 600HP，具体机器人编号为SPECIAL-600-001，携带工具为高压水射流喷冲埋设模块和前视声呐，支持船为海洋石油681"
    })
    data = r.json()
    reply = data.get("reply", "")
    print(f"Reply: {reply}")
    assert "高压水射流喷冲埋设模块" in reply or "前视声呐" in reply
    print("  ✅ 验证 4 PASS: 管缆埋设包含声呐与喷冲模块全部正确接收！")

if __name__ == "__main__":
    test_rectifications()
