# 2026-07-22 工作简述

## 总体目标

今天主要围绕 `seagent1.0-main_asr_LHL` 分支做合并和可用性修复：以 LHL 目录为修改目标，吸收 main 中较完整的任务收集、知识库检索、输出构建、结果持久化能力，同时修复合并后接口不一致导致的启动和对话不可用问题。

## 主要修改范围

- `src/dialogue_manager.py`
  - 合并主对话流程，保留 LHL 的任务收集链路。
  - 接入 `FieldNormalizer`、`IntentRouter`、`intent_id` 自动生成与发布校验。
  - 修复确认函数参数不一致问题。
  - 将查询、控制、聊天、写入线路拆开，避免查询误写 SlotStore。

- `src/extractor.py`
  - 明确职责为“槽位候选抽取器”。
  - 删除旧的意图分类职责，避免和 `IntentRouter` 重复判断。

- `src/intent_router.py`
  - 新增独立交互路由器。
  - 第一层判断 `QUERY / WRITE / CONTROL / CHAT / AMBIGUOUS`。
  - 新增 `RouteValidator`，LLM 判断后必须经过程序校验。

- `src/llm_client.py`
  - 增加 `classify_interaction()` 作为路由专用接口。
  - `extract_json()` 只服务字段抽取，不再混用旧意图分类。
  - 删除旧的 `classify_intent` 冗余逻辑。

- `src/normalizer.py`
  - 合并 allowed_values 规范化逻辑。
  - 保证字段值收敛到配置/知识库给出的合法候选。

- `src/output_builder.py`
  - 合并动态缺失字段与 allowed_values 解析。
  - 支持机器人系列、型号、编号按依赖关系动态生成候选。

- `src/slot_store.py`
  - 合并事务提交、版本控制、动态缺失字段、输出投影能力。
  - 支持查询线路前后 SlotStore version 不变的安全检查。

- `src/task_intent_builder.py`
  - 合并任务意图 JSON 构建与发布能力。
  - 增加 `intent_id` 校验、staging 文件、发布冲突保护。

- `src/history_manager.py`
  - 合并历史记录路径与 `intent_id` 关联保存。

- `src/id_sequence.py`
  - 合并每日递增 ID 生成逻辑。
  - 增加 `intent_id` 格式校验。

- `src/knowledge_retriever.py`
  - 合并机器人设备、别名、工具、能力等知识库检索接口。
  - 支持设备 full_name 候选读取。

- `src/prompts.py`
  - 强化 allowed_values 展示约束。
  - 要求所有候选项逐字使用后端给出的 allowed_values，不允许 LLM 自行改写、简称或补充。

- `src/validator.py`
  - 合并约束校验相关变更。

- `web_backend.py`
  - 合并前后端接口适配相关变更。

- 新增 `src/exceptions.py`
  - 统一任务发布、冲突、持久化等异常类型。

- 新增 `src/result_paths.py`
  - 统一结果、历史、任务文件路径。

- `tests/test_dialogue_manager_rov.py`
  - 增加路由与写入门禁回归测试。

## 今天修复的关键问题

1. `DialogueManager._resolve_pending_oilfield_confirmation()` 参数不匹配导致 `/api/chat` 500。
2. “我想做管缆巡检，开始时间现在，结束时间五小时后，管缆类型海底油气管道” 被误判成知识库查询，导致没有槽位写入。
3. Extractor 同时承担意图分类和槽位抽取，接口边界混乱。
4. LLM 回复设备候选时可能改写 allowed_values，导致前端展示和后端候选不一致。
5. WRITE 路由如果抽不到合法字段，原设计存在创建空任务或污染状态的风险。

## 验证结果

已执行定向验证：

```text
python -m py_compile src/intent_router.py src/llm_client.py src/extractor.py src/dialogue_manager.py tests/test_dialogue_manager_rov.py

python -m unittest \
  tests.test_dialogue_manager_rov.DialogueManagerROVTest.test_interaction_router_prioritizes_write_over_entity_keyword_query \
  tests.test_dialogue_manager_rov.DialogueManagerROVTest.test_interaction_router_keeps_real_cable_type_question_as_query \
  tests.test_dialogue_manager_rov.DialogueManagerROVTest.test_interaction_router_treats_expected_slot_answer_as_write \
  tests.test_dialogue_manager_rov.DialogueManagerROVTest.test_dialogue_manager_writes_compound_create_message_slots \
  tests.test_dialogue_manager_rov.DialogueManagerROVTest.test_write_route_without_extracted_candidates_does_not_mutate_slots
```

结果：

```text
Ran 5 tests in 0.066s
OK
```

## 当前结论

本次修改后的路由、抽取、写入接口边界已经比合并初期稳定：查询不进入写入链路，写入必须经过 Extractor 与程序校验，空写入不会污染 SlotStore。

但目前只做了定向测试，尚未声明全量系统完全稳定。后续建议继续做一次真实 `/api/chat` 前后端联调。
