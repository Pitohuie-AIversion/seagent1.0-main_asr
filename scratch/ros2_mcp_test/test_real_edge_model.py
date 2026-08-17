"""
真实端侧模型推理验证脚本 (Real Edge Model Test)
使用本地 RTX 5090 显卡加载真实的 Qwen3.5-9B 模型（通过 vLLM 推理引擎），
执行真实自然语言任务对话，验证端侧模型的抽取与回复能力。
"""

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

LOCAL_MODEL_PATH = "/root/autodl-tmp/model/Qwen3.5-9B"


def main():
    print("==================================================")
    print("🚀 [1/3] 正在加载本地真实端侧模型:", LOCAL_MODEL_PATH)
    print("==================================================")
    
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
        max_model_len=8192,
        gpu_memory_utilization=0.85,
    )
    
    print("==================================================")
    print("⚙️ [2/3] 正在初始化 SEAgent 核心组件 (LLMClient + KnowledgeBase + DialogueManager)")
    print("==================================================")
    
    llm_client = LLMClient(llm_instance=llm_instance, tokenizer=tokenizer)
    kb = KnowledgeBase()
    dm = DialogueManager(llm=llm_client, kb=kb)
    
    test_user_input = "安排奇点1号机明天早上8点去流花11-1油田水深300米进行采油树阀门操作"
    print(f"\n🗣️ [3/3] 输入测试指令: \"{test_user_input}\"\n")
    print("⏳ 端侧大模型正在执行真实推理 (InteractionPlan 路由 -> Extractor 抽取 -> 约束校验 -> 回复生成)...")
    
    reply = dm.process(test_user_input)
    
    print("\n==================================================")
    print("🎉 端侧模型真实输出回复:")
    print("==================================================")
    print(reply)
    print("==================================================")
    print("📊 任务槽位当前状态 (Task State Snapshot):")
    print(dm.task_state)
    print("==================================================")
    print("✅ 真实端侧模型端到端运行验证完成！")


if __name__ == "__main__":
    main()
