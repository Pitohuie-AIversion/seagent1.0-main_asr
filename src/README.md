# SEAgent Core Source Architecture (`src/`)

本目录包含 SEAgent（水下机器人任务智能交互与编排层）的核心源码。
代码已完成系统的领域驱动（DDD）父子层级结构治理与物理子包划分，同时在根目录保留向前兼容别名导出（Forwarding Shims），实现 100% 平滑零破坏调用。

---

## 1. 架构分层全景与物理目录结构

```
src/
├── handlers/                  # DialogueManager HSM 层次状态机子处理器
│   ├── base.py                # Handler 基类与双向属性透明代理 (__getattr__ / __setattr__)
│   ├── conversation_router.py # 意图路由、任务识别与闲聊分流
│   ├── slot_filling.py        # 槽位收集、事务性更新与指代消解
│   ├── equipment_cascade.py   # 4级设备级联（类别->系列->型号->单机）折叠与投影
│   ├── task_transition.py     # 任务切换、跨任务槽位非继承与流转判定
│   ├── constraint_decision.py # 软硬约束校验、软警告忽略与硬阻断门禁
│   └── task_commit.py         # 任务确认发布流水线、原子发布与快照归档
│
├── temporal/                  # 时间语义解析与时间范围计算域
│   ├── duration_parser.py     # 持续时长与时间跨度解析
│   ├── relative_time_parser.py# 相对时间、口语日期与 Temporal IR 解析器
│   ├── temporal_parser.py     # 时间表达式抽取与标准化总门面
│   ├── time_range_engine.py   # 时间关系与时间范围计算引擎
│   ├── time_context.py        # 对话时间上下文传递
│   └── simulated_time.py      # 系统基准时钟与仿真时间控制
│
├── asr/                       # 语音识别服务与音频标准化域
│   ├── asr_service.py         # ASR 语音转写服务
│   └── asr_normalizer.py      # 语音识别专用同音词与领域词纠错
│
├── slots/                     # 槽位存储、候选值仲裁与快照持久化域
│   ├── slot_store.py          # 槽位存储、版本递增与事务管理
│   ├── slot_snapshot_codec.py # 槽位快照的序列化/反序列化与安全校验
│   ├── slot_list_mutation.py  # 列表槽位差量增删与更新合并引擎
│   ├── candidate_resolver.py  # 槽位抽取候选值仲裁与确认
│   ├── normalization_contract.py # 规范化契约定义与数据模型
│   ├── task_patch.py          # 任务 Patch 生成与 Diff 对比
│   ├── task_slot_filter.py    # 任务类型有效槽位过滤
│   └── visible_selection_provenance.py # 界面可见选项溯源
│
├── validation/                # 安全门禁、规则引擎与约束验证域
│   ├── validator.py           # 业务软硬约束、地理围栏与遥测规则校验门面
│   ├── rule_engine.py         # 规则引擎与业务约束推导
│   ├── spatial_validator.py   # 空间范围、禁入区与底质规则
│   ├── telemetry_gate.py      # 机器人实时遥测、电量、传感器安全门禁
│   └── task_request_guard.py  # 任务请求有效性防御闸门
│
├── dispatch/                  # 任务意图构建、适配器与发布派发域
│   ├── task_intent_builder.py # TaskIntent 构建、staging/final/quarantine 原子发布与文件锁
│   ├── task_dispatch.py       # 任务派发投递与归档
│   ├── task_capability_adapter.py # 机器人能力匹配与载荷适配器
│   ├── id_sequence.py         # 任务编号生成与持久化序列管理
│   ├── result_paths.py        # 持久化文件与产物路径管理
│   └── output_builder.py      # 对话文本响应与卡片组装生成
│
├── extraction/                # NLU、抽取与规范化域
│   ├── extractor.py           # LLM 槽位抽取、Few-shot 示例解析与纠错
│   ├── normalizer.py          # 字段值语义规范化
│   ├── coord_parser.py        # 经纬度/坐标多格式解析
│   ├── prompts.py             # 核心 Prompt 模板与元规则定义
│   ├── model_profile.py       # 模型 Profile 与参数配置
│   └── oilfield_linker.py     # 油田实体、作业区与井口实体链接器
│
├── session/                   # 对话会话状态、交互计划与路由域
│   ├── session_state.py       # 会话生命周期与元数据
│   ├── session_state_shadow.py# 影子状态跟踪与快照审计
│   ├── history_manager.py     # 会话轮次历史记录
│   ├── ui_state_builder.py    # 前端 UI 卡片与状态渲染
│   ├── interaction_plan.py    # 交互计划与槽位提问生成
│   ├── dialogue_snapshot.py   # 对话全局快照与恢复
│   └── intent_router.py       # 意图路由器（普通闲聊 vs 任务规划）
│
├── knowledge/                 # 知识库与本体选择引擎
│   └── ...
├── types/                     # 路由与交互核心数据类型定义
├── utils/                     # 通用工具函数集合
├── web/                       # FastAPI 路由与服务端点
│
└── 顶层核心门面与基础设施
    ├── dialogue_manager.py    # 对话总协调器门面 (HSM Facade)
    ├── constants.py           # 全局业务字段标签与常量单点真实源 (SSOT)
    ├── exceptions.py          # 核心业务与持久化自定义异常
    ├── llm_client.py          # 大模型统一接口客户端
    ├── hot_reload.py          # 规则、本体与配置热重载系统
    ├── knowledge_retriever.py # 知识库门面入口
    ├── environment_info.py    # 外部海况环境信息入口
    └── state_info.py          # 机器人实时状态信息入口
```

---

## 2. 核心架构约束与工程原则

1. **零破坏门面兼容（Zero-breakage Forwarding Shims）**：
   - 40 个被下沉到各领域子包的文件在 `src/` 根目录均保留了轻量代理门面（Forwarding Shim），利用 `sys.modules[__name__] = _target_mod` 保证历史外部导入与单元测试百分百兼容。
2. **单点真实源 (SSOT)**：
   - 字段标签与保留集合统一在 `src/constants.py` 维护；
   - 槽位状态以 `src/slots/slot_store.py` 为唯一权威事实；
   - 对话流转状态以 `DialogueManager` / HSM Handlers 为状态机权威。
3. **软硬约束严格隔离**：
   - 软约束警告（`blocked_soft`）允许用户通过明确的忽略关键词（`SOFT_IGNORE_KEYWORDS`）继续；
   - 硬约束违规（`blocked_hard`）具有最高阻断优先级，禁止通过任何“忽略/确认/继续”输入绕过。
4. **持久化安全与原子替换**：
   - 任务文件发布必须执行跨进程排他文件锁（`TaskPublishLock`）；
   - 先写临时文件再原子重命名（`os.replace`），并同步调用 `fsync` 确保落盘一致性；
   - 异常情况必须 fail-closed 并将未决文件保留在 `quarantine/`。
