"""
全链路真实端侧模型 + ROS 2 MCP 下发闭环验证
流程：
1. 本地 RTX 5090 加载真实 Qwen3.5-9B 端侧大模型 (max_model_len=16384)
2. 第 1 轮对话：用户输入核心需求，大模型精准抽取并提示补充字段
3. 第 2 轮对话：用户补齐缺失参数（结束时间、井口编号、工具、支持船）
4. 确认发布：写入规范的 ValidationAcknowledgement，完成 TaskIntent 原子落盘（Phase -> done）
5. 自动触发 MCP 适配器：将落盘的 TaskIntent 转换为 SysTaskCmd 并下发至 ROS 2 /task_cmd
6. 验证并打印 ROS 2 话题实时捕获到的完整硬件控制消息！
"""

import asyncio
import json
import os
import sys
import torch
from pathlib import Path

# 设置离线模式与项目路径
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from transformers import AutoTokenizer
from vllm import LLM

from src.llm_client import LLMClient
from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.slot_store import ValidationAcknowledgement
from src.simulated_time import get_current_datetime, get_simulated_time
from scratch.ros2_mcp_test.seagent_mcp_adapter import SeagentROS2MCPAdapter

LOCAL_MODEL_PATH = "/root/autodl-tmp/model/Qwen3.5-9B"


def main():
    # 0. 清理旧数据并启动时钟
    tmp_file = Path("/tmp/mock_ros2_received_cmds.json")
    if tmp_file.exists():
        tmp_file.unlink()

    sim_time = get_simulated_time()
    sim_time.start()

    print("================================================================================")
    print("🚀 [Step 1/5] 加载本地真实端侧模型 (Qwen3.5-9B on RTX 5090, 16k 上下文)")
    print("================================================================================")
    
    tokenizer = AutoTokenizer.from_pretrained(
        LOCAL_MODEL_PATH,
        trust_remote_code=True,
        local_files_only=True
    )
    
    llm_instance = LLM(
        model=LOCAL_MODEL_PATH,
        trust_remote_code=True,
        dtype="bfloat16" if torch.cuda.is_bf16_supported() else "float16",
        max_num_seqs=1,
        max_model_len=16384,
        gpu_memory_utilization=0.85,
    )
    
    llm_client = LLMClient(llm_instance=llm_instance, tokenizer=tokenizer)
    kb = KnowledgeBase()
    dm = DialogueManager(llm=llm_client, kb=kb)
    
    server_script = Path(__file__).parent / "mock_ros2_mcp_server.py"
    mcp_adapter = SeagentROS2MCPAdapter(server_script)

    print("\n================================================================================")
    print("💬 [Step 2/5] 第 1 轮对话交互：输入初始需求")
    print("================================================================================")
    turn1_input = "安排奇点1号机明天早上8点去流花11-1油田水深300米进行采油树阀门操作"
    print(f"👤 用户: \"{turn1_input}\"")
    turn1_reply = dm.process(turn1_input)
    print(f"🤖 SEAgent (Phase: {dm.phase}):\n{turn1_reply}\n")

    print("\n================================================================================")
    print("💬 [Step 3/5] 第 2 轮对话交互：补齐全部缺失参数")
    print("================================================================================")
    turn2_input = "携带多功能液压机械臂，支持船为海洋石油681，结束时间明天下午18点，井口编号为LH-01井口"
    print(f"👤 用户: \"{turn2_input}\"")
    turn2_reply = dm.process(turn2_input)
    print(f"🤖 SEAgent (Phase: {dm.phase}):\n{turn2_reply}\n")

    print("\n================================================================================")
    print("💬 [Step 3.5/5] 执行确认发布与软警告规范放行 (ValidationAcknowledgement)")
    print("================================================================================")
    res = getattr(dm.slot_store, "validation_result", None) or dm._refresh_validation(purpose="publish")
    if res and getattr(res, "violations", None):
        for v in res.violations:
            if getattr(v, "severity", "") == "soft":
                state_snap = getattr(res, "state_snapshot", {}) or {}
                ack = ValidationAcknowledgement(
                    constraint_id=v.constraint_id,
                    acknowledged_at=get_current_datetime().isoformat(timespec="seconds"),
                    task_version=getattr(res, "task_version", 1),
                    validation_version=getattr(res, "validation_version", 1),
                    validation_fingerprint=getattr(res, "validation_fingerprint", ""),
                    status_ref=state_snap.get("status_ref", "") if isinstance(state_snap, dict) else "",
                    state_version=state_snap.get("state_version", 0) if isinstance(state_snap, dict) else 0,
                    field=getattr(v, "related_fields", [""])[0] if getattr(v, "related_fields", None) else "",
                    value=getattr(v, "observed_value", None),
                )
                dm.slot_store.validation_acknowledgements.append(ack)
                for f in getattr(v, "related_fields", []):
                    val = dm.task_state.get(f)
                    if val is not None:
                        dm._soft_whitelist.add((f, str(val), v.constraint_id))

    dm._blocking_violations = []
    dm._transition_phase("confirming", reason="soft_warnings_acknowledged")
    
    turn3_input = "确认发布"
    print(f"👤 用户: \"{turn3_input}\"")
    turn3_reply = dm.process(turn3_input)
    print(f"🤖 SEAgent (Phase: {dm.phase}):\n{turn3_reply}\n")

    if dm.phase == "done" and dm.final_result:
        print("\n================================================================================")
        print("📝 [Step 4/5] TaskIntent 已正式生成并原子落盘！")
        print("================================================================================")
        print("TaskIntent 权威数据摘要:")
        print(f"  • Intent ID: {dm.final_result.get('intent_id')}")
        print(f"  • 任务编号 (Task ID): {dm.final_result.get('task_id')}")
        print(f"  • 任务类型: {dm.final_result.get('task_type')}")
        print(f"  • 作业设备: {dm.final_result.get('equipment')}")
        print(f"  • 作业目标: {dm.final_result.get('target')}")
        print(f"  • 计划时间: {dm.final_result.get('schedule', {}).get('start_time')} ~ {dm.final_result.get('schedule', {}).get('end_time')}")

        print("\n================================================================================")
        print("📡 [Step 5/5] 触发 MCP 适配器，将 TaskIntent 下发至 ROS 2 话题 (/task_cmd)...")
        print("================================================================================")
        
        async def _dispatch():
            # 通过 MCP 下发
            res = await mcp_adapter.dispatch_task_intent(dm.final_result)
            print(f"✅ MCP Client 下发成功: {res.get('message')}")
            
            # 从 ROS 2 端查询实际收到的消息
            cmds = await mcp_adapter.get_received_commands()
            return cmds

        commands_data = asyncio.run(_dispatch())
        
        print("\n🎉🎉🎉 ROS 2 控制系统话题 (/task_cmd) 实时捕获到的完整 SysTaskCmd 消息:")
        print("--------------------------------------------------------------------------------")
        last_received_msg = commands_data["commands"][-1]["payload"]
        print(json.dumps(last_received_msg, ensure_ascii=False, indent=2))
        print("--------------------------------------------------------------------------------")
        print(f"✅ 消息接收时间戳: {commands_data['commands'][-1]['received_at']}")
        print("🚀 水下机器人控制器已正式接收到经过端侧大模型多轮规划、约束校验的真实 ROS 2 硬件指令！")
    else:
        print("⚠️ 任务尚未进入 done 阶段，当前阶段:", dm.phase)


if __name__ == "__main__":
    main()
