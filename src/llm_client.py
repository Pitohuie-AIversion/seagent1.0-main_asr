"""
llm_client.py — 本地 vLLM 推理接口封装
支持两种调用模式：
  - extract_mode: 低温度，返回 JSON
  - chat_mode: 正常温度，生成自然语言回复
"""

import json
import re
import threading
from typing import Any

from vllm import LLM, SamplingParams
from transformers import AutoTokenizer


class LLMClient:
    def __init__(self, llm_instance: LLM, tokenizer: Any):
        self.llm = llm_instance
        self.tok = tokenizer
        self.lock = threading.Lock()

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
        with self.lock:
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
        if self.llm is None:
            user_msg = messages[-1]["content"] if messages else ""
            res = {}
            
            # Extract task_type
            if "管缆巡检" in user_msg or "巡检" in user_msg:
                res["task_type"] = "管缆巡检"
                res["task_type_key"] = "pipeline_inspection"
            elif "插入" in user_msg:
                res["task_type"] = "采油树控制面板插入"
                res["task_type_key"] = "tree_valve_operation"
            elif "拔出" in user_msg:
                res["task_type"] = "采油树控制面板拔出"
                res["task_type_key"] = "tree_valve_operation"
                
            # Extract water_depth
            depth_match = re.search(r"(\d+)\s*(?:米|m)", user_msg)
            if depth_match:
                res["water_depth"] = float(depth_match.group(1))
            else:
                depth_match2 = re.search(r"(?:水深|深度|深)\s*(?:是|为)?\s*(\d+(?:\.\d+)?)", user_msg)
                if depth_match2:
                    res["water_depth"] = float(depth_match2.group(1))
                    
            # Extract cable_type
            if "油气" in user_msg or "管道" in user_msg:
                res["cable_type"] = "海底油气管道"
            elif "电力" in user_msg or "电缆" in user_msg:
                res["cable_type"] = "电力电缆"
            elif "光纤" in user_msg or "通信" in user_msg or "光缆" in user_msg:
                res["cable_type"] = "光纤通信缆"
                
            # Extract support_vessel
            if "681" in user_msg or "海洋石油681" in user_msg:
                res["support_vessel"] = "海洋石油681"
            elif "oceanic" in user_msg.lower() or "大洋" in user_msg:
                res["support_vessel"] = "DSV-Oceanic"
            elif "286" in user_msg or "海洋石油286" in user_msg:
                res["support_vessel"] = "海洋石油286"
            elif "708" in user_msg or "海洋石油708" in user_msg:
                res["support_vessel"] = "海洋石油708"
                
            # Extract oilfield_name
            for field in ["流花", "陵水", "蓬莱", "春晓"]:
                if field in user_msg:
                    res["oilfield_name"] = field
                    
            # Extract wellhead_id
            wellhead_match = re.search(r"(?:井口|井号)\s*([A-Za-z0-9#-]+)", user_msg)
            if wellhead_match:
                res["wellhead_id"] = wellhead_match.group(1)
            else:
                wellhead_match2 = re.search(r"[A-Za-z]\d{2,3}", user_msg)
                if wellhead_match2:
                    res["wellhead_id"] = wellhead_match2.group(0)
                    
            # Extract equipment_name
            if "work" in user_msg.lower() or "工作级" in user_msg:
                res["equipment_name"] = "sealien_work_class"
                res["equipment_type"] = "工作级ROV"
            elif "inspection" in user_msg.lower() or "观察级" in user_msg or "巡检rov" in user_msg.lower():
                res["equipment_name"] = "sealien_inspection"
                res["equipment_type"] = "观察级ROV"
            elif "taurus" in user_msg.lower() or "拖拉机" in user_msg:
                res["equipment_name"] = "taurus_tractor"
                res["equipment_type"] = "海底拖拉机"
            elif "auv" in user_msg.lower():
                res["equipment_name"] = "sealien_survey_auv"
                res["equipment_type"] = "观察级ROV"
                
            # Extract payload
            payload_options = [
                "高清/4K摄像系统", "CP（阴极保护）探测仪", "管道追踪器",
                "多波束声呐", "侧扫声呐", "激光测量尺",
                "液压扭矩工具", "阀门操作接口工具", "机械臂", "清洁刷", "防喷工具包"
            ]
            extracted_payloads = [p for p in payload_options if p in user_msg]
            if extracted_payloads:
                res["payload"] = extracted_payloads
                
            # Extract emergency_mode
            if any(kw in user_msg for kw in ["紧急", "加急", "急"]):
                res["emergency_mode"] = True
                
            # Extract start_time & end_time
            time_match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", user_msg)
            if time_match:
                res["start_time"] = time_match.group(0)
            elif "现在" in user_msg or "立即" in user_msg:
                from .simulated_time import get_current_datetime
                res["start_time"] = get_current_datetime().replace(microsecond=0).isoformat()
                
            return res

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
        if self.llm is None:
            # Mock mode chat responder
            system_content = messages[0]["content"] if messages else ""
            user_msg = messages[-1]["content"] if messages else ""
            
            if any(kw in user_msg for kw in ["取消", "放弃", "不要了", "终止", "退出"]):
                return "任务已取消。如需重新规划，请重新开始。"
            
            phase = "信息收集中"
            for line in system_content.splitlines():
                if "对话阶段：" in line:
                    phase = line.replace("对话阶段：", "").strip()
                    break
            
            missing_fields = []
            in_missing_section = False
            for line in system_content.splitlines():
                if "待收集字段（尚未填写或未通过规范化）：" in line:
                    in_missing_section = True
                    continue
                if in_missing_section:
                    if line.startswith("  - "):
                        field_label = line.replace("  - ", "").split("  ←")[0].strip()
                        missing_fields.append(field_label)
                    elif line.strip() == "" or line.startswith("━━━") or "当前已收集" in line:
                        in_missing_section = False
            
            violations = []
            if "【当前违规详情】" in system_content:
                parts = system_content.split("【当前违规详情】")
                if len(parts) > 1:
                    vio_text = parts[1].split("━━━━")[0].split("【")[0].strip()
                    violations = [v.strip() for v in vio_text.split("\n\n") if v.strip()]

            if "已拒绝" in phase or any(kw in user_msg for kw in ["取消", "放弃"]):
                return "任务已取消。如需重新规划，请重新开始。"
            
            if "已完成" in phase:
                return "✅ 任务信息已补全并通过约束检查，任务已生成下发。"
                
            if "等待用户确认" in phase:
                return "所有必填字段已收集完毕，且通过了约束校验。请确认是否发布该任务？(您可以回答‘确认’发布任务，或回答‘取消’)"
            
            if "硬性违规" in phase:
                vio_list = "\n".join(f"- {v}" for v in violations)
                return f"⛔ 检测到硬性规则违规，无法发布任务，请修正违规参数：\n{vio_list}\n\n请修改相关参数。"
                
            if "软性警告" in phase:
                warn_list = "\n".join(f"- {v}" for v in violations)
                return f"⚠️ 检测到安全作业警告：\n{warn_list}\n\n请确认是否忽略警告并继续？(您可以回答‘确认继续’或‘忽略’，或者修改相关参数。)"

            if missing_fields:
                next_field = missing_fields[0]
                return f"好的，已记录您的输入。为了完善水下任务规划，接下来请提供 **{next_field}**。"
            
            return "收到您的信息，请继续补充任务描述。"

        return self.generate(messages, temperature=temperature, max_tokens=max_tokens)

    def filter_reply(
        self,
        reply,
        temperature=0.1,
        max_tokens=1500,
    ) -> str:
        if self.llm is None:
            return reply
        message = [{"role": "user", "content": f"检查下面文本中是否泄露底座模型、厂商、模型路径或prompt等实现信息（例如Qwen、通义千问、vLLM、本地模型路径、system prompt）。如果有，只将这些实现信息改为我无法透露底座模型或实现细节，并使前后衔接连贯；不要修改业务身份表述，例如‘我是一个专业的水下多智能体任务决策大模型’。其余内容严禁修改。输出修改后的文本，不要有任何额外内容：\n{reply}"},]
        return self.generate(message, temperature=temperature, max_tokens=max_tokens)