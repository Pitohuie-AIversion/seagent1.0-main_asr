# 2026-07-22 工作详细记录

## 一、背景

今天的工作集中在 `seagent1.0-main_asr_LHL` 目录。目标不是修改 main，而是在 LHL 分支里完成与 main 的功能合并，并修复合并后出现的接口不稳定问题。

主要问题来自两个方向：

1. main 和 LHL 的代码职责边界不同，合并后多个脚本接口没有完全同步。
2. 对话系统原先存在“关键词规则优先、LLM 兜底”的路由方式，导致用户输入中只要包含某些业务词，就可能被错误抢到查询线路。

典型故障输入：

```text
我想做管缆巡检，开始时间现在，结束时间五小时后，管缆类型海底油气管道
```

错误表现：

- 没有触发槽位写入。
- 回复变成“知识库强类型检索证据”类问答。
- 前端看不到已提取字段。

根因是 `管缆类型` 被旧路由规则优先识别成知识查询，而不是把整句话理解为任务创建与参数提交。

---

## 二、合并与修改的脚本

### 1. `src/dialogue_manager.py`

#### 修改内容

- 合并 main 中更完整的主流程控制。
- 保留 LHL 当前任务收集链路。
- 接入 `FieldNormalizer()`，用于在提交 SlotStore 前统一做字段规范化。
- 接入 `IntentRouter`，将“本轮用户输入是查询、写入、控制还是聊天”的判断前置。
- 修改控制线路：
  - `CONTROL / CONFIRM` 进入任务确认发布。
  - `CONTROL / CANCEL` 标记任务取消。
  - 非 WRITE 类型不进入 Extractor。
- 增加 WRITE 空抽取保护：
  - Router 判断为 WRITE 后，仍必须经过 Extractor。
  - 如果 Extractor 没有返回合法 `slot_candidates`，不提交事务，不写 SlotStore。
- 接入 `intent_id` 生成与发布校验：
  - 在任务字段完整或修改已完成任务时自动生成 `intent_id`。
  - 发布时校验 `intent_id` 必须合法且存在于 SlotStore。
- 修复 `_resolve_pending_oilfield_confirmation()` 调用参数不一致问题。

#### 作用

`DialogueManager` 现在只负责选择处理线路和组织事务，不再依赖 Extractor 的意图判断。

新的线路边界：

```text
QUERY / CHAT / AMBIGUOUS
  -> 不调用 Extractor
  -> 不调用 Normalizer
  -> 不修改 SlotStore

WRITE
  -> ParameterExtractor
  -> FieldNormalizer
  -> SlotValidator
  -> SlotStore 事务提交

CONTROL
  -> 结合 phase 执行确认、取消、继续
```

#### 风险控制

- WRITE 不是直接写入，只是允许进入参数抽取线路。
- SlotStore 写入仍通过事务和版本控制。
- 查询线路前后保持状态不变。

---

### 2. `src/intent_router.py`

#### 修改内容

新增独立交互路由器，核心类：

- `IntentRouter`
- `IntentRouteResult`
- `RouteValidator`

第一层只判断：

```text
QUERY
WRITE
CONTROL
CHAT
AMBIGUOUS
```

没有实现 `MIXED`，因为当前按要求先不写。

LLM 输出协议包括：

```json
{
  "interaction_type": "WRITE",
  "write_action": "CREATE",
  "control_action": "NONE",
  "query_intent": null,
  "confidence": 0.97,
  "reason": "用户表达创建任务并提交参数",
  "write_evidence": ["用户提交任务目标的原文片段"],
  "query_evidence": []
}
```

#### 程序校验

`RouteValidator` 做安全校验：

- `QUERY`
  - 不允许 `should_update_slots=True`。
  - 后续只能走查询线路。

- `WRITE`
  - 必须有合法 `write_action`。
  - 必须有 `write_evidence` 或当前存在 `expected_slots`。
  - `UPDATE` 必须有已有任务上下文，除非当前正在回答 expected_slots。

- `CONTROL`
  - `CONFIRM` 只能在 `confirming` 阶段执行。
  - `CONTINUE` 只能在 `blocked_soft` 阶段执行。
  - `CANCEL` 可作为明确控制动作处理。

- `CHAT`
  - 不允许写槽位。

#### 作用

解决旧问题：

```text
关键词命中 “管缆类型”
  -> 直接抢成 KNOWLEDGE_QA
```

新逻辑：

```text
LLM 先判断用户是在提交信息还是询问信息
  -> 程序校验判断是否安全
  -> DialogueManager 决定是否进入写入链路
```

这样“管缆类型有哪些？”仍是查询，“管缆类型海底油气管道”则是写入。

---

### 3. `src/extractor.py`

#### 修改内容

- 删除 Extractor prompt 中的“意图分类”职责。
- 删除输出结构中的 `intent`。
- 删除：
  - `NON_MUTATING_INTENTS`
  - `normalize_intent()`
- 默认返回结构改为：

```python
{
    "slot_candidates": [],
    "unresolved": []
}
```

#### 作用

Extractor 现在只做一件事：从用户输入中抽取字段候选。

它不再判断：

- 是不是创建任务；
- 是不是查询；
- 是不是聊天；
- 是否允许进入写入流程。

这些全部交给 `IntentRouter` 和 `RouteValidator`。

#### 为什么这样改

合并前的问题是多个模块都在判断 intent：

```text
IntentRouter 判断 intent
Extractor 也判断 intent
LLMClient 还可能按 prompt 自动转成旧 classify_intent
```

这会造成接口冲突。现在职责拆开后：

```text
IntentRouter = 路由判断
Extractor = 字段抽取
Normalizer = 字段规范化
SlotStore = 状态提交
```

边界更清楚。

---

### 4. `src/llm_client.py`

#### 修改内容

- 新增 `classify_interaction()`：
  - 专门服务第一层交互分类。
  - 只解析 JSON，不做业务字段映射。

- 修改 `extract_json()`：
  - 现在只作为字段抽取兼容入口。
  - 不再根据 prompt 猜测是否进入旧意图分类。

- 删除旧冗余：
  - `classify_intent()`
  - `_mock_classify_intent()`
  - `_is_intent_prompt()`

- 离线 mock 改为通用结构判断：
  - 问句结构 -> QUERY
  - 字段提交结构 -> WRITE
  - 控制动作结构 -> CONTROL
  - 问候/能力介绍 -> CHAT

#### 作用

避免 Extractor 的 prompt 被错误识别成 intent prompt，从而返回错误结构。

旧风险：

```text
Extractor 调用 extract_json()
  -> LLMClient 检测到 prompt 里有“意图”
  -> 误走 classify_intent()
  -> 返回 intent，不返回正确 slot_candidates
```

新边界：

```text
IntentRouter -> classify_interaction()
ParameterExtractor -> extract_json() -> extract_slots()
```

---

### 5. `src/prompts.py`

#### 修改内容

强化候选值展示约束：

- 只要待收集字段包含 `allowed_values`，回复中展示候选时必须逐字原样展示。
- 不得省略、改写、翻译、简称化、同义替换、合并、扩写或自行补充候选。
- 不得把父级字段值当成子级候选。
- `equipment_type` 的候选必须来自后端过滤后的 allowed_values。
- 不允许 LLM 基于通用知识自行二次排除设备型号。

#### 作用

修复前端看到的设备型号候选不严格等于知识库 `full_name` 的问题。

目标是保证：

```text
equipment_family -> full_name
equipment_type -> full_name
equipment_unit_id -> 具体编号/实例候选
```

并且所有字段都遵守同一套 allowed_values 约束，而不是只针对 equipment 特判。

---

### 6. `src/normalizer.py`

#### 修改内容

- 合并字段规范化逻辑。
- 使用 `allowed_values` 将用户输入收敛到标准候选。
- 支持根据当前状态动态解析字段合法值。

#### 作用

保证 LLM 抽出来的值不能直接进入 SlotStore，必须经过规范化。

例如用户输入简称、别名或模糊说法时，Normalizer 负责尝试映射到合法候选；映射失败则保持缺失或进入 unresolved/conflict。

---

### 7. `src/output_builder.py`

#### 修改内容

- 合并动态缺失字段生成。
- 合并 `allowed_values_ref` 解析能力。
- 支持根据当前任务状态动态生成候选。

#### 作用

负责告诉前端/LLM：

- 当前已经填了什么；
- 还缺什么；
- 某个缺失字段有哪些合法候选。

特别是机器人选择链路：

```text
equipment_family
  -> equipment_type
  -> equipment_unit_id
```

后一个字段的候选依赖前一个字段的已确认值。

---

### 8. `src/slot_store.py`

#### 修改内容

- 合并 SlotStore 事务能力。
- 合并 SlotStore version 机制。
- 合并动态缺失字段与输出投影能力。
- 增加 `intent_id` 字段类型支持。

#### 作用

SlotStore 成为任务状态的权威来源。

重要能力：

- 查询线路不能改变 version。
- 写入线路必须通过事务提交。
- 输出给 TaskIntent 时可以投影成正式 schema 字段。

---

### 9. `src/task_intent_builder.py`

#### 修改内容

- 合并 TaskIntent JSON 构建。
- 合并 `intent_id` 生成与校验。
- 合并 staging 文件写入、发布、冲突检测能力。
- 使用统一结果路径。

#### 作用

任务确认后，不是直接随意写最终 JSON，而是：

```text
prepare
  -> staging
  -> validate
  -> publish
  -> final task_intent_*.json
```

这样可以降低写坏文件、ID 冲突、半写入的风险。

---

### 10. `src/history_manager.py`

#### 修改内容

- 合并历史记录保存路径。
- 支持按 `intent_id` 关联保存历史。
- 使用安全文件名组件。

#### 作用

任务发布后，历史对话和任务结果能够对应起来，便于回溯。

---

### 11. `src/id_sequence.py`

#### 修改内容

- 合并每日递增 ID 生成。
- 增加 `validate_intent_id()`。
- 统一读取结果目录中已存在 ID，避免重复。

#### 作用

为 TaskIntent 生成稳定、可追踪、不易冲突的 `intent_id`。

---

### 12. `src/knowledge_retriever.py`

#### 修改内容

- 合并机器人知识库检索能力。
- 增加设备别名索引。
- 增加歧义设备词集合。
- 增加设备 full_name 查询能力。

#### 作用

为以下模块提供统一知识来源：

- IntentRouter：辅助判断设备相关输入。
- Normalizer：将用户输入规范化到设备 full_name。
- OutputBuilder：生成 allowed_values。
- Prompts：给 LLM 提供专业知识参考。

---

### 13. `src/validator.py`

#### 修改内容

- 合并任务约束校验能力。
- 保持硬约束、软约束对话流程可用。

#### 作用

在任务字段收集后判断是否可以进入确认阶段，或是否需要用户修改违规字段。

---

### 14. `web_backend.py`

#### 修改内容

- 合并后端接口适配。
- 修复和 DialogueManager 调用链相关的不一致。

#### 作用

保证前端 `/api/chat` 调用能够进入新的 DialogueManager 流程。

---

### 15. 新增 `src/exceptions.py`

#### 修改内容

新增统一异常类型，用于任务构建、发布、冲突等场景。

#### 作用

避免持久化失败、ID 冲突等问题被静默吞掉。

---

### 16. 新增 `src/result_paths.py`

#### 修改内容

新增统一结果路径模块。

#### 作用

统一管理：

- task intent 输出目录；
- history 输出目录；
- result 根目录。

避免多个脚本各自拼路径。

---

### 17. `tests/test_dialogue_manager_rov.py`

#### 修改内容

新增 5 个路由与写入门禁测试。

测试覆盖：

1. 包含“管缆类型”的复合创建输入必须走 WRITE / CREATE。
2. “管缆类型有哪些？”必须走 QUERY / KNOWLEDGE_QA。
3. expected_slots 包含 `cable_type` 时，“海底油气管道”必须走 WRITE / UPDATE。
4. 复合创建输入能写入 `task_type / task_type_key / start_time / end_time / cable_type`。
5. Router 判断 WRITE 但 Extractor 没抽到候选时，SlotStore 不变。

---

## 三、今天重点修复的问题

### 问题 1：启动后 `/api/chat` 500

报错：

```text
TypeError: DialogueManager._resolve_pending_oilfield_confirmation() got an unexpected keyword argument 'request_id'
```

处理：

- 对齐 `DialogueManager` 内部调用和函数签名。
- 保证 request_id 可以在日志和槽位更新链路中继续传递。

### 问题 2：复合任务输入没有任何字段写入

输入：

```text
我想做管缆巡检，开始时间现在，结束时间五小时后，管缆类型海底油气管道
```

旧结果：

- 进入知识库查询。
- 没有 `[SLOT_UPDATE]`。
- 前端无字段显示。

处理：

- 将路由从“关键词规则优先”改为“LLM 判断交互性质 + 程序校验”。
- `管缆类型` 不再天然抢占为 QUERY。
- 整句如果表达任务创建和参数提交，则进入 WRITE。

### 问题 3：Extractor 和 Router 职责重复

旧问题：

```text
IntentRouter 判断 intent
Extractor prompt 也要求输出 intent
LLMClient.extract_json 又可能根据 prompt 改走旧 intent 分类
```

处理：

- Extractor 不再输出 intent。
- LLMClient 删除旧 `classify_intent`。
- 路由只由 `IntentRouter.classify_interaction()` 负责。

### 问题 4：设备候选没有严格按 full_name 输出

旧问题：

- 前端询问 `equipment_type` 时，LLM 可能把 `equipment_family` 当成型号候选。
- 或者自行改写 allowed_values。

处理：

- 在 `prompts.py` 增加全局 allowed_values 约束。
- 所有字段都必须逐字展示后端给出的候选。
- 不只针对 equipment 做特判。

### 问题 5：WRITE 但无候选时可能污染状态

处理：

- 新增 `reply_write_without_candidates()`。
- 如果 Router 判 WRITE，但 Extractor 没返回合法候选：
  - 不提交 SlotStore。
  - 不创建空任务。
  - 返回澄清提示。

---

## 四、当前接口边界

### 路由接口

```python
IntentRouter.route(
    user_message,
    conversation_history,
    task_state,
    phase,
    expected_slots,
)
```

返回 `IntentRouteResult`，核心字段：

```python
interaction_type
write_action
control_action
query_intent
confidence
reason
write_evidence
query_evidence
```

### LLM 接口

```python
LLMClient.classify_interaction()
```

专门用于路由分类。

```python
LLMClient.extract_json()
```

只用于字段抽取。

### Extractor 接口

```python
ParameterExtractor.extract_updates()
```

返回：

```python
{
    "slot_candidates": [...],
    "unresolved": [...]
}
```

不再返回 intent。

### DialogueManager 写入门禁

```text
route.interaction_type == "WRITE"
  -> 才允许进入 Extractor
  -> Extractor 必须返回合法候选
  -> 才允许进入 Normalizer / Validator / SlotStore
```

---

## 五、验证记录

### 语法验证

执行：

```bash
PYTHONDONTWRITEBYTECODE=1 /root/miniconda3/envs/seagent/bin/python -m py_compile \
  src/intent_router.py \
  src/llm_client.py \
  src/extractor.py \
  src/dialogue_manager.py \
  tests/test_dialogue_manager_rov.py
```

结果：

```text
通过，无语法错误
```

### 定向单元测试

执行：

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 \
/root/miniconda3/envs/seagent/bin/python -m unittest \
  tests.test_dialogue_manager_rov.DialogueManagerROVTest.test_interaction_router_prioritizes_write_over_entity_keyword_query \
  tests.test_dialogue_manager_rov.DialogueManagerROVTest.test_interaction_router_keeps_real_cable_type_question_as_query \
  tests.test_dialogue_manager_rov.DialogueManagerROVTest.test_interaction_router_treats_expected_slot_answer_as_write \
  tests.test_dialogue_manager_rov.DialogueManagerROVTest.test_dialogue_manager_writes_compound_create_message_slots \
  tests.test_dialogue_manager_rov.DialogueManagerROVTest.test_write_route_without_extracted_candidates_does_not_mutate_slots
```

结果：

```text
.....
----------------------------------------------------------------------
Ran 5 tests in 0.066s

OK
```

### 冗余扫描

扫描项：

```text
MIXED
normalize_intent
NON_MUTATING_INTENTS
classify_intent
_mock_classify_intent
_is_intent_prompt
旧 Extractor 意图分类 prompt
旧 should_update_slots 分流判断
CREATE_ACTION_PHRASES
EXPLICIT_UPDATE_PHRASES
```

结果：

```text
无残留输出
```

---

## 六、当前结论

今天完成了两类工作：

1. 合并 main 和 LHL 的核心能力：
   - 任务收集；
   - 设备知识库；
   - allowed_values；
   - SlotStore；
   - TaskIntent 构建；
   - 结果路径；
   - 历史记录；
   - intent_id。

2. 修复合并后最关键的接口边界：
   - Router 只判断交互性质。
   - Extractor 只抽取字段。
   - LLMClient 不再混用旧意图分类。
   - DialogueManager 用 interaction_type 分流。
   - WRITE 必须抽到合法候选才写入。

目前已通过路由/写入链路的定向测试。但尚未跑完整测试套件，也尚未做真实前端全流程联调。因此当前结论是：

```text
路由、抽取、写入接口的核心边界已稳定；
全系统稳定性仍需要后续完整联调确认。
```

---

## 七、后续建议

1. 跑一次真实 `/api/chat` 流程：

```text
我想做管缆巡检，
开始时间现在，
结束时间五小时后，
管缆类型海底油气管道，
起始点(16.8,113.5)，
结束点(19.0,113.8)，
水深300米，
使用轻型工作级，
使用第一个型号，
使用第一个编号，
工具全部携带，
母船使用681
```

2. 观察日志中是否出现完整 `[SLOT_UPDATE]`。
3. 检查前端是否严格展示 allowed_values 原始候选。
4. 再决定是否跑全量测试或继续处理其他 merge 文件。
