# 贡献指南 (Contributing Guidelines)

感谢参与 SEAgent（深海多 Agent 任务规划与 ASR 交互系统）的开发与维护！为保障代码库质量、系统稳定性和团队高效协作，请遵守本指南。

---

## 1. 开发环境准备

1. **环境依赖**：
   - Python 版本：Python 3.10 或更高版本。
   - 依赖安装：
     ```bash
     pip install -r requirements/test.txt
     ```
   - 若需本地运行 ASR 语音识别服务，需额外安装 GPU 依赖：
     ```bash
     pip install -r requirements/gpu.txt
     ```

2. **环境变量与离线运行**：
   - 系统支持离线模式运行，开发与测试时推荐设置：
     ```bash
     export TRANSFORMERS_OFFLINE=1
     export HF_HUB_OFFLINE=1
     ```

---

## 2. 分支管理与开发流程

### 2.1 分支创建规范
- 主分支 `main` 受到保护，禁止直接推送（`git push origin main`）。
- 所有新功能与修复必须基于最新 `main` 分支拉取开发分支。
- **分支命名格式**：
  - 功能特性：`feature/<feature-name>`
  - 问题修复：`fix/<issue-or-bug-name>`
  - 测试增加与重构：`test/<test-name>`
  - 文档更新：`docs/<doc-name>`
  - 代码重构：`refactor/<component-name>`

### 2.2 开发与提交要求
1. **基线确认**：开始修改前，请先运行本地全量测试，确保基线通过。
2. **单一职责 PR**：一个 PR 应聚焦解决一个主要问题或实现一个独立特性。
3. **提交信息规范 (Commit Message)**：
   - 格式：`<type>(<scope>): <short summary>`
   - 示例：
     - `feat(router): add rule fallback for intent router`
     - `fix(task_intent): fix atomic staging publish race condition`
     - `docs(testing): update unit test run instructions`

---

## 3. 测试与验证标准

在提交 Pull Request 前，必须在本地完成以下命令验证：

1. **Python 语法与编译检查**：
   ```bash
   python -m compileall -q src tests
   ```

2. **全量单元测试**：
   ```bash
   python -m unittest discover tests
   ```

> **注意**：PR 不得降低 [src/task_intent_builder.py](file:///root/mzy/seagent1.0-main_asr/src/task_intent_builder.py) (`TaskPublishLock`)、[src/slot_store.py](file:///root/mzy/seagent1.0-main_asr/src/slot_store.py) (`SlotStore`) 或 [src/intent_router.py](file:///root/mzy/seagent1.0-main_asr/src/intent_router.py) (`IntentRouter`) 的安全校验逻辑。新增逻辑必须包含对应的单元测试。

---

## 4. Pull Request 提交与 Review

1. **提交模板**：请完整填写 [.github/PULL_REQUEST_TEMPLATE.md](file:///root/mzy/seagent1.0-main_asr/.github/PULL_REQUEST_TEMPLATE.md) 要求的各个栏目。
2. **Checklist 确认**：
   - [ ] 分支基于最新 `main`
   - [ ] 无未解决的 merge conflict
   - [ ] `compileall` 编译通过
   - [ ] `unittest` 全量回归测试通过
   - [ ] 未降低核心安全校验与约束规则
   - [ ] 文档已同步更新
3. **Code Review 要求**：
   - 至少需要 1 位核心维护者 Review 并 Approve 后方可 Merge。
   - 评审重点：业务契约不变性、并发落盘安全、状态只读保护、测试覆盖率。

---

## 5. Merge 与后续处理

- 合并方式推荐采用 **Squash and merge** 或 **Rebase and merge**，保持 `main` 分支历史干净透明。
- PR 合并后，请及时删除远端及本地的功能分支：
  ```bash
  git branch -d <branch-name>
  git push origin --delete <branch-name>
  ```
