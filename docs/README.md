# 文档索引

整理日期：2026-09-30。当前实现以代码、配置和实际测试结果为依据；历史报告中的用例数量、阈值和缺陷状态只对应记录时的版本。逐文件范围、修订状态和剩余证据缺口见[文档审查清单](documentation-audit.md)。

## 当前使用与维护

| 内容 | 入口 |
| --- | --- |
| 安装与启动 | [项目 README](../README.md) |
| 运行架构、模块状态、遥测门禁 | [架构总览](architecture/overview.md) |
| 路由、槽位、候选和持久化契约 | [当前设计契约](current_design_contract.md) |
| 归档、发送状态与重试边界 | [执行下发契约](execution_dispatch_contract.md) |
| 自动化测试、CPU/GPU 环境与产物隔离 | [开发测试指南](development/testing.md) |
| 治理场景与对应测试 | [验收矩阵](development/governance-acceptance-matrix.md) |
| ROS 2 配置与集成 | [运行配置设计](architecture/ros2_runtime_configuration.md)、[ROS MCP 说明](../mcp/ros-mcp/README.md) |
| 海流查询与作业窗口 | [海流 MCP 说明](../mcp/operation-time-window/README.md) |
| 贡献流程 | [CONTRIBUTING](../CONTRIBUTING.md) |
| 文档覆盖与本轮验证 | [逐文件审查清单](documentation-audit.md)、[测试运行记录](../test_logs/documentation-review-2026-09-30.md) |

## 决策与历史证据

- [ADR 目录](decisions/) 保存设计决策；后续决策可能替代早期方案，例如 ADR-005 更新 ADR-001 的语义路由方式。
- [治理基线](architecture/governance-baseline.md) 保留 G0.1 的背景与保护边界，并标注后续变化。
- [Phase 1.5 验证记录](progress/phase-1-5-validation.md) 是历史阶段记录。
- [2026-09-26 用户流程验收](progress/2026-09-26-user-journey-audit.md) 和 [2026-09-26 至 27 日真实模型验收](progress/2026-09-26-real-model-dialogue-audit.md) 包含当时的环境、结果与验证限制。
- [海流 MCP 设计与实现状态](../mcp/operation-time-window/DESIGN_SPEC.md) 描述独立模块的现状；[历史海流测试记录](../mcp/operation-time-window/evidence/current_status.json) 文件名中的 `current` 不代表实时结果。该模块尚未成为主 DialogueManager 的默认运行链路。
- ROS MCP 的[当前协议报告](../mcp/ros-mcp/docs/adapter_specification_report.md)及[2026-09-30 协议核对 PDF](../mcp/ros-mcp/docs/SEAgent_ROS2_MCP_Protocol_Review_20260930.pdf)区分生产适配器和 stdio Mock；[原集成 PDF](../mcp/ros-mcp/docs/SEAgent_ROS2_MCP_Integration_Report.pdf)保留为历史材料，不能独立作为现行接口说明。
- 时间调研的[来源核对与实现边界](../artifacts/temporal-research-source-review.md)及[PDF](../artifacts/temporal-research-source-review.pdf)补充本次核实的一手资料、当前代码对照和定向测试；[原草案](../artifacts/deep-research-report.md)仍保留历史方案属性，旧引用的原 URL 映射尚未恢复。
- [CHANGELOG](../CHANGELOG.md)、[历史产物报告](../artifacts/) 和 [测试报告](../test_logs/) 保留演进证据。机器临时目录中的原始日志可能不随仓库克隆提供。

## 文档维护约定

- 仓库内文件使用相对链接，避免 `file://` 和某台机器的工作区绝对路径。
- 当前文档应区分默认启用、开关控制、外部依赖和规划能力。
- 历史报告保留原结论；补充日期和迁移说明，不把过去的测试结果改写成当前验收结果。
- 测试引用必须指向实际文件及函数。无法核实的覆盖项标为待验证，不凭测试名称宣布通过。
- 文档修订检查本地链接、测试引用和补丁格式；涉及行为结论时运行对应已有测试。完整应用验收仍按开发测试指南执行。
