# 文档索引

整理日期：2026-09-29。当前实现以代码、配置和实际测试结果为依据；历史报告中的用例数量、阈值和缺陷状态只对应记录时的版本。

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

## 决策与历史证据

- [ADR 目录](decisions/) 保存设计决策；后续决策可能替代早期方案，例如 ADR-005 更新 ADR-001 的语义路由方式。
- [治理基线](architecture/governance-baseline.md) 保留 G0.1 的背景与保护边界，并标注后续变化。
- [Phase 1.5 验证记录](progress/phase-1-5-validation.md) 是历史阶段记录。
- [2026-09-26 用户流程验收](progress/2026-09-26-user-journey-audit.md) 和 [2026-09-26 至 27 日真实模型验收](progress/2026-09-26-real-model-dialogue-audit.md) 包含当时的环境、结果与验证限制。
- [CHANGELOG](../CHANGELOG.md)、[历史产物报告](../artifacts/) 和 [测试报告](../test_logs/) 保留演进证据。机器临时目录中的原始日志可能不随仓库克隆提供。

## 文档维护约定

- 仓库内文件使用相对链接，避免 `file://` 和某台机器的工作区绝对路径。
- 当前文档应区分默认启用、开关控制、外部依赖和规划能力。
- 历史报告保留原结论；补充日期和迁移说明，不把过去的测试结果改写成当前验收结果。
- 测试引用必须指向实际文件及函数。无法核实的覆盖项标为待验证，不凭测试名称宣布通过。
- 文档修订检查本地链接、测试引用和补丁格式；涉及行为结论时运行对应已有测试。完整应用验收仍按开发测试指南执行。
