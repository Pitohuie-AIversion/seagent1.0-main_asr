# SEAgent 治理验收矩阵 (Governance Acceptance Matrix)

本文档跟踪 SEAgent 系统治理各场景的行为特征、不变量与缺陷判定、当前验证状态（`VERIFIED` / `KNOWN_DEFECT` / `NOT_YET_VERIFIED`），以及对应的真实测试用例位置。

---

## 1. 验收与追溯矩阵 (Acceptance Matrix Table)

| ID | Category | Scenario | Expected Effect | Slot Mutation | Publish | Classification | Current Status | Test |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **AM-01** | `general_chat` | 问候与闲聊 ("你好") | 返回问候引导语，保持 Session 只读 (INV-01) | 无 (`SlotStore.version` 不变) | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv01_query_read_only` |
| **AM-02** | `general_knowledge` | 通用知识问答 ("什么是 DVL？") | 期望调用 General Reasoning 解答；状态只读隔离 (INV-01 / EXP-02) | 无 (`SlotStore.version` 不变) | False | EXPECTED_BEHAVIOR | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv01_query_read_only` |
| **AM-03** | `project_fact` | 任务或状态查询 ("当前任务进度？") | 返回当前阶段与槽位信息 (INV-01) | 无 (`SlotStore.version` 不变) | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv01_query_read_only` |
| **AM-04** | `task_create` | 明确任务创建 ("创建一个管缆巡检任务") | 路由至 task_collection，初始化任务类型 (INV-02) | `task_type=管缆巡检`, `task_type_key=pipeline_inspection` | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv02_real_write_path_task_create` |
| **AM-05** | `task_create` | 填充具体参数 ("水深300米") | 走真实 DM WRITE 链路更新规范化水深 300.0 (INV-02 / INV-03) | `water_depth=300.0` (status=valid) | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv02_real_write_path_water_depth` |
| **AM-06** | `task_modify` | 修改参数 ("水深改成500米") | 走真实 DM WRITE 链路覆盖更新为 500.0 (INV-02 / INV-03) | `water_depth=500.0` (version 增加) | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_real_task_modify_flow` |
| **AM-07** | `task_modify` | 非法修改 ("水深改成差不多很深") | 非法输入绝对不作为正式事实覆盖旧 valid 300.0 (INV-04) | `get_task_state` 排除非法值；`raw_value` 保存输入 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv04_invalid_input_never_overwrites_valid_fact` |
| **AM-08** | `hard_constraint` | 硬约束触发时输入确认/忽略 ("没问题") | blocked_hard 下拒绝绕过发布 (INV-05) | 无 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv05_hard_cannot_be_bypassed` |
| **AM-09** | `soft_warning` | 软告警显式忽略 ("忽略警告") | blocked_soft 下忽略并生成绑定指纹的 ValidationAcknowledgement (INV-06) | 生成 ValidationAcknowledgement | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv06_soft_ack_is_distinct` |
| **AM-10** | `confirmation` | 确认发布成功路径 ("确认发布") | prepare -> create_staging -> publish_staging 成功生成 final 文件 (INV-07/08) | 锁定正式槽位状态 | True | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_publish_success_path` |
| **AM-11** | `persistence` | 发布中写盘/暂存失败 | Fail-Closed：完整还原 Snapshot 且 phase != done (INV-07) | 完整还原快照 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv07_publish_fail_closed` |
| **AM-12** | `confirmation` | 终态后重复确认 ("确认发布") | phase==done 时幂等响应，无二次写盘 (INV-08) | 无 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv08_duplicate_confirm_is_idempotent` |
| **AM-13** | `concurrency` | 多 Session 并发操作 | Session A 与 Session B 彻底物理隔离 (INV-09) | 各自更新独立 SlotStore | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv09_session_isolation` |
| **AM-14** | `persistence` | 目标 Final 文件冲突 | 拒绝覆盖已有同名 final 文件并抛出 IntentIdConflict (INV-10) | 还原原文件状态 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv10_final_no_overwrite` |
| **AM-15** | `traceability` | /api/chat 透传显式 request_id | 客户端传入的 request_id 全链路透传至 DialogueManager.process (INV-11 Path A) | 无 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv11_request_traceability_explicit` |
| **AM-16** | `traceability` | /api/chat 自动生成 request_id | 客户端未传 request_id 时自动生成非空 req_xxx 并透传 (INV-11 Path B) | 无 | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_inv11_request_traceability_auto_generated` |
| **AM-17** | `emergency_control` | 紧急停止指令 ("立即停止当前任务") | 正向识别控制指令为 stop (INV-01/02) | `control_state=stop_requested` | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_emergency_control_routing` |
| **AM-18** | `emergency_control` | 否定式控制指令 ("不要停止当前任务") | 绝对不触发 stop 控制动作 (INV-01/02) | `control_state=idle` | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_negative_control_request` |
| **AM-19** | `emergency_control` | 询问式控制 ("如果停止当前任务会怎样？") | 只读咨询，不触发控制动作 (INV-01/02) | `control_state=idle` | False | INVARIANT | VERIFIED | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_query_control_distinction` |
| **AM-20** | `known_defect` | 终态下 UIcan_send 锁定 (KD-01) | ui_state 硬编码 can_send=False 导致无法继续交互 | 无 | False | KNOWN_DEFECT | KNOWN_DEFECT | `tests/test_ui_state_contract.py` |
| **AM-21** | `known_defect` | 知识库未命中缺少 LLM 兜底 (KD-02) | found=false 时直接预设拒绝，未调用 Reasoning 兜底 | 无 | False | KNOWN_DEFECT | KNOWN_DEFECT | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_golden_corpus_executable_cases` |
| **AM-22** | `known_defect` | LLM 模板硬编码 enable_thinking=False (KD-03) | 禁用思维链推理模式 | 无 | False | KNOWN_DEFECT | KNOWN_DEFECT | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_golden_corpus_executable_cases` |
| **AM-23** | `known_defect` | Normalization 失败时保留原始输入 (KD-04) | 归一化失败返回 None 时保留原始未处理输入 | 复制原始 updates | False | KNOWN_DEFECT | KNOWN_DEFECT | `tests/test_governance_invariants.py::TestGovernanceInvariants::test_golden_corpus_executable_cases` |

---

## 2. 规则说明

1. **Classification 字段限定**：只能使用 `INVARIANT`、`EXPECTED_BEHAVIOR` 或 `KNOWN_DEFECT`。
2. **Current Status 字段限定**：只能使用 `VERIFIED`、`KNOWN_DEFECT` 或 `NOT_YET_VERIFIED`。
3. **VERIFIED 的判定规则**：必须存在直接测试该场景的自动化测试用例，并且测试用例绝对存在于代码库中。测试用例名称格式必须为 `tests/test_file.py::TestClass::test_method`。
