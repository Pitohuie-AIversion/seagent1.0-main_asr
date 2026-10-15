# Changelog

All notable changes to the SEAgent multi-agent task planning system will be documented in this file.

> 历史变更记录：以下描述与测试数字对应各条目记录时的版本，本轮未重新执行其历史实验。2026-09-30 核对发现部分能力描述已不适合作为现状：语义路由仍有局部规则修正，机器人候选入口未接入完整水深/载荷过滤，遥测也不能以单一“24 小时”概括。当前边界见[架构总览](docs/architecture/overview.md)和[设计契约](docs/current_design_contract.md)。

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

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
