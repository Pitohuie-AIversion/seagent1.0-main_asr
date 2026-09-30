# 工作区文档审查清单

| 项目 | 范围与结果 |
| --- | --- |
| 核对日期 | 2026-09-30 |
| 清单基线 | 修订开始时纳入 Git 的 64 个 Markdown、1 个 PDF，共 65 份 |
| 范围 | 根目录、docs、.agents、两个 MCP 子项目、src、scripts、scratch、artifacts、record、test_logs、人工测试文档 |
| 审查方式 | 阅读文档，核对关键代码/配置、历史证据存续、相对链接与测试引用；补查时间调研的一手来源 |
| 本轮执行 | 海流组合 90 passed / 1 skipped；ROS 协议及对话集成 11 passed；治理矩阵 121 passed / 2 subtests passed；后续时间模块 155 passed / 4 subtests passed；文档示例 7 项固定检查通过 |
| 未执行 | 全仓回归、真实模型/ASR、真实海流下载、ROS实机、历史性能实验 |

## 1. 完成范围与结论

65 份基线文档均已纳入清单。当前说明中已确认的接口、参数和能力边界错误已修订；历史报告保留原始实验范围，并更正测试口径和证据状态。这里的“已核对”指文档与关键实现或记录的对照，不代表逐句形式化证明，也不代表全系统验收。

仍不能宣称所有资料的事实与实验都已重新验证：时间调研已补充关键主张的一手来源，但旧引用的原 URL 映射未恢复；部分历史日志已缺失，真实模型和现场实验未重跑。下面逐文件保留这种差别，不以链接有效替代内容核验。

依赖清单（requirements）、生成的 egg-info、原始 JSON/XML 日志和源代码注释不作为这 65 份叙述文档计数；必要时作为证据核对。新增 3 份 Markdown 和 3 份 PDF 后，共 71 份叙述文档；基线清单保持 65 行，便于追踪。业务代码修改不属于本轮文档修订。

## 2. 关键修订及实现入口

| 文档问题 | 当前说明依据 |
| --- | --- |
| 计划能力写成已实现 | [架构总览](architecture/overview.md)与[ADR-008](decisions/ADR-008-constraint-aware-robot-selection.md)：候选域完整型号过滤尚未接入 |
| 路由绝对权威描述与代码不符 | [设计契约](current_design_contract.md)与[ADR-005](decisions/ADR-005-llm-semantic-authority.md)：记录局部规则修正 |
| 归档、发送和完成混淆 | [执行下发契约](execution_dispatch_contract.md)与[协议报告](../mcp/ros-mcp/docs/adapter_specification_report.md)：分别说明提交不确定、SENT及机器人终态 |
| 海流设计承诺超出现有实现 | [海流实现状态](../mcp/operation-time-window/DESIGN_SPEC.md)：主应用未接入、线程不可强制终止、缓存与数据校验差距 |
| 维护指南参数过时 | [.agents说明](../.agents/README.md)与维护技能：60分钟窗口、output_schema、逐条ASR阈值 |
| 历史数字被当作当前验收 | 历史报告增加日期/证据边界；性能报告区分E2E均摊耗时与纯解码TPOT |
| 时间标准、工具语言能力及厂商能力描述过满 | [时间调研来源核对](../artifacts/temporal-research-source-review.md)：限定 RFC/厂商接口的适用范围，修正中文支持与无 offset 时间示例，并对照本地 vLLM 和时间解析实现 |

## 3. 逐文件覆盖

“实现对照”核对关键接口与行为；“维护规则”审读流程与引用；“历史核界”核对背景、统计口径和现存证据，未重做历史实验；“关键外源复核”只认证另列的一手主张，保留旧引用未恢复的缺口。核对后保留表示本轮未发现需要修改的相应说明，不表示文件未经检查。

| 文件 | 审查层级 | 本轮处理 | 依据或限制 |
| --- | --- | --- |
| [.agents/AGENTS.md](../.agents/AGENTS.md) | 维护规则 | 已修订 | 修正执行适配、窗口和发布语义，保留工作流程约定。 |
| [.agents/README.md](../.agents/README.md) | 维护规则 | 已修订 | 修正执行适配、窗口和发布语义，保留工作流程约定。 |
| [.agents/rules/seagentrules.md](../.agents/rules/seagentrules.md) | 维护规则 | 已修订 | 修正执行适配、窗口和发布语义，保留工作流程约定。 |
| [.agents/skills/constraint_validation_maintenance/SKILL.md](../.agents/skills/constraint_validation_maintenance/SKILL.md) | 维护规则 | 已修订 | 核对维护入口与关键代码；保留既有技能名。 |
| [.agents/skills/system_operations/SKILL.md](../.agents/skills/system_operations/SKILL.md) | 维护规则 | 已修订 | 核对维护入口与关键代码；保留既有技能名。 |
| [.agents/skills/task_collection_maintenance/SKILL.md](../.agents/skills/task_collection_maintenance/SKILL.md) | 维护规则 | 已修订 | 核对维护入口与关键代码；保留既有技能名。 |
| [.agents/skills/technical-report/SKILL.md](../.agents/skills/technical-report/SKILL.md) | 维护规则 | 核对后保留 | 全文审读报告规则；本轮按证据分层并提供MD/PDF。 |
| [.agents/skills/terminology_maintenance/SKILL.md](../.agents/skills/terminology_maintenance/SKILL.md) | 维护规则 | 已修订 | 核对维护入口与关键代码；保留既有技能名。 |
| [.agents/workflows/architectureandprogressreview.md](../.agents/workflows/architectureandprogressreview.md) | 维护规则 | 核对后保留 | 全文审读；属于执行流程，不是已完成的验收报告。 |
| [.agents/workflows/generatenextrectificationprompt.md](../.agents/workflows/generatenextrectificationprompt.md) | 维护规则 | 核对后保留 | 全文审读；属于执行流程，不是已完成的验收报告。 |
| [.agents/workflows/phaseacceptancereview.md](../.agents/workflows/phaseacceptancereview.md) | 维护规则 | 核对后保留 | 全文审读；属于执行流程，不是已完成的验收报告。 |
| [.agents/workflows/reviewcurrentchanges.md](../.agents/workflows/reviewcurrentchanges.md) | 维护规则 | 核对后保留 | 全文审读；属于执行流程，不是已完成的验收报告。 |
| [.github/PULL_REQUEST_TEMPLATE.md](../.github/PULL_REQUEST_TEMPLATE.md) | 维护规则 | 已修订 | 去除预填Pass，要求填写实际通过/失败/未执行结果。 |
| [CHANGELOG.md](../CHANGELOG.md) | 历史核界 | 已修订 | 增加历史边界；旧版本数字与能力描述不作为当前验收。 |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | 维护规则 | 核对后保留 | 核对本地贡献与测试流程；未核实远端分支保护设置。 |
| [README.md](../README.md) | 实现对照 | 已修订 | 更正路由局部修正、候选过滤范围；保留安装与运行入口。 |
| [artifacts/deep-research-report.md](../artifacts/deep-research-report.md) | 关键外源复核 | 已修订 | 新增独立来源说明；37个旧引文块无损存档，32个旧ID的原URL映射仍缺。 |
| [artifacts/time_module_v2_upgrade_report.md](../artifacts/time_module_v2_upgrade_report.md) | 历史核界 | 已修订 | 原126例与可追溯源码129例未对齐，历史JUnit缺失；当前155通过/4子测试另记。 |
| [docs/README.md](README.md) | 实现对照 | 已修订 | 汇总当前入口、历史报告、PDF及逐文件覆盖。 |
| [docs/architecture/frontend-markdown-rendering.md](architecture/frontend-markdown-rendering.md) | 实现对照 | 已修订 | 核对正文关键实现、开关和边界；历史指标不作为新验收。 |
| [docs/architecture/governance-baseline.md](architecture/governance-baseline.md) | 实现对照 | 已修订 | 核对正文关键实现、开关和边界；历史指标不作为新验收。 |
| [docs/architecture/overview.md](architecture/overview.md) | 实现对照 | 已修订 | 核对正文关键实现、开关和边界；历史指标不作为新验收。 |
| [docs/architecture/ros2_runtime_configuration.md](architecture/ros2_runtime_configuration.md) | 实现对照 | 已修订 | 核对正文关键实现、开关和边界；历史指标不作为新验收。 |
| [docs/ci_dependency_failure.md](ci_dependency_failure.md) | 历史核界 | 已修订 | 保留历史故障，转向当前CI的CPU torch与editable test安装入口。 |
| [docs/current_design_contract.md](current_design_contract.md) | 实现对照 | 已修订 | 修正确认/警告接受、语义路由例外和候选域未接入层。 |
| [docs/debug-design-qa-write-failure.md](debug-design-qa-write-failure.md) | 历史核界 | 已修订 | 归档未附日志的排查假设；未宣称缺陷已复现或修复。 |
| [docs/decisions/ADR-001-write-query-routing.md](decisions/ADR-001-write-query-routing.md) | 决策/实现对照 | 已修订 | 核对当前代码及后续决策取代关系；未重新验收全部不变量。 |
| [docs/decisions/ADR-002-slotstore-source-of-truth.md](decisions/ADR-002-slotstore-source-of-truth.md) | 决策/实现对照 | 已修订 | 核对当前代码及后续决策取代关系；未重新验收全部不变量。 |
| [docs/decisions/ADR-003-task-intent-atomic-persistence.md](decisions/ADR-003-task-intent-atomic-persistence.md) | 决策/实现对照 | 已修订 | 核对当前代码及后续决策取代关系；未重新验收全部不变量。 |
| [docs/decisions/ADR-004-deterministic-task-request-guard.md](decisions/ADR-004-deterministic-task-request-guard.md) | 决策/实现对照 | 已修订 | 核对当前代码及后续决策取代关系；未重新验收全部不变量。 |
| [docs/decisions/ADR-005-llm-semantic-authority.md](decisions/ADR-005-llm-semantic-authority.md) | 决策/实现对照 | 已修订 | 核对当前代码及后续决策取代关系；未重新验收全部不变量。 |
| [docs/decisions/ADR-006-two-capability-welcome-message.md](decisions/ADR-006-two-capability-welcome-message.md) | 决策/实现对照 | 已修订 | 核对当前代码及后续决策取代关系；未重新验收全部不变量。 |
| [docs/decisions/ADR-007-atomic-snapshot-restore.md](decisions/ADR-007-atomic-snapshot-restore.md) | 决策/实现对照 | 已修订 | 核对当前代码及后续决策取代关系；未重新验收全部不变量。 |
| [docs/decisions/ADR-008-constraint-aware-robot-selection.md](decisions/ADR-008-constraint-aware-robot-selection.md) | 决策/实现对照 | 已修订 | 核对当前代码及后续决策取代关系；未重新验收全部不变量。 |
| [docs/decisions/ADR-009-test-runtime-artifact-isolation.md](decisions/ADR-009-test-runtime-artifact-isolation.md) | 决策/实现对照 | 核对后保留 | 核对当前代码及后续决策取代关系；未重新验收全部不变量。 |
| [docs/development/governance-acceptance-matrix.md](development/governance-acceptance-matrix.md) | 实现对照 | 已修订 | 测试映射不等于新验收；标注KD历史条目与当前实现差异。 |
| [docs/development/testing.md](development/testing.md) | 实现对照 | 已修订 | 对照pyproject、CI、状态隔离及可选外部测试开关。 |
| [docs/execution_dispatch_contract.md](execution_dispatch_contract.md) | 实现对照 | 核对后保留 | 核对归档、SENT/UNKNOWN/SCHEDULED与不确定提交恢复边界。 |
| [docs/progress/2026-09-26-real-model-dialogue-audit.md](progress/2026-09-26-real-model-dialogue-audit.md) | 历史核界 | 核对后保留 | 历史实验有全量失败及后续定向修复；不写成最终全量绿灯。 |
| [docs/progress/2026-09-26-user-journey-audit.md](progress/2026-09-26-user-journey-audit.md) | 历史核界 | 核对后保留 | 保留日期与验证限制；未重跑真实模型/浏览器实验。 |
| [docs/progress/optimization_progress_report.md](progress/optimization_progress_report.md) | 历史核界 | 已修订 | 注明2026-09-09分支/提交范围；旧接口和统计保留历史含义。 |
| [docs/progress/phase-1-5-validation.md](progress/phase-1-5-validation.md) | 历史核界 | 核对后保留 | 保留阶段结果；当前模块路径/协议需看现行契约。 |
| [docs/superpowers/plans/2026-07-17-equipment-unit-id-candidate-chain.md](superpowers/plans/2026-07-17-equipment-unit-id-candidate-chain.md) | 历史核界 | 已修订 | 历史计划，不是当前待办；旧技能、命令和会话约束不继续生效。 |
| [docs/test_coverage_strategy.md](test_coverage_strategy.md) | 实现对照 | 已修订 | 更新状态隔离与TS-19现已无expected_failure标记。 |
| [frontend/vendor/README.md](../frontend/vendor/README.md) | 实现对照 | 核对后保留 | 本地两个SHA-256与许可证存在性已核对；未核验外部发布状态。 |
| [mcp/operation-time-window/DESIGN_SPEC.md](../mcp/operation-time-window/DESIGN_SPEC.md) | 实现对照 | 已修订 | 明确线程超时、网格/深度/时间检查、缓存和主应用接入缺口。 |
| [mcp/operation-time-window/README.md](../mcp/operation-time-window/README.md) | 实现对照 | 已修订 | 核对独立模块、命令、合成数据门禁；本轮90通过/1跳过。 |
| [mcp/ros-mcp/README.md](../mcp/ros-mcp/README.md) | 实现对照 | 核对后保留 | 核对生产/Mock链路、绑定辅助函数和派发状态；无实机验收。 |
| [mcp/ros-mcp/docs/SEAgent_ROS2_MCP_Integration_Report.pdf](../mcp/ros-mcp/docs/SEAgent_ROS2_MCP_Integration_Report.pdf) | 历史PDF核界 | 原件保留，另附勘误 | 读取3页并保留原字节；2026-08-26编写，另附当前协议PDF。 |
| [mcp/ros-mcp/docs/adapter_presentation_slides_outline.md](../mcp/ros-mcp/docs/adapter_presentation_slides_outline.md) | 实现对照 | 已修订 | 同步协议/Mock边界，保留历史实验的限定语义。 |
| [mcp/ros-mcp/docs/adapter_specification_report.md](../mcp/ros-mcp/docs/adapter_specification_report.md) | 实现对照 | 已修订 | 区分生产与stdio Mock，修正API和载荷；本轮定向11项通过。 |
| [mcp/ros-mcp/docs/live_e2e_debugging_guide.md](../mcp/ros-mcp/docs/live_e2e_debugging_guide.md) | 实现对照 | 核对后保留 | 核对当前运行入口与现场前提；未实际连接网关。 |
| [mcp/ros-mcp/docs/mcp_execution_verification_report.md](../mcp/ros-mcp/docs/mcp_execution_verification_report.md) | 实现对照 | 已修订 | 保留历史控制台块和120项表；另列当前11项验证。 |
| [record/7.22/details.md](../record/7.22/details.md) | 历史核界 | 已修订 | 保留原始记录；旧路径与当时测试不是现行操作指南。 |
| [record/7.22/summary.md](../record/7.22/summary.md) | 历史核界 | 已修订 | 保留原始记录；旧路径与当时测试不是现行操作指南。 |
| [scratch/README.md](../scratch/README.md) | 实现对照 | 已修订 | 说明默认pytest排除、历史脚本的固定路径和写入行为。 |
| [scratch/ros2_mcp_test/README.md](../scratch/ros2_mcp_test/README.md) | 实现对照 | 已修订 | 核对临时状态fixture、真实写入与固定/tmp文件；本轮未重跑沙箱。 |
| [scripts/e2e_suites/README.md](../scripts/e2e_suites/README.md) | 实现对照 | 已修订 | 限定手动HTTP脚本、状态写入、固定日期及非浏览器/非协同调度范围。 |
| [src/README.md](../src/README.md) | 实现对照 | 已修订 | 补state/catalog/控制处理器；修正OutputBuilder职责和无覆盖提交。 |
| [test_logs/Qwen3.5-9B_RTX5090_VRAM0.95_Benchmark_Report.md](../test_logs/Qwen3.5-9B_RTX5090_VRAM0.95_Benchmark_Report.md) | 历史核界 | 已修订 | 测量表数值保留；缺逐次计时，生成器部分环境文字写死；未重跑硬件实验。 |
| [test_logs/Qwen3.5-9B_RTX5090_vs_HB10_Performance_Report.md](../test_logs/Qwen3.5-9B_RTX5090_vs_HB10_Performance_Report.md) | 历史核界 | 已修订 | 测量表数值保留；按表内数字更正一处增幅为27.0%，不追认原实验。 |
| [test_logs/SEAgent1.0_LLM_Full_Product_Test_Report_20260825.md](../test_logs/SEAgent1.0_LLM_Full_Product_Test_Report_20260825.md) | 历史核界 | 已修订 | 历史Mock HTTP：现存JSONL为55通过/2失败/3警告；汇总JSON为空。 |
| [tests/test_accumulation/测试机.md](../tests/test_accumulation/测试机.md) | 历史核界 | 已修订 | 历史人工场景/问题记录；修正文档口径，未执行在线状态修改。 |
| [tests/test_accumulation/测试集05.md](../tests/test_accumulation/测试集05.md) | 历史核界 | 已修订 | 历史人工场景/问题记录；修正文档口径，未执行在线状态修改。 |
| [tests/test_accumulation/问题.md](../tests/test_accumulation/问题.md) | 历史核界 | 已修订 | 历史人工场景/问题记录；修正文档口径，未执行在线状态修改。 |

## 4. 验证与新增材料

- [本轮测试运行记录](../test_logs/documentation-review-2026-09-30.md)保存实际命令、结果及海流、时间模块的原始日志/JUnit；ROS 11项及治理121项输出明确标为工具转录。治理组使用另一Python解释器，带1个已有异步插件配置警告，详见记录。
- [当前协议PDF](../mcp/ros-mcp/docs/SEAgent_ROS2_MCP_Protocol_Review_20260930.pdf)为新增4页核对说明；原3页PDF保持历史原件，不能独立作为当前接口说明。
- [时间调研来源与实现核对](../artifacts/temporal-research-source-review.md)另附[PDF](../artifacts/temporal-research-source-review.pdf)，区分标准事实、厂商公开接口、库的语言能力与项目实现；原引用保存在[JSON存档](../artifacts/temporal-research-legacy-citations.json)，不将新链接假定为旧ID对应来源。
- 当前时间模块定向测试为155 passed / 4 subtests passed；两段文档示例另有7项固定检查通过，并保留[输入、结果与代码哈希](../test_logs/documentation-review-20260930/temporal-example-check.json)。这些检查不代表完整时间语义验收。
- 本清单另提供[PDF副本](documentation-audit.pdf)；新增清单、测试记录、来源说明及3份PDF不重复计入65份基线。
- 技能frontmatter与引用检查完成；通用skill验证器仍提示4份原有下划线技能名不符合hyphen-case规则，调用名称未为消除此提示而变更。
- 收尾检查覆盖67份Markdown的436个本地文件链接，失效0；文档补丁空白检查通过。65份基线清单与Git文件列表一致，海流及当前时间模块日志/JUnit副本与当次原始文件逐字节一致。链接检查不含外网可用性和Markdown标题锚点。

## 5. 尚未补齐的证据

1. `artifacts/deep-research-report.md` 的旧引用原 URL 映射仍未恢复。本轮已独立核对关键一手来源并更正相关主张，未逐句复核全文，也未为旧ID猜测或补造映射。
2. 历史性能/时间模块报告中的部分临时日志不可获得，时间报告分类计数与可追溯源码不一致；已限定结论，不能用当前测试追认原实验。
3. 全仓测试此前被 `Killed` 中断，没有完整通过结果，本轮未查明终止原因或重跑全量。
4. 实机 ROS、真实 Copernicus、真实模型/ASR及现场精度仍需独立执行验证；文档修订不会完成这些验收。
5. 海流线程取消、数据完整性及主流程接入，ADR-008完整候选过滤、ADR-005局部规则差异均为实现层事项。本轮已经如实记载，没有借文档修订修改业务行为。
