# SEAgent 治理验收矩阵 (Governance Acceptance Matrix)

本文档跟踪 SEAgent 系统治理各场景的行为特征、不变量与缺陷判定、当前验证状态（`VERIFIED` / `KNOWN_DEFECT` / `NOT_YET_VERIFIED`），以及对应的真实测试用例位置。

2026-09-30 按代码、测试断言与[治理基线](../architecture/governance-baseline.md)复核。`VERIFIED` 仅表示下列测试覆盖的程序行为已验证；这些测试使用脚本化计划或模型桩，不证明真实模型可识别所有自然表达。`NOT_YET_VERIFIED` 表示该行完整场景证据不足，不自动等于生产缺陷。

---

## 1. 验收与追溯矩阵 (Acceptance Matrix Table)

| ID | Category | Scenario | Expected Effect | Slot Mutation | Publish | Classification | Current Status | Test |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **AM-01** | `general_chat` | 问候与闲聊 ("你好") | GENERAL_CHAT 不调用抽取器，任务状态保持只读 (INV-01) | 无 (`SlotStore.version` 不变) | False | INVARIANT | VERIFIED | `tests/test_slot_consistency.py::SlotConsistencyTest::test_10_general_chat_leaves_slot_store_untouched` |
| **AM-02** | `general_knowledge` | 只读概念问答 ("什么是 DVL？") | 状态只读隔离，不产生槽位变动，不进入发布 (INV-01) | 无 (`SlotStore.version` 不变) | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv01_query_read_only` |
| **AM-02B** | `general_knowledge` | 通用领域问题进入 General Reasoning (EXP-02) | 通用概念问题使用 GENERAL_REASONING 角色，不注入无关项目 KB；不推广为项目事实缺失时由模型补全 | 无 | False | EXPECTED_BEHAVIOR | VERIFIED | `tests/test_semantic_freedom_boundaries.py::test_general_domain_question_skips_project_kb_dump`；`tests/test_semantic_freedom_boundaries.py::test_general_chat_uses_general_reasoning_role` |
| **AM-03** | `project_fact` | 任务或状态查询 ("当前任务进度？") | 返回当前阶段与槽位信息，同时完整保持任务状态只读 (INV-01) | 无 (`SlotStore.version` 不变) | False | INVARIANT | NOT_YET_VERIFIED | 原引用只测试 DVL 概念问答，未直接证明本行完整状态查询契约 |
| **AM-04** | `task_create` | 明确任务创建 ("创建一个管缆巡检任务") | 路由至 task_collection，初始化任务类型 (INV-02) | `task_type=管缆巡检`, `task_type_key=pipeline_inspection` | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv02_real_write_path_task_create` |
| **AM-05** | `task_create` | 填充具体参数 ("水深300米") | 走真实 DM WRITE 链路更新规范化水深 300.0 (INV-02 / INV-03) | `water_depth=300.0` (status=valid) | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv02_real_write_path_water_depth` |
| **AM-06** | `task_modify` | 修改参数 ("水深改成500米") | 走真实 DM WRITE 链路覆盖更新为 500.0 (INV-02 / INV-03) | `water_depth=500.0` (version 增加) | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_real_task_modify_flow` |
| **AM-07** | `task_modify` | 非法修改 ("水深改成差不多很深") | 非法输入绝对不作为正式事实覆盖旧 valid 300.0 (INV-04) | `get_task_state` 排除非法值；`raw_value` 保存输入 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv04_invalid_input_never_overwrites_valid_fact` |
| **AM-08** | `hard_constraint` | 硬约束触发时输入确认/忽略 ("没问题") | blocked_hard 下拒绝绕过发布 (INV-05) | 无 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv05_hard_cannot_be_bypassed` |
| **AM-09** | `soft_warning` | 软告警显式忽略 ("忽略警告") | blocked_soft 下忽略并生成绑定指纹的 ValidationAcknowledgement (INV-06) | 生成 ValidationAcknowledgement | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv06_soft_ack_is_distinct` |
| **AM-10** | `confirmation` | 确认发布成功路径 ("确认发布") | prepare -> create_staging -> publish_staging 成功生成 final 文件 (INV-07/08) | 锁定正式槽位状态 | True | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_publish_success_path` |
| **AM-11** | `persistence` | create_staging 抛出持久化异常 | Fail-Closed：异常透传，phase 保持 confirming，final_result 为空 (INV-07) | 本用例未断言完整快照等价 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv07_publish_fail_closed` |
| **AM-12** | `confirmation` | 终态后重复确认 ("确认发布") | phase==done 时幂等响应，无二次写盘 (INV-08) | 无 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv08_duplicate_confirm_is_idempotent` |
| **AM-13** | `session_isolation` | 两个 Session 依次操作 | 更新 Session A 不改变 Session B 的 SlotStore (INV-09)；此用例不是并发压力测试 | 各自独立 SlotStore | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv09_session_isolation` |
| **AM-14** | `persistence` | 目标 Final 文件冲突 | 拒绝覆盖已有同名 final 文件并抛出 IntentIdConflict (INV-10) | 无；原文件内容保持不变 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv10_final_no_overwrite` |
| **AM-15** | `traceability` | /api/chat 透传显式 request_id | 客户端传入的 request_id 全链路透传至 DialogueManager.process (INV-11 Path A) | 无 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv11_request_traceability_explicit` |
| **AM-16** | `traceability` | /api/chat 自动生成 request_id | 客户端未传 request_id 时自动生成非空 req_xxx 并透传 (INV-11 Path B) | 无 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv11_request_traceability_auto_generated` |
| **AM-17** | `emergency_control` | 已发布任务收到 stop CONTROL 计划 | 脚本化 CONTROL Plan 进入处理器并记录 stop；不证明真实模型语义识别或机器人已停止 (INV-01/02) | `control_state=stop_requested` | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_emergency_control_routing` |
| **AM-18** | `emergency_control` | 否定式控制指令 ("不要停止当前任务") | 绝对不触发 stop 控制动作 (INV-01/02) | `control_state=idle` | False | INVARIANT | NOT_YET_VERIFIED | 待补充直接回归测试 |
| **AM-19** | `emergency_control` | 询问式控制 ("如果停止当前任务会怎样？") | 只读咨询，不触发控制动作 (INV-01/02) | `control_state=idle` | False | INVARIANT | NOT_YET_VERIFIED | 待补充直接回归测试 |
| **AM-20** | `terminal_ui` | 终态任务的 UI 操作权限 | `done` / `rejected` 的任务字段只读，`can_send=True`；done 切至知识问答仍只读 | 无 | False | INVARIANT | VERIFIED | `tests/test_issue_31_ui_state_contract.py::TestUIStateContract::test_actions_done_phase`；`tests/test_issue_31_ui_state_contract.py::TestUIStateContract::test_actions_rejected_phase`；`tests/test_issue_31_ui_state_contract.py::TestUIStateContract::test_done_phase_with_knowledge_qa_preserves_readonly` |
| **AM-21** | `knowledge_boundary` | 原 KD-02：按查询范围处理 KB miss | 旧“所有 miss 都拒答”描述已失效。通用概念可进入模型问答；缺失的项目实体事实仍须澄清或说明缺失 | 无 | False | EXPECTED_BEHAVIOR | NOT_YET_VERIFIED | 代码见 `src/handlers/conversation_router.py::_handle_knowledge_query`；完整 miss 分支组合尚未在本矩阵建立直接验收证据 |
| **AM-22** | `model_configuration` | 原 KD-03：legacy 与可选 Profile 的 thinking 配置 | legacy 关闭 thinking；`model_profiles_v2` 开启后按角色配置，ROUTER / EXTRACTOR 仍关闭，GENERAL_REASONING 可开启 | 无 | False | EXPECTED_BEHAVIOR | VERIFIED | `tests/test_model_profiles.py::TestLegacyModeBehaviors::test_legacy_generate_text_keeps_thinking_false`；`tests/test_model_profiles.py::TestProfileModeBehaviors::test_general_reasoning_profile_can_enable_thinking`；同类 `test_router_profile_applied` / `test_extractor_profile_applied` |
| **AM-23** | `normalization_failure` | Normalization 失败保留旧 valid 值 (INV-04) | 规范化失败时保留旧 valid 值，保存候选并置 conflict/invalid (INV-04) | 保存旧 valid 值与冲突候选 | False | INVARIANT | VERIFIED | `tests/test_normalization_failure_contract.py::TestNormalizationFailureContract::test_conflict_preserves_old_value_but_suspends_task_projection` |

---

## 2. 规则说明

1. **Classification 字段限定**：只能使用 `INVARIANT`、`EXPECTED_BEHAVIOR` 或 `KNOWN_DEFECT`。
2. **Current Status 字段限定**：只能使用 `VERIFIED`、`KNOWN_DEFECT` 或 `NOT_YET_VERIFIED`。
3. **VERIFIED 的判定规则**：存在直接覆盖该行断言的自动化测试，并有对应执行结果。只有测试文件存在或名称相近不足以标记 VERIFIED；场景文字不得扩大测试实际断言。测试用例名称格式为 `<test_file>.py::TestClass::test_method` 或 `<test_file>.py::test_function`。
4. **历史缺陷编号**：KD 编号保留用于追溯。旧描述不适用或已提供实现后，应同步 Classification、现状和验证范围，不能继续把旧结论登记为当前已知缺陷。

## 3. 本次证据复核

2026-09-30 执行以下已有测试：

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 python -m pytest -q \
  tests/test_governance_invariants.py \
  tests/test_normalization_failure_contract.py \
  tests/test_issue_31_ui_state_contract.py \
  tests/test_model_profiles.py \
  tests/test_semantic_freedom_boundaries.py \
  tests/test_slot_consistency.py::SlotConsistencyTest::test_10_general_chat_leaves_slot_store_untouched
```

结果：121 passed、2 subtests passed，0 failed、0 skipped，15.45 秒。另有 1 个 `Unknown config option: asyncio_mode` 警告；当前 Python 环境未加载相应异步插件，本组同步测试正常结束。本次未重跑全仓、真实模型、ASR 或机器人硬件验收。
