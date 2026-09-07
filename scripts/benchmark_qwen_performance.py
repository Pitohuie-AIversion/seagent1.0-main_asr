#!/usr/bin/env python3
"""
Qwen3.5-9B on NVIDIA RTX 5090 (32GB) 性能基准测试脚本 (gpu_memory_utilization=0.95)
用于得出极限显存利用下的核心推理性能指标，以便与 HB10 或其他芯片进行横向对比。
"""

import os
import sys
import time
import json
import torch
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Any

# 避免 libgomp 报错
os.environ["OMP_NUM_THREADS"] = "8"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"

MODEL_PATH = "/root/autodl-tmp/model/Qwen3.5-9B"
GPU_MEMORY_UTILIZATION = 0.95

def get_gpu_memory_info():
    import subprocess
    try:
        res = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free", "--format=csv,nounits,noheader"],
            encoding="utf-8"
        ).strip().split(",")
        return {
            "total_mb": float(res[0].strip()),
            "used_mb": float(res[1].strip()),
            "free_mb": float(res[2].strip())
        }
    except Exception as e:
        return {"error": str(e)}

def run_benchmark():
    print(f"================================================================================")
    print(f"🚀 启动 Qwen3.5-9B 性能基准压测 (vLLM 0.95 显存利用率)")
    print(f"================================================================================")
    
    mem_before = get_gpu_memory_info()
    print(f"[*] 初始显存状态: 总量 {mem_before.get('total_mb', 0):.1f} MB, 已用 {mem_before.get('used_mb', 0):.1f} MB, 空闲 {mem_before.get('free_mb', 0):.1f} MB")

    from transformers import AutoTokenizer
    from vllm import LLM, SamplingParams

    print(f"[*] 正在加载 Tokenizer: {MODEL_PATH}")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, trust_remote_code=True, local_files_only=True)

    print(f"[*] 正在初始化 vLLM 引擎 (gpu_memory_utilization={GPU_MEMORY_UTILIZATION}, BF16)...")
    init_start = time.time()
    llm = LLM(
        model=MODEL_PATH,
        trust_remote_code=True,
        dtype="bfloat16" if torch.cuda.is_bf16_supported() else "float16",
        gpu_memory_utilization=GPU_MEMORY_UTILIZATION,
        max_num_seqs=64,
        max_model_len=16384,
        enable_prefix_caching=False,  # 基准测试禁用 prefix caching 以测试无缓存真实算力
    )
    init_duration = time.time() - init_start
    print(f"✅ vLLM 引擎加载完成，耗时: {init_duration:.2f} 秒")

    mem_after_load = get_gpu_memory_info()
    print(f"[*] 模型及 KV Cache 加载后显存: 总量 {mem_after_load.get('total_mb', 0):.1f} MB, 已用 {mem_after_load.get('used_mb', 0):.1f} MB ({mem_after_load.get('used_mb', 0)/mem_after_load.get('total_mb', 1)*100:.1f}%), 空闲 {mem_after_load.get('free_mb', 0):.1f} MB")

    # 预热 Warmup
    print(f"[*] 正在执行引擎预热 (Warmup)...")
    warmup_params = SamplingParams(temperature=0.0, max_tokens=32)
    llm.generate(["你好，请介绍一下你自己。"], warmup_params)
    print(f"✅ 预热完成！")

    benchmark_results = {
        "device": "NVIDIA GeForce RTX 5090",
        "vram_total_mb": mem_after_load.get("total_mb", 0),
        "vram_allocated_mb": mem_after_load.get("used_mb", 0),
        "gpu_memory_utilization_setting": GPU_MEMORY_UTILIZATION,
        "model": "Qwen3.5-9B",
        "precision": "bfloat16",
        "latency_scenarios": [],
        "throughput_scenarios": []
    }

    # =========================================================================
    # 测试维度 1：单请求时延基准测试 (Batch Size = 1)
    # =========================================================================
    print(f"\n" + "="*80)
    print(f"📊 阶段 1: 单请求基准时延测试 (Batch Size = 1)")
    print(f"="*80)

    latency_configs = [
        {"name": "短文本交互", "input_len": 128, "output_len": 128},
        {"name": "标准对话/任务抽取", "input_len": 512, "output_len": 256},
        {"name": "长上下文对话", "input_len": 2048, "output_len": 512},
    ]

    base_dummy_text = "水下多智能体系统在深海巡检任务中具有至关重要的作用。" * 100

    for cfg in latency_configs:
        prompt_tokens = tokenizer.encode(base_dummy_text)[:cfg["input_len"]]
        prompt_text = tokenizer.decode(prompt_tokens)
        sampling_params = SamplingParams(
            temperature=0.0,
            max_tokens=cfg["output_len"],
            ignore_eos=True  # 强制生成满 output_len 个 token 以精确测算生成速率
        )

        # 运行 3 次取平均
        times = []
        out_tokens_counts = []
        for _ in range(3):
            torch.cuda.synchronize()
            t0 = time.perf_counter()
            outputs = llm.generate([prompt_text], sampling_params)
            torch.cuda.synchronize()
            t1 = time.perf_counter()
            times.append(t1 - t0)
            out_tokens_counts.append(len(outputs[0].outputs[0].token_ids))

        avg_latency = sum(times) / len(times)
        actual_output_tokens = out_tokens_counts[0]
        tpot = (avg_latency / actual_output_tokens) * 1000  # ms per token
        decode_speed = actual_output_tokens / avg_latency   # tokens/s

        res_item = {
            "scenario": cfg["name"],
            "input_tokens": cfg["input_len"],
            "output_tokens": actual_output_tokens,
            "avg_e2e_latency_s": round(avg_latency, 4),
            "tpot_ms_per_token": round(tpot, 2),
            "generation_tokens_per_s": round(decode_speed, 2)
        }
        benchmark_results["latency_scenarios"].append(res_item)

        print(f"  - [{cfg['name']}] Input: {cfg['input_len']}t, Output: {actual_output_tokens}t")
        print(f"    端到端延迟: {avg_latency*1000:.2f} ms | TPOT (单token延迟): {tpot:.2f} ms/token | 解码吞吐: {decode_speed:.2f} tokens/s")

    # =========================================================================
    # 测试维度 2：多并发与极限吞吐测试 (Batch Size = 4, 8, 16, 32, 64)
    # =========================================================================
    print(f"\n" + "="*80)
    print(f"📊 阶段 2: 多并发阶梯吞吐测试 (Input: 512, Output: 256)")
    print(f"="*80)

    concurrency_list = [4, 8, 16, 32, 64]
    input_len = 512
    output_len = 256

    prompt_tokens = tokenizer.encode(base_dummy_text)[:input_len]
    prompt_text = tokenizer.decode(prompt_tokens)

    for concurrency in concurrency_list:
        prompts = [prompt_text] * concurrency
        sampling_params = SamplingParams(
            temperature=0.0,
            max_tokens=output_len,
            ignore_eos=True
        )

        torch.cuda.synchronize()
        t0 = time.perf_counter()
        outputs = llm.generate(prompts, sampling_params)
        torch.cuda.synchronize()
        t1 = time.perf_counter()
        total_time = t1 - t0

        total_input_tokens = input_len * concurrency
        total_output_tokens = sum(len(out.outputs[0].token_ids) for out in outputs)
        total_tokens = total_input_tokens + total_output_tokens

        output_throughput = total_output_tokens / total_time
        total_throughput = total_tokens / total_time
        avg_req_latency = total_time

        mem_now = get_gpu_memory_info()

        item = {
            "concurrency": concurrency,
            "input_tokens_per_req": input_len,
            "output_tokens_per_req": output_len,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            "duration_s": round(total_time, 4),
            "output_throughput_tps": round(output_throughput, 2),
            "total_throughput_tps": round(total_throughput, 2),
            "avg_latency_per_batch_s": round(avg_req_latency, 4),
            "vram_used_mb": mem_now.get("used_mb", 0)
        }
        benchmark_results["throughput_scenarios"].append(item)

        print(f"  - [并发 Concurrency = {concurrency:2d}] 耗时: {total_time:.2f}s")
        print(f"    生成吞吐 (Output Tokens/s): {output_throughput:7.2f} tokens/s")
        print(f"    总吞吐 (Total Tokens/s) : {total_throughput:7.2f} tokens/s")
        print(f"    批次延迟: {avg_req_latency:.2f}s | 显存占用: {mem_now.get('used_mb', 0):.1f} MB")

    # =========================================================================
    # 生成性能报告 Markdown 文件
    # =========================================================================
    report_path = "/root/mzy/seagent1.0-main_asr/test_logs/Qwen3.5-9B_RTX5090_VRAM0.95_Benchmark_Report.md"
    generate_markdown_report(benchmark_results, report_path)
    print(f"\n" + "="*80)
    print(f"🎉 测试完成！性能对比报告已生成: {report_path}")
    print(f"="*80)

def generate_markdown_report(data: Dict[str, Any], output_path: str):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# Qwen3.5-9B 推理性能基准测试报告 (vLLM 0.95 最大显存配置)\n\n")
        f.write("> **测试目的**：评测 RTX 5090 (32GB) 在显存分配达 95% 极限利用率下的推理性能指标（时延、解码速率、并发吞吐量），供后续与专用加速芯片（如 HB10）进行横向性能对比。\n\n")
        
        f.write("## 1. 硬件与环境配置 (Hardware & Environment)\n\n")
        f.write(f"- **测试设备**: `{data['device']}` (显存总量: `{data['vram_total_mb']:.1f} MB` / ~32GB)\n")
        f.write(f"- **显存占用分配设置**: `gpu_memory_utilization = {data['gpu_memory_utilization_setting']}`\n")
        f.write(f"- **实测显存占用水位**: `{data['vram_allocated_mb']:.1f} MB` ({data['vram_allocated_mb']/data['vram_total_mb']*100:.1f}% 饱和)\n")
        f.write(f"- **模型权重**: `{data['model']}` (BF16, 物理路径 `/root/autodl-tmp/model/Qwen3.5-9B`)\n")
        f.write(f"- **推理引擎**: `vLLM 0.18.1` (Prefix Caching: 关闭以测纯算力, PagedAttention 启用)\n")
        f.write(f"- **CUDA / 驱动**: `CUDA 13.2` / `Driver 595.71.05`\n\n")

        f.write("## 2. 单请求基准时延测试 (Batch Size = 1 Latency)\n\n")
        f.write("主要用于衡量单个交互请求的端到端时延和解码流式生成速度。\n\n")
        f.write("| 业务场景 | 输入 Token | 输出 Token | 端到端总时延 (s) | TPOT (单Token延迟) | 解码生成速率 (Tokens/s) |\n")
        f.write("| :--- | :--- | :--- | :--- | :--- | :--- |\n")
        for s in data["latency_scenarios"]:
            f.write(f"| {s['scenario']} | {s['input_tokens']} | {s['output_tokens']} | {s['avg_e2e_latency_s']} s | **{s['tpot_ms_per_token']} ms** | **{s['generation_tokens_per_s']} tokens/s** |\n")
        f.write("\n")

        f.write("## 3. 多并发极限吞吐测试 (Concurrency & Throughput)\n\n")
        f.write("固定输入 512 Tokens，输出 256 Tokens（典型智能体任务抽取负载），测试不同并发压力下的吞吐提升与显存饱和度。\n\n")
        f.write("| 并发序列数 (Concurrency) | 生成 Token 总数 | 批次总耗时 (s) | **生成吞吐 (Output Tokens/s)** | **系统总吞吐 (Total Tokens/s)** | 显存占用 (MB) |\n")
        f.write("| :---: | :---: | :---: | :---: | :---: | :---: |\n")
        for t in data["throughput_scenarios"]:
            f.write(f"| **{t['concurrency']}** | {t['total_output_tokens']} | {t['duration_s']} s | **{t['output_throughput_tps']}** | **{t['total_throughput_tps']}** | {t['vram_used_mb']:.1f} |\n")
        f.write("\n")

        f.write("## 4. 关键结论与 HB10 对比建议 (Conclusions & Comparison Guide)\n\n")
        
        # 提取极值
        max_output_tps = max(t["output_throughput_tps"] for t in data["throughput_scenarios"])
        max_total_tps = max(t["total_throughput_tps"] for t in data["throughput_scenarios"])
        bs1_speed = data["latency_scenarios"][1]["generation_tokens_per_s"]
        bs1_tpot = data["latency_scenarios"][1]["tpot_ms_per_token"]

        f.write(f"1. **单卡单流首字与解码表现**: 在标准交互场景 (512 in / 256 out) 下，单流生成速率达到 **{bs1_speed} tokens/s**，单 Token 解码延迟 (TPOT) 为 **{bs1_tpot} ms**。\n")
        f.write(f"2. **显存 95% 饱和吞吐极限**: 当并发提升至 64 时，32GB 显存几乎完全被 KV Cache 充满，生成吞吐达到峰值 **{max_output_tps} tokens/s**（系统总吞吐达 **{max_total_tps} tokens/s**）。\n")
        f.write("3. **与 HB10 对比的关键对照基准**:\n")
        f.write("   - **吞吐量维度**: 对比相同模型 (Qwen-9B/8B 量级) 在 BS=1, 8, 32, 64 时的生成 Tokens/s。\n")
        f.write("   - **时延维度**: 对比首字延迟 (TTFT) 及每 Token 解码延迟 (TPOT)。\n")
        f.write("   - **显存与上下文容量**: 对比最大可容纳并发 KV Cache 块数及最长支持的 Context 长度。\n")

if __name__ == "__main__":
    run_benchmark()
