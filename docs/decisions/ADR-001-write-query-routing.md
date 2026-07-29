# ADR-001：WRITE / QUERY 双通道意图路由与只读状态保护

## 状态
Accepted

## 背景
在早期对话系统设计中，用户的文本/语音输入混合进入唯一的抽取与状态更新流程。这导致当用户在对话中提问系统能力、查询机器人状态、索取环境信息或进行澄清性提问时，提取器误将查询语句中的词汇（如“水深能达到多少米？”中的“水深”）提炼为任务槽位并尝试更新 `SlotStore`，造成任务槽位污染、覆盖与无预期的状态机变更。

## 决策
引入双通道意图路由机制 [src/intent_router.py](file:///root/mzy/seagent1.0-main_asr/src/intent_router.py)，在提取前将用户输入划分为：
1. `WRITE`：用户提交、修改或回答任务参数，允许进入抽取器更新 `SlotStore`。
2. `QUERY`：用户询问信息、状态、能力或普通对话，进入只读查询处理流程。

在 [src/dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py) 中，针对 `QUERY` 路由增加严格的快照记录与状态不变性断言，确保查询路径绝对不修改任何任务槽位。

## 修改位置
- [src/intent_router.py](file:///root/mzy/seagent1.0-main_asr/src/intent_router.py) (`IntentRouter`, `IntentRouteResult`)
- [src/dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py) (`DialogueManager._handle_non_task_route`, `_handle_knowledge_query`, `_handle_status_query`)

## 核心逻辑
```python
# 意图分路处理伪代码
route = intent_router.route(user_message, conversation_history, task_state)

if route.interaction_type == "QUERY":
    # 1. 记录处理前的 SlotStore 与状态机镜像快照
    snapshot_before = slot_store.export_snapshot()
    
    # 2. 执行只读知识/遥测响应
    reply = handle_query_route(user_message, route)
    
    # 3. 校验状态不变性，确保无槽位修改
    assert slot_store.export_snapshot() == snapshot_before
    return reply
else:
    # WRITE 路径：允许进入提取器更新 SlotStore
    extract_and_update_slots(user_message)
```

## 正面影响
1. **防止槽位污染**：查询与提问绝不意外篡改已有或缺失的任务参数。
2. **职责清晰**：将自然语言理解解耦为“意图路由”与“参数提取”两个专职模块，提升大模型提示词效率与规则兜底准确率。
3. **架构确定性**：引入运行期断言，一旦发生只读状态篡改立即抛异常并终止操作。

## 代价与限制
1. 增加了额外的意图路由 LLM 调用或规则匹配开销。
2. 规则兜底逻辑 (`_rule_fallback_route`) 需要持续维护常用查询与写入关键词。

## 验证
- 单元测试：[tests/test_intent_routing.py](file:///root/mzy/seagent1.0-main_asr/tests/test_intent_routing.py), [tests/test_intent_routing_matrix.py](file:///root/mzy/seagent1.0-main_asr/tests/test_intent_routing_matrix.py), [tests/test_query_write_mixed_benchmark.py](file:///root/mzy/seagent1.0-main_asr/tests/test_query_write_mixed_benchmark.py)
- CI 门控：在 mandatory unittest 自动化流水线中覆盖。
