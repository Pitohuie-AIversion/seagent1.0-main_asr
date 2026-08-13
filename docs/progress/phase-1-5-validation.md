# Phase 1.5 架构验证与问题跟踪报表

本文档以结构化报表形式记录 SEAgent 系统在 Phase 1.5 阶段针对核心架构缺陷的验证情况、关联修改文件、闭环效果以及尚需持续增强的留存事项。

---

## 1. Phase 1.5 核心架构验证报表

| 编号 | 缺陷 / 问题主题 | 修改文件 | 核心修改说明 | 完成效果 | 新发现问题 / 留存风险 | 状态 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **01** | **任务槽位一致性** | [src/slot_store.py](file:///root/mzy/seagent1.0-main_asr/src/slot_store.py)<br>[src/dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py) | 引入 `SlotStore` 作为唯一事实源，统一管理 `Slot` 状态，新增快照与版本自增控制。 | 彻底解决了多字典状态冲突，任务 JSON 导出一致性达到 100%。 | Schema 频繁切换时，特定历史 Candidate 清理需进一步自动化。 | **已合并 main** |
| **02** | **Payload 多值丢失** | [src/extractor.py](file:///root/mzy/seagent1.0-main_asr/src/extractor.py)<br>[tests/test_payload_multivalue.py](file:///root/mzy/seagent1.0-main_asr/tests/test_payload_multivalue.py) | 修改 `Extractor` 的列表型候选处理逻辑，由直接覆盖改为合并追加去重。 | 解决多工具/多载荷（如机械臂+多波束声呐）同时提交时旧值被覆盖问题。 | 极少数非标多语法并列提炼时对置信度加权算法仍需微调。 | **已合并 main** |
| **03** | **QUERY/WRITE 混淆** | [src/intent_router.py](file:///root/mzy/seagent1.0-main_asr/src/intent_router.py)<br>[src/dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py) | 增加 `IntentRouter` 分路，并在 `_handle_non_task_route` 增加只读快照断言。 | 提问、知识查询与状态索取不再污染任务槽位，只读保护断言率 100%。 | 边缘模糊复合句（“修改为500米合适吗？”）的意图置信度有待提升。 | **已合并 main** |
| **04** | **实时机器人状态** | [src/knowledge_retriever.py](file:///root/mzy/seagent1.0-main_asr/src/knowledge_retriever.py)<br>[src/state_info.py](file:///root/mzy/seagent1.0-main_asr/src/state_info.py) | 新增 `status_ref` 解析，强制由 `config/state.yaml` 动态读取遥测数据。 | 实现了物理/海况限制与机器人当前实时健康度的强绑定校验。 | 遥测数据更新超过 1 小时的过期阻断机制在极特殊模拟时钟下需同步扩展。 | **已完成基础闭环，仍需增强** |
| **05** | **知识问答与澄清** | [src/knowledge_retriever.py](file:///root/mzy/seagent1.0-main_asr/src/knowledge_retriever.py)<br>[src/prompts.py](file:///root/mzy/seagent1.0-main_asr/src/prompts.py) | 优化类型化知识检索 (`execute_typed_query`)，增加水深与设备能力不匹配时的直接拒答。 | 知识 QA 命中率与能力询问拒绝提示更加精准 scannable。 | 复杂跨域问答的上下文拼接Token消耗需进一步优化。 | **已合并 main** |
| **06** | **Alias 语义解析** | [src/output_builder.py](file:///root/mzy/seagent1.0-main_asr/src/output_builder.py)<br>[src/extractor.py](file:///root/mzy/seagent1.0-main_asr/src/extractor.py) | 实现 `canonical_exact` -> `alias_exact` -> `llm_semantic` 递进解析与系列/型号/单机别名隔离。 | 解决了用户使用俗称或简称（如“海狮号”、“作业级ROV”）时的精确规范化归一。 | 未录入的极度冷门俗称仍需依赖 LLM 语义解析，响应延时较高。 | **已合并 main** |
| **07** | **TaskIntent 文件发布** | [src/task_intent_builder.py](file:///root/mzy/seagent1.0-main_asr/src/task_intent_builder.py)<br>[src/exceptions.py](file:///root/mzy/seagent1.0-main_asr/src/exceptions.py) | 实现 Staging 暂存区、跨进程排他锁 `TaskPublishLock` 与原子无覆盖提交。 | 解决了并发发布半写入与同名 Intent ID 文件被非法覆盖的严重安全隐患。 | 跨 OS 挂载盘（如 NFS）对 `os.link` 的兼容性需要环境说明。 | **已合并 main** |
| **08** | **Agent 行为测试** | [tests/test_p0_final_closeout.py](file:///root/mzy/seagent1.0-main_asr/tests/test_p0_final_closeout.py)<br>[tests/test_adversarial_p0.py](file:///root/mzy/seagent1.0-main_asr/tests/test_adversarial_p0.py) | 编写多轮对话对抗测试、状态一致性基准测试与完整端到端链路回归用例。 | 建立了自动化 CI 质量门控，拦截了多起意图穿透与状态漂移缺陷。 | 对极长上下文（>20轮）的对抗用例执行时间较长，需在 CI 中分级调度。 | **已合并 main** |

---

## 2. 状态说明与分级定义

在跟踪系统演进时，各问题状态定义如下：

1. **已合并 main (Merged into GitHub main)**：
   - 核心代码变动与自动化测试已完全通过 GitHub CI 校验并合并至 `origin/main` 主分支。

2. **仅在本地验证但尚未提交 (Validated locally but not yet committed)**：
   - 在本地沙箱环境完成了功能验证与测试套件通过，但尚未通过 Pull Request 合并至主分支。

3. **已完成基础闭环但仍需增强 (Basic closeout complete, enhancement ongoing)**：
   - **注意：禁止将此类状态标注为“所有问题彻底解决”**。表示已具备满足当前版本的核心防护能力，但在更复杂海况、边缘算力或极长上下文场景下仍存在优化空间。

---

## 2. Phase 2 核心变更验证报表

| 编号 | 变更主题 | 关联 ADR / 主要修改文件 | 核心实现说明 | 完成效果 | 留存风险 | 状态 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **P2-01** | **LLM 语义权威路由（第一阶段）** | ADR-005<br>[src/interaction_plan.py](file:///root/mzy/seagent1.0-main_asr/src/interaction_plan.py)<br>[src/intent_router.py](file:///root/mzy/seagent1.0-main_asr/src/intent_router.py) | 引入 `InteractionPlan`，以 `operation` 字段（READ/WRITE/CONTROL/CLARIFY）为唯一路由权威；移除业务关键词证据否决门；低置信度 WRITE/CONTROL 降级 CLARIFY。 | 自然表达不再因措辞缺少关键词被错误拒绝；模型 READ 在任务草稿上下文中保持 QUERY；普通对话路径恢复稳定。 | 离线 Mock 无法完整伪装自然语言理解能力；跨进程会话恢复暂不处理。 | **已合并 main** |
| **P2-02** | **LLM 语义权威路由（第二阶段）** | ADR-005 §第二阶段<br>[src/dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py)<br>[src/visible_selection_provenance.py](file:///root/mzy/seagent1.0-main_asr/src/visible_selection_provenance.py) | 枚举消歧优先使用模型语义；可见编号候选校验（`VisibleSelectionProvenance`）阻止基于隐藏顺序的误写；Grounded 字段推荐基于已确认任务条件而非列表默认。 | 模糊枚举表达可经模型映射到合法候选；域外结果仍被拒绝；可见编号跨轮可选；多候选无法可靠推荐时不默认选列表第一项。 | 可见来源校验仅覆盖紧邻 assistant 回复；历史长上下文场景下旧编号授权边界需持续观察。 | **已合并 main** |
| **P2-03** | **约束驱动机器人候选自动收敛** | ADR-008<br>[src/knowledge_retriever.py](file:///root/mzy/seagent1.0-main_asr/src/knowledge_retriever.py)<br>[src/validator.py](file:///root/mzy/seagent1.0-main_asr/src/validator.py)<br>[src/slot_store.py](file:///root/mzy/seagent1.0-main_asr/src/slot_store.py) | `get_feasible_robot_selection_domain()` 成为唯一权威候选入口；water_depth 过滤 Variant、payload 过滤型号、即时任务运行状态过滤 Unit；0/1/多三段决策；Validator 与 Snapshot restore 复用同一四级关系校验。 | 系统条件充分时自动唯一锁定机器人，减少无效用户交互；零可行候选 fail-closed；用户显式选择保留并由 Validator 阻断，不误呈现为"设备信息缺失"。 | `burial_depth`、航程、续航过滤暂未接入（当前 schema 无对应任务字段）；未来任务不使用当前忙闲状态过滤，仍在发布前重检。 | **已合并 main** |
| **P2-04** | **快照恢复内存原子性** | ADR-007<br>[src/dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py) | `load_snapshot()` 采用隔离候选管理器方案：所有字段恢复与枚举校验在候选对象完成后，才一次性提交到当前管理器；`phase` 必须属于 `VALID_PHASES`，不依赖特性开关。 | 非法 phase / mode 的恢复请求不再污染运行中会话；半恢复状态彻底消除。 | 极端晚期故障（恢复后立即崩溃）可能造成每日 Intent ID 跳号，不影响安全性。 | **已合并 main** |
| **P2-05** | **Validation 不 fallback 到 Clarification** | commit 8254b37<br>[src/validator.py](file:///root/mzy/seagent1.0-main_asr/src/validator.py)<br>[src/dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py) | 禁用约束失败退化为 CLARIFY 的兜底路径；`blocked_hard` / `blocked_soft` 直接返回明确错误原因，要求用户修正参数，而非以追问掩盖约束问题。 | 约束阻断语义清晰；用户不再收到含糊的"请提供更多信息"提示；约束 → 修正 → 发布闭环路径可测。 | 部分边界场景需补充专项测试：同时触发 hard + soft 约束时的优先级展示顺序。 | **已合并 main** |
| **P2-06** | **Slot Schema 切换过滤增强** | commit 8254b37<br>[src/slot_store.py](file:///root/mzy/seagent1.0-main_asr/src/slot_store.py) | 任务类型切换后，不属于新 schema 的旧 candidate / conflict / invalid 机器人选择自动失效；同一任务内的冲突审计状态仍完整保留。 | 跨任务类型状态污染问题消除；任务语境明确后旧机器人候选不再干扰新任务收集。 | 极复杂多类型连续切换场景下的测试覆盖尚需补充。 | **已合并 main** |
| **P2-07** | **InteractionPlan + 测试基础设施** | commit 07fc1de<br>[src/interaction_plan.py](file:///root/mzy/seagent1.0-main_asr/src/interaction_plan.py)<br>[tests/test_interaction_plan.py](file:///root/mzy/seagent1.0-main_asr/tests/test_interaction_plan.py) | `InteractionPlan` schema 接入核心逻辑；测试套件强制要求每轮产出结构化 turn plan；pending actions 积累测试场景支持。 | 每轮 LLM 决策可被测试断言，测试可见性大幅提升。 | 离线 Mock InteractionPlan 无法完整代表在线 LLM 真实语义判断，需持续补充 Mock 覆盖边界。 | **已合并 main** |
| **P2-08** | **坐标解析确定性** | commit 0b50d2c<br>[src/coord_parser.py](file:///root/mzy/seagent1.0-main_asr/src/coord_parser.py) | 重构坐标解析，保证各输入格式产生唯一确定性输出；消除格式差异引发的坐标漂移。 | 坐标提取结果与输入格式无关，测试可严格断言。 | 极罕见非标坐标表达（如度分秒混写 + 中文单位）仍需扩展测试。 | **已合并 main** |

---

## 3. 已知缺陷（未关闭，截至 2026-08-13）

参见 [docs/architecture/governance-baseline.md](file:///root/mzy/seagent1.0-main_asr/docs/architecture/governance-baseline.md) § 已知缺陷章节：

| 缺陷 ID | 简述 | 优先级 |
| :--- | :--- | :--- |
| **KD-01** | `ui_state_builder.py` 在 `done` / `rejected` 时返回 `can_send=False`，任务终态与会话交互终态过紧耦合。 | P2 |
| **KD-02** | `dialogue_manager.py` 知识问答 `kb_evidence` 未命中时直接返回预设文案，未调用底座模型 General Reasoning。 | P2 |
| **KD-03** | `llm_client.py` 在 `apply_chat_template` 中硬编码 `enable_thinking=False`。 | P3 |
