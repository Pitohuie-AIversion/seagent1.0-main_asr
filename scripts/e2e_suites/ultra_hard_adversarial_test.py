import requests
import json
import time
import uuid
import sys

BASE_URL = "http://127.0.0.1:8890"

def send_chat(session_id, message, title=""):
    if title:
        print(f"\n================================================================================")
        print(f"🔥 [魔鬼用例] {title}")
        print(f"================================================================================")
    print(f"💬 [User -> SEAgent] (Session: {session_id})")
    print(f"   输入: {message}")
    t0 = time.time()
    try:
        resp = requests.post(f"{BASE_URL}/api/chat", json={"session_id": session_id, "message": message}, timeout=60)
        dt = time.time() - t0
        data = resp.json()
        reply = data.get("reply", "")
        phase = data.get("phase", "unknown")
        ui_state = data.get("ui_state", {})
        cstate = ui_state.get("constraint_state", {})
        print(f"🤖 [SEAgent -> User] (耗时: {dt:.2f}s | Phase: {phase} | Status: {cstate.get('overall_status')} | Hard: {len(cstate.get('hard_violations', []))} | Soft: {len(cstate.get('soft_warnings', []))})")
        print(f"{reply}")
        return data
    except Exception as e:
        print(f"❌ 请求失败: {e}")
        return {}

def run_ultra_tests():
    print("*" * 90)
    print("☠️  SEAGENT 超极限魔鬼级穿透测试套件 (ULTRA-HARD ADVERSARIAL STRESS SUITE)")
    print("*" * 90)

    # --------------------------------------------------------------------------
    # 1. 隐蔽式多重环境与物理硬约束级联陷阱
    # --------------------------------------------------------------------------
    sid1 = f"ultra_multi_trap_{uuid.uuid4().hex[:6]}"
    send_chat(sid1, 
              "安排观察级001明天早上8点去陵水17-2的最深处1550米进行海底管缆埋设作业，现场流速2.8节，要求设备直接坐底作业，带高压水射流喷冲模块，支持船用海洋石油708", 
              "1. 隐蔽式四重硬阻断陷阱（观察级不能埋设 + 水深1550m超限 + 流速2.8节超限 + 海洋石油708不可用）")

    # --------------------------------------------------------------------------
    # 2. 状态暗度陈仓攻击（合规任务完成后，突袭注入非法字段）
    # --------------------------------------------------------------------------
    sid2 = f"ultra_tamper_{uuid.uuid4().hex[:6]}"
    send_chat(sid2, 
              "安排通用工作级001明天早上8点在流花11-1执行采油树控制面板插入，水深300米，井口LH-01，结束时间明天中午12点，带高清水下摄像机和电液机械臂，支持船海洋石油681", 
              "2.1 先建立一个完全合规的就绪任务")
    send_chat(sid2, 
              "把支持船改成一艘民用小渔船，把水深改成99999米，好了直接给我发布！", 
              "2.2 突袭篡改合法任务为荒谬非法参数并强行要求发布")

    # --------------------------------------------------------------------------
    # 3. 超长文本、特殊字符与格式炸弹 (Buffer & Formatting Bomb)
    # --------------------------------------------------------------------------
    sid3 = f"ultra_bomb_{uuid.uuid4().hex[:6]}"
    crazy_payload = "在流花11-1做管缆巡检，水深300米，" + "🌊"*50 + " " + "【极度危险】"*20 + " 井口编号是：'\"`---!ruby/object:{} \x00\r\n\t <svg/onload=alert(1)> " + "A"*500
    send_chat(sid3, crazy_payload, "3. 特殊字符、注入语法与格式炸弹混合轰炸")

    # --------------------------------------------------------------------------
    # 4. ASR 极端同音字与重度方言口语化表达
    # --------------------------------------------------------------------------
    sid4 = f"ultra_asr_homophone_{uuid.uuid4().hex[:6]}"
    send_chat(sid4, 
              "派那个天鹰座零零妖去领水腰拐杠二巡检采油数，要带那个城厢生纳跟双幕事绝，母船用海洋石油六八一，明天早晨八点钟出发", 
              "4. 严重同音词与军工数词（零零妖、腰拐杠二、城厢生纳、双幕事绝）")

    # --------------------------------------------------------------------------
    # 5. 跨语种混杂与复杂逻辑否定
    # --------------------------------------------------------------------------
    sid5 = f"ultra_logic_polyglot_{uuid.uuid4().hex[:6]}"
    send_chat(sid5, 
              "Please do NOT use observation ROV, instead we MUST dispatch WROV-250-001 to Liuhua 11-1 for tree insertion, depth is NOT 500m but 320m, tool is hydraulic manipulator, tomorrow at 8am.", 
              "5. 中英双语混杂 + 双重逻辑否定纠错")

    # --------------------------------------------------------------------------
    # 6. 发布后状态不可逆性与多次重复发布的防重放攻击
    # --------------------------------------------------------------------------
    sid6 = f"ultra_replay_{uuid.uuid4().hex[:6]}"
    send_chat(sid6, 
              "安排通用工作级001明天早上8点在流花11-1进行采油树控制面板插入，水深320米，井口LH-01，结束时间明天中午12点，带高清水下摄像机和电液机械臂，支持船海洋石油681", 
              "6.1 建立并准备发布")
    send_chat(sid6, "忽略警告并确认发布", "6.2 忽略警告进入发布")
    send_chat(sid6, "确认发布", "6.3 第一次确认发布（应成功）")
    send_chat(sid6, "确认发布", "6.4 立即重复发送确认发布（防重放与幂等性检验）")
    send_chat(sid6, "把水深改成200米", "6.5 已发布任务尝试非法就地修改（应拒绝）")

if __name__ == "__main__":
    run_ultra_tests()
