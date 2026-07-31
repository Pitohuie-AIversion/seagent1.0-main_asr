---
description: 
---

---

## description: Issue to PR Bugfix Workflow

# Issue to PR Bugfix Workflow

## 1. Read and Validate the Issue

开始修改前，读取完整 Issue 并确认：

* Issue 编号；
* 严重程度；
* 当前问题；
* 根因或待验证假设；
* 涉及文件；
* 必须保持不变的行为；
* 非目标；
* 禁止方案；
* 必须新增或修改的测试；
* 验证命令；
* 验收标准。

如果缺少影响正确性的关键信息，输出：

```text
BLOCKED
```

并说明缺少的信息。

不得在未理解调用链和状态变化前直接修改代码。

---

## 2. Establish the Baseline

检查工作区：

```bash
git status --short
git branch --show-current
git fetch origin
```

工作区必须干净。禁止使用 `git reset --hard` 清理未知修改。

同步主分支：

```bash
git switch main
git pull --ff-only origin main
git rev-parse HEAD
```

记录输出为：

```text
BASELINE_SHA=<sha>
```

运行基线检查：

```bash
python -m compileall -q src tests
```

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
python -m unittest discover tests -v 2>&1 | tee baseline_full_test.log
```

记录：

```text
tests_run=
failures=
errors=
skipped=
collection_errors=
```

如果基线已有失败，必须先判断：

* 是否为本 Issue 的已知失败；
* 是否为环境失败；
* 是否为仓库当前未解决回归。

无法区分时，任务结论为 `BLOCKED`。

---

## 3. Create the Working Branch

Bug 修复：

```bash
git switch -c fix/issue-<number>-<short-description>
```

例如：

```bash
git switch -c fix/issue-3-task-intent-semantics
```

不得在 `main` 上开发。

---

## 4. Trace the Call Chain

修改前必须检查：

* 输入入口；
* 直接调用者；
* 被调用函数；
* 状态读写位置；
* Slot 变化；
* phase 变化；
* 约束检查；
* 持久化操作；
* API 返回；
* 现有测试。

需要输出一份简洁调用链，例如：

```text
/api/chat
→ DialogueManager.process()
→ _process_internal()
→ ParameterExtractor
→ SlotStore
→ TaskValidator
→ TaskIntentBuilder.prepare()
→ create_staging()
→ publish_staging()
→ API final_json
```

不得仅根据报错行修改代码。

---

## 5. Reproduce the Failure First

优先使用现有失败测试。

如果没有，应新增定向测试。

修复前运行：

```bash
python -m unittest <targeted-test-module> -v \
  2>&1 | tee red_test.log
```

失败测试必须：

* 稳定复现；
* 对应真实业务问题；
* 在旧代码上失败；
* 不依赖偶发模型输出；
* 不使用过度 mock 绕过核心路径。

如果新增测试在旧代码上直接通过，应重新检查测试设计，不得立即修改实现。

记录：

```text
failing_test=
failure_type=
expected_behavior=
actual_behavior=
```

---

## 6. Implement the Minimum Safe Change

只修改 Issue 范围内的文件。

修改必须：

* 修复根因；
* 保持公开接口；
* 保持无关行为；
* 保持普通 LLM 对话；
* 保持 ASR 输入路径；
* 保持 SlotStore 事务语义；
* 保持硬约束和软约束语义；
* 保持持久化安全机制。

禁止：

* 无关重构；
* 测试专用逻辑；
* 降低验证强度；
* 吞掉异常；
* 修改测试接受错误行为；
* 新增 skip 或 expected failure；
* 未说明的 Schema 修改；
* 未说明的 API 契约修改。

如发现必须扩大范围，先更新 Issue 后再继续。

---

## 7. Run Layered Verification

### 7.1 Syntax

```bash
python -m compileall -q src tests
```

### 7.2 Targeted Tests

```bash
python -m unittest <targeted-tests> -v
```

### 7.3 Related Module Tests

```bash
python -m unittest <related-module-tests> -v
```

### 7.4 Full Test Suite

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
python -m unittest discover tests -v 2>&1 | tee final_full_test.log
```

记录：

```text
tests_run=
failures=
errors=
skipped=
collection_errors=
```

### 7.5 Service E2E

如果本次修改涉及：

* API；
* DialogueManager；
* ASR；
* 遥测；
* TaskIntent 发布；
* history；
* 前端交互；

则必须启动隔离服务并运行对应 E2E。

服务必须：

* 使用临时结果目录；
* 使用隔离 session；
* 恢复修改过的 fixture；
* 关闭后不残留进程。

### 7.6 Real Model Acceptance

如果 Issue 涉及 LLM 或 ASR 真实行为，必须单独报告：

* 模型名称；
* 模型版本；
* GPU；
* 测试输入；
* 测试数量；
* failures；
* errors；
* 是否使用标注数据。

Mock 测试不得被描述为真实模型验收。

---

## 8. Review the Diff

执行：

```bash
git diff --check
git status --short
git diff --stat
git diff
```

检查：

* 没有无关修改；
* 没有调试代码；
* 没有临时文件；
* 没有日志文件；
* 没有修改模型权重；
* 没有密钥、token 或 `.env`；
* 没有本地绝对路径；
* 没有意外修改 `config/state.yaml`；
* 没有新增 skip；
* 没有新增 expected failure；
* 没有削弱断言；
* 没有重复类或重复函数；
* 没有未定义变量；
* 没有死代码掩盖真实实现。

不得未经检查直接执行：

```bash
git add .
```

---

## 9. Commit Explicit Files

显式添加文件：

```bash
git add <file-1> <file-2> <test-files>
```

提交：

```bash
git commit -m "<type>: <clear description>"
```

记录真实 commit：

```bash
git rev-parse HEAD
```

输出：

```text
FIX_SHA=<sha>
```

推送：

```bash
git push -u origin <branch-name>
```

未实际 commit 或 push 时，不得声称已经完成。

---

## 10. Create the Pull Request

PR 标题应明确问题和严重程度。

PR 必须包含：

```markdown
## Related Issue

Closes #<issue-number>

## Root Cause

说明问题发生的调用链、状态变化或数据流。

## Changes

列出核心修改。

## Modified Files

列出全部修改文件及原因。

## Before Fix

- Baseline SHA:
- Failing test:
- Failure evidence:

## Verification

### Targeted tests

- Command:
- Result:

### Related module tests

- Command:
- Result:

### Full suite

- Tests run:
- Failures:
- Errors:
- Skipped:
- Collection errors:

### Service E2E

- Command:
- Result:

### Real model acceptance

- Not required / Completed
- Evidence:

## Git Evidence

- Fix SHA:
- Branch:
- Pushed:
- CI run:

## Remaining Issues

说明未解决问题；没有则写 `None`。

## Acceptance Checklist

逐项复制 Issue 验收标准并勾选。
```

PR 不得在 CI 运行前合并。

---

## 11. Independent Review

审查者不得依赖开发者总结。

必须检查：

* PR diff；
* 调用链；
* 状态转移；
* 数据修改；
* 异常行为；
* 测试代码；
* CI 输出；
* commit SHA；
* Issue 验收标准。

审查结论：

```text
PASS
PASS WITH CONDITIONS
FAIL
BLOCKED
```

出现 P0 或 P1 问题时必须拒绝合并。

---

## 12. Merge and Verify Main

PR 合并后：

```bash
git switch main
git pull --ff-only origin main
git rev-parse HEAD
```

记录：

```text
MERGED_MAIN_SHA=<sha>
```

重新运行：

```bash
python -m compileall -q src tests
```

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
python -m unittest discover tests -v
```

如果涉及服务主流程，再运行服务级 E2E。

PR 分支通过但合并后 `main` 失败时，Issue 不得关闭。

---

## 13. Close the Issue

关闭前确认：

* PR 已合并；
* CI 已通过；
* main 已验证；
* 验收标准全部满足；
* 没有未说明 P0/P1；
* 最新报告绑定 `MERGED_MAIN_SHA`；
* PR 中存在真实测试和 Git 证据。

完成后在 Issue 留言：

```markdown
## Completion Evidence

- PR:
- Merged main SHA:
- CI:
- Full suite:
- Service E2E:
- Remaining issues:
- Acceptance result: PASS
```

然后关闭 Issue。

---

## Required Final Response

Agent 完成任务时必须返回：

1. 当前结论；
2. 根因分析；
3. 修改摘要；
4. 修改文件；
5. 新增和修改测试；
6. 修复前失败测试；
7. 修复后定向测试；
8. 模块测试；
9. 完整测试；
10. 服务 E2E；
11. 真实模型验收；
12. `git diff --check`；
13. baseline SHA；
14. fix SHA；
15. branch；
16. 是否 push；
17. PR；
18. CI；
19. 未解决问题；
20. 是否满足验收标准。

缺少真实证据时不得输出 `PASS`。
