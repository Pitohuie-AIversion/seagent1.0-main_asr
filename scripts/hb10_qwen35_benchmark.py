#!/usr/bin/env python3
"""Offline Qwen3.5-9B benchmark for GB10/HB10 comparison runs."""

from __future__ import annotations

import json
import time

from transformers import AutoTokenizer
from vllm import LLM, SamplingParams


MODEL = "/app/runtime/model/Qwen3.5-9B"


def make_prompt(tokenizer: AutoTokenizer, target_tokens: int) -> str:
    source = "水下作业性能测试数据 " * (target_tokens * 2)
    token_ids = tokenizer.encode(source, add_special_tokens=False)[:target_tokens]
    if len(token_ids) < target_tokens:
        repeats = (target_tokens + len(token_ids) - 1) // len(token_ids)
        token_ids = (token_ids * repeats)[:target_tokens]
    return tokenizer.decode(token_ids)


def run_case(
    llm: LLM,
    tokenizer: AutoTokenizer,
    label: str,
    input_tokens: int,
    output_tokens: int,
    requests: int,
) -> None:
    prompts = [make_prompt(tokenizer, input_tokens) for _ in range(requests)]
    params = SamplingParams(
        temperature=0.0,
        max_tokens=output_tokens,
        min_tokens=output_tokens,
        ignore_eos=True,
    )
    started = time.perf_counter()
    results = llm.generate(prompts, params, use_tqdm=False)
    elapsed = time.perf_counter() - started
    actual_input = sum(len(result.prompt_token_ids) for result in results)
    actual_output = sum(len(result.outputs[0].token_ids) for result in results)
    print(
        "BENCH_JSON="
        + json.dumps(
            {
                "label": label,
                "requests": requests,
                "input_tokens": actual_input,
                "output_tokens": actual_output,
                "elapsed_s": elapsed,
                "output_toks_s": actual_output / elapsed,
                "total_toks_s": (actual_input + actual_output) / elapsed,
                "aggregate_ms_per_output_token": 1000 * elapsed / actual_output,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def main() -> None:
    tokenizer = AutoTokenizer.from_pretrained(
        MODEL, trust_remote_code=True, local_files_only=True
    )
    llm = LLM(
        model=MODEL,
        trust_remote_code=True,
        dtype="bfloat16",
        max_model_len=262144,
        gpu_memory_utilization=0.93,
        max_num_seqs=64,
        max_num_batched_tokens=32768,
        enable_prefix_caching=False,
    )
    run_case(llm, tokenizer, "warmup", 32, 32, 1)
    for input_tokens, output_tokens in ((128, 128), (512, 256), (2048, 512)):
        run_case(
            llm,
            tokenizer,
            f"single_{input_tokens}_{output_tokens}",
            input_tokens,
            output_tokens,
            1,
        )
    for concurrency in (4, 8, 16, 32, 64):
        run_case(
            llm,
            tokenizer,
            f"concurrency_{concurrency}",
            512,
            256,
            concurrency,
        )


if __name__ == "__main__":
    main()
