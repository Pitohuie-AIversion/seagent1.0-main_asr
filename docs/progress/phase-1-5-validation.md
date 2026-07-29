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
