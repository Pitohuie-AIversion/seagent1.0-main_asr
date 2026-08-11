"""
测试意图路由 LLM-First 修复效果的脚本
运行: OFFLINE_MOCK=1 python tests/test_router_constraints.py

修复前: 11/11 (100%) 的用例被前置规则拦截，LLM完全没机会
修复目标: 让 LLM 有机会优先判断（安全红线场景除外）
"""
import sys
import os
import json
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("OFFLINE_MOCK", "1")

from src.intent_router import IntentRouter, ModelRole
from src.llm_client import LLMClient
from src.knowledge_retriever import KnowledgeBase


class SmartMockLLM(LLMClient):
    """智能模拟 LLM，基于常见场景返回合理的路由 JSON —— 验证 LLM-First 是否生效。

    关键：override classify_interaction，避免基类 is_mock=True 时
    走内部 _mock_classify_interaction 规则抢占，直接返回语义路由结果。
    """

    def __init__(self):
        super().__init__(None, None)
        self.last_prompts_seen = []
        self.call_count = 0

    # ===================================================================
    # 核心：语义路由逻辑，和 extract_json / classify_interaction 共用
    # ===================================================================
    def _semantic_route(self, user_message: str) -> dict:
        msg = user_message
        if not msg:
            return self._clarify("空输入")

        # === 紧急控制 ===
        if any(kw in msg for kw in ("立即停止", "立刻停止", "紧急停止")):
            return self._control("stop", "立即停止当前任务")
        if any(kw in msg for kw in ("终止任务", "结束任务")):
            return self._control("abort", "终止当前任务")
        if "撤销任务" in msg or "取消任务" in msg:
            return self._control("cancel", "取消任务")
        if re.search(r"(暂停|先停)", msg):
            return self._control("pause", "暂停任务")

        # === WRITE: 任务创建自然表达 ===
        task_create_patterns = [
            r"(想|要|准备|打算|计划|帮我|请|给我|需要)\s*(个|个水下)?\s*(管缆|管道|油气|水下)?\s*(巡检|埋设|作业|任务|阀门)",
            r"(开始|做|弄|搞|去)\s*(管缆|水下)?\s*(巡检|埋设|采集|作业|阀门)",
        ]
        if any(re.search(p, msg) for p in task_create_patterns):
            if "阀门" in msg or "操作阀" in msg:
                task_type = "tree_valve_operation"
            elif "埋设" in msg:
                task_type = "pipeline_burial"
            else:
                task_type = "pipeline_inspection"
            return self._write_task_create(f"TASK_CREATE_{task_type.upper()}")

        # === WRITE: 设备选择/口语化 ===
        if re.search(r"(用|选|就用|选个|就选)\s*(金牛座|天鹰座|海龙|发现|工作级|LROV|观察级)", msg):
            return self._write_param("设备选择", "EQUIPMENT_SELECTION")

        # === WRITE: 有数字 + 任务上下文信号 (depth, coords) ===
        if re.search(r"(水深|深度|位置|坐标|经纬度)", msg) and re.search(r"\d+", msg):
            return self._write_param("参数赋值", "PARAMETER_ASSIGNMENT")

        # === WRITE: 纯数字或纯领域词（上下文有 task） ===
        if re.match(r"^\d+(\.\d+)?(米|m)?$", msg.strip()):
            return self._write_param("纯数字参数", "PURE_NUMBER_ASSIGNMENT")

        # === WRITE: 管缆类型选择（expected_slots 追问响应） ===
        if re.search(r"(电力电缆|光纤复合缆|脐带缆|输油管道)", msg):
            return self._write_param("管缆类型选择", "CABLE_TYPE_ASSIGNMENT")

        # === WRITE: 调整参数 ===
        if re.search(r"(调整到|调到|设为|改成|换成)", msg) and re.search(r"\d+", msg):
            return self._write_param("参数调整", "PARAMETER_ADJUSTMENT")

        # === READ: 知识库查询 / 设备介绍 / 设备对比 ===
        if re.search(r"(介绍|对比|区别|什么是|属于|哪个家族|family|支持哪些|payload|有哪些)", msg):
            return self._read_knowledge(msg)
        if re.search(r"(怎么样|如何|说说|看一下.*情况)", msg) and not re.search(r"\d+", msg):
            return self._read_knowledge(msg)

        # === READ: 条件疑问 / 查询准备 ===
        if re.search(r"(如果|要是|假如|假使|若|假设|万一).*(需要准备|一般|通常|怎么|怎么办|如何)", msg):
            return self._read_knowledge(msg)

        # === WRITE: 都想用（隐含任务创建） ===
        if re.search(r"(都想用|都需要|都要)", msg) and re.search(r"(ROV|AUV|机器人)", msg):
            return self._write_task_create("TASK_CREATE_MULTI_ROV")

        # === CONTROL: 算了不要这个任务 ===
        if re.search(r"(算了|不想要了|不要这个了|取消这个任务)", msg):
            return self._control("cancel", "取消当前任务")

        # === 问句形式 + 任务创建动词 + 参数 ===
        if re.search(r"(创建|生成|安排|规划|做|弄|搞).*(巡检|埋设|作业|任务|阀门)", msg):
            return self._write_task_create("TASK_CREATE_QUESTION_FORM")

        # === 裸词 / 极短 ===
        if len(msg) <= 8 and not re.search(r"(想|要|做|弄|搞|开始|去)", msg):
            return self._clarify(f"裸词/短输入: {msg}")

        # === 默认：让后端补齐（返回部分字段） ===
        return {
            "schema_version": 1,
            "operation": "READ",
            "dialogue_mode": "knowledge_qa",
            "query_intent": "GENERAL_CHAT",
            "subject_type": None,
            "subject_text": None,
            "relation": None,
            "source_policy": None,
            "needs_clarification": False,
            "clarification_reason": None,
            "emergency_action": None,
            "confidence": 0.55,
            "reason_code": "LLM_DEFAULT_READ",
        }

    def _write_task_create(self, intent: str) -> dict:
        return {
            "schema_version": 1,
            "operation": "WRITE",
            "dialogue_mode": "task_collection",
            "query_intent": "TASK_CREATE",
            "subject_type": "task",
            "subject_text": "水下作业任务",
            "relation": "define",
            "source_policy": "session_state",
            "needs_clarification": False,
            "clarification_reason": None,
            "emergency_action": None,
            "confidence": 0.85,
            "reason_code": intent,
        }

    def _write_param(self, label: str, code: str) -> dict:
        return {
            "schema_version": 1,
            "operation": "WRITE",
            "dialogue_mode": "task_collection",
            "query_intent": "TASK_UPDATE",
            "subject_type": "task",
            "subject_text": label,
            "relation": "update",
            "source_policy": "session_state",
            "needs_clarification": False,
            "clarification_reason": None,
            "emergency_action": None,
            "confidence": 0.8,
            "reason_code": code,
        }

    def _read_knowledge(self, msg: str) -> dict:
        subject_text = "设备"
        if "金牛座" in msg:
            subject_text = "金牛座"
        elif "ROV" in msg and "AUV" in msg:
            subject_text = "ROV/AUV"
        elif "阀门" in msg:
            subject_text = "阀门操作"
        return {
            "schema_version": 1,
            "operation": "READ",
            "dialogue_mode": "knowledge_qa",
            "query_intent": "KNOWLEDGE_QA",
            "subject_type": "device" if "金牛座" in msg or "ROV" in msg else "procedure",
            "subject_text": subject_text,
            "relation": "describe",
            "source_policy": "project_kb",
            "needs_clarification": False,
            "clarification_reason": None,
            "emergency_action": None,
            "confidence": 0.9,
            "reason_code": "KNOWLEDGE_QUERY",
        }

    def _control(self, action: str, reason_code: str) -> dict:
        return {
            "schema_version": 1,
            "operation": "CONTROL",
            "dialogue_mode": "emergency_intervention",
            "query_intent": "EMERGENCY",
            "subject_type": "task",
            "subject_text": "当前任务",
            "relation": "procedure",
            "source_policy": "session_state",
            "needs_clarification": False,
            "clarification_reason": None,
            "emergency_action": action,
            "confidence": 0.98,
            "reason_code": f"CONTROL_{reason_code.upper()}",
        }

    def _clarify(self, reason: str) -> dict:
        return {
            "schema_version": 1,
            "operation": "CLARIFY",
            "dialogue_mode": "knowledge_qa",
            "query_intent": "CLARIFICATION",
            "subject_type": None,
            "subject_text": None,
            "relation": None,
            "source_policy": None,
            "needs_clarification": True,
            "clarification_reason": reason,
            "emergency_action": None,
            "confidence": 0.4,
            "reason_code": "CLARIFY_" + reason[:20].upper().replace(" ", "_"),
        }

    def _extract_user_input(self, messages) -> str:
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m["content"]
                break
        m = re.search(r"【最新用户输入】:\s*\"(.+?)\"", user_msg, re.DOTALL)
        if m:
            return m.group(1).strip()
        m2 = re.search(r"用户输入:\s*(.+?)(?:\n|$)", user_msg)
        if m2:
            return m2.group(1).strip()
        return user_msg[:150]

    # ===================================================================
    # Override 协议方法：确保语义路由每次都真正被调用（count++）
    # ===================================================================
    def classify_interaction(self, messages, max_tokens=320, role=None):
        """Override: 避免基类 is_mock=True 走 _mock_classify_interaction 规则抢占。"""
        self.call_count += 1
        self.last_prompts_seen.append(messages)
        user_input = self._extract_user_input(messages)
        print(f"\n  [LLM调用 ✅] 语义处理: {user_input[:60]}")
        return self._semantic_route(user_input)

    def extract_json(self, messages, max_tokens=320, role=None, temperature=0.1):
        self.call_count += 1
        self.last_prompts_seen.append(messages)
        user_input = self._extract_user_input(messages)
        print(f"\n  [LLM调用 ✅] 语义处理: {user_input[:60]}")
        return self._semantic_route(user_input)

    def chat(self, messages, temperature=0.7, max_tokens=800, role=None):
        self.call_count += 1
        self.last_prompts_seen.append(messages)
        return "这是默认回复。"


def print_route_result(label, user_msg, res, llm_was_called, expected_op=None):
    plan = res.interaction_plan
    op = plan.operation if plan else "N/A"
    mode = plan.dialogue_mode if plan else "N/A"
    nc = plan.needs_clarification if plan else "N/A"
    reason = plan.clarification_reason if plan else "N/A"
    code = plan.reason_code if plan else "N/A"

    status = "✅ PASS"
    detail = ""
    if expected_op:
        if op != expected_op:
            status = "❌ FAIL"
            detail = f" [预期: {expected_op}, 实际: {op}]"
        else:
            status = "✅ PASS"
            detail = f" [正确路由: {op}]"

    print(f"\n{'─'*60}")
    print(f"📝 {label}")
    print(f"   用户输入: {user_msg}")
    print(f"   结果: operation={op} | mode={mode} | clarify={nc}")
    print(f"   reason_code={code}")
    if nc and reason:
        print(f"   澄清原因: {reason}")
    print(f"   LLM调用: {'✅ YES' if llm_was_called else '❌ NO(被规则拦截)'}")
    print(f"   校验: {status}{detail}")
    print(f"{'─'*60}")

    return op, expected_op and op == expected_op


def main():
    print("\n" + "="*80)
    print("  LLM-First 意图路由修复效果验证")
    print("  修复前: 11/11 (100%) 用例被前置规则拦截, LLM无机会")
    print("  修复目标: 安全红线除外, LLM优先判断")
    print("="*80)

    kb = KnowledgeBase()
    mock_llm = SmartMockLLM()
    router = IntentRouter(mock_llm)

    test_cases = [
        ("自然语言创建任务-无显式WRITE动词",
         "我想明天去做一个水下管缆巡检作业，水深大概500米吧",
         {}, "collecting", None, "WRITE"),

        ("设备介绍查询",
         "帮我看看那个金牛座怎么样",
         {}, "collecting", None, "READ"),

        ("问句形式的任务创建请求",
         "你能帮我创建一个巡检任务吗？水深大概300米",
         {}, "collecting", None, "WRITE"),

        ("数字参数赋值（无显式动词）",
         "水深500，管缆位置北纬19.8东经113.5",
         {"task_type_key": "pipeline_inspection"}, "collecting", None, "WRITE"),

        ("口语化取消当前任务",
         "算了这个任务我不想要了",
         {"task_type_key": "pipeline_inspection"}, "collecting", None, "CONTROL"),

        ("纯裸词歧义",
         "机器人",
         {}, "collecting", None, None),  # 裸词允许 CLARIFY / READ

        ('自然语言修改参数（无"改变"动词）',
         "那个水深的话我觉得调整到800比较合适",
         {"task_type_key": "pipeline_inspection", "water_depth": 500}, "collecting", None, "WRITE"),

        ("追问下的犹豫但明确回答",
         "我想想...管缆类型的话还是用电力电缆吧",
         {"task_type_key": "pipeline_inspection"}, "collecting", ["cable_type"], "WRITE"),

        ("条件式知识库查询（任务相关）",
         "如果我要做一个阀门操作，一般需要准备什么",
         {}, "collecting", None, "READ"),

        ("多设备组合（隐含任务创建）",
         "ROV和AUV我都想用一下",
         {}, "collecting", None, "WRITE"),

        # 安全红线：紧急控制必须被安全门控拦截（可不交给LLM）
        ("紧急控制-立即停止",
         "立即停止当前任务",
         {"task_type_key": "pipeline_inspection"}, "collecting", None, "CONTROL"),
    ]

    never_given_chance = 0
    llm_called_count = 0
    op_matches = 0
    op_tests = 0

    for label, user_msg, task_state, phase, expected_slots, expected_op in test_cases:
        count_before = mock_llm.call_count

        res = router.route(
            user_message=user_msg,
            conversation_history=[],
            task_state=task_state,
            phase=phase,
            expected_slots=expected_slots,
        )

        llm_called = mock_llm.call_count > count_before
        if llm_called:
            llm_called_count += 1
        else:
            never_given_chance += 1

        op, match = print_route_result(label, user_msg, res, llm_called, expected_op)
        if expected_op is not None:
            op_tests += 1
            if match:
                op_matches += 1

    total = len(test_cases)
    print("\n" + "="*80)
    print("  验证总结")
    print("="*80)
    print(f"  LLM 获得调用机会: {llm_called_count}/{total} ({100*llm_called_count//total}%)")
    print(f"     修复前 0% (100%被拦截) → 修复后目标: ≥ 80%")
    print(f"  操作类型语义匹配: {op_matches}/{op_tests} ({100*op_matches//max(1,op_tests)}%)")
    if llm_called_count >= total - 2:
        print("  ✅ LLM-First 架构生效: 模型获得优先判断机会")
    else:
        print("  ⚠️  LLM 仍被拦截较多，可能安全门控过严")
    if op_tests > 0 and op_matches == op_tests:
        print("  ✅ 操作类型语义判断正确")
    else:
        print("  ⚠️  部分操作类型判断待优化")
    print("="*80)


if __name__ == "__main__":
    main()
