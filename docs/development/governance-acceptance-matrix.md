# SEAgent Governance Acceptance Matrix

本文档记录 SEAgent 治理基线的验收矩阵（Acceptance Matrix）。包含各类输入场景的期望效果、槽位变动约束、发布结果、当前验证状态（`VERIFIED` / `KNOWN_DEFECT` / `NOT_YET_VERIFIED`）及对应自动化测试用例映射。

---

## 验收矩阵 (Governance Matrix)

| ID | Category | Input/Scenario | Expected Effect | Slot Mutation | Publish | Current Status | Test |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **GAM-01** | `general_chat` | "你好" / "谢谢" / "你能做什么" | 返回礼貌引导语，不进入任务槽位收集 | 无变动 | 否 | `VERIFIED` | `test_governance_invariants.py::test_inv02_write_only_mutates_task` |
| **GAM-02** | `general_knowledge` | "什么是 DVL？" / "AUV 和 ROV 有什么区别？" | 返回专业水下知识解答，只读保护验证通过 | 无变动 (version不变) | 否 | `VERIFIED` | `test_governance_invariants.py::test_inv01_query_read_only` |
| **GAM-03** | `project_fact` | "支持的最大作业水深是多少？" / "当前可用机器人列表" | 结合知识库/实体数据准确回答，无任务侧干扰 | 无变动 | 否 | `VERIFIED` | `test_governance_invariants.py::test_inv01_query_read_only` |
| **GAM-04** | `task_create` | "创建一个管缆巡检任务" | 识别任务类型，初始化 required slots，进入 `collecting` 阶段 | 新增 `task_type` 与 `task_type_key` | 否 | `VERIFIED` | `test_governance_invariants.py::test_inv03_valid_slot_is_fact` |
| **GAM-05** | `task_create` | "水深300米" | 抽取 `water_depth` 并规范化为 `300.0`，状态为 `valid` | 增加 `water_depth` 槽位 | 否 | `VERIFIED` | `test_governance_invariants.py::test_inv03_valid_slot_is_fact` |
| **GAM-06** | `task_modify` | "水深改成500米" | 覆盖更新 `water_depth` 槽位值为 `500.0`，版本递增 | 更新 `water_depth` | 否 | `VERIFIED` | `test_governance_invariants.py::test_inv03_valid_slot_is_fact` |
| **GAM-07** | `soft_warning` | 触发软警告后输入 "忽略警告" / "继续" | 生成与快照绑定的 `ValidationAcknowledgement`，解除阻塞并推进流程 | 记录 `validation_acknowledgements` | 补满后发布 | `VERIFIED` | `test_governance_invariants.py::test_inv05_soft_ack_is_distinct` |
| **GAM-08** | `hard_constraint` | `blocked_hard` 阶段输入 "确认" / "忽略警告" / "没问题" | 拒绝绕过硬约束，保持 `blocked_hard` 阶段并提示修复 | 无合法更新 | 否 | `VERIFIED` | `test_governance_invariants.py::test_inv04_hard_cannot_be_bypassed` |
| **GAM-09** | `confirmation` | 所有必需槽位填满后输入 "确认发布" | 触发 `TaskIntentBuilder` 原子写盘，生成 `task_intent_TIxxxx.json` | 终态锁定 | 是 | `VERIFIED` | `test_governance_invariants.py::test_inv06_publish_fail_closed` |
| **GAM-10** | `confirmation` | 任务已在 `done` 阶段再次输入 "确认发布" | 提示任务已发布，幂等响应，无二次建单 | 无变动 | 否 | `VERIFIED` | `test_governance_invariants.py::test_inv07_duplicate_confirm_is_idempotent` |
| **GAM-11** | `emergency_control` | 已发布任务后输入 "立即停止当前任务" | 捕获紧急控制意图，更新 `control_state="stop_requested"` | 记录 `control_state` | 否 | `VERIFIED` | `test_governance_invariants.py::test_emergency_control_routing` |
| **GAM-12** | `emergency_control` | "如果停止当前任务会怎样？" | 识别为只读咨询查询 (Read-only query)，不触发控制动作 | 无变动 | 否 | `VERIFIED` | `test_governance_invariants.py::test_query_control_distinction` |
| **GAM-13** | `emergency_control` | "不要停止当前任务" | 识别为否定句，不得执行 stop/cancel 动做 | 无变动 | 否 | `VERIFIED` | `test_governance_invariants.py::test_negative_control_request` |
| **GAM-14** | `ASR` | 包含术语误读的语音转写文本 | 经过 `asr_normalizer` 纠错后送入统一 Dialogue 流水线 | 按纠错后文本处理 | 视意图定 | `VERIFIED` | `test_asr_normalizer.py` |
| **GAM-15** | `concurrency` | Session A 修改槽位，Session B 查询/修改 | 证明两个 Session 的 `SlotStore` 与 `phase` 物理隔离 | Session A 独占更新 | Session A 独立 | `VERIFIED` | `test_governance_invariants.py::test_inv08_session_isolation` |
| **GAM-16** | `persistence` | 目标 `task_intent_TIxxxx.json` 已存在时尝试发布 | 抛出冲突并拒绝对现有 final 文件进行无条件覆盖 | 无变动 (回滚) | 否 (Rollback) | `VERIFIED` | `test_governance_invariants.py::test_inv09_final_no_overwrite` |
| **GAM-17** | `persistence` | 发布中途底层 IO 异常 | Fail-closed 保障，状态与内存还原至发布前 | 还原原 Snapshot | 否 | `VERIFIED` | `test_governance_invariants.py::test_inv06_publish_fail_closed` |
| **GAM-18** | `traceability` | 客户端传入/未传入 `request_id` | HTTP 请求生成的 `request_id` 100% 传达至 `mgr.process()` | 审计记录携带 | 视流程定 | `VERIFIED` | `test_governance_invariants.py::test_inv10_request_traceability` |
| **KD-01** | `ui_state` | 任务进入 `done` / `rejected` 阶段 | 当前 `ui_state_builder` 设置 `can_send=False` 导致输入框禁用 | 终态锁定 | 否 | `KNOWN_DEFECT` | `test_ui_state_contract.py` |
| **KD-02** | `knowledge_qa` | KB 知识库中查无此项（`found=false`） | 当前未触发底层 LLM 独立 Reasoning 补充回答，直接弹失败文案 | 无变动 | 否 | `KNOWN_DEFECT` | `test_intent_routing.py` |
| **KD-03** | `llm_client` | 任何 LLM 请求生成 | `apply_chat_template` 被硬编码 `enable_thinking=False` | 无变动 | 否 | `KNOWN_DEFECT` | `test_llm_client.py` |
| **KD-04** | `normalizer` | 用户输入无法被映射为枚举或标准数值 | `normalize_updates` 保留原始输入字典，未能擦除或重置该槽位 | 保留原始 `raw` 提交 | 否 | `KNOWN_DEFECT` | `test_normalizer.py` |
