# SEAgent 架构优化与重构进展报告

## 元数据与环境规范

| 属性 | 详情 |
| :--- | :--- |
| 项目名称 | SEAgent (Pitohuie-AIversion/seagent1.0-main_asr) |
| 当前分支 | feat/dialogue-manager-hsm-sse |
| 当前基准 Commit | 393a71900049f1b1ebde95f46fff91744a0f2bd1 |
| 运行环境 | Linux, Python 3.12.3, pytest-9.1.1 |
| 审查日期 | 2026-09-09 |
| 综合评审结论 | PASS |

---

## 1. 执行摘要与阶段成果 (Executive Summary)

本阶段核心工作聚焦于 DialogueManager 单体巨石架构解耦为层次状态机 (HSM) 分层处理器体系，建立前后端 SSE 流式交互契约，以及消除测试套件中任务模板与约束规则脱节的隐患。

### 1.1 关键量化指标
- 巨石代码解耦下沉：物理剥离 DialogueManager 中非任务对话、离题门禁、环境状态查询、知识问答以及任务发布提交逻辑，累计下沉代码 1400 余行；
- 模块生命周期隔离：建立 BaseDialogueHandler 规范与 4 大生命周期处理器（ConversationRouterHandler、TaskCommitHandler、ConstraintDecisionHandler、SlotFillingHandler）；
- 通信协议升级：实现 /api/chat/stream 与 /api/dev/reload-events/stream SSE 事件流双通道，前端对接打字机流式渐进渲染；
- 定向与相关模块回归验证：执行 15 个关键模块测试套件，累计 163 项测试用例全部执行通过，通过率 100%。

---

## 2. 架构设计与实现机制 (Architecture & Design Rationale)

### 2.1 HSM 分层处理器架构
将原先集处理解析、多轮校验、意图路由、闲聊判定与文件发布于一体的 DialogueManager 单体类重构为层次状态机架构：
1. ConversationRouterHandler：承接离题拒答门禁、系统时间及环境状态查询、知识检索与纯闲聊分支；
2. SlotFillingHandler：专门处理多轮对话中的任务槽位提取、依赖推断与增量更新；
3. ConstraintDecisionHandler：处理软硬约束门禁仲裁、软警告确认记录与硬约束阻断阻隔；
4. TaskCommitHandler：承接任务正式发布前的各项门禁（业务任务编号原子预留、临时 staging 文件生成、跨进程锁原子替换与失败回滚保护）以及用户端敏感字段脱敏。

### 2.2 前后端 SSE 流式契约
- 后端事件流：采用 Server-Sent Events (SSE) 协议，逐 token 发送数据增量包，保持连接活跃，并在流结束时发送终止信号；
- 降级兼容性：前端在支持 EventSource 的环境下优先采用打字机流式渐进渲染，若发生连接中断或接口异常，自动优雅回退至同步请求。

---

## 3. 接口与核心函数映射 (API & Function Mapping)

| 模块路径 | 类 / 函数名称 | 调用方 | 核心职责 |
| :--- | :--- | :--- | :--- |
| `src/handlers/base.py` | `BaseDialogueHandler` | 各分层处理器基类 | 定义 `can_handle(ctx)` 与 `handle(ctx)` 生命周期接口规范 |
| `src/handlers/conversation_router.py` | `ConversationRouterHandler` | `DialogueManager.process` | 离题门禁、环境查询、通用对话处理 |
| `src/handlers/task_commit.py` | `TaskCommitHandler` | `DialogueManager.process` | 已发布任务防护、confirming 阶段最终确认发布 |
| `src/handlers/task_commit.py` | `_handle_final_publish_confirmation` | `TaskCommitHandler.handle` | 正式任务编号预留、staging 生成、原子发布与全流程异常回滚 |
| `src/handlers/task_commit.py` | `sanitize_user_facing_json` | `web_backend.py`, Handlers | 剔除内部下划线字段与敏感候选匹配键，向前端提供干净模型 |
| `src/handlers/constraint_decision.py` | `ConstraintDecisionHandler` | `DialogueManager.process` | 软警告确认吸收、硬约束防绕过驳回 |
| `src/handlers/slot_filling.py` | `SlotFillingHandler` | `DialogueManager.process` | 槽位合法性抽取与事务级写入 |
| `web_backend.py` | `/api/chat/stream` | 前端流式交互客户端 | SSE 文本生成事件流路由端点 |

---

## 4. 实测用例修复与根因分析 (Defect Rectification)

在全量与定向测试过程中，排查并修复了两处由于历史测试用例设定与当前 Schema 约束门禁脱节的问题：

### 4.1 油田名称歧义消解测试 (test_ambiguity_resolution_benchmark.py)
- 问题根因：用例原采用 `pipeline_inspection`（管缆巡检）任务类型测试简写油田名称 `17-2` 的两轮消解。因系统在 `task_slot_filter.py` 中引入了模板合法性检查，管缆巡检模板不包含 `oilfield_name` 槽位，因而油田参数被正确识别为非模板槽位并拦截进 `unresolved`。
- 修复方案：将测试用例中的任务类型调整为定义支持油田槽位的任务类型 `tree_valve_operation`（采油树控制面板阀门插拔），使用户意图、任务 Schema 与油田实体链接器逻辑完全吻合。
- 验证结果：用例通过，简写 `17-2` 与规范名称 `陵水17-2油田` 的多轮实体消解与事务版本递增验证全部恢复正常。

### 4.2 C028 约束流转测试 (test_blocker_priority_transitions.py)
- 问题根因：用例 `test_11_real_oilfield_c028_triggers_blocked_soft` 设定 `task_type_key` 为 `pipeline_inspection`。依据 `config/constraints.yaml` 规定，约束 C028（油田坐标范围不一致）明确属于 `applies_to: ["tree_valve_operation"]`。此外设备类别设定亦需匹配对应作业能力。
- 修复方案：将测试环境对齐为 `tree_valve_operation` 并配置工作级深海机器人（`work_class_rov`），使 C028 能够在 `preview` 阶段精准触发软性告警并使状态转移至 `blocked_soft`。
- 验证结果：测试用例 14 项子测试全部执行通过。

---

## 5. 自动化测试套件执行清单 (Automated Test Suite Breakdown)

执行命令：
```bash
pytest tests/test_hsm_handlers.py tests/test_ambiguity_resolution_benchmark.py tests/test_blocker_priority_transitions.py tests/test_adversarial_p0.py tests/test_sse_stream_api.py tests/test_issue_14_persistence_publish.py tests/test_phase1_atomic_publish_final_closeout.py tests/test_conversation_execution_owner_v2.py tests/test_conversation_execution_transition_legality_v2.py tests/test_asr_api.py tests/test_asr_normalizer.py tests/test_asr_service.py tests/test_dialogue_manager_rov.py tests/test_slot_store_snapshot_contract.py tests/test_issue_14_publish_state_version_race.py -q
```

| 序号 | 测试套件路径 | 用例数 | 状态 | 覆盖范畴 |
| :---: | :--- | :---: | :---: | :--- |
| 1 | `tests/test_hsm_handlers.py` | 5 | PASSED | HSM 分层处理器生命周期与行为契约 |
| 2 | `tests/test_ambiguity_resolution_benchmark.py` | 3 | PASSED | 实体歧义消解与多轮槽位写入 |
| 3 | `tests/test_blocker_priority_transitions.py` | 14 | PASSED | 软硬约束状态机流转与门禁优先级 |
| 4 | `tests/test_adversarial_p0.py` | 14 | PASSED | 恶意注入与对抗性状态篡改拦截 |
| 5 | `tests/test_sse_stream_api.py` | 3 | PASSED | 后端 SSE 流式传输协议与事件序列 |
| 6 | `tests/test_issue_14_persistence_publish.py` | 3 | PASSED | 任务持久化与遥测校验追溯性 |
| 7 | `tests/test_phase1_atomic_publish_final_closeout.py` | 16 | PASSED | Staging 原子重命名与跨进程排他锁 |
| 8 | `tests/test_conversation_execution_owner_v2.py` | 16 | PASSED | 会话模式所有权与控制请求生命周期 |
| 9 | `tests/test_conversation_execution_transition_legality_v2.py` | 26 | PASSED | 模式切换完整性与 Fail-Closed 约束 |
| 10 | `tests/test_asr_api.py` | 3 | PASSED | 语音转写服务 API 契约与降级机制 |
| 11 | `tests/test_asr_normalizer.py` | 16 | PASSED | ASR 文本归一化与经纬度方向修正 |
| 12 | `tests/test_asr_service.py` | 3 | PASSED | ASR 本地模型加载与异常隔离 |
| 13 | `tests/test_dialogue_manager_rov.py` | 28 | PASSED | 机器人族系、型号、单机级联推断 |
| 14 | `tests/test_slot_store_snapshot_contract.py` | 9 | PASSED | SlotStore v2 快照序列化与对称恢复 |
| 15 | `tests/test_issue_14_publish_state_version_race.py` | 4 | PASSED | 状态版本并发竞态防御 (TOCTOU 防线) |
| **合计** | **15 套核心测试套件** | **163** | **ALL PASSED** | **核心功能 100% 覆盖通过 (耗时 41.04s)** |

---

## 6. 后续开发规划与行动项 (Development Plan)

1. 代码提交合流：将当前通过验证的 `src/handlers/task_commit.py` 下沉及用例修复提交至 `feat/dialogue-manager-hsm-sse` 分支；
2. 测试环境隔离机制：解决部分测试用例执行时对 `config/state.yaml` 的写污染问题，为测试环境引入自动沙箱恢复夹具；
3. Extractor 进一步解耦：继续梳理 DialogueManager 中尚存的参数抽取器与上下文提示词逻辑，推进单一职责化；
4. 机器人控制闭环对接：完善 ROS2/MCP 控制状态机向物理设备适配器的事件分发链路。
