---
description: Review Current Changes
---

# Review Current Changes

## Trigger

当用户提出以下请求时运行：

- 检查代码；
- 检查当前修改；
- 检查 commit；
- 检查 PR；
- 检查测试结果；
- 继续检查；
- 当前实现是否可以验收；
- 合并后重新检查。

## Procedure

### Step 1: Determine Review Target

确认本轮检查对象：

- 当前工作区；
- git diff；
- 某个 commit；
- 某个 PR；
- 某个测试输出；
- 某个开发报告。

优先读取真实代码、diff 和测试结果。

如果缺少必要证据，不根据开发者总结直接认定通过。

### Step 2: Identify Changed Scope

列出：

- 修改文件；
- 修改函数或类；
- 修改目标；
- 是否涉及公共接口；
- 是否涉及 schema；
- 是否涉及状态机；
- 是否涉及文件持久化；
- 是否存在无关改动。

### Step 3: Build the Call Chain

对每个关键修改点识别：

1. 输入入口；
2. 上游调用方；
3. 当前函数；
4. 状态变化；
5. 数据写入；
6. 返回结果；
7. 异常处理；
8. 下游调用方。

### Step 4: Review Correctness

重点检查：

- 是否修复真实根因；
- 是否只针对单个测试硬编码；
- phase 是否被提前修改；
- old phase 是否正确保存；
- Slot 是否错误覆盖；
- 普通 LLM 对话是否受影响；
- ASR 路径是否受影响；
- 硬约束是否可能被绕过；
- 文件操作是否原子；
- 是否存在竞态；
- 是否存在重复发布；
- 异常是否被吞掉；
- API 契约是否改变；
- snapshot 是否仍可恢复。

### Step 5: Review Tests

确认：

- 是否存在原始失败用例；
- 是否新增回归测试；
- 正向场景是否覆盖；
- 反向场景是否覆盖；
- 边界场景是否覆盖；
- 定向测试是否运行；
- 模块测试是否运行；
- 完整测试是否运行；
- 是否存在 skipped、warnings、timeout 或卡死。

### Step 6: Classify Findings

按照 P0、P1、P2、P3 分类。

每个问题必须说明：

- 位置；
- 触发条件；
- 实际行为；
- 预期行为；
- 根因；
- 修复方向；
- 验证方法。

### Step 7: Return Final Review

输出格式：

1. 当前结论；
2. 检查范围；
3. 已确认正确；
4. P0/P1/P2/P3 问题；
5. 根因分析；
6. 回归风险；
7. 测试证据；
8. 下一步最小任务；
9. 验收标准。

只有实现和测试证据均充分时才输出 PASS。