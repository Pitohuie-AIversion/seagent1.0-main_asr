"""
scratch/live_real_model_test.py — 真实 LLM 模型 (Qwen3.5-9B) 与 HTTP API 多轮实测脚本
验证目标：
Turn 1: 提供部分槽位，服务端返回 phase 为 collecting 阶段，缺失槽位列表包含时间/设备/坐标等。软警告不阻断参数收集。
Turn 2: 提供设备/时间/水深等，但缺少坐标和载荷，阶段仍为 collecting，仍提示补充坐标和载荷。
Turn 3: 补充坐标。
Turn 4: 补充工具载荷。所有必填槽位收集完毕，系统触发全量动态遥测与环境校核，进入 blocked_soft / confirming 阶段，并在回复中包含设备状态复核与环境软警告提示。
"""

import requests
import json
import uuid

BASE_URL = "http://127.0.0.1:8890/api/chat"

def main():
    session_id = f"real_model_test_{uuid.uuid4().hex[:8]}"
    print(f"=== 启动真实 LLM (Qwen3.5-9B) 交互测试 (session_id={session_id}) ===")

    # Turn 1: 仅提供任务类型与油田
    payload_turn1 = {
        "message": "在流花11-1油田执行管缆埋设作业",
        "session_id": session_id,
        "mode": "task"
    }
    print("\n--- Turn 1 请求: '在流花11-1油田执行管缆埋设作业' ---")
    res1 = requests.post(BASE_URL, json=payload_turn1, timeout=60)
    data1 = res1.json()
    ui_state1 = data1.get("ui_state", {})
    phase1 = ui_state1.get("phase")
    missing1 = data1.get("missing", [])

    print("Turn 1 HTTP Status:", res1.status_code)
    print("Turn 1 阶段 (phase):", phase1)
    print("Turn 1 缺失槽位列表:", missing1)
    print("Turn 1 包含 机器人状态校核:", "设备状态复核" in data1.get("reply", "") or "动态状态校核摘要" in data1.get("reply", ""))
    print("Turn 1 回复内容片段:\n", data1.get("reply", "")[:200] + "...\n")

    assert phase1 == "collecting", f"Turn 1 phase 应为 collecting，实际为: {phase1}"
    assert len(missing1) > 0, "Turn 1 仍有缺失槽位"

    # Turn 2: 提供设备/时间/水深
    payload_turn2 = {
        "message": "使用CRAWLER-1600-001，开始时间2026-08-24T09:00:00，结束时间2026-08-24T17:00:00，水深130米，管缆类型电力电缆，支持船海洋石油681",
        "session_id": session_id,
        "mode": "task"
    }
    print("\n--- Turn 2 请求: 提供时间设备水深 ---")
    res2 = requests.post(BASE_URL, json=payload_turn2, timeout=60)
    data2 = res2.json()
    ui_state2 = data2.get("ui_state", {})
    phase2 = ui_state2.get("phase")
    missing2 = data2.get("missing", [])

    print("Turn 2 HTTP Status:", res2.status_code)
    print("Turn 2 阶段 (phase):", phase2)
    print("Turn 2 缺失槽位列表:", missing2)
    print("Turn 2 包含 机器人状态校核:", "设备状态复核" in data2.get("reply", "") or "动态状态校核摘要" in data2.get("reply", ""))
    print("Turn 2 回复内容片段:\n", data2.get("reply", "")[:200] + "...\n")

    assert phase2 == "collecting", f"Turn 2 缺少坐标时仍应为 collecting，实际为: {phase2}"
    assert len(missing2) > 0, "Turn 2 仍有缺失坐标槽位"

    # Turn 3: 补充起止点经纬度坐标（使用标准北纬/东经描述）
    payload_turn3 = {
        "message": "起点坐标：北纬20.8度，东经115.7度；终点坐标：北纬20.82度，东经115.75度",
        "session_id": session_id,
        "mode": "task"
    }
    print("\n--- Turn 3 请求: 补充标准起止坐标 ---")
    res3 = requests.post(BASE_URL, json=payload_turn3, timeout=60)
    data3 = res3.json()
    ui_state3 = data3.get("ui_state", {})
    phase3 = ui_state3.get("phase")
    missing3 = data3.get("missing", [])

    print("Turn 3 HTTP Status:", res3.status_code)
    print("Turn 3 阶段 (phase):", phase3)
    print("Turn 3 缺失槽位列表:", missing3)

    # Turn 4: 补充工具载荷
    payload_turn4 = {
        "message": "携带工具选择：机械切割开沟模块、TSS管缆跟踪系统",
        "session_id": session_id,
        "mode": "task"
    }
    print("\n--- Turn 4 请求: 补充携带工具 (完成所有槽位收集) ---")
    res4 = requests.post(BASE_URL, json=payload_turn4, timeout=60)
    data4 = res4.json()
    ui_state4 = data4.get("ui_state", {})
    phase4 = ui_state4.get("phase")
    missing4 = data4.get("missing", [])

    has_telemetry_check = "设备状态复核" in data4.get("reply", "") or "设备实时状态校核" in data4.get("reply", "") or "机器人实时状态校核" in data4.get("reply", "") or "动态状态校核摘要" in data4.get("reply", "") or "环境遥测" in data4.get("reply", "")
    has_soft_warning = "软性约束警告" in data4.get("reply", "") or "安全约束预警" in data4.get("reply", "") or "流速" in data4.get("reply", "")

    print("Turn 4 HTTP Status:", res4.status_code)
    print("Turn 4 阶段 (phase):", phase4)
    print("Turn 4 缺失槽位列表:", missing4)
    print("Turn 4 包含 设备/机器人状态校核与遥测:", has_telemetry_check)
    print("Turn 4 包含 软警告与安全预警提示:", has_soft_warning)
    print("\nTurn 4 回复完整内容:\n", data4.get("reply", ""))

    assert phase4 in ("confirming", "blocked_soft"), f"Turn 4 phase 应为 confirming/blocked_soft，实际为: {phase4}"
    assert len(missing4) == 0, f"Turn 4 必填槽位应已全部收集完成，实际缺失: {missing4}"
    assert has_telemetry_check, "Turn 4 收集完毕后必须呈现设备状态复核/环境遥测"
    assert has_soft_warning, "Turn 4 收集完毕后必须呈现软警告环境评估信息"

    print("\n🎉🎉🎉 真实 LLM 模型 (Qwen3.5-9B) 与 HTTP API 多轮全流程实测 100% 成功通过！🎉🎉🎉")

if __name__ == "__main__":
    main()
