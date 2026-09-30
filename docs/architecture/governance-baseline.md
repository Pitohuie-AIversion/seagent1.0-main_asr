# SEAgent 治理基线 (Governance Baseline)

本文档定义 SEAgent 系统的治理基线（Governance Baseline），明确系统的当前架构事实、系统不变量（Invariants）、期望行为（Expected Behaviors）、已知缺陷（Known Defects），以及防护区域划分（Frozen Core 与 Governance Zone）。

本文保留 G0.1 治理编号，并按 2026-09-30 的代码更新实现状态。现行行为同时参照[架构总览](overview.md)、[当前设计契约](../current_design_contract.md)和后续 ADR；下文的阶段冻结范围是历史记录，不是永久禁止维护的规则。

---

## 1. 当前架构事实 (Current Architecture Facts)

根据仓库当前真实代码，系统的逻辑调用与数据流转路径如下：

```text
Frontend / ASR 接口层
       │
       ▼
   web_backend (多 session 隔离、request_id 产生与透传)
       │
       ▼
 DialogueManager (主控状态机、并发锁与会话快照)
       │
       ▼
  IntentRouter (InteractionPlan 校验与意图分流)
       │
  ┌────┴──────────────────────────┐
  ▼                               ▼
QUERY 路径 (只读保护)           WRITE 路径 (槽位更新)
  │                               │
  ▼                               ▼
Knowledge / StateInfo           Extractor / OilfieldLinker (候选提取)
(只读快照与不可变断言)                  │
                                  ▼
                              SlotStore ( Single Source of Truth 状态中心)
                                  │
                                  ▼
                              Validator (Hard/Soft 物理与环境约束校验)
                                  │
                                  ▼
                              Confirmation (用户确认/忽略控制)
                                  │
                                  ▼
                              TaskIntentBuilder (TaskPublishLock & os.link 原子落盘)
```

> [!IMPORTANT]
> **架构事实说明**：
> - 当前架构中，`DialogueManager` 充当主控，`IntentRouter` 负责语义分类。
> - 上图简化了查询与写入两条路径；当前计划还包含 `CLARIFY` 和 `CONTROL`。控制操作经阶段与权限处理器执行，不要求经过字段抽取器。
> - `src/slots/task_patch.py`、`src/slots/normalization_contract.py` 和 `src/session/session_state.py` 已实现并接入抽取或会话边界。TaskPatch v2、规范化 v2 和三类状态契约分别受 `task_patch_v2`、`normalization_contract_v2`、`session_state_v2` 控制，仓库配置默认关闭。三类状态契约的存在不表示已替换全部旧状态转移逻辑。
> - 通用 Robot Gateway、Task Graph 和动态重规划仍为规划能力；可选 ROS 2 MCP 桥接器已经实现，负责具体协议的发送与状态跟踪。

---

## 2. 三类治理定义 (Governance Categories)

为了确保治理过程不将现有 Bug 误固化为标准行为，系统将所有行为严格划分为以下三类：

### A. 系统不变量 (INVARIANT)
当前以及未来任何重构都**绝对不得破坏**的系统基本保证与物理性约束。
对 Issue #33 定义的 11 条不变量对齐如下：

1. **INV-01 QUERY_READ_ONLY**：所有 QUERY 路径（知识问答、设备查询、状态查询、普通闲聊）必须保证 `SlotStore.version`、`SlotStore.export_snapshot()`、`task_state` 不发生变动，不创建 `TaskIntent`，不进入发布流程。
2. **INV-02 WRITE_ONLY_MUTATES_TASK**：任务参数新增或修改只允许由授权的 WRITE 流程提交；明确的 CONTROL 可按阶段权限取消草稿、确认警告或发布任务。常规问答或闲聊绝不产生任务槽位更新。
3. **INV-03 VALID_SLOT_IS_FACT**：`SlotStore.get_task_state()` 仅投影 `status == "valid"` 且 `value != None` 的正式事实；`candidate`、`invalid`、`conflict`、`unresolved` 绝对不得成为正式任务状态。
4. **INV-04 INVALID_INPUT_NEVER_OVERWRITES_VALID_FACT**：无法合法规范化或校验失败的新输入，绝不能将非法值作为正式事实覆盖已有合法值（`old valid value`）。`get_task_state()` 中不得出现非法文本。旧 valid 事实在 conflict 状态下安全保留在 Slot.value 中，但暂停进入正式 `get_task_state()` 导出，直到冲突解决或恢复。
5. **INV-05 HARD_CANNOT_BE_BYPASSED**：在 `blocked_hard` 阶段，任何确认/忽略/通用肯定性词汇（如“确认”、“继续”、“忽略警告”、“没问题”）均不得绕过硬约束并发布任务。
6. **INV-06 SOFT_ACK_IS_DISTINCT**：在 `blocked_soft` 阶段，用户明确忽略/确认软警告后可继续流程，但该确认记录（`ValidationAcknowledgement`）必须与其触发时的 `task_version`、`validation_version` 和 `validation_fingerprint` 强绑定。
7. **INV-07 PUBLISH_FAIL_CLOSED**：发布失败不得标记 `phase="done"` 或返回成功。正式文件提交前失败时回滚内存事务；文件已生成但后续持久化确认失败时保留正式文件、编号及待核对意图，进入不确定提交恢复，不能谎称完全回滚或重新发布。参见 [ADR-003](../decisions/ADR-003-task-intent-atomic-persistence.md)。
8. **INV-08 DUPLICATE_CONFIRM_IDEMPOTENT**：任务发布成功（`phase=="done"`）后，再次输入“确认”或“确认发布”，不得生成第二个 `TaskIntent`，不得覆盖已有 `TaskIntent`，不得重新分配任务 ID。
9. **INV-09 SESSION_ISOLATION**：不同 `session_id` 拥有各自独立的 `DialogueManager` 与 `SlotStore` 实例；Session A 的槽位修改绝对不得污染 Session B 的状态。
10. **INV-10 FINAL_NO_OVERWRITE**：当目标 `task_intent_TIxxxx.json` 文件已存在时，系统绝对不得无条件覆盖，也不得因冲突错误删除原有正式任务文件。
11. **INV-11 REQUEST_TRACEABILITY**：`/api/chat` 生成（自动生成）或接收（客户端显式传入）的 `request_id` 必须真实透传至 `DialogueManager.process(message, request_id=request_id)`，保证全链路可追溯。

### 详细 Slot 状态契约 (Slot Status Contract)

| 状态 | `value` | `candidate_value` | `raw_value` | 投影至 `get_task_state()` | 转换条件 / 说明 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **valid** | 规范化正式事实 | `None` | 用户或系统表达 | **包含** | 成功通过规范化与校验的正式槽位事实。 |
| **conflict** | 最近一次 confirmed old valid value | 待决规范化候选 | 真正用户原始文本 | **不包含 (暂停)** | 已有 valid 值但用户提交了非法/未规范化新候选；旧事实不丢，但暂停导出。 |
| **invalid** | `None` | 被拒规范化候选 | 真正用户原始文本 | **不包含** | 槽位原无 valid 值且用户新输入规范化失败。 |
| **conflict (取消恢复)** | 恢复 old valid value | `None` | 原用户表达 | **包含** | 用户对 conflict 槽位执行定向取消（如"取消修改水深"），恢复 valid 状态。 |
| **conflict (合法替换)** | 规范化新值 | `None` | 新用户表达 | **包含** | 用户提交合法新值（如"改成500米"），直接替换并恢复为 valid 状态。 |

### B. 期望目标行为 (EXPECTED_BEHAVIOR)
目标架构应该具备、但在当前实现中可能尚未完全实现或需要重构优化的行为。这些行为作为后续 Phase 的设计目标。

- **EXP-01（已有实现）**：任务发布完成不等于会话关闭。当前 `done` / `rejected` 仍允许普通对话和只读查询；后续 WRITE 按事务创建新草稿，已发布文件不可覆盖。`published` / `active` 是早期目标用语，不是当前 phase 枚举。
- **EXP-02（按查询范围实现）**：通用概念问答可调用底座模型；项目私有事实（如设备参数、油田坐标）必须来自项目事实源。不能把所有 KB miss 都转为模型补全项目事实。

### C. 已知缺陷 (KNOWN_DEFECT)
当前代码中已知存在、需在后续治理阶段专门修复的缺陷。**严禁将 Known Defect 写入 Golden Behavior 或降级测试使其通过**。

- **KD-01（已解决）**：`src/session/ui_state_builder.py` 已将 `done` / `rejected` 任务的任务字段保持为只读，同时允许 `can_send=True` 继续普通对话和只读查询；由 `tests/test_issue_31_ui_state_contract.py` 覆盖。
- **KD-02（原描述已不适用）**：知识查询已移至 `src/handlers/conversation_router.py`。未命中项目证据的通用概念问题可走 `_handle_general_chat()`；具体项目实体、歧义别名和未收录事实仍返回澄清或缺失说明。这是证据边界，不应统一登记为拒答缺陷。
- **KD-03（已提供可选实现）**：`src/llm_client.py` 的 legacy 模式仍使用 `enable_thinking=False`；开启 `model_profiles_v2` 后按 `config/model_profiles.yaml` 的角色配置选择参数，ROUTER / EXTRACTOR 等结构化角色仍强制关闭 thinking。默认关闭特性开关不代表角色配置能力不存在。
- **KD-04 (RESOLVED)**：`src/extraction/normalizer.py` 规范化失败覆盖旧 valid 槽位缺陷已在 Commit `c50a60a` 修复，并通过双路 Normalization Failure 状态契约验证。

---

## 3. 受保护的核心模块 (Frozen Core)

以下模块在历史 G0.1 测量阶段被指定为 **FROZEN CORE**。当时限制修改生产实现以建立基线；后续维护应遵守相应不变量并提供回归证据，不将该阶段限制外推为永久冻结：

| 模块 | 文件路径 | 冻结原因与安全语义 |
| :--- | :--- | :--- |
| **SlotStore** | `src/slots/slot_store.py` | 状态单源真理 (SSOT)、版本控制与事务回滚的核心逻辑。 |
| **Validator** | `src/validation/validator.py` | 多层级物理/环境/水深 Hard 与 Soft 约束校验核心逻辑。 |
| **TaskIntentBuilder** | `src/dispatch/task_intent_builder.py` | Staging、`TaskPublishLock` 硬链接与防覆盖写盘逻辑。 |
| **StateInfo** | `src/state_info.py` | 现有遥测事实与 `guard_unit_state_version` 隔离语义。 |
| **TaskIntent Schema** | `schema_version=2` | 现有的 TaskIntent JSON 数据契约结构。 |
| **Persistence Directory** | `staging / snapshot / final / quarantine` | 现有文件系统目录结构与隔离清理语义。 |

---

## 4. 后续治理区域 (Governance Zone)

以下是 G0.1 为后续 G1 ~ G4 记录的治理范围；多个模块现已完成拆分或提供受开关控制的实现，当前职责以架构总览为准：

- `src/llm_client.py`（模型 Profile 与能力封装）
- `src/extraction/prompts.py`（Prompt 模版与提示工程）
- `src/session/intent_router.py`（意图路由器与语义匹配）
- `src/dialogue_manager.py`（主控状态机拆解与重构）
- `src/extraction/extractor.py`（参数抽取器）
- `src/extraction/normalizer.py`（规范化契约）
- `src/knowledge_retriever.py`（知识检索与融合）
- `src/session/ui_state_builder.py`（UI 状态构建与解耦）
