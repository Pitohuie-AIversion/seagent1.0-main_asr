# 系统架构总览 (System Architecture Overview)

本文档阐述 SEAgent 深海多 Agent 任务规划与 ASR 交互系统的整体架构设计、模块协同机制、控制/数据流向以及核心系统契约与不变量。

> [!NOTE]
> 本文档反映 `8254b37`（2026-08-13）及以前的代码事实。规划中尚未实现的模块（Task Graph、多机器人分配、动态重规划、Robot Gateway）已在本文档中明确标注。

---

## 1. 系统目标

SEAgent 旨在为深海水下机器人作业（包含巡检、清洗、阀门操作、故障排查等）提供强约束、多模态（语音与文本）、高可信的对话交互与 TaskIntent 任务构建能力。系统需在复杂海况、物理限制（水深、载荷、机械臂能力）与实时遥测状态下，确保任务参数的完整性、一致性与落盘安全性。

---

## 2. 模块架构与关系

系统由前端/ASR 接口层、意图路由层、抽取与状态管理层、知识与约束校验层以及持久化发布层构成。

```mermaid
graph TD
    UI[用户输入 / ASR语音转写] --> IPlan[InteractionPlan 结构化语义计划]
    IPlan --> Router[IntentRouter 双通道路由]

    Router -->|READ / CLARIFY 路径| DM_Q[_handle_non_task_route 只读保护]
    Router -->|WRITE / CONTROL 路径| Extractor[Extractor 候选值提取与解析]

    DM_Q --> KB[KnowledgeBase 静态知识/动态遥测]
    KB --> DM_Q

    Extractor --> SlotStore[SlotStore 单源真理状态中心]
    SlotStore --> Validator[Validator 多层级约束校验]

    Validator -->|约束通过| DM_W[DialogueManager 确认/发布状态机]
    DM_W --> TIBuilder[TaskIntentBuilder 安全持久化]
    TIBuilder --> FileSystem[(TaskIntent JSON 落盘)]

    KB -->|get_feasible_robot_selection_domain| SlotStore
```

### 核心模块职责映射

| 模块 | 关键类 / 文件 | 主要职责 |
| :--- | :--- | :--- |
| **ASR 服务与纠错** | [src/asr_service.py](file:///root/mzy/seagent1.0-main_asr/src/asr_service.py)<br>[src/asr_normalizer.py](file:///root/mzy/seagent1.0-main_asr/src/asr_normalizer.py)<br>[src/oilfield_linker.py](file:///root/mzy/seagent1.0-main_asr/src/oilfield_linker.py) | 语音转文本、ASR 候选词+上下文纠错、油田实体 Link 评分与标准化。 |
| **交互计划** | [src/interaction_plan.py](file:///root/mzy/seagent1.0-main_asr/src/interaction_plan.py)<br>`InteractionPlan` | LLM 每轮输出的结构化语义计划；`operation` 字段（READ / WRITE / CONTROL / CLARIFY）为唯一路由权威；后端不根据关键词覆盖此决策。 |
| **意图路由器** | [src/intent_router.py](file:///root/mzy/seagent1.0-main_asr/src/intent_router.py)<br>`IntentRouter`, `IntentRouteResult` | 消费 `InteractionPlan.operation`，将输入严格划分为 `WRITE`（写任务状态）或 `QUERY`（读知识与状态）。 |
| **对话状态管理** | [src/dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py)<br>`DialogueManager` | 主控状态机，调度路由、只读保护、追问生成与确认发布流程；`load_snapshot()` 采用隔离候选管理器原子恢复。 |
| **候选提取与解析** | [src/extractor.py](file:///root/mzy/seagent1.0-main_asr/src/extractor.py)<br>`Extractor` | 提取参数候选，采用 `canonical_exact` -> `alias_exact` -> `llm_semantic` 递进解析。 |
| **状态中心** | [src/slot_store.py](file:///root/mzy/seagent1.0-main_asr/src/slot_store.py)<br>`SlotStore`, `Slot` | Single Source of Truth，管理所有任务槽位状态、版本自增与事务管理；任务类型切换时自动过滤无效机器人 candidate。 |
| **知识、遥测与候选域** | [src/knowledge_retriever.py](file:///root/mzy/seagent1.0-main_asr/src/knowledge_retriever.py)<br>[src/state_info.py](file:///root/mzy/seagent1.0-main_asr/src/state_info.py) | 提供设备静态能力查询与实时遥测状态读取；`get_feasible_robot_selection_domain()` 为机器人候选计算的唯一权威入口（water_depth + payload + 即时运行状态三层过滤）。 |
| **物理约束校验** | [src/validator.py](file:///root/mzy/seagent1.0-main_asr/src/validator.py)<br>`TaskValidator` | 执行水深、载荷、海况、时间有效性及机器人物理约束 Hard/Soft 校验；复用 KnowledgeBase 四级静态关系校验。 |
| **可见来源追踪** | [src/visible_selection_provenance.py](file:///root/mzy/seagent1.0-main_asr/src/visible_selection_provenance.py)<br>`VisibleSelectionProvenance` | 校验编号候选选择的可见来源，确保写入的枚举值在紧邻 assistant 回复中明确展示，阻止基于隐藏顺序的误写。 |
| **任务字段补丁** | [src/task_patch.py](file:///root/mzy/seagent1.0-main_asr/src/task_patch.py)<br>`TaskPatch` | 支持任务参数的字段级补丁操作，不覆盖整体 SlotStore 事务边界。 |
| **任务请求守卫** | [src/task_request_guard.py](file:///root/mzy/seagent1.0-main_asr/src/task_request_guard.py)<br>`TaskRequestGuard` | 防止同轮内多任务意图的误触发，确保复合请求的确定性分句与路由。 |
| **坐标解析** | [src/coord_parser.py](file:///root/mzy/seagent1.0-main_asr/src/coord_parser.py) | 将各类格式的地理坐标表达确定性解析为标准经纬度，与输入格式无关。 |
| **TaskIntent 持久化** | [src/task_intent_builder.py](file:///root/mzy/seagent1.0-main_asr/src/task_intent_builder.py)<br>`TaskIntentBuilder`, `TaskPublishLock` | Staging 暂存、排他锁控制与 `_atomic_commit_noreplace` 无覆盖原子落盘。 |

> [!NOTE]
> 以下模块已在 `src/` 中存在但属于计划/实验阶段，尚未完整接入主流程：`src/model_profile.py`（模型 Profile 封装）、`src/normalization_contract.py`（规范化契约形式化）、`src/session_state.py` / `src/session_state_shadow.py`（SessionState 契约验证）、`src/simulated_time.py`（模拟时钟）、`src/time_context.py`（时间上下文）。

---

## 3. 控制流与状态流

系统交互分为 QUERY（查询）和 WRITE（写入）两条互斥的路径。

```mermaid
sequenceDiagram
    autonumber
    actor User as 用户 / 客户端
    participant LLM as LLM → InteractionPlan
    participant Router as IntentRouter
    participant DM as DialogueManager
    participant Store as SlotStore
    participant Ext as Extractor
    participant KB as KnowledgeBase
    participant Val as Validator
    participant TIB as TaskIntentBuilder

    User->>LLM: 发送自然语言消息
    LLM-->>Router: InteractionPlan (operation: READ/WRITE/CONTROL/CLARIFY)
    Router-->>DM: 返回 IntentRouteResult

    alt READ / CLARIFY 路径 (只读)
        DM->>DM: 记录 SlotStore 及系统状态快照镜像
        DM->>DM: 执行 _handle_non_task_route (知识/状态查询)
        DM->>DM: 断言状态不变性 (如状态受损则抛 RuntimeException)
        DM-->>User: 返回查询回答 (不更新任务槽位)
    else WRITE / CONTROL 路径 (状态更新)
        DM->>KB: get_feasible_robot_selection_domain (water_depth/payload/状态过滤)
        KB-->>Store: 注入约束驱动候选树 (0关闭/1自动绑定/多消歧)
        DM->>Ext: 调用 normalize_and_resolve 提取候选
        Ext->>Store: 更新 / 写入 Slot 候选值
        DM->>Val: 执行物理与环境约束校验
        alt 存在缺失或硬违规
            DM-->>User: 追问缺失参数或提示 Hard 违规 (blocked_hard)
        else 存在软违规
            DM-->>User: 展示软警告 (blocked_soft, 可明确忽略)
        else 满足发布条件
            DM->>TIB: 调用 create_staging 创建暂存文件
            DM->>TIB: 调用 publish_staging (获取 TaskPublishLock)
            TIB-->>DM: 完成原子落盘并返回 TaskIntent
            DM-->>User: 返回最终构建成功确认
        end
    end
```

---

## 4. 关键边界与系统不变量

> [!IMPORTANT]
> **1. QUERY 路径只读隔离**
> - `READ` / `CLARIFY` 路由路径下**绝对不允许修改** `SlotStore` 中的任何槽位状态或对话阶段（Phase）。
> - `_handle_non_task_route` 在处理前会捕获全量快照，并在处理后进行强制一致性断言。

> [!IMPORTANT]
> **2. LLM InteractionPlan 为唯一路由权威**
> - `InteractionPlan.operation` 是路由的唯一不可缺少的语义字段；后端确定性代码不根据业务关键词覆盖此路由决策。
> - 低置信度 WRITE / CONTROL 继续澄清，避免不确定语义产生状态副作用。
> - LLM 路由失败或协议非法时统一退化为 CLARIFY，不猜测写入。

> [!IMPORTANT]
> **3. WRITE 路径为唯一槽位修改入口**
> - 仅当 `IntentRouter` 判断为 `WRITE` 路由时，用户输入才允许送入 [src/extractor.py](file:///root/mzy/seagent1.0-main_asr/src/extractor.py) 进行提炼，并更新 [src/slot_store.py](file:///root/mzy/seagent1.0-main_asr/src/slot_store.py) 中的 `Slot` 状态。

> [!IMPORTANT]
> **4. SlotStore 作为 Single Source of Truth**
> - 系统中所有任务导出的 JSON 结构及下游校验输入，必须直接从 [src/slot_store.py](file:///root/mzy/seagent1.0-main_asr/src/slot_store.py) 导出 (`get_task_state`)，禁止绕过 `SlotStore` 直接使用临时上下文拼装任务状态。

> [!IMPORTANT]
> **5. 机器人候选域为唯一权威入口**
> - `KnowledgeBase.get_feasible_robot_selection_domain()` 是机器人候选计算的唯一权威入口。Validator 和 Snapshot restore 复用同一四级关系校验，避免自动绑定结果与 UI 候选列表不一致。

> [!WARNING]
> **6. 动态机器人遥测状态隔离**
> - 机器人的实时状态（如电池残量、推进器健康度、故障标志）必须通过 [src/state_info.py](file:///root/mzy/seagent1.0-main_asr/src/state_info.py) 动态查询 `config/state.yaml` 或底层遥测接口获取。**不得使用历史对话记忆替代实时遥测数据**。
> - snapshot validity window 为 24 小时（commit c7b3289）；超时遥测数据阻断发布。

> [!CAUTION]
> **7. TaskIntent 文件原子持久化**
> - [src/task_intent_builder.py](file:///root/mzy/seagent1.0-main_asr/src/task_intent_builder.py) 中的"原子持久化"（`_atomic_commit_noreplace` + `TaskPublishLock`）指 **TaskIntent JSON 文件在文件系统上的原子落盘与无覆盖安全保障**（文件要么完整生成，要么不生成，杜绝中间态与覆盖风险）。
> - 该概念**绝非** Task Graph 任务拆解中的"不可分割原子任务"概念，二者在架构上位于不同层级。

> [!CAUTION]
> **8. Validation 不 fallback 到 Clarification**
> - 约束校验失败（`blocked_hard` / `blocked_soft`）不退化为 CLARIFY 追问；约束阻断信息明确返回给用户，要求修正任务参数，而非以追问掩盖。

---

## 5. TaskIntent 安全发布工作流

TaskIntent 的文件落盘采用严格的三阶段发布机制，确保并发写操作安全与防篡改：

```mermaid
flowchart TD
    Start[触发 TaskIntent 发布] --> Step1[TaskIntentBuilder.prepare 纯内存构建 JSON]
    Step1 --> Step2[TaskIntentBuilder.create_staging 创建独占暂存文件 .staging_PID_TID_UUID]
    Step2 --> Lock[获取跨进程排他锁 TaskPublishLock]

    Lock --> CheckExist{目标 task_intent_TIxxxx.json 是否已存在?}
    CheckExist -- 已存在 --> ThrowConflict[抛出 IntentIdConflict 拒绝覆盖]

    CheckExist -- 不存在 --> InspectFD[打开 FD 并校验 fstat/Inode/PID 归属]
    InspectFD --> AtomicCommit[os.link 硬链接原子提交至 final_file]

    AtomicCommit --> CleanStaging[清除暂存文件并释放 TaskPublishLock]
    CleanStaging --> End[发布成功]
    ThrowConflict --> Rollback[保留或清理 staging 触发回滚]
```

---

## 6. 规划中（尚未实现）的模块

以下能力在路线图中存在，但**当前代码中尚未实现**，不得将其描述为已有功能：

| 能力 | 当前状态 | 预计阶段 |
| :--- | :--- | :--- |
| Robot Gateway（机器人执行适配层） | 规划中 | Later |
| Task Graph（任务拆解与有向图） | 规划中 | Later |
| 多机器人分配与协同 | 规划中 | Later |
| 动态重规划与执行反馈闭环 | 规划中 | Later |
| 弱通信下的多机协调 | 规划中 | Later |


---

## 2. 模块架构与关系

系统由前端/ASR 接口层、意图路由层、抽取与状态管理层、知识与约束校验层以及持久化发布层构成。

```mermaid
graph TD
    UI[用户输入 / ASR语音转写] --> Router[IntentRouter 双通道路由]
    
    Router -->|QUERY 路径| DM_Q[_handle_non_task_route 只读保护]
    Router -->|WRITE 路径| Extractor[Extractor 候选值提取与解析]
    
    DM_Q --> KB[KnowledgeBase 静态知识/动态遥测]
    KB --> DM_Q
    
    Extractor --> SlotStore[SlotStore 单源真理状态中心]
    SlotStore --> Validator[Validator 多层级约束校验]
    
    Validator -->|校验通过| DM_W[DialogueManager 确认/发布状态机]
    DM_W --> TIBuilder[TaskIntentBuilder 安全持久化]
    TIBuilder --> FileSystem[(TaskIntent JSON 落盘)]
```

### 核心模块职责映射

| 模块 | 关键类 / 文件 | 主要职责 |
| :--- | :--- | :--- |
| **ASR 服务与纠错** | [src/asr_service.py](file:///root/mzy/seagent1.0-main_asr/src/asr_service.py)<br>[src/asr_normalizer.py](file:///root/mzy/seagent1.0-main_asr/src/asr_normalizer.py)<br>[src/oilfield_linker.py](file:///root/mzy/seagent1.0-main_asr/src/oilfield_linker.py) | 语音转转文本、ASR 候选词+上下文纠错、油田实体 Link 评分与标准化。 |
| **意图路由器** | [src/intent_router.py](file:///root/mzy/seagent1.0-main_asr/src/intent_router.py)<br>`IntentRouter`, `IntentRouteResult` | 将输入严格划分为 `WRITE`（写任务状态）或 `QUERY`（读知识与状态）。 |
| **对话状态管理** | [src/dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py)<br>`DialogueManager` | 主控状态机，调度路由、只读保护、追问生成与确认发布流程。 |
| **候选提取与解析** | [src/extractor.py](file:///root/mzy/seagent1.0-main_asr/src/extractor.py)<br>`Extractor` | 提取参数候选，采用 `canonical_exact` -> `alias_exact` -> `llm_semantic` 递进解析。 |
| **状态中心** | [src/slot_store.py](file:///root/mzy/seagent1.0-main_asr/src/slot_store.py)<br>`SlotStore`, `Slot` | Single Source of Truth，管理所有任务槽位状态、版本自增与事务管理。 |
| **知识与遥测** | [src/knowledge_retriever.py](file:///root/mzy/seagent1.0-main_asr/src/knowledge_retriever.py)<br>[src/state_info.py](file:///root/mzy/seagent1.0-main_asr/src/state_info.py) | 提供设备静态能力查询，并读取 `config/state.yaml` 中的实时遥测状态。 |
| **物理约束校验** | [src/validator.py](file:///root/mzy/seagent1.0-main_asr/src/validator.py)<br>`TaskValidator` | 执行水深、载荷、海况、时间有效性及机器人物理约束 Hard/Soft 校验。 |
| **TaskIntent 持久化** | [src/task_intent_builder.py](file:///root/mzy/seagent1.0-main_asr/src/task_intent_builder.py)<br>`TaskIntentBuilder`, `TaskPublishLock` | Staging 暂存、排他锁控制与 `_atomic_commit_noreplace` 无覆盖原子落盘。 |

---

## 3. 控制流与状态流

系统交互分为 QUERY（查询）和 WRITE（写入）两条互斥的路径。

```mermaid
sequenceDiagram
    autonumber
    actor User as 用户 / 客户端
    participant Router as IntentRouter
    participant DM as DialogueManager
    participant Store as SlotStore
    participant Ext as Extractor
    participant Val as Validator
    participant TIB as TaskIntentBuilder

    User->>Router: 发送自然语言消息
    Router-->>DM: 返回 IntentRouteResult (WRITE 或 QUERY)

    alt QUERY 路径 (只读)
        DM->>DM: 记录 SlotStore 及系统状态快照镜像
        DM->>DM: 执行 _handle_non_task_route (知识/状态查询)
        DM->>DM: 断言状态不变性 (如状态受损则抛 RuntimeException)
        DM-->>User: 返回查询回答 (不更新任务槽位)
    else WRITE 路径 (状态更新)
        DM->>Ext: 调用 normalize_and_resolve 提取候选
        Ext->>Store: 更新 / 写入 Slot 候选值
        DM->>Val: 执行物理与环境约束校验
        alt 存在缺失或硬违规
            DM-->>User: 追问缺失参数或提示 Hard 违规
        else 满足发布条件
            DM->>TIB: 调用 create_staging 创建暂存文件
            DM->>TIB: 调用 publish_staging (获取 TaskPublishLock)
            TIB-->>DM: 完成原子落盘并返回 TaskIntent
            DM-->>User: 返回最终构建成功确认
        end
    end
```

---

## 4. 关键边界与系统不变量

为确保系统运行的确定性与数据安全，系统设计中明确并强制执行以下边界与不变量：

> [!IMPORTANT]
> **1. QUERY 路径只读隔离**
> - `QUERY` 路由路径下**绝对不允许修改** `SlotStore` 中的任何槽位状态或对话阶段（Phase）。
> - [src/dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py) 的 `_handle_non_task_route` 在处理前会捕获全量快照，并在处理后进行强制一致性断言。

> [!IMPORTANT]
> **2. WRITE 路径为唯一槽位修改入口**
> - 仅当 `IntentRouter` 判断为 `WRITE` 路由时，用户输入才允许送入 [src/extractor.py](file:///root/mzy/seagent1.0-main_asr/src/extractor.py) 进行提炼，并更新 [src/slot_store.py](file:///root/mzy/seagent1.0-main_asr/src/slot_store.py) 中的 `Slot` 状态。

> [!IMPORTANT]
> **3. SlotStore 作为 Single Source of Truth**
> - 系统中所有任务导出的 JSON 结构及下游校验输入，必须直接从 [src/slot_store.py](file:///root/mzy/seagent1.0-main_asr/src/slot_store.py) 导出 (`get_task_state`)，禁止绕过 `SlotStore` 直接使用临时上下文拼装任务状态。

> [!WARNING]
> **4. 动态机器人遥测状态隔离**
> - 机器人的实时状态（如电池残量、推进器健康度、故障标志）必须通过 [src/state_info.py](file:///root/mzy/seagent1.0-main_asr/src/state_info.py) 动态查询 `config/state.yaml` 或底层遥测接口获取。**不得使用历史对话记忆替代实时遥测数据**。

> [!CAUTION]
> **5. TaskIntent 文件原子持久化概念澄清**
> - [src/task_intent_builder.py](file:///root/mzy/seagent1.0-main_asr/src/task_intent_builder.py) 中提供的“原子持久化”（`_atomic_commit_noreplace` + `TaskPublishLock`）指 **TaskIntent JSON 文件在文件系统上的原子落盘与无覆盖安全保障**（即文件要么完整生成，要么不生成，杜绝中间态与覆盖风险）。
> - 该概念**绝非** Task Graph 任务拆解中的“不可分割原子任务 (Atomic Task)”概念，二者在架构上位于不同层级。

---

## 5. TaskIntent 安全发布工作流

TaskIntent 的文件落盘采用严格的三阶段发布机制，确保并发写操作安全与防篡改：

```mermaid
flowchart TD
    Start[触发 TaskIntent 发布] --> Step1[TaskIntentBuilder.prepare 纯内存构建 JSON]
    Step1 --> Step2[TaskIntentBuilder.create_staging 创建独占暂存文件 .staging_PID_TID_UUID]
    Step2 --> Lock[获取跨进程排他锁 TaskPublishLock]
    
    Lock --> CheckExist{目标 task_intent_TIxxxx.json 是否已存在?}
    CheckExist -- 已存在 --> ThrowConflict[抛出 IntentIdConflict 拒绝覆盖]
    
    CheckExist -- 不存在 --> InspectFD[打开 FD 并校验 fstat/Inode/PID 归属]
    InspectFD --> AtomicCommit[os.link 硬链接原子提交至 final_file]
    
    AtomicCommit --> CleanStaging[清除暂存文件并释放 TaskPublishLock]
    CleanStaging --> End[发布成功]
    ThrowConflict --> Rollback[保留或清理 staging 触发回滚]
```
