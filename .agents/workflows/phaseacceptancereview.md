---
description: Phase Acceptance Review
---

# Phase Acceptance Review

## Trigger

当用户提出以下请求时运行：

- 第一阶段完成了吗；
- 当前阶段可以验收吗；
- 是否可以进入下一阶段；
- 回顾阶段完成情况；
- 对照 roadmap 验收；
- 合并 PR 后阶段是否结束。

## Procedure

### Step 1: Determine Acceptance Baseline

从以下内容中整理本阶段验收标准：

- 项目 roadmap；
- 已确认任务；
- 设计要求；
- 安全要求；
- 测试要求；
- Git 和交付要求；
- 已知 P0/P1 问题。

不得只根据最近一次开发报告定义验收标准。

### Step 2: Build Acceptance Matrix

每项标记为：

- Verified：有代码和测试证据；
- Implemented but Unverified：已有实现，但缺少完整验证；
- Partial：只完成部分；
- Failed：当前仍失败；
- Missing：尚未实现；
- Unknown：证据不足。

### Step 3: Check Blocking Issues

重点检查：

- 是否仍有 P0；
- 是否仍有 P1；
- 完整测试是否失败；
- 普通 LLM 对话是否回归；
- ASR 主路径是否失败；
- 状态机是否存在错误；
- 硬约束是否可绕过；
- 文件事务是否不安全；
- schema 变更是否未经验证；
- 是否缺少恢复测试；
- 是否只运行了定向测试。

### Step 4: Determine Conclusion

只能使用：

- ACCEPTED；
- CONDITIONALLY ACCEPTED；
- NOT ACCEPTED；
- BLOCKED BY MISSING EVIDENCE。

代码已合并不等于阶段已验收。

测试通过也不自动代表设计、安全和恢复要求全部满足。

### Step 5: Output

固定输出：

1. 阶段结论；
2. 验收基准；
3. 验收矩阵；
4. 已验证项目；
5. 已实现但未验证项目；
6. 阻断项；
7. 剩余风险；
8. 进入下一阶段前必须完成的任务；
9. 建议的下一轮整改范围。

### Step 6: Next Phase Gate

只有满足以下条件时才建议进入下一阶段：

- 无未解决 P0；
- 无影响主流程的 P1；
- 关键定向测试通过；
- 相关模块测试通过；
- 完整测试集通过；
- 普通 LLM 和 ASR 路径无回归；
- 发布和恢复机制满足阶段要求；
- Git 和交付证据完整。