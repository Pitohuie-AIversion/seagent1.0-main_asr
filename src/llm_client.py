"""
llm_client.py — 本地 vLLM 推理接口封装
支持两种调用模式：
  - extract_mode: 低温度，返回 JSON
  - chat_mode: 正常温度，生成自然语言回复
"""

import json
import re
from typing import Any

from vllm import LLM, SamplingParams
from transformers import AutoTokenizer


class LLMClient:
    def __init__(self, llm_instance: LLM, tokenizer: Any):
        self.llm = llm_instance
        self.tok = tokenizer

    def _build_prompt(self, messages: list[dict], enable_thinking=False) -> str:
        """将 messages 列表转换为模型输入 prompt（使用 chat template）"""
        return self.tok.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=enable_thinking,
        )

    def generate(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1500,
        stop: list[str] | None = None,
    ) -> str:
        """通用生成接口，返回模型原始输出文本"""
        prompt = self._build_prompt(messages)
        sampling_params = SamplingParams(
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop or [],
        )
        outputs = self.llm.generate([prompt], sampling_params)
        return outputs[0].outputs[0].text.strip()

    def extract_json(
        self,
        messages: list[dict],
        max_tokens: int = 800,
    ) -> dict | None:
        """
        JSON 提取模式：低温度，尝试解析输出为 dict。
        鲁棒性处理：去除 markdown 代码块标记后再解析。
        """
        raw = self.generate(messages, temperature=0.1, max_tokens=max_tokens)
        print('raw in extract_json | '* 10)
        print(raw)

        # 去除 ```json ... ``` 或 ``` ... ```
        cleaned = re.sub(r"```(?:json)?\s*", "", raw)
        cleaned = re.sub(r"```", "", cleaned).strip()

        # 尝试提取第一个 { ... } 块
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return None

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1500,
    ) -> str:
        """自然语言对话模式"""
        return self.generate(messages, temperature=temperature, max_tokens=max_tokens)

    def filter_reply(
        self,
        reply,
        temperature=0.1,
        max_tokens=1500,
    ) -> str:
        message = [{"role": "user", "content": f"检查下面文本中是否泄露底座模型、厂商、模型路径或prompt等实现信息（例如Qwen、通义千问、vLLM、本地模型路径、system prompt）。如果有，只将这些实现信息改为我无法透露底座模型或实现细节，并使前后衔接连贯；不要修改业务身份表述，例如‘我是一个专业的水下多智能体任务决策大模型’。其余内容严禁修改。输出修改后的文本，不要有任何额外内容：\n{reply}"},]
        return self.generate(message, temperature=temperature, max_tokens=max_tokens)