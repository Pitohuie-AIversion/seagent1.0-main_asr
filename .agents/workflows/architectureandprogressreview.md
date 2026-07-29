---
description: Architecture and Progress Review
---

# Architecture and Progress Review

## Trigger

当用户提出以下请求时运行：

- 当前项目进度；
- 当前软件架构；
- 当前 roadmap；
- 已解决和未解决的问题；
- 合并 PR 后重新检查；
- 当前下一步应该开发什么；
- 总结当前项目状态。

## Procedure

### Step 1: Read Current Repository State

优先检查：

- 默认分支；
- 当前 HEAD；
- 最近 commit；
- 已合并 PR；
- git status；
- 目录结构；
- 入口文件；
- 核心模块；
- 测试目录；
- 当前失败测试；
- README；
- 设计和验收文档。

当前代码优先于历史总结和规划文档。

### Step 2: Build Actual Architecture

按照以下层级整理：

1. User and Frontend；
2. API Routing；
3. ASR and Input Normalization；
4. LLM and Intent Routing；
5. DialogueManager；
6. Slot Store；
7. Constraint Validation；
8. Task Intent Builder；
9. Persistence；
10. Robot Execution Adapter；
11. Logging, Observability and Tests。

每个模块标记：

- Implemented；
- Partially Implemented；
- Planned；
- Defective；
- Unknown。

### Step 3: Build Actual Runtime Flows

至少梳理：

- 普通文本对话；
- ASR 输入；
- 任务识别；
- 槽位收集；
- 槽位修改；
- 约束验证；
- 用户确认；
- 软警告忽略；
- 硬约束阻断；
- staging 创建；
- final publish；
- snapshot load；
- 异常恢复。

每条流程应标出：

- 入口；
- 关键模块；
- 状态变化；
- 持久化节点；
- 失败出口；
- 当前缺陷。

### Step 4: Determine Project Status

分别列出：

- 已完成并验证；
- 已实现但未完整验证；
- 部分实现；
- 当前 P0/P1/P2/P3 问题；
- 技术债；
- 规划中；
- 尚未开始；
- 当前不应开展的工作。

### Step 5: Produce Roadmap

使用：

#### Now

当前必须完成，用于消除阻断项和稳定主流程。

#### Next

主流程稳定后开始，用于接口、测试和可维护性完善。

#### Later

后续扩展，例如：

- 世界模型；
- 任务图；
- 多机器人任务分配；
- 弱通信协同；
- 动态重规划；
- 机器人执行反馈闭环。

每个任务必须包含：

- 目标；
- 涉及模块；
- 前置条件；
- 主要风险；
- 验收标准。

### Step 6: Output

固定输出：

1. 当前总体结论；
2. 当前 HEAD 或检查基准；
3. 实际软件架构；
4. 实际运行流程；
5. 已完成并验证；
6. 已实现但未验证；
7. 未解决问题；
8. 技术债；
9. Now / Next / Later roadmap；
10. 下一步最小可验收任务。

不得把目标架构误写成当前已实现架构。