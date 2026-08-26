#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Full GPU mode E2E validation script - UTF-8 safe"""
import requests, json, time, uuid, sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE = "http://localhost:8890"
SID = f"e2e_{uuid.uuid4().hex[:10]}"
print(f"=== Full GPU E2E Test Session: {SID} ===")
results = {}

def chat(msg, label, timeout=120):
    t0 = time.time()
    try:
        r = requests.post(f"{BASE}/api/chat", json={
            "session_id": SID, "message": msg, "language": "zh"
        }, timeout=timeout)
        dt = time.time() - t0
        data = r.json() if r.headers.get("content-type","").startswith("application/json") else {"reply": r.text}
        ok = r.status_code == 200 and data.get("code") == 200
        print(f"\n{'='*60}\n[{label}] HTTP={r.status_code} T={dt:.1f}s  OK={ok}")
        reply = str(data.get("reply", ""))
        print(f"  Reply[:200]: {reply[:200].encode('unicode_escape').decode() if any(ord(c)>127 for c in reply) else reply[:200]}")
        missing = data.get("missing", [])
        collected = data.get("collected", {})
        print(f"  Missing({len(missing)}): {str(missing)[:150]}")
        print(f"  Collected({len(collected)}): {json.dumps(collected, ensure_ascii=False)[:200]}")
        print(f"  Phase={data.get('phase')}  Mode={data.get('mode')}  TaskType={data.get('task_type')}")
        return {"ok": ok, "status": r.status_code, "dt": dt, "data": data, "label": label}
    except Exception as e:
        dt = time.time() - t0
        print(f"\n[{label}] EXCEPTION after {dt:.1f}s: {type(e).__name__}: {e}")
        return {"ok": False, "status": 0, "dt": dt, "data": {}, "label": label, "error": str(e)}

# ========== T01: Knowledge Query ==========
results["T01"] = chat(
    "金牛座一号机的最大作业水深是多少？额定功率是多少？",
    "T01_知识查询_金牛座一号机参数"
)

# ========== T02: Task trigger ==========
time.sleep(2)
results["T02"] = chat(
    "我要在流花11-1油田执行管缆巡检作业，作业水深大约300米",
    "T02_任务触发_管缆巡检流花11-1"
)

# ========== T03: Add robot ==========
time.sleep(2)
results["T03"] = chat(
    "使用观察级深海机器人来执行这次巡检任务",
    "T03_补充参数_指定观察级机器人"
)

# ========== T04: Add time ==========
time.sleep(2)
results["T04"] = chat(
    "任务计划在2026年9月15日上午8点开始，预计作业时间6小时",
    "T04_补充参数_任务时间和时长"
)

# ========== T05: Confirm ==========
time.sleep(2)
results["T05"] = chat(
    "好的，以上所有参数我都确认无误，请发布任务",
    "T05_用户确认发布"
)

# ========== T06: Session state pull ==========
time.sleep(1)
try:
    t0 = time.time()
    r = requests.get(f"{BASE}/api/session/state", params={"session_id": SID}, timeout=10)
    dt = time.time()-t0
    data = r.json()
    ok = r.status_code == 200
    results["T06"] = {"ok": ok, "status": r.status_code, "dt": dt, "data": data, "label": "T06_会话状态查询"}
    print(f"\n[T06_会话状态查询] HTTP={r.status_code} T={dt:.1f}s OK={ok}")
    print(f"  Phase={data.get('phase')}  Filled={len(data.get('filled',{}))}  Missing={len(data.get('missing',[]))}")
    print(f"  HardViolations={data.get('hard_violations',[])}  SoftViolations={data.get('soft_violations',[])}")
except Exception as e:
    results["T06"] = {"ok": False, "status": 0, "dt": 0, "data": {}, "label": "T06_会话状态查询", "error": str(e)}
    print(f"[T06] EXCEPTION: {e}")

# ========== Error handling tests ==========
time.sleep(1)
print("\n" + "="*60)
print("=== Error / Edge case tests ===")
print("="*60)

# E01: Empty message
try:
    t0 = time.time()
    r = requests.post(f"{BASE}/api/chat", json={"session_id": SID+"_err", "message": ""}, timeout=10)
    dt = time.time()-t0
    results["E01"] = {"ok": r.status_code==200, "status": r.status_code, "dt": dt,
                      "data": r.json() if r.headers.get("content-type","").startswith("application/json") else {},
                      "label": "E01_异常_空消息"}
    print(f"[E01_空消息] HTTP={r.status_code} T={dt:.1f}s  resp_code={r.json().get('code') if r.headers.get('content-type','').startswith('application/json') else 'N/A'}")
except Exception as e:
    results["E01"] = {"ok": False, "status": 0, "dt": 0, "data": {}, "label": "E01_异常_空消息", "error": str(e)}
    print(f"[E01] EXCEPTION: {e}")

# E02: Missing session_id
try:
    t0 = time.time()
    r = requests.post(f"{BASE}/api/chat", json={"message": "hello"}, timeout=10)
    dt = time.time()-t0
    j = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
    results["E02"] = {"ok": r.status_code in (200,400), "status": r.status_code, "dt": dt, "data": j,
                      "label": "E02_异常_缺失session_id"}
    print(f"[E02_缺session] HTTP={r.status_code} T={dt:.1f}s  code={j.get('code')}  err={j.get('error','')[:60]}")
except Exception as e:
    results["E02"] = {"ok": False, "status": 0, "dt": 0, "data": {}, "label": "E02_异常_缺失session_id", "error": str(e)}
    print(f"[E02] EXCEPTION: {e}")

# E03: ASR endpoint - missing audio file
try:
    t0 = time.time()
    r = requests.post(f"{BASE}/api/asr", data={}, timeout=10)
    dt = time.time()-t0
    j = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
    results["E03"] = {"ok": r.status_code in (200,400,503), "status": r.status_code, "dt": dt, "data": j,
                      "label": "E03_异常_ASR缺音频"}
    print(f"[E03_ASR缺音频] HTTP={r.status_code} T={dt:.1f}s  code={j.get('code')}  err={j.get('error','')[:60]}")
except Exception as e:
    results["E03"] = {"ok": False, "status": 0, "dt": 0, "data": {}, "label": "E03_异常_ASR缺音频", "error": str(e)}
    print(f"[E03] EXCEPTION: {e}")

# E04: Robot state update with invalid payload
try:
    t0 = time.time()
    r = requests.post(f"{BASE}/api/robot/set-state-info", json={"robot_name": "", "params": {}}, timeout=10)
    dt = time.time()-t0
    j = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
    results["E04"] = {"ok": r.status_code in (200,400,503), "status": r.status_code, "dt": dt, "data": j,
                      "label": "E04_异常_机器人状态空参数"}
    print(f"[E04_机器人空参数] HTTP={r.status_code} T={dt:.1f}s  code={j.get('code')}  err={j.get('error','')[:60]}")
except Exception as e:
    results["E04"] = {"ok": False, "status": 0, "dt": 0, "data": {}, "label": "E04_异常_机器人状态空参数", "error": str(e)}
    print(f"[E04] EXCEPTION: {e}")

# E05: Large payload (>25KB)
try:
    t0 = time.time()
    r = requests.post(f"{BASE}/api/chat", json={
        "session_id": SID+"_big",
        "message": "x" * 30000
    }, timeout=15)
    dt = time.time()-t0
    j = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
    results["E05"] = {"ok": r.status_code in (200,400,413), "status": r.status_code, "dt": dt, "data": j,
                      "label": "E05_边界_超大消息体(30K)"}
    print(f"[E05_30K消息] HTTP={r.status_code} T={dt:.1f}s  code={j.get('code')}")
except Exception as e:
    results["E05"] = {"ok": False, "status": 0, "dt": 0, "data": {}, "label": "E05_边界_超大消息体(30K)", "error": str(e)}
    print(f"[E05] EXCEPTION: {e}")

# ========== Summary ==========
print("\n" + "="*70)
print("FINAL SUMMARY - Full GPU E2E Validation")
print("="*70)
pass_c = sum(1 for v in results.values() if v.get("ok"))
total = len(results)
for k, v in results.items():
    mark = "[PASS]" if v.get("ok") else "[FAIL]"
    s = v.get("status", 0)
    dt = v.get("dt", 0)
    label = v.get("label", k)
    print(f"  {mark}  {label:<40} HTTP={s:<4}  {dt:>6.1f}s")
    if not v.get("ok") and v.get("error"):
        print(f"           err: {v['error'][:100]}")

print(f"\n  Result: {pass_c}/{total} passed ({pass_c*100//total if total else 0}%)")
print(f"  SessionID: {SID}")
print(f"  TotalElapsed: {sum(v.get('dt',0) for v in results.values()):.1f}s")
