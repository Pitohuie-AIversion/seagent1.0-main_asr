# SEAgent Core Source Architecture (`src/`)

本目录包含 SEAgent（水下机器人任务智能交互与编排层）的核心源码。

---

## 1. 架构分层全景

```
src/
├── handlers/              # DialogueManager HSM 层次状态机子处理器
│   ├── base.py            # Handler 基类与双向属性透明代理 (__getattr__ / __setattr__)
│   ├── conversation_router.py # 意图路由、任务识别与闲聊分流
│   ├── slot_filling.py    # 槽位收集、事务性更新与指代消解
│   ├── equipment_cascade.py # 4级设备级联（类别->系列->型号->单机）折叠与投影
│   ├── task_transition.py # 任务切换、跨任务槽位非继承与流转判定
│   ├── constraint_decision.py # 软硬约束校验、软警告忽略与硬阻断门禁
│   └── task_commit.py     # 任务确认发布流水线、原子发布与快照归档
│
├── 对话协调与状态表现层
│   ├── dialogue_manager.py        # 对话总协调器门面 (HSM Facade)
│   ├── state_info.py              # 对话状态快照与只读视图
│   ├── session_state.py           # 会话生命周期与元数据
│   ├── session_state_shadow.py    # 影子状态跟踪
│   ├── history_manager.py         # 会话轮次历史记录
│   ├── interaction_plan.py        # 交互计划与槽位提问生成
│   ├── ui_state_builder.py        # 前端 UI 卡片与状态渲染
│   └── output_builder.py          # 对话文本响应与语音播报生成
│
├── 槽位、规范化与约束验证
│   ├── slot_store.py              # 槽位存储、版本递增与事务管理
│   ├── validator.py               # 业务软硬约束、地理围栏与遥测规则校验
│   ├── normalizer.py              # 字段值语义规范化
│   ├── normalization_contract.py  # 规范化契约定义与数据模型
│   ├── coord_parser.py            # 经纬度/坐标多格式解析
│   └── visible_selection_provenance.py # 界面可见选项溯源
│
├── 任务生命周期与持久化
│   ├── task_intent_builder.py     # TaskIntent 构建、staging/final/quarantine 原子发布与文件锁
│   ├── task_patch.py              # 任务 Patch 生成与 Diff 对比
│   ├── id_sequence.py             # 任务编号生成与持久化序列管理
│   ├── task_slot_filter.py        # 任务类型有效槽位过滤
│   ├── task_request_guard.py      # 任务请求有效性防御闸门
│   ├── task_capability_adapter.py # 机器人能力匹配与适配器
│   └── result_paths.py            # 持久化文件与产物路径管理
│
├── 知识库与油田环境上下文
│   ├── knowledge_retriever.py     # 知识库检索、语义匹配与 RAG 向量检索
│   ├── oilfield_linker.py         # 油田实体、作业区与井口实体链接器
│   └── environment_info.py        # 外部海况、气象与机器人实时状态解析
│
├── NLU、抽取与 LLM 客户端
│   ├── extractor.py               # LLM 槽位抽取、Few-shot 示例解析与纠错
│   ├── prompts.py                 # 核心 Prompt 模板定义
│   ├── llm_client.py              # 大模型统一接口客户端
│   ├── intent_router.py           # 意图路由器（普通闲聊 vs 任务规划）
│   └── model_profile.py           # 模型 Profile 与参数配置
│
├── 时间与时序解析
│   ├── relative_time_parser.py    # 相对时间、口语日期与 Temporal IR 核心解析器
│   ├── duration_parser.py         # 时间跨度与持续时长解析
│   ├── simulated_time.py          # 系统基准时钟与仿真时间控制
│   └── time_context.py            # 时间上下文传递
│
├── ASR 语音识别服务
│   ├── asr_service.py             # ASR 语音转写服务
│   └── asr_normalizer.py          # 语音识别专用同音词与领域词纠错
│
└── 基础设施与公共组件
    ├── constants.py               # 全局业务字段标签与常量单点真实源 (SSOT)
    ├── exceptions.py              # 核心业务与持久化自定义异常
    ├── hot_reload.py              # 规则、本体与配置热重载系统
    ├── types/routing_types.py     # 路由与交互数据类型定义
    └── utils/common.py            # 通用工具函数集合
```

---

## 2. 核心架构约束与工程原则

1. **单点真实源 (SSOT)**：
   - 字段标签与保留集合统一在 `src/constants.py` 维护；
   - 槽位状态以 `SlotStore` 为唯一权威事实；
   - 对话流转状态以 `DialogueManager` / HSM Handlers 为状态机权威。
2. **双向状态同步代理**：
   - 所有 HSM Handler 继承自 `BaseDialogueHandler`，内部直接穿透读写 Manager 状态，禁止在 Handler 内部缓存冗余副本。
3. **软硬约束严格隔离**：
   - 软约束警告（`blocked_soft`）允许用户通过明确的忽略关键词（`SOFT_IGNORE_KEYWORDS`）继续；
   - 硬约束违规（`blocked_hard`）具有最高阻断优先级，禁止通过任何“忽略/确认/继续”输入绕过。
4. **持久化安全与原子替换**：
   - 任务文件发布必须执行跨进程排他文件锁（`TaskPublishLock`）；
   - 先写临时文件再原子重命名（`os.replace`），并同步调用 `fsync` 确保落盘一致性；
   - 异常情况必须 fail-closed 并将未决文件保留在 `quarantine/`。
