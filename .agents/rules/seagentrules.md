---
trigger: always_on
---

# SEAgent Engineering Guidelines

## 1. Project Identity

本 Workspace 对应 SEAgent 项目。

Repository:

`Pitohuie-AIversion/seagent1.0-main_asr`

SEAgent 位于用户任务需求与机器人控制系统之间，负责将自然语言或语音输入转化为可验证、可确认、可持久化和可执行的结构化任务。

当前主要模块包括：

- Web frontend；
- Backend API；
- 普通 LLM 对话；
- ASR 语音识别；
- 任务意图识别；
- DialogueManager；
- Slot Store；
- 约束验证；
- Task Intent Builder；
- staging、snapshot、final、claim、temp、quarantine 持久化；
- 后续机器人执行适配层。

当前目标是先完成稳定、可测试、可恢复的软件闭环，再扩展世界模型、多机器人协同和动态重规划。

---

## 2. Priority Order

开发优先级必须遵循：

1. 普通 LLM 对话保持可用；
2. ASR 输入能够正确进入统一对话流程；
3. DialogueManager 状态机稳定；
4. Slot schema、校验和版本管理正确；
5. 软约束、硬约束和用户确认语义正确；
6. 任务文件原子发布、并发安全和异常恢复可靠；
7. 前后端 API 契约稳定；
8. 完整回归测试建立；
9. 再扩展世界模型、任务图、多机器人分配和动态重规划。

存在未解决的 P0 或 P1 问题时，不应直接进入大规模新功能开发。

---

## 3. Ordinary Chat and Task Mode

系统必须同时支持：

- 普通文本 LLM 对话；
- ASR 转写后的普通对话；
- 明确任务输入；
- 任务槽位收集；
- 任务修改；
- 风险检查；
- 用户确认；
- 任务发布。

不得因为增强任务模式而破坏普通 LLM 对话。

当输入不构成明确任务时，系统不应强制进入：

- Slot 收集；
- 风险检查；
- 发布确认；
- Task Intent 构建。

普通对话路径和任务路径应有清晰、可测试的路由边界。

---

## 4. DialogueManager Constraints

`DialogueManager` 必须被视为显式状态机，而不是简单关键词脚本。

修改 `dialogue_manager.py` 时必须检查：

- 输入前的 phase；
- 当前事件；
- 判断使用的是旧 phase 还是新 phase；
- phase 在何处发生变化；
- 是否需要保留 `old_phase`；
- 当前输入是否可能命中多个意图；
- 状态是否被提前重置；
- 当前状态是否允许该操作；
- 异常后状态是否一致。

必须区分：

- 确认任务；
- 确认发布；
- 修改任务；
- 拒绝任务；
- 取消任务；
- 忽略软警告；
- 硬约束阻断。

具体规则：

- `blocked_soft` 可以在用户明确忽略软警告后继续；
- `blocked_hard` 不得被“忽略”“继续”“确认”等输入绕过；
- “忽略警告”不应只依赖通用 `_user_confirmed()`；
- 状态判断不得依赖已经被修改或重置的 `self.phase`；
- 必要时先保存 `old_phase`，再执行状态转换；
- 关键状态转换必须有正向、反向和边界测试。

---

## 5. Slot Constraints

Slot 应维护并正确序列化：

- `value`
- `value_type`
- `status`
- `source`
- `raw_value`
- `confidence`
- `validation_error`
- `updated_at`
- `version`
- `candidate_value`

Slot 更新时必须考虑：

- 原始输入与规范化值；
- 候选值与正式值；
- 验证成功和验证失败；
- 已确认值是否应保留；
- version 是否递增；
- snapshot 恢复兼容；
- 旧 schema 是否需要迁移；
- 序列化与反序列化是否对称。

禁止在验证失败时静默覆盖已确认的有效值。

如果 candidate value 尚未通过验证，不得直接替换正式 value。

---

## 6. Constraint Handling

软约束和硬约束必须分离。

### Soft Constraint

- 可以展示警告；
- 用户可明确选择忽略；
- 忽略行为必须被记录；
- 忽略后仍需保持任务数据一致；
- 不得将模糊输入自动视为忽略。

### Hard Constraint

- 必须阻断发布；
- 不得通过确认、继续、忽略警告等普通输入绕过；
- 必须返回明确错误原因；
- 必须保留可修正的任务上下文；
- 修正约束后才可以重新进入发布流程。

---

## 7. Persistence and Atomicity

涉及 `task_intent_builder.py`、snapshot、staging、final、claim、temp 或 quarantine 时必须遵守：

- 先写临时文件，再进行原子替换；
- 文件写入完成后执行必要的 `fsync`；
- 必要时同步父目录；
- 发布、读取、恢复和关键清理操作使用一致的跨进程锁；
- 禁止不安全的 `stat -> unlink` 流程；
- 禁止根据未经验证的路径猜测删除文件；
- 无法证明可安全删除时，应保留到 quarantine；
- final 已存在时，不得无条件删除 staging 或旧文件；
- 不完整事务必须能够恢复或明确失败；
- 持久化失败必须 fail closed；
- 磁盘写入失败后不得向前端报告发布成功；
- snapshot 必须进行 schema 和数据完整性验证。

重点检查：

- TOCTOU；
- 重复发布；
- 多进程并发；
- staging/final 竞争；
- 进程中断恢复；
- 锁覆盖范围；
- 异常分支中的无锁删除；
- 替换文件后的旧文件保留策略。

---

## 8. Code Modification Procedure

修改前必须：

1. 查看相关代码入口；
2. 追踪完整调用链；
3. 确认状态和数据的真实修改位置；
4. 阅读现有测试；
5. 复现或理解失败；
6. 明确根因；
7. 制定最小修改方案；
8. 再执行修改。

禁止：

- 为单个测试增加测试专用逻辑；
- 只增加一个关键词而不分析完整语义；
- 删除失败断言；
- 将测试标记为 skip 来隐藏问题；
- 使用宽泛异常捕获吞掉错误；
- 修改 schema 但不考虑恢复兼容；
- 修改返回结构但不更新调用方；
- 将规划模块描述为已实现；
- 在一个 commit 中混入大量无关重构。

---

## 9. Testing Requirements

每轮修改至少执行三级验证。

### Level 1: Targeted Tests

运行直接覆盖本次问题的测试。

例如：

`pytest path/to/test_file.py::test_specific_case -q`

命令必须依据实际仓库结构确定，不得虚构测试路径。

### Level 2: Related Module Tests

运行相关模块测试，例如：

- DialogueManager tests；
- Slot tests；
- Task Intent Builder tests；
- Persistence tests；
- API tests；
- Frontend interaction tests。

### Level 3: Full Regression

运行项目完整测试集。

通常为：

`pytest -q`

如果项目使用其他测试入口，应采用仓库实际命令。

测试报告必须包括：

- passed；
- failed；
- skipped；
- warnings；
- timeout 或卡死情况；
- 执行命令；
- 测试范围。

---

## 10. Required Regression Scenarios

测试应覆盖：

- 普通 LLM 对话；
- ASR 输入；
- 明确任务输入；
- 非任务输入；
- 缺失槽位；
- 槽位修正；
- candidate value 验证失败；
- 用户确认；
- 用户拒绝；
- 用户取消；
- 软警告；
- 忽略警告；
- 硬约束阻断；
- 重复确认；
- 重复发布；
- staging 存在；
- final 已存在；
- snapshot 不合法；
- 进程中断恢复；
- 并发发布；
- 文件锁；
- 前端普通对话回归。

---

## 11. Code Review Output

代码审查统一使用：

### Current Conclusion

`PASS / PASS WITH CONDITIONS / FAIL / BLOCKED`

### Scope

- commit、PR 或 diff；
- 涉及文件；
- 测试范围；
- 未检查部分。

### Confirmed Correct

只列出有代码或测试证据支持的内容。

### Findings

每个问题包含：

- Severity；
- 文件和函数；
- 触发条件；
- 实际行为；
- 预期行为；
- 根因；
- 修复方向；
- 所需测试。

### Regression Risk

说明可能影响的其他模块。

### Verification

列出实际执行的命令和结果。

### Acceptance Criteria

明确进入下一阶段前必须满足的条件。

---

## 12. Git Requirements

完成修改后必须检查：

`git status`

`git diff`

`git diff --stat`

提交前确认：

- 无临时文件；
- 无调试输出；
- 无 token、密钥和本地绝对路径；
- 无无关改动；
- 测试证据完整；
- commit message 与实际修改一致。

只有真正执行后才能声称：

- 已 commit；
- 已 push；
- 已合并；
- CI 已通过。

引用 commit 时必须使用真实 hash。

---

## 13. Architecture and Documentation

架构图和流程图必须依据实际代码调用关系。

必须区分：

- 已实现；
- 部分实现；
- 规划中；
- 存在缺陷。

流程图至少应区分：

- Frontend；
- API；
- ASR；
- LLM；
- intent routing；
- DialogueManager；
- Slot Store；
- constraint validation；
- Task Intent Builder；
- persistence；
- robot execution adapter。

普通对话路径和任务创建路径必须分别展示。

不得把目标架构写成当前实际架构。

---

## 14. Project Status Reporting

项目进度必须分为：

- 已实现且验证；
- 已实现但未完整验证；
- 部分实现；
- 当前缺陷；
- 技术债；
- 规划中；
- 尚未开始。

README、设计文档、开发总结和 commit message 不能单独作为功能完成证据。

代码事实和测试结果优先。