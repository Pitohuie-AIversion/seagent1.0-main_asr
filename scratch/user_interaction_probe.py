#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scratch/user_interaction_probe.py
模拟真实用户与 SEAgent (http://127.0.0.1:8892) 进行真实交互会话，
并详细打印每一步的回复、状态机阶段 (phase)、槽位 (slots)、警告与阻断。
"""

import json
import time
import uuid
import requests

BASE_URL = "http://127.0.0.1:8892"

def print_separator(title=""):
    print("\n" + "=" * 30 + f" {title} " + "=" * 30)

def chat_step(session_id: str, message: str, step_desc: str = ""):
    print(f"\n[用户 -> SEAgent] ({step_desc}): {message}")
    payload = {
        "session_id": session_id,
        "message": message,
        "request_id": f"req_{uuid.uuid4().hex[:8]}"
    }
    t0 = time.time()
    try:
        resp = requests.post(f"{BASE_URL}/api/chat", json=payload, timeout=60)
        dt = time.time() - t0
        print(f"[耗时: {dt:.2f}s] [HTTP {resp.status_code}]")
        if resp.status_code != 200:
            print(f"[ERROR Response]: {resp.text}")
            return None
        
        data = resp.json()
        reply = data.get("reply", "")
        ui_state = data.get("ui_state", {})
        phase = ui_state.get("phase")
        task_type_key = ui_state.get("task_type_key")
        slots_list = ui_state.get("slots", [])
        valid_slots = {s["key"]: (s.get("value") or s.get("candidate_value")) for s in slots_list if s.get("status") == "valid"}
        missing_slots = [s["key"] for s in slots_list if s.get("status") == "missing"]
        constraint_state = ui_state.get("constraint_state", {})
        warnings = constraint_state.get("soft_warnings", [])
        hard_violations = constraint_state.get("hard_violations", [])
        
        print(f"[SEAgent 回复]:\n{reply}")
        print(f"[State]: phase={phase} | task_type_key={task_type_key}")
        if valid_slots:
            print(f"[Valid Slots]: {json.dumps(valid_slots, ensure_ascii=False)}")
        if missing_slots:
            print(f"[Missing Slots]: {missing_slots}")
        if warnings:
            print(f"[Warnings]: {warnings}")
        if hard_violations:
            print(f"[Hard Violations]: {hard_violations}")
        return data
    except Exception as e:
        print(f"[Exception]: {e}")
        return None

def run_scenarios():
    print_separator("场景 1: 普通闲聊与知识问答 (不能触发任务收集)")
    sid1 = f"session_test_chat_{uuid.uuid4().hex[:6]}"
    chat_step(sid1, "你好，请介绍一下你自己和你的能力", "问候与能力询问")
    chat_step(sid1, "海马号水下机器人的最大作业水深是多少？", "知识库查询-机器人能力")
    chat_step(sid1, "今天天气不错，深海作业需要注意什么？", "普通安全闲聊")

    print_separator("场景 2: 任务构建全流程 - 管缆巡检任务")
    sid2 = f"session_test_task_{uuid.uuid4().hex[:6]}"
    chat_step(sid2, "帮我安排一次管缆巡检作业", "发起任务意图")
    chat_step(sid2, "作业时间从明天上午8点到下午4点，水深1200米", "补充时间和水深")
    chat_step(sid2, "使用海马号，搭载高清相机和声学成像仪", "补充设备和载荷")
    chat_step(sid2, "起点坐标东经115.5度北纬19.8度，终点东经115.6度北纬19.9度", "补充作业范围坐标")

    print_separator("场景 3: 槽位修改、别名解析与非模板槽位输入引导")
    sid3 = f"session_test_modify_{uuid.uuid4().hex[:6]}"
    chat_step(sid3, "我想做个管缆埋设任务，水深500米，埋深1.5米", "发起管缆埋设")
    chat_step(sid3, "这个任务在流花11-1油田执行", "输入非管缆埋设模板的油田槽位")
    chat_step(sid3, "把埋设深度改成2.0米，作业船用海洋石油201", "修改槽位与母船别名")

    print_separator("场景 4: 任务类型歧义澄清与硬约束阻断校验")
    sid4 = f"session_test_constraint_{uuid.uuid4().hex[:6]}"
    chat_step(sid4, "在崖城13-1气田进行水面采油树阀门操作，水深50米", "发起采油树任务（未指定插入/拔出）")
    chat_step(sid4, "执行采油树控制面板插入作业", "明确澄清为插入操作")
    chat_step(sid4, "计划让海欣号在水深5000米处执行作业", "故意触发水深超限硬约束")
    chat_step(sid4, "我确认，直接下发吧", "试图用'确认'强行绕过硬约束")

if __name__ == "__main__":
    run_scenarios()
