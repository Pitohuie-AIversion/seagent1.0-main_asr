# 贡献指南 (Contributing Guidelines)

感谢参与 SEAgent（深海多 Agent 任务规划与 ASR 交互系统）的开发与维护！为保障代码库质量、系统稳定性和团队高效协作，请严格遵守本指南。

---

## 1. 开发环境准备与配置

1. **环境依赖**：
   - Python 版本：Python 3.10 或更高版本。
   - 基础与测试依赖安装：
     ```bash
     python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
     python -m pip install -e '.[test]'
     ```
   - 若需真实本地模型，安装 GPU 扩展依赖并准备模型文件：
     ```bash
     python -m pip install -e '.[test,gpu]'
     ```

2. **环境变量与离线运行**：
   - 系统支持离线模式运行，开发与测试时推荐设置：
     ```bash
     export TRANSFORMERS_OFFLINE=1
     export HF_HUB_OFFLINE=1
     ```

---

## 2. 分支管理与 Repository Governance 规范

### 2.1 主分支保护规则 (Main Branch Protection)
- `main` 分支受到严格保护，**禁止直接推送**（`git push origin main`）。
- 所有代码变更必须通过 **Pull Request (PR)** 提交。
- PR 合并的强制条件：
  1. **Require Pull Request**：不允许直接提交至 `main`。
  2. **Require CI Status Checks**：普通 PR 运行离线 pytest 全量套件；真实模型、ASR 和 Chrome E2E 由独立 GPU 工作流定时或手动执行。
  3. **Require Conversation Resolution**：所有 Reviewer 提出的 Discussion/Comment 必须标记为 Resolved。
  4. **Prevent Direct Push**：对所有贡献者（包括管理员）生效。

### 2.2 分支命名规范
所有新功能与修复必须基于最新 `main` 分支拉取开发分支。

- **功能特性**：`feature/<feature-name>`
- **问题修复**：`fix/<issue-or-bug-name>`
- **测试增加与重构**：`test/<test-name>`
- **文档更新**：`docs/<doc-name>`
- **代码重构**：`refactor/<component-name>`

### 2.3 提交信息规范 (Commit Message Standard)
- 格式：`<type>(<scope>): <short summary>`
- 常用 `<type>`：`feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `style`
- 示例：
  - `feat(router): add rule fallback for intent router`
  - `fix(task_intent): fix atomic staging publish race condition`
  - `docs(contributing): update verification command suite`
  - `test(frontend): migrate integrity tests to frontend/ directory`

---

## 3. 本地验证与验收命令 (Mandatory Verification Protocol)

在提交 Pull Request 或发起 Code Review 前，开发者必须在本地完整运行以下验证命令集：

### 3.1 语法与编译检查
```bash
python -m compileall -q src tests mcp/ros-mcp mcp/operation-time-window web_backend.py run.py
```

### 3.2 定向前端单元测试
```bash
python -m unittest \
  tests.test_frontend_integrity \
  tests.test_frontend_welcome_message \
  -v
```

### 3.3 定向持久化单元测试
```bash
python -m unittest \
  tests.test_phase1_publish_cleanup_true_closeout \
  -v
```

### 3.4 残留引用与绝对路径检查
```bash
# 1. 检查已删除文件与废弃脚本的残留引用
git grep -n \
  -e 'root_dir / "index.html"' \
  -e 'probe_claim_cleanup' \
  -e 'probe_temp_rollback' \
  -e 'probe_final_exists_staging' \
  -e 'probe_load_snapshot_schema' \
  -e 'probe_lock_blocking'

# 2. 检查硬编码本地路径
git grep -n 'file:///root/'

# 3. 检查未被忽略的临时生成文件
git ls-files | grep -E 'port_forward\.log|chrome_e2e_screenshot\.png'
```

### 3.5 全量自动化测试与 E2E 检查
```bash
# 全量 pytest 套件（离线环境；包含两个 MCP 子项目）
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 python -m pytest -q

# Chrome E2E 端到端自动化测试（需要浏览器和后端）
python tests/run_chrome_e2e.py

# Git diff 空行与格式校验
git diff --check
git status --short
```

---

## 4. Pull Request 提交与 Code Review 流程

1. **提交模板**：完整填写 `.github/PULL_REQUEST_TEMPLATE.md`。
2. **Checklist 确认**：
   - [ ] 基于最新 `main` 分支拉取
   - [ ] 无未解决的 merge conflict
   - [ ] `compileall` 编译通过
   - [ ] 全量 pytest 测试通过；涉及真实模型或浏览器流程时补充对应 E2E 结果或说明环境限制
   - [ ] 未降低核心安全校验与约束规则（特别是 `DialogueManager`, `SlotStore`, `TaskPublishLock`）
   - [ ] 文档与注释已同步更新
3. **Code Review 要求**：
   - 至少 1 位维护者 Approve 方可合并。
   - 核心原则：业务契约不变性、并发落盘安全、只读隔离保护、测试覆盖率。

---

## 5. 核心保护与禁止操作 (Prohibited Actions)

- **禁止吞掉异常**：不得添加无视根因的空 `except Exception: pass`。
- **禁止硬编码跳过测试**：不得通过修测试断言或强行 `skip` 掩盖缺陷。
- **禁止绕过硬约束**：硬约束违反必须阻断发布，不得被 generic confirmation（如“确认/继续”）绕过。
- **禁止非原子持久化**：TaskIntent 发布必须使用 `TaskPublishLock` 与无覆盖硬链接提交；状态、历史文件使用各自已有的锁和原子替换流程。
