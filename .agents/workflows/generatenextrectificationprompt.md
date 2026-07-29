---
description: Generate Next Rectification Prompt
---

# Generate Next Rectification Prompt

## Trigger

当用户提出以下请求时运行：

- 给出下一步提示词；
- 给出下一轮提示词；
- 让代码 Agent 继续修改；
- 根据检查结果生成修改任务；
- 下一阶段应该让 AI 做什么。

## Objective

生成一段可以直接交给 Codex、Claude Code 或其他代码 Agent 执行的工程提示词。

每一轮只处理一个可以独立验证的问题集合，不混入无关重构。

## Required Prompt Structure

### 1. Task Title

标题必须明确描述本轮目标。

例如：

`Fix blocked_soft continuation without weakening hard constraints`

### 2. Repository Context

写明：

- Repository；
- 当前分支或基准 commit；
- 当前开发阶段；
- 已知通过内容；
- 本轮不得破坏的行为。

### 3. Confirmed Problem

写明：

- 失败现象；
- 触发输入；
- 当前状态；
- 实际行为；
- 预期行为；
- 对应测试或日志。

### 4. Root Cause

如果根因已确认，明确说明。

如果根因尚未确认，应要求 Agent 先验证假设，不得把推测写成事实。

### 5. Files and Functions

列出需要重点检查的文件、类和函数。

没有确认实际代码时，不得虚构行号。

### 6. Required Investigation

要求 Agent：

1. 阅读入口；
2. 追踪调用链；
3. 检查状态修改顺序；
4. 检查数据写入；
5. 阅读现有测试；
6. 复现失败；
7. 确认根因后再修改。

### 7. Required Changes

明确：

- 应修改什么；
- 哪些行为必须保持；
- 是否需要兼容旧数据；
- 异常应如何处理；
- 是否需要日志；
- 是否需要新增保护条件。

### 8. Prohibited Solutions

禁止：

- 测试专用分支；
- 删除断言；
- skip 测试；
- 弱化硬约束；
- 只增加表面关键词匹配；
- 大范围无关重构；
- 吞掉异常；
- 无证据删除文件；
- 未验证便修改持久化 schema；
- 为通过测试伪造成功结果。

### 9. Required Tests

要求覆盖：

- 原失败用例；
- 正向用例；
- 反向用例；
- 边界用例；
- 普通对话回归；
- 相关状态机回归；
- 必要的持久化或并发用例。

### 10. Test Commands

要求依次运行：

1. 定向测试；
2. 相关模块测试；
3. 完整测试集。

测试路径和命令必须依据真实仓库确定。

### 11. Git Requirements

要求 Agent：

- 检查 git diff；
- 检查 git status；
- 不提交临时文件；
- 不提交密钥；
- 使用单一职责 commit；
- 只有实际执行后才能报告 commit 和 push。

### 12. Required Final Report

Agent 最终必须返回：

1. 根因；
2. 修改文件；
3. 关键实现说明；
4. 新增或修改的测试；
5. 实际执行的命令；
6. 定向测试结果；
7. 模块测试结果；
8. 完整测试结果；
9. skipped、warnings 和 timeout；
10. 未解决问题；
11. git status；
12. commit hash；
13. push 状态；
14. 本轮验收标准是否全部满足。

## Quality Standard

提示词必须具体到 Agent 能够直接开始调查和修改，不需要重新猜测任务。

不得预先指定未经验证的实现细节。