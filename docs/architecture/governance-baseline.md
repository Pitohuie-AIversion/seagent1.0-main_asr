# SEAgent 治理基线 (Governance Baseline)

本文档定义 SEAgent 系统的治理基线（Governance Baseline），明确系统的当前架构事实、系统不变量（Invariants）、期望行为（Expected Behaviors）、已知缺陷（Known Defects），以及防护区域划分（Frozen Core 与 Governance Zone）。

本文档是后续 ModelProfile、TaskPatch、Normalizer 契约、三状态机、Router 和 DialogueManager 治理与重构的最高规范参照。

---

## 1. 当前架构事实 (Current Architecture Facts)

根据仓库当前真实代码，系统的逻辑调用与数据流转路径如下：

```text
Frontend / ASR 接口层
       │
       ▼
   web_backend (多 session 隔离、request_id 产生与传递)
       │
       ▼
 DialogueManager (主控状态机、并发锁与会话快照)
       │
       ▼
  IntentRouter (双通道意图分流与状态识别)
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
> - 规划中的 Robot Gateway、Task Patch 引擎、三状态机及 Dynamic Replanning 尚未在底层逻辑中实现，不得将其写为已存在功能。

---

## 2. 三类治理定义 (Governance Categories)

为了确保治理过程不将现有 Bug 误固化为标准行为，系统将所有行为严格划分为以下三类：

### A. 系统不变量 (INVARIANT)
当前以及未来任何重构都**绝对不得破坏**的系统基本保证与物理性约束。
任何对不变量的违反均视为 P0/P1 严重事故。

1. **INV-01 QUERY_READ_ONLY**：所有 QUERY 路径（知识问答、设备查询、状态查询、普通闲聊）必须保证 `SlotStore.version`、`SlotStore.export_snapshot()`、`task_state` 不发生变动，不创建 `TaskIntent`，不进入发布流程。
2. **INV-02 WRITE_ONLY_MUTATES_TASK**：仅明确的任务 WRITE 流程允许改变 `SlotStore` 槽位状态。常规问答或闲聊绝不产生任务槽位更新。
3. **INV-03 VALID_SLOT_IS_FACT**：`SlotStore.get_task_state()` 仅投影 `status == "valid"` 且 `value != None` 的正式事实；`candidate`、`invalid`、`conflict`、`unresolved` 绝对不得成为正式任务状态。
4. **INV-04 HARD_CANNOT_BE_BYPASSED**：在 `blocked_hard` 阶段，任何确认/忽略/通用肯定性词汇（如“确认”、“继续”、“忽略警告”、“没问题”）均不得绕过硬约束并发布任务。
5. **INV-05 SOFT_ACK_IS_DISTINCT**：在 `blocked_soft` 阶段，用户明确忽略/确认软警告后可继续流程，但该确认记录（`ValidationAcknowledgement`）必须与其触发时的 `task_version`、`validation_version` 和 `validation_fingerprint` 强绑定。
6. **INV-06 PUBLISH_FAIL_CLOSED**：发布链路中任意步骤（ reserve ID、slot transaction、validation、prepare、create_staging、publish_staging ）失败时，系统必须 Fail-Closed：不得标记 `phase="done"`，不得返回发布成功，必须触发完整状态还原。
7. **INV-07 DUPLICATE_CONFIRM_IS_IDEMPOTENT**：任务发布成功（`phase=="done"`）后，再次输入“确认”或“确认发布”，不得生成第二个 `TaskIntent`，不得覆盖已有 `TaskIntent`，不得重新分配任务 ID。
8. **INV-08 SESSION_ISOLATION**：不同 `session_id` 拥有各自独立的 `DialogueManager` 与 `SlotStore` 实例；Session A 的槽位修改绝对不得污染 Session B 的状态。
9. **INV-09 FINAL_NO_OVERWRITE**：当目标 `task_intent_TIxxxx.json` 文件已存在时，系统绝对不得无条件覆盖，也不得因冲突错误删除原有正式任务文件。
10. **INV-10 REQUEST_TRACEABILITY**：`/api/chat` 生成或接收的 `request_id` 必须真实透传至 `DialogueManager.process(message, request_id=request_id)`，保证全链路可追溯。

### B. 期望目标行为 (EXPECTED_BEHAVIOR)
目标架构应该具备、但在当前实现中可能尚未完全实现或需要重构优化的行为。这些行为作为后续 Phase 的设计目标。

- **EXP-01**：Task 发布完成不等于 Conversation 关闭。未来架构允许任务处于 `published` 状态的同时会话保持 `active`，用户可继续提问或创建下一个新任务。
- **EXP-02**：通用知识问答应当允许使用底座模型的推理能力；项目私有事实（如设备参数、油田坐标）必须由项目事实源强约束。
- **EXP-03**：未来 Normalization Failure 不得覆盖已存在的合法旧值（`old valid value`），也不得静默丢弃用户的原始输入（`raw_value`）。

### C. 已知缺陷 (KNOWN_DEFECT)
当前代码中已知存在、需在后续治理阶段专门修复的缺陷。**严禁将 Known Defect 写入 Golden Behavior 或降级测试使其通过**。

- **KD-01**：`src/ui_state_builder.py` 在 `phase in ("done", "rejected")` 时返回 `can_send=False`，导致任务终态与会话交互终态过紧耦合。
- **KD-02**：`src/dialogue_manager.py` 在知识问答 `kb_evidence` 未找到（`found=false`）时直接返回预设拒绝文案，未调用底座模型 General Reasoning。
- **KD-03**：`src/llm_client.py` 在 `apply_chat_template` 中硬编码 `enable_thinking=False`。
- **KD-04**：`src/normalizer.py` 的 `normalize_updates` 初始时复制了原始 `updates` 字典；当 `normalize()` 失败返回 `None` 时，原始未规范化的输入值被保留了下来。

---

## 3. 受保护的核心模块 (Frozen Core)

为了确保治理基线的稳定性，以下模块在 G0 阶段被指定为 **FROZEN CORE**。除非发现阻断基线测试建立的严重 P0/P1 Bug，否则禁止修改其业务实现：

| 模块 | 文件路径 | 冻结原因与安全语义 |
| :--- | :--- | :--- |
| **SlotStore** | `src/slot_store.py` | 状态单源真理 (SSOT)、版本控制与事务回滚的核心逻辑。 |
| **Validator** | `src/validator.py` | 多层级物理/环境/水深 Hard 与 Soft 约束校验核心逻辑。 |
| **TaskIntentBuilder** | `src/task_intent_builder.py` | Staging、`TaskPublishLock` 硬链接与防覆盖写盘逻辑。 |
| **StateInfo** | `src/state_info.py` | 现有遥测事实与 `guard_unit_state_version` 隔离语义。 |
| **TaskIntent Schema** | `schema_version=2` | 现有的 TaskIntent JSON 数据契约结构。 |
| **Persistence Directory** | `staging / snapshot / final / quarantine` | 现有文件系统目录结构与隔离清理语义。 |

---

## 4. 后续治理区域 (Governance Zone)

以下模块属于后续治理与重构的目标范围（G1 ~ G4 阶段），在 G0 阶段**仅进行测量与不变量测试建立，不重写其架构**：

- `src/llm_client.py`（模型 Profile 与能力封装）
- `src/prompts.py`（Prompt 模版与提示工程）
- `src/intent_router.py`（意图路由器与语义匹配）
- `src/dialogue_manager.py`（主控状态机拆解与重构）
- `src/extractor.py`（参数抽取器）
- `src/normalizer.py`（规范化契约）
- `src/knowledge_retriever.py`（知识检索与融合）
- `src/ui_state_builder.py`（UI 状态构建与解耦）
