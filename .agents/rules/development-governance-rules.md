---
trigger: always_on
---

# Development Governance Rules

## Branch protection
- 禁止直接在 main 上修改功能代码。
- 每个 P0/P1 修复必须使用独立分支。
- 分支格式：fix/issue-<number>-<short-description>。

## Issue scope
- 一个 Issue 只处理一个可独立验收的问题集合。
- 开始开发前必须记录基线 commit SHA。
- 必须明确本轮目标、非目标、允许修改文件和禁止修改文件。
- 不得顺手修改 ASR、遥测、前端或其他无关模块。

## Test-first defect repair
- Bug 修复必须先新增或确认一个能够在旧代码上失败的测试。
- 必须保留修复前失败证据和修复后通过证据。
- 不得修改测试来接受错误行为。
- 不得新增 skip、expectedFailure 或降低断言强度来通过验收。

## Verification
- 验证顺序必须为：
  1. 语法检查；
  2. 定向测试；
  3. 相关模块测试；
  4. 完整测试集；
  5. 服务级 E2E；
  6. 必要时真实模型验收。
- 测试结果必须绑定真实 commit SHA。
- 没有真实测试输出时，不得声称通过。

## Pull requests
- P0/P1 修改必须通过 Pull Request 合并。
- PR 必须关联 Issue，例如 `Closes #3`。
- PR 必须包含修改摘要、根因、测试结果、未解决问题和 commit SHA。
- CI 未通过时禁止合并。

## Phase gate
- 存在未解决 P0/P1 时，禁止进入下一阶段大型功能开发。
- 合并后必须重新验证 main。
- Issue 只有在 main 验证通过后才能关闭。