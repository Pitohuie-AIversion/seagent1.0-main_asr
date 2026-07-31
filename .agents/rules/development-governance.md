---
trigger: always_on
---

---

## trigger: always_on

# SEAgent Development Governance Rules

## 1. Purpose

本规则用于约束 SEAgent 的代码修改、缺陷修复、测试、审查、提交、合并和阶段验收。

规则适用于：

* 人工开发；
* AI Agent 开发；
* Bug 修复；
* 安全整改；
* 状态机修改；
* Slot 与 Schema 修改；
* ASR、前端和 API 修改；
* TaskIntent、snapshot、staging、final、quarantine 等持久化修改；
* 后续 Task Graph、任务分配和机器人执行接口开发。

代码、真实测试输出和 Git 证据优先于开发者描述、AI 总结和文档声明。

---

## 2. Branch Rules

### 2.1 禁止直接修改 main

功能开发、Bug 修复、重构和测试增强必须在独立分支进行。

允许直接修改 `main` 的内容仅限于仓库管理员批准的紧急操作。任何紧急操作后仍必须补充 Issue、测试和审查记录。

### 2.2 分支命名

Bug 修复：

```text
fix/issue-<issue-number>-<short-description>
```

功能开发：

```text
feature/issue-<issue-number>-<short-description>
```

测试改进：

```text
test/issue-<issue-number>-<short-description>
```

文档改进：

```text
docs/issue-<issue-number>-<short-description>
```

示例：

```text
fix/issue-3-task-intent-semantics
```

---

## 3. Issue Scope Rules

每个 Issue 只能解决一个可独立验收的问题集合。

开始开发前必须明确：

* Issue 编号；
* 严重程度；
* 当前问题；
* 根因或待验证假设；
* 涉及文件；
* 必须保持不变的行为；
* 非目标；
* 禁止采用的方案；
* 必须新增或修改的测试；
* 验证命令；
* 验收标准。

禁止在修复某个问题时顺手修改无关模块。

如调查发现修改范围必须扩大，应先更新 Issue，说明原因、影响范围和新增测试，再继续修改。

---

## 4. Severity Rules

缺陷严重程度统一使用：

### P0

包括但不限于：

* 数据丢失；
* 错误任务发布；
* 安全约束被绕过；
* 文件损坏；
* 持久化失败却报告成功；
* 核心主流程不可用；
* 并发导致任务覆盖或状态丢失。

### P1

包括但不限于：

* DialogueManager 状态机错误；
* Slot 有效值被错误覆盖；
* 普通 LLM 对话或 ASR 主路径回归；
* 硬约束和软约束语义混淆；
* API 契约明显错误；
* 主要功能在常规输入下失败。

### P2

包括但不限于：

* 边界条件；
* 可恢复性不足；
* 日志和可观测性不足；
* 测试覆盖不足；
* 配置耦合；
* 可维护性问题。

### P3

包括但不限于：

* 命名；
* 注释；
* 文档；
* 代码风格；
* 不影响行为的轻微重复。

存在未解决 P0 或 P1 时，禁止进入下一阶段的大规模功能开发。

---

## 5. Test-First Bug Fix Rules

### 5.1 修复前必须复现

Bug 修复必须先完成以下至少一项：

* 找到现有失败测试；
* 新增一个能够在旧代码上稳定失败的测试；
* 提供可重复运行的服务级复现脚本。

测试必须验证真实业务行为，而不是仅检查某一行实现。

### 5.2 必须保留修复前证据

修复前必须记录：

* baseline commit SHA；
* 测试命令；
* 失败测试名称；
* 失败或错误输出；
* failures、errors、skips 和 collection errors 数量。

### 5.3 禁止修改测试掩盖问题

禁止：

* 删除失败断言；
* 降低断言强度；
* 把失败标记为 skip；
* 新增永久 `expectedFailure`；
* 增加测试专用代码分支；
* mock 掉本应验证的核心行为；
* 修改测试预期以接受错误实现。

修复完成后，同一个失败测试必须转为通过。

---

## 6. Required Verification Order

验证必须按以下顺序执行：

1. Python 语法和编译检查；
2. 与本次修改直接相关的定向测试；
3. 相关模块完整测试；
4. 完整测试集；
5. 服务级 E2E 测试；
6. 必要时真实 LLM、ASR 或 GPU 环境验收；
7. Git diff 检查。

基础命令：

```bash
python -m compileall -q src tests
```

```bash
python -m unittest <targeted-tests> -v
```

```bash
python -m unittest <related-module-tests> -v
```

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
python -m unittest discover tests -v
```

```bash
git diff --check
git status --short
git diff --stat
```

没有真实测试输出时，只能写：

```text
实现看起来合理，但尚未完成验证。
```

不得写：

```text
已通过验收。
```

---

## 7. Critical System Invariants

### 7.1 Ordinary Conversation

普通 LLM 对话、任务对话和 ASR 输入必须共存。

不构成明确任务的输入不得强制进入：

* Slot 收集；
* 约束检查；
* 确认发布；
* TaskIntent 构建。

### 7.2 DialogueManager

`DialogueManager` 必须作为状态机处理。

修改时必须检查：

* 修改前 phase；
* 用户事件；
* 转移条件；
* 修改后 phase；
* 是否需要保留 `old_phase`；
* phase 是否被提前覆盖；
* 同一输入是否被多个处理器重复处理。

硬约束不得通过以下输入绕过：

```text
确认
继续
忽略警告
没问题
OK
```

忽略软警告和最终确认发布必须是两个独立事件。

### 7.3 Slot

验证失败不得覆盖已确认的有效值。

`candidate_value` 未通过验证前不得提升为正式 `value`。

Slot 序列化和反序列化必须保持对称。

修改 Slot Schema 时必须考虑旧 snapshot 兼容或迁移。

### 7.4 Persistence

任务持久化必须：

* 先写临时文件；
* 执行必要的 `flush` 和 `fsync`；
* 原子替换或原子无覆盖提交；
* 必要时同步父目录；
* 使用跨进程锁；
* fail closed；
* 保留不确定文件到 quarantine。

禁止：

* 未加锁删除 staging；
* 猜测路径后删除文件；
* 使用不安全的 `stat -> unlink`；
* final 已存在时覆盖正式文件；
* 持久化失败后返回发布成功。

### 7.5 TaskIntent

必须保证：

* 顶层 `task_type` 与 `task.type` 一致；
* robot type 与 KnowledgeBase 中的机器人类别一致；
* API `final_json` 与正式落盘文件一致；
* history 中的最终状态与正式发布内容一致；
* 未知或不一致设备信息 fail closed；
* 重复确认不得重复发布或重写正式文件。

---

## 8. Pull Request Rules

P0 和 P1 修改必须通过 Pull Request 合并。

PR 必须：

* 关联 Issue；
* 使用 `Closes #<issue-number>`；
* 描述根因；
* 描述修改范围；
* 列出修改文件；
* 提供修复前失败测试；
* 提供修复后定向测试；
* 提供完整测试结果；
* 提供 commit SHA；
* 提供 CI 证据；
* 说明未解决问题。

CI 未通过时禁止合并。

PR 中不得包含：

* 无关重构；
* 临时日志；
* 本地绝对路径；
* 密钥、token 或私有配置；
* 模型权重；
* 测试生成的状态文件；
* 未说明的 Schema 变化。

---

## 9. Independent Review Rules

实现者不得独自完成最终验收。

审查应至少分为两个角色：

### Implementation Review

检查：

* 是否修复根因；
* 控制流是否完整；
* 状态变化是否正确；
* 是否存在回归；
* 是否有无关修改；
* 是否存在死代码、重复定义或未生效实现。

### Verification Review

检查：

* 测试是否真实运行；
* 测试是否对应当前 commit SHA；
* failures、errors、skips 和 collection errors；
* CI 是否通过；
* 完整测试和服务级 E2E 是否完成；
* 验收标准是否逐项满足。

审查结论只能使用：

```text
PASS
PASS WITH CONDITIONS
FAIL
BLOCKED
```

---

## 10. Merge and Main Verification

PR 合并后必须重新验证 `main`：

```bash
git switch main
git pull --ff-only origin main
git rev-parse HEAD
python -m compileall -q src tests
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
python -m unittest discover tests -v
```

正式回归报告必须绑定合并后的真实 `main` SHA。

PR 分支测试通过不能替代合并后的 `main` 验证。

---

## 11. Issue Closure Rules

Issue 只有在以下条件全部满足后才能关闭：

* 修复代码已合并；
* CI 已通过；
* main 已重新验证；
* 验收标准全部满足；
* 没有未说明的 P0/P1 遗留；
* commit SHA 和 PR 链接可复核；
* 文档和测试报告已与当前实现同步。

仅提交代码、仅 push 分支或仅创建 PR 均不代表 Issue 已完成。

---

## 12. Phase Gate Rules

进入下一阶段前必须检查：

* 开放 P0 数量；
* 开放 P1 数量；
* 当前 main SHA；
* 完整测试结果；
* 服务级 E2E 结果；
* 真实模型验收状态；
* 普通 LLM 对话回归；
* ASR 路径状态；
* TaskIntent 发布正确性；
* 持久化与恢复机制；
* 已知限制。

只有满足以下条件才能进入下一阶段：

* 开放 P0 为 0；
* 开放 P1 为 0；
* 完整测试零失败、零错误；
* 服务级 E2E 通过；
* 最新验收报告绑定当前 main SHA；
* 核心主流程可以稳定复现；
* 失败路径能够 fail closed。

不得因为计划进度、展示需求或功能扩展压力而降低阶段准入标准。
