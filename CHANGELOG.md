# Changelog

All notable changes to the SEAgent multi-agent task planning system will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Added
- **WRITE / QUERY 意图分路路由**：新增 [src/intent_router.py](file:///root/mzy/seagent1.0-main_asr/src/intent_router.py) (`IntentRouter`, `IntentRouteResult`)，将用户输入解耦为写任务状态 (`WRITE`) 与读知识/状态 (`QUERY`) 两个独立通道。
- **SlotStore 状态中心与事务机制**：引入 [src/slot_store.py](file:///root/mzy/seagent1.0-main_asr/src/slot_store.py) (`SlotStore`, `Slot`) 作为 Single Source of Truth，支持版本号追踪、快照导出/恢复与回滚事务。
- **QUERY 通道只读状态保护**：在 [src/dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py) (`_handle_non_task_route`) 中新增快照与不变性断言机制，确保查询请求绝不意外篡改任务槽位。
- **TaskIntent 原子安全持久化**：在 [src/task_intent_builder.py](file:///root/mzy/seagent1.0-main_asr/src/task_intent_builder.py) 中引入暂存区 Staging 机制、跨进程排他锁 `TaskPublishLock` 与原子提交 `_atomic_commit_noreplace`，防止落盘并发覆盖与非法篡改。
- **设备候选解析与别名层级约束**：在 [src/output_builder.py](file:///root/mzy/seagent1.0-main_asr/src/output_builder.py) 和 [src/extractor.py](file:///root/mzy/seagent1.0-main_asr/src/extractor.py) 中支持多层级（系列/型号/单机）别名映射及 `canonical_exact` / `alias_exact` / `llm_semantic` 递进解析算法。
- **实时机器人状态与物理海况约束**：通过 [src/knowledge_retriever.py](file:///root/mzy/seagent1.0-main_asr/src/knowledge_retriever.py) 和 [src/state_info.py](file:///root/mzy/seagent1.0-main_asr/src/state_info.py) 读取 `config/state.yaml` 中的实时遥测数据，支持 `status_ref` 解析与物理限制校验。
- **语音识别 (ASR) 与领域实体 Link**：集成 [src/asr_service.py](file:///root/mzy/seagent1.0-main_asr/src/asr_service.py)、[src/asr_normalizer.py](file:///root/mzy/seagent1.0-main_asr/src/asr_normalizer.py)（候选词+上下文纠错）与 [src/oilfield_linker.py](file:///root/mzy/seagent1.0-main_asr/src/oilfield_linker.py)（油田实体打分 Link）。
- **CI 与自动化回归测试**：在 [.github/workflows/tests.yml](file:///root/mzy/seagent1.0-main_asr/.github/workflows/tests.yml) 中配置语法编译检查、全量单元测试与回归报告生成流水线。

### Changed
- **多 Payload 合并策略**：在 [src/extractor.py](file:///root/mzy/seagent1.0-main_asr/src/extractor.py) 中优化列表类型槽位解析逻辑，避免重复提炼或新旧载荷覆盖丢失。
- **对话状态机拒绝流控制**：在 [src/dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py) 中优化硬约束违规计数器与拒绝警告机制。

### Fixed
- **CI 环境目录权限兼容**：在 [src/result_paths.py](file:///root/mzy/seagent1.0-main_asr/src/result_paths.py) 中修复非 root Runner 环境下目录创建的 `PermissionError` 回退机制。
- **设备别名映射标准化**：修复设备名称在对话管理、 SlotStore 与路由层之间标识符不一致的问题。

### Security
- **Path Traversal 与 TaskIntent 篡改防御**：在 [src/task_intent_builder.py](file:///root/mzy/seagent1.0-main_asr/src/task_intent_builder.py) (`create_staging`, `publish_staging`) 中严格校验目标路径、符号链接与 PID 拥有权，禁用硬删与强制替换。
