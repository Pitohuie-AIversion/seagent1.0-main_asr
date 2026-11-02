# 测试覆盖策略决策记录

原始决策日期：2026-07-28；运行方式核对：2026-09-30。下文保留策略并标注已经变化的执行细节，不表示本轮完成全量或真实模型验收。

## 已批准的决策

采用“平衡方案”：

- `config/constraints.yaml` 是启用约束集合的唯一真源。
- 普通 PR CI 运行离线、确定性的全量单元测试，不加载真实大模型。
- 真实 Qwen、真实 ASR 和 Chrome 链路由独立的手动或定时 GPU 任务运行。
- 所有当前启用约束必须在确定性约束矩阵中拥有直接触发用例；新增约束未补测试时，矩阵守卫必须失败。
- 真实模型积累测试覆盖当前三类任务、七类设备型号中的代表场景，并验证最终确认发布响应。
- 普通测试通过 `tests/runtime_isolation.py` 使用状态文件副本和独立产物目录；手动 HTTP/真实服务测试需另外隔离或恢复服务端状态，不能假定继承 pytest 隔离。
- TS-19 曾按已知回归管理；当前积累脚本未为该用例设置 `expected_failure=True`，按普通用例判定。脚本仍支持其他用例的 `XFAIL/XPASS` 机制；标记移除本身不代表本轮已重跑真实模型。

## 覆盖分层

1. 约束矩阵：验证约束代码、严重级别和精确数值边界。
2. 离线业务回归：验证对话状态、路由、持久化、安全和 API 契约。
3. 真实模型积累测试：验证自然语言抽取、约束提示、多轮修正、设备变体和确认发布闭环。
4. 真实 ASR/Chrome：验证模型加载、音频上传、浏览器交互及前后端集成。

## 运行策略

- PR 门禁与 CI 全量入口：`python -m pytest -q`。
  `pytest` 按 `pyproject.toml` 同时收集核心测试和两个 MCP 子项目；不要用旧的
  `unittest discover` 代替全量验证。
- 真实对话：`python tests/run_accumulation_integration_tests.py`
- 真实 ASR：`python tests/run_real_asr_integration.py`
- Chrome：`python tests/run_chrome_e2e.py`

真实服务运行时应设置独立的 `SEAGENT_RESULT_DIR`，并在积累测试中设置
`SEAGENT_VERIFY_ARTIFACTS=1`，以验证 TaskIntent 与历史快照产物。

## 约束与假设

- 真实测试在独占本地服务和遥测文件的环境中运行。
- 仓库不提交模型权重、密钥或真实运行产物。
- 真实 ASR 需要外部提供有明确期望文本的领域音频夹具。
- 原决策的实施范围为测试及测试运行基础设施；当前文档修订不改变业务规则。
