# Changelog

All notable changes to the SEAgent multi-agent task planning system will be documented in this file.

> 历史变更记录：以下描述与测试数字对应各条目记录时的版本，本轮未重新执行其历史实验。2026-09-30 核对发现部分能力描述已不适合作为现状：语义路由仍有局部规则修正，机器人候选入口未接入完整水深/载荷过滤，遥测也不能以单一“24 小时”概括。当前边界见[架构总览](docs/architecture/overview.md)和[设计契约](docs/current_design_contract.md)。

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Refactored — 上帝代码深度治理、模块父子目录化与 Handlers/Dispatch/Web 领域解耦（2026-10-28）

- **Dispatch 调度领域存储与模式校验父子分层结构化**：
  - 将原平铺在 `src/dispatch/` 的 5 个从属底层模块按职责沉降至专有子包：
    * `src/dispatch/storage/`：承载 POSIX link 原子持久化、跨进程文件锁、检疫区管理、统一产物路径与自增序号生成（`atomic_task_storage.py`, `result_paths.py`, `id_sequence.py`）；
    * `src/dispatch/validation/`：承载 TaskIntent v1/v2 JSON 模式校验与机器人能力适配器（`task_intent_schema_validator.py`, `task_capability_adapter.py`）；
  - `src/dispatch/` 根目录仅收敛保留 Level-1 任务意图编排主门面（`task_intent_builder.py`, `task_dispatch.py`, `catalog_resolver.py`, `output_builder.py`）；
  - 保留 100% 透明动态兼容垫片（`sys.modules[__name__] = _impl`），平滑兼容历史猴子补丁打桩与外部模块引用。
- **Web 控制器路由沉降至独立子包**：
  - 创建 `src/web/routes/` 子包，将平铺在根目录的 8 个端点文件（`routes_asr.py`, `routes_chat.py`, `routes_dev.py`, `routes_mcp.py`, `routes_pages.py`, `routes_robot.py`, `routes_time_history.py`, `routes_translate.py`）整体迁移沉降为子模块；
  - `src/web/` 根目录仅收敛保留服务端单例底座与流处理基础设施（`state.py`, `chat_stream.py`, `chat_request_cache.py`）；
  - 原平铺路径保留完全透明的别名垫片，支持 `web_backend.py` 与各测试无感运行。
- **WriteReplyGrounder 上帝模块（1,544 行）深度流水线拆解**：
  - 将原单文件内聚的庞大逻辑按职责拆解为 3 大高内聚子模块：
    * `assertion_scrubber.py`（411 行）：模型未提交断言、幻觉词及排版语法清洗；
    * `field_grounder.py`（570 行）：槽位事实、时间窗口、水深与载荷断言对齐校验；
    * `receipt_builder.py`（282 行）：权威客观回执构建、候选投影与回复组装；
  - 主门面 `write_reply_grounder.py` 瘦身至 291 行（减少 81.1%），并保持对静态方法的类属性委托绑定，确保向后兼容。
- **Handlers 领域 24 个扁平文件父子分层结构化**：
  - 将原平铺在 `src/handlers/` 根目录的 24 个文件按职责沉降划分为 5 大从属领域子包：
    * `equipment/`：设备 4 级拓扑级联推导、候选折叠与选项范围限定（`equipment_cascade.py`, `equipment_collapse.py`, `equipment_scoping.py`, `equipment_assertion_grounding.py`）；
    * `extraction/`：多轮槽位抽取主管道、事务控制、载荷变异、油田消歧与显式物理锚定（`slot_extraction_pipeline.py`, `slot_transaction.py`, `payload_mutation.py`, `oilfield_confirmation.py`, `explicit_value_grounding.py`）；
    * `grounding/`：写操作回复多阶段事实证据比对与模版装配；
    * `lifecycle/`：任务生命周期阶段跃迁、紧急控制与命令意图确定性谓词（`task_transition.py`, `execution_control.py`, `command_predicates.py`）；
    * `routing/`：离题门禁、目录问答、实时遥测状态查询、拟态时间推演与准入放行解耦（`off_topic_gate.py`, `grounded_catalog.py`, `telemetry_status.py`, `hypothetical_time_preview.py`, `knowledge_reply_grounder.py`, `task_structure_grounding.py`）。
  - `src/handlers/` 根目录仅收敛保留 Level-1 HSM 核心状态机生命周期门面（`base.py`, `conversation_router.py`, `slot_filling.py`, `constraint_decision.py`, `task_commit.py`）。
- **Knowledge 机器人选型子领域实体下沉**：
  - 将 `selection_engine.py` 内部实例化的变体规格评估器（`variant_evaluator.py`）、实体机消歧解析器（`unit_resolver.py`）、拓扑有向图（`hierarchy_graph.py`）及数据模型（`models.py`）整体下沉至 `src/knowledge/selection/` 专有子包。
- **100% 透明动态模块别名兼容（sys.modules Aliasing）**：
  - 所有原顶级平铺路径均保留轻量兼容别名模块，采用 `sys.modules[__name__] = _impl` 保证模块单例与 `monkeypatch` 打桩兼容，彻底杜绝已有测试断链。
- **超长单体方法（God Methods）深度拆解**：
  - 分解 `equipment_cascade.py`（1300+ 行）、`slot_extraction_pipeline.py`（774 行）、`write_reply_grounder.py`（1500+ 行）中的超长单体方法为单一职责阶段流水线；
  - 彻底清除 `BaseDialogueHandler` 危险的 `__setattr__` 反向篡改宿主与 `TaskTransitionManager` 隐式 `__getattr__` 弹跳。
- **多级全量回归验证**：
  - 执行 13 套关键领域回归测试套件（434 项测试）、8 套 Web 路由测试（175 项测试）、7 套 Grounding 回归测试（845 项测试）、持久化与自增序号测试（73 项测试），累计超过 1,500 项测试 100% 通过（0 失败）。

### Added & Enhanced — 任务核对优化、移动端工作台与无障碍交互（2026-10-27）

- **工作台字段分组与参数输入引导**：
  - [frontend/js/task-insights.js](frontend/js/task-insights.js) 引入基于 Section 的参数分组聚合渲染（任务基础属性、时空与环境约束、机器人与执行负载），按业务重要性与填报状态动态排序与状态着色。
  - 新增参数焦点引导机制（Parameter Focus Guidance），当关键参数缺失时高亮提示并联动输入框聚焦，引导用户高效补充缺失项。
- **确认卡片紧凑摘要与折叠详情**：
  - [frontend/js/task-insights.js](frontend/js/task-insights.js) 与 [frontend/css/workspace.css](frontend/css/workspace.css) 重构确认卡片：主区域紧凑展示核心决策字段（任务类型、作业区域、执行设备、启动与持续时间），次要参数与扩展元数据收拢至“查看完整参数详情”折叠面板中，降低用户决策认知负荷。
- **移动端工作表快捷栏与状态深度同步**：
  - [frontend/js/workspace.js](frontend/js/workspace.js) 新增移动端（`max-width: 760px`）底部快捷任务栏，引入 `hadActiveTask` 追踪机制，解决移动端初始化空白状态与任务进入时工作表展开/收起（`aria-expanded`）的可见性冲突；支持快速一键直达工作表标题而不丢失当前输入草稿。
- **翻译条交互与键盘无障碍访问修复**：
  - [frontend/css/workspace.css](frontend/css/workspace.css) 优化 `msg-translate-bar` CSS 选择器作用域为 `.message:not(:hover):not(:focus-within) .msg-translate-bar[data-translation-state="idle"]`，修复键盘 Tab 键焦点无法触发翻译工具栏显示的样式特异性（specificity）Bug。
- **UI 资产更新与全量 Playwright 浏览器自动化回归**：
  - 新增 `docs/ui-preview/task-next-step.png` 与 `docs/ui-preview/confirmation-detail.png` 等多端实测预览图；
  - 新增 [tests/test_frontend_workspace_browser.py](tests/test_frontend_workspace_browser.py) 浏览器自动化测试套件；
  - 经验证：Playwright 端到端浏览器测试 74 项用例 100% 通过（67.18s）。

### Fixed & Enhanced — 真实遥测时效对齐与多轮事实锚定 Grounding（2026-10-26）

- **统一遥测新鲜度门禁（Telemetry Freshness Grounding）**：
  - 新增 [src/telemetry_evidence.py](src/telemetry_evidence.py) 与增强 [src/state/runtime_checker.py](src/state/runtime_checker.py)，建立全局统一遥测时效检查器；严格区分当前新鲜遥测（fresh ≤ 300s）、已过期遥测（stale）与未来预约任务（future scheduled），防止调度器将未来预约任务错误当作当前环境生效。
- **大模型 Prompt 与回复事实硬锚定（Reply Grounding）**：
  - [src/handlers/write_reply_grounder.py](src/handlers/write_reply_grounder.py) 与 [src/handlers/task_structure_grounding.py](src/handlers/task_structure_grounding.py) 强化写操作回复事实核验：仅当底层 SlotStore 存在已持久化的真实修改时，才允许模型回复确认或已保存；杜绝大模型在参数被拦截或未生效时回复幻觉。
- **假设性时间预览与增量载荷解析**：
  - 新增 [src/handlers/hypothetical_time_preview.py](src/handlers/hypothetical_time_preview.py)，支持“如果现在启动…预计何时结束”等假设性提问的推演预览，不污染当前会话的物理槽位。
  - [src/handlers/payload_mutation.py](src/handlers/payload_mutation.py) 修复增量列表修改时的目录前缀同步。
- **海博会演示脚本与现场记录归档**：
  - 整理并新增 [docs/development/haibo-demo-dialogue.md](docs/development/haibo-demo-dialogue.md)、[docs/development/haibo-demo-dialogue-record-20261021.md](docs/development/haibo-demo-dialogue-record-20261021.md) 以及 [docs/development/haibo-demo-five-hour-submission.md](docs/development/haibo-demo-five-hour-submission.md)，记录 7 轮真实场景演练测试用例。

### Added & Enhanced — 实体抽取稳定化、机器人可行性域选择与品牌资产（2026-10-21）

- **多模态参数标量抽取与时序解析稳定化**：
  - [src/temporal/temporal_parser.py](src/temporal/temporal_parser.py) 与 [src/temporal/relative_time_parser.py](src/temporal/relative_time_parser.py) 支持小数（如“1.5小时”）与复合持续时间解析。
  - [src/extraction/coord_parser.py](src/extraction/coord_parser.py) 重构空间坐标提取，增强边界范围检测与起止点约束推断；支持 `water_depth` 水深与 `cable_type` 海缆类型等领域标量参数的精准绑定。
- **机器人静态可行性硬过滤（Robot Feasibility Filtering）**：
  - [src/dispatch/catalog_resolver.py](src/dispatch/catalog_resolver.py) 与 [src/knowledge/selection_engine.py](src/knowledge/selection_engine.py) 接入变体静态参数（作业水深上限、载荷承载阈值），在候选池初筛阶段剔除不符合物理条件的机器人型号或系列。
- **写操作回复事实核验与子句级历史作用域隔离**：
  - [src/handlers/explicit_value_grounding.py](src/handlers/explicit_value_grounding.py) 与 [src/handlers/oilfield_confirmation.py](src/handlers/oilfield_confirmation.py) 引入子句级撤回与冲突保护，用户在修改参数或否定旧参数时精确清除旧事实，避免历史槽位脏读。
- **SEALIEN 品牌视觉资产与无损图标接入**：
  - 引入 SEALIEN 官方蓝色/白色高保真 Logo、69x69 无损螺旋桨 Mark 图标与全套 favicon 资产，统一前端工作台视觉规范。
  - 新增 [docs/architecture/session-confirmation-recovery.md](docs/architecture/session-confirmation-recovery.md) 文档。

### Refactored — 机器人单机解析上下文增强与设备级联重构（2026-10-20）

- **单机编号注入父级系列与型号上下文**：
  - [src/knowledge/unit_resolver.py](src/knowledge/unit_resolver.py) 与 [src/handlers/equipment_cascade.py](src/handlers/equipment_cascade.py) 重构：在 Unit 单机编号解析（如“1号机/2号机”）时主动注入父级 Family（系列）和 Variant（型号）上下文，消除同名编号在不同型号间的歧义绑定。
- **设备多级从属交互推导**：
  - 优化 [src/knowledge/selection_engine.py](src/knowledge/selection_engine.py) 与 [src/knowledge/query_executor.py](src/knowledge/query_executor.py)，支持采油树（“深海1号”）级联推导相关水下阀门和机器人组别。

### Added — 前端遥测仪表板与实时确认卡片集成（2026-10-19）

- **前端遥测仪表板（LLM Telemetry Dashboard）**：
  - [frontend/index.html](frontend/index.html) 与 [frontend/js/index.js](frontend/js/index.js) 集成模型性能与遥测状态看板，实时直观展示上下文预算（context tokens）、推理耗时、网络延迟与遥测新鲜度状态灯。
- **结构化确认卡片交互组件**：
  - 在消息流中提供结构化确认卡片，支持多行核对、约束状态展示与一键交互。
- **矢量图标与现代 UI 资产扩充**：
  - 新增 11 个现代纯矢量 SVG 图标（`clock.svg`, `edit.svg`, `history.svg`, `mic.svg`, `publish.svg`, `reset.svg`, `stop.svg`, `send.svg` 等），提升界面工程质感。

### Added & Fixed — 整轮编辑保护、时序范围隔离与工作台增强（2026-10-19）

- **整轮编辑保护与断言范围检测（Whole-Turn Edit Protection）**：
  - 新增 [src/extraction/assertion_scope.py](src/extraction/assertion_scope.py)，在单轮多子句输入中识别撤销、修改与保持语句的作用域边界。
- **时序与载荷跨轮隔离保护**：
  - 在 [src/temporal/temporal_parser.py](src/temporal/temporal_parser.py) 和 [src/handlers/payload_mutation.py](src/handlers/payload_mutation.py) 中接入断言作用域，防止用户在一句话中提到“不要A只要B”时旧状态污染。
- **会话竞态 HTTP 409 保护**：
  - 在 [src/web/routes_chat.py](src/web/routes_chat.py) 中增加会话并发重置/查询互斥保护，当客户端在处理长请求期间重置或并发修改会话时，明确返回 HTTP 409 Conflict，防止会话状态被静默覆盖。
- **工作台 UI、Task Insights HUD 与 ROS 2 监控升级**：
  - 重构 [frontend/js/task-insights.js](frontend/js/task-insights.js) 和 [frontend/js/workspace.js](frontend/js/workspace.js)，引入专业海洋工程 HUD 界面、任务槽位实时生命周期展示与 ROS 2 执行进度看板。
- **UI 预研与国际主流工业界面对标**：
  - 撰写 [docs/ui-references/README.md](docs/ui-references/README.md)，对标 Palantir Workshop、Foxglove Agent、Kongsberg OceanView 与 Greensea Workspace 架构设计；新增 [docs/progress/2026-10-19-whole-turn-protection-fixes.md](docs/progress/2026-10-19-whole-turn-protection-fixes.md)。

### Fixed — 空间约束 C033 管缆巡检起止重合端点强阻断（2026-10-19）

- **空间硬约束 C033 规则落地**：
  - [config/constraints.yaml](config/constraints.yaml) 补充定义 `C033` 规则（管缆巡检任务必须具有位移距离，起点与终点不能重合）。
  - [src/validation/spatial_validator.py](src/validation/spatial_validator.py) 实现 `start_end_points_distinct` 校验方法并在 [src/validation/validator.py](src/validation/validator.py) 注册，彻底杜绝起点终点坐标相同时通过审核；约束失败直接硬阻断（`blocked_hard`），必须由用户修正坐标。

### Added — 请求重放幂等缓存、SSE 进度流式通知与生命周期追踪（2026-10-16）

- **有限请求重放缓存（ChatRequestCache）**：
  - 新增 [src/web/chat_request_cache.py](src/web/chat_request_cache.py) 提供基于请求指纹的 bounded replay cache，杜绝网络重试引发的重复执行和状态重复漂移，提升接口幂等性。
- **SSE 实时进度流式通知（Live Progress Streaming）**：
  - 新增 [src/web/chat_stream.py](src/web/chat_stream.py)，支持在长耗时推理和校验阶段主动向前端推送阶段事件（`intent_routing` -> `slot_extraction` -> `constraint_checking` -> `done`），前端 [frontend/js/index.js](frontend/js/index.js) 接入全生命周期跟踪。

### Fixed — 载荷显式删除保护与装备目录前缀同步（2026-10-16）

- **载荷显式删除与保留子句同步（Payload Mutations）**：
  - [src/handlers/payload_mutation.py](src/handlers/payload_mutation.py) 修复复合指令中“保留机械手但去掉声呐”场景下删除指令被保留子句覆盖的漏洞，确保删除与保留意图在同一轮处理中均生效，并同步目录前缀。

### Fixed — 复合持续时间解析与显式时间回复事实锚定（2026-10-16）

- **裸半数时长复合解析修复（Temporal Composite Parsing）**：
  - [src/temporal/duration_parser.py](src/temporal/duration_parser.py) 与 [src/temporal/temporal_parser.py](src/temporal/temporal_parser.py) 修复“一个半小时”、“半天”等自然语言时长的复合提取缺陷。
- **显式时间回复事实锚定**：
  - [src/handlers/write_reply_grounder.py](src/handlers/write_reply_grounder.py) 增强时间回复事实证据比对，避免在用户指定绝对时间时回复未格式化或过时的时区推断。

### Docs — 大模型真实边界复测记录与压力测试归档（2026-10-16）

- **真实模型边界测试与压力验证归档**：
  - 补充归档 [docs/progress/2026-10-15-real-model-followup.md](docs/progress/2026-10-15-real-model-followup.md)、[docs/progress/2026-10-15-stress-boundary-followup.md](docs/progress/2026-10-15-stress-boundary-followup.md) 及 [docs/progress/2026-10-15-stress-boundary-fixes.md](docs/progress/2026-10-15-stress-boundary-fixes.md)，记录基于本地真实 Qwen3.5-9B 模型的并发、压力测试与长对话评测事实。

### Refactored — Dispatch 领域 TaskIntent 上帝代码解耦与职责分治（2026-10-15）

- **抽离底层原子文件锁与写入基础设施**：新增 [src/dispatch/atomic_task_storage.py](src/dispatch/atomic_task_storage.py)（83 行），专职负责 `TaskPublishLock`（跨进程排他锁）、`TaskCommitUncertainError`、`_write_all`（POSIX 完整写入保障）与 `_atomic_commit_noreplace`（`os.link` 原子无覆盖发布机制）。
- **抽离 TaskIntent 双模态契约校验器**：新增 [src/dispatch/task_intent_schema_validator.py](src/dispatch/task_intent_schema_validator.py)（234 行），专职负责 `validate_task_intent_v1`、`validate_task_intent_v2`、`validate_task_intent` 双模态分派校验，以及时空坐标、时间范围与有效负载等纯函数校验。
- **构建器实体聚焦与透明兼容**：[src/dispatch/task_intent_builder.py](src/dispatch/task_intent_builder.py) 从 946 行缩减至 671 行（净减 274 行），专注领域实体构造与事务管理，并在模块顶层透明 re-export 所有下沉符号，保证全库既有接口与测试 100% 向后兼容。
- **三级测试全绿验收**：Level 1 定向测试 73 项（17 subtests）、Level 2 治理与架构不变量 36 项、Level 3 核心业务回归 171 项全量通过（0 失败）。

### Fixed & Enhanced — 即时启动时间物理锚定与流式槽位监控面板（2026-10-15）

- **即时启动时间确定性物理锚定**：
  - [src/temporal/relative_time_parser.py](src/temporal/relative_time_parser.py) 扩充即时词汇覆盖（新增“即刻”、“当即”等）；
  - [src/handlers/explicit_value_grounding.py](src/handlers/explicit_value_grounding.py) 新增确定性即时 `start_time` 物理锚定兜底：当检测到“任务现在开始/即刻开始/立刻开始”且无否定/条件时，直接调用 `get_current_datetime()` 生成绝对 ISO 时间并规范化；
  - [src/extraction/extractor.py](src/extraction/extractor.py) 增强 Prompt 规则 8，指导大模型将即时启动词映射为绝对时间，并补充正则过滤彻底剔除大模型生成的元思考反思。
- **流式接口补齐状态卡片打印**：[src/web/routes_chat.py](src/web/routes_chat.py) 在 `/api/chat/stream` 每次调用 `mgr.process()` 后补齐 `print_status(mgr)` 调用，修复前端 HUD 走 SSE 流式时后端控制台不打印结构化槽位状态面板的缺陷。
- **启动与并发死锁根除**：[run.py](run.py) 移除实例化 vLLM 前调用 `torch.cuda.is_bf16_supported()` 造成的 CUDA 预初始化；将 multiprocess 方式锁定为 `fork`，彻底杜绝子进程 `spawn` 死锁与超时。


### Refactored — DDD 领域子包模块化与根目录 Shims 彻底清除（2026-09-20）

- **`src/` 扁平文件领域分层**：将原先平铺在 `src/` 根目录的 48 个 Python 文件按 DDD 领域驱动高内聚原则彻底划分为 12 个领域子包（`asr/`, `temporal/`, `slots/`, `validation/`, `dispatch/`, `extraction/`, `session/`, `handlers/`, `knowledge/`, `types/`, `utils/`, `web/`）。
- **彻底消除 40 个转发垫片（Shims）**：物理删除 `src/` 根目录下遗留的 40 个 shim 转发文件，根目录收敛为仅保留 10 个系统总门面与全局基础设施文件（`dialogue_manager.py`, `llm_client.py`, `knowledge_retriever.py`, `constants.py` 等）。
- **全仓库依赖与绝对导入对齐**：全量更新 160+ 个模块及测试文件的导入路径，统一指向正规的 `src.<subpackage>.<mod>`；修复 `src/handlers/` 下 17 处原指向根目录的两层相对导入。
- **子包按需延迟加载机制**：各子包 `__init__.py` 采用 PEP 562 延迟加载机制（`__getattr__`），杜绝相互引用产生的循环导入风险，优化冷启动开销。
- **全量回归与端到端真实测试通过**：`pytest -q` 全量 2631 个测试用例（530 subtests）100% 通过（0 失败）；并完成基于真实 Qwen3.5-9B 与 ASR 模型的端到端多轮用户交互体验与任务原子落盘验收。

### Added — Phase 2：LLM 语义权威 & 约束驱动机器人候选（2026-08-13）

- **LLM 语义权威路由（ADR-005 第二阶段）**：[src/session/interaction_plan.py](src/session/interaction_plan.py) 引入 `InteractionPlan`，`operation`（READ / WRITE / CONTROL / CLARIFY）成为每轮路由的唯一语义权威字段；后端确定性代码不再根据业务关键词改变操作类型。
- **约束驱动机器人候选自动收敛（ADR-008）**：`KnowledgeBase.get_feasible_robot_selection_domain()` 成为唯一权威候选计算入口，将已确认 `water_depth`、`payload` 与即时任务运行状态纳入候选树过滤，执行"0 关闭 / 1 自动绑定 / 多等待消歧"三段决策。Validator 与 Snapshot restore 复用同一四级关系校验。
- **快照恢复内存原子性（ADR-007）**：`DialogueManager.load_snapshot()` 采用隔离候选管理器方案，所有字段与枚举校验在候选对象完成后才一次性提交，防止半恢复状态污染运行中会话。
- **Validation 不再 fallback 到 Clarification**：禁用校验失败退化为 CLARIFY 的兜底行为；约束失败产生明确的 `blocked_hard` 或 `blocked_soft`。
- **Slot Schema 过滤增强**：任务类型切换后，不属于新 schema 的旧 candidate / conflict / invalid 机器人选择自动失效，防止跨任务类型状态污染。
- **可见来源选择追踪**：新增 [src/slots/visible_selection_provenance.py](src/slots/visible_selection_provenance.py)，严格校验编号候选选择的可见来源，阻止基于隐藏候选顺序的误写。
- **Grounded 字段推荐逻辑**：引入 `recommend` 关系，`DialogueManager` 基于已确认任务条件给出有证据的字段推荐，不以列表第一项冒充智能推荐。
- **坐标解析确定性增强**：[src/extraction/coord_parser.py](src/extraction/coord_parser.py) 重构，保证坐标解析结果与输入格式无关的唯一确定性。
- **遥测状态快照窗口扩展至 24 小时**（commit c7b3289）：snapshot validity window 从 300 秒扩展到 24 小时，与现场操作实际时间跨度匹配；同时增强设备单元 ID 解析逻辑。
- **双能力欢迎消息（ADR-006）**：欢迎消息精确呈现两项能力（知识问答 + 任务创建与准入），不再提及紧急模式。
- **前端 Markdown 安全渲染（PR #41）**：assistant 消息内容进行安全 Markdown 渲染，防止 XSS 同时支持格式化展示。
- **新增架构决策记录 ADR-004 ～ ADR-008**：
  - [ADR-004: 确定性任务请求守卫](docs/decisions/ADR-004-deterministic-task-request-guard.md)
  - [ADR-005: LLM 语义权威](docs/decisions/ADR-005-llm-semantic-authority.md)
  - [ADR-006: 双能力欢迎消息](docs/decisions/ADR-006-two-capability-welcome-message.md)
  - [ADR-007: 快照恢复内存原子性](docs/decisions/ADR-007-atomic-snapshot-restore.md)
  - [ADR-008: 约束驱动机器人候选收敛](docs/decisions/ADR-008-constraint-aware-robot-selection.md)

### Added — Phase 1.9+：交互规划 & 测试基础设施（2026-08-12）

- **InteractionPlan 第一阶段集成**（commit 07fc1de）：`InteractionPlan` 接入核心对话逻辑，测试套件强制要求每轮产出结构化 turn plan。
- **TaskPatch 引擎**：新增 [src/slots/task_patch.py](src/slots/task_patch.py)，支持任务参数字段级补丁操作，不覆盖整体 SlotStore。
- **任务请求守卫（TaskRequestGuard）**：新增 [src/validation/task_request_guard.py](src/validation/task_request_guard.py)，防止同轮内多任务意图误触发。
- **Pending Action 积累测试支持**：`run_accumulation_integration_tests.py` 更新，支持 pending actions 状态场景验证。
- **机器人能力自动级联收敛**（commit 8e1a079）：从 Class 层级逐级向下自动绑定唯一候选，实现 capability 预选。

### Added — Phase 1.5：核心架构闭环（已合并 main）

- **WRITE / QUERY 意图分路路由**：新增 [src/session/intent_router.py](src/session/intent_router.py) (`IntentRouter`, `IntentRouteResult`)，将用户输入解耦为写任务状态 (`WRITE`) 与读知识/状态 (`QUERY`) 两个独立通道。
- **SlotStore 状态中心与事务机制**：引入 [src/slots/slot_store.py](src/slots/slot_store.py) (`SlotStore`, `Slot`) 作为 Single Source of Truth，支持版本号追踪、快照导出/恢复与回滚事务。
- **QUERY 通道只读状态保护**：在 [src/dialogue_manager.py](src/dialogue_manager.py) (`_handle_non_task_route`) 中新增快照与不变性断言机制，确保查询请求绝不意外篡改任务槽位。
- **TaskIntent 原子安全持久化**：在 [src/dispatch/task_intent_builder.py](src/dispatch/task_intent_builder.py) 中引入暂存区 Staging 机制、跨进程排他锁 `TaskPublishLock` 与原子提交 `_atomic_commit_noreplace`，防止落盘并发覆盖与非法篡改。
- **设备候选解析与别名层级约束**：支持多层级（系列/型号/单机）别名映射及 `canonical_exact` / `alias_exact` / `llm_semantic` 递进解析算法。
- **实时机器人状态与物理海况约束**：读取 `config/state.yaml` 实时遥测数据，支持 `status_ref` 解析与物理限制校验。
- **语音识别 (ASR) 与领域实体 Link**：集成 ASR 候选词纠错与油田实体打分 Link。
- **CI 与自动化回归测试**：配置语法编译检查、全量单元测试与回归报告生成流水线。

### Changed

- **Responder 消息生成重构**：[src/dispatch/output_builder.py](src/dispatch/output_builder.py) 消除固定关键词依赖，由模型语义权威驱动结构化回复生成。
- **路由器设备缩写处理（PR #42）**：修复设备缩写紧邻中文文字时的路由识别问题。
- **Candidate 自动 collapse 契约对齐（issue #40）**：明确 candidate 自动折叠触发条件并恢复安全边界测试覆盖。
- **多 Payload 合并策略**：优化列表类型槽位解析，避免新旧载荷覆盖丢失。
- **对话状态机拒绝流控制**：优化硬约束违规计数器与拒绝警告机制。

### Fixed

- **CI 环境目录权限兼容**：修复非 root Runner 环境下目录创建的 `PermissionError` 回退机制。
- **设备别名映射标准化**：修复设备名称在对话管理、SlotStore 与路由层之间标识符不一致问题。
- **设计问答 WRITE 过度拦截（commit 0f8c1fe）**：修复设计类问答被写操作证据门误拦截，恢复普通对话路径。
- **WRITE 证据门正则精化**（commits 86fe37f、ec89e4c）：精确匹配参数赋值与 payload 动作，降低误拦截率。
- **ASR `update_robot_fleet`**：修复机器人舰队动态更新接口。

### Security

- **Path Traversal 与 TaskIntent 篡改防御**：在 [src/dispatch/task_intent_builder.py](src/dispatch/task_intent_builder.py) 中严格校验目标路径、符号链接与 PID 拥有权，禁用硬删与强制替换。
