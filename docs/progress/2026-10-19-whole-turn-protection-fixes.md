# 2026-10-19 整轮限制与状态查询修复

## 当前结论

**PASS WITH CONDITIONS（本轮修复范围）**：全局修改限制、时间恢复保护、载荷保护和状态查询竞态修复已完成。冻结源码全量回归 **3969 passed、18 skipped**，真实模型 **14 次 chat、556/556 项检查通过**。两条回复原因解释偏差及复杂时长解析边界保留为待改进项，不宣称全部语言表达已正确处理。

基线：`feature/marine-current-fastmcp`，HEAD `d8b8d69f5061a7aaabd1622158483479e800f426`，加本轮工作区修改。开始时工作区干净。本轮未提交、未推送。

## 根因与修改

| 范围 | 修复前实际行为 | 修改 |
| --- | --- | --- |
| 整轮暂停修改 | 模型返回 WRITE 后，“不要修改任务”仍可能写入其他字段；只挡时间或工具不足以保护整个任务 | 新增 `assertion_scope.py` 的完整子句识别，在任务候选、待确认选项写入之前阻止本轮修改；紧急控制与只读路由保留原处理顺序 |
| 暂停时的状态副作用 | 无候选回复仍重新校验，可能改变校验快照、阶段或硬约束拒绝计数 | `slot_filling.py` 在该路径使用已保存约束上下文，不重跑校验；完整快照测试不允许元数据变化 |
| 时间证据恢复 | “不要改任何时间，持续半天两小时”被按后半句恢复为 14 小时；讨论中的绝对时间、区间也可能重新成为候选 | `temporal_parser.py` 在恢复前检查整轮时间限制；统一端点、区间和时长的非断言上下文，保留独立明确时间修改与其他字段 |
| 载荷恢复 | 拆句后丢失全局或工具字段限制；“不要改工具”还可能被当成删除名为“改工具”的工具；“删除A，可以吗？”丢失后半句问句范围 | `payload_mutation.py` 在卡片确认及确定性恢复前区分全局、字段和单操作限制；防止讨论中的连接词生成删除命令；问句按句末边界保护，后续独立肯定句仍可写入 |
| GET 与 reset 竞态 | GET 等待旧 manager 锁期间，reset 可移除或替换 manager；GET 随后返回甚至刷新旧实例 | `routes_chat.py` 获得会话锁后重新核对 manager 身份；失效实例返回 HTTP 409 `SessionReset`，不再读取或刷新 |

没有新增依赖，无 Slot、快照或 TaskIntent schema 变化。成功 API 响应保持兼容；GET 新增的 409 边界已核对前端两处消费者，其 `res.ok`/`data.ok` 检查不会将错误当成正常状态。接口和作用范围见[当前设计契约](../current_design_contract.md)。

新增回归文件为 `test_task_edit_pause.py`、`test_temporal_full_turn_protection.py`、`test_payload_full_turn_protection.py`，并扩展 `test_web_context_lifecycle.py`。覆盖 legacy、TaskPatch v2、normalization v2，模型候选缺失或错误、局部与全局限制、独立肯定修改，以及快照恢复、版本和阶段不变。测试替身结果不计作真实模型调用。

## 回归证据

原始证据目录：`/tmp/seagent-fix-20261019/`。各组用例有重叠，不累计为独立总数。

| 阶段 | 结果 | 日志 |
| --- | --- | --- |
| 全局保护定向 | 36 passed，2 warnings，20.85 秒 | `global/green-safety.log` |
| 全局保护及语义、控制、油田确认、软警告相关 | 137 passed，2 warnings，32.29 秒 | `global/related.log/.xml` |
| 时间最终定向 | 76 passed，2 warnings，14.55 秒 | `temporal/target-frozen.log/.xml` |
| 时间最终相关 | 355 passed，3 subtests，2 warnings，17.39 秒 | `temporal/related-frozen.log/.xml` |
| 载荷最终定向 | 129 passed，2 warnings，62.40 秒 | `payload/targeted-final.log/.xml` |
| 载荷、卡片、恢复、知识证据及来源契约相关 | 475 passed，2 subtests，2 warnings，174.77 秒 | `payload/related-final.log/.xml` |
| GET/reset 竞态定向 | 4 passed，8 deselected，2 warnings，10.14 秒 | `api/green.log` |
| Web 状态、请求重放及前端相关 | 58 passed，6 subtests，2 warnings，17.88 秒 | `api/related.log/.xml` |

上述运行退出 0，无跳过、超时或挂起；warning 为既有 SWIG 弃用提示，退出时另有 `swigvarlink` 提示。红测原始日志保留：GET 4 例失败；全局保护最终红测 12 failed/9 passed；时间首批 42 failed/23 passed，显式区间补充 2 例失败。全局测试的最初正例夹具将合法写入初始化的派生元数据当成异常，已修正为验证所有既有字段，未作为产品缺陷计数。全局暂停仍要求整个槽位快照完全相同。

统一命令前缀（在项目 Python 环境中运行）：

```bash
OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 python -m pytest -q
```

定向参数分别为 `tests/test_task_edit_pause.py`、`tests/test_temporal_full_turn_protection.py`、`tests/test_payload_full_turn_protection.py`。Web 定向参数为 `tests/test_web_context_lifecycle.py -k state_query_rejects_manager_reset`。

全局相关参数：`tests/test_task_edit_pause.py tests/test_llm_semantic_authority.py tests/test_semantic_freedom_boundaries.py tests/test_execution_control.py tests/test_oilfield_context_fail_closed.py tests/test_multi_turn_soft_warning_persistence.py`。

Web 相关参数：`tests/test_web_context_lifecycle.py tests/test_issue_31_ui_state_contract.py tests/test_chat_request_idempotency.py tests/test_frontend_request_lifecycle.py`。

时间相关参数：`tests/test_duration_parser.py tests/test_explicit_time_field_recovery.py tests/test_user_journey_time_regressions.py tests/test_relative_time_parser.py tests/test_time_module_v2_upgrade.py tests/test_time_relation_protocol.py tests/test_real_burial_time_shift.py tests/test_extractor_submodules.py tests/test_src_p0_p1_repairs.py`。

载荷相关参数：`tests/test_payload_write_grounding_regressions.py tests/test_payload_field_label_grounding.py tests/test_issue_13_payload_mutations.py tests/test_payload_editor.py tests/test_payload_editor_intent_boundary.py tests/test_payload_mutation_restoration.py tests/test_onboard_payload_mutation_filtering.py tests/test_multi_payload_normalization.py tests/test_grouped_payloads.py tests/test_query_write_mixed_benchmark.py tests/test_coord_and_payload_normalization_fix.py tests/test_payload_recall.py tests/test_payload_knowledge_evidence.py tests/test_payload_source_contract.py`。

载荷首批红测 60 failed/24 passed，操作级讨论范围补充 3 例失败，讨论连接词补充 6 例失败，跨逗号问句补充 21 例失败，日志均保留。为补齐问句保护，首轮相关运行被主动中断，记录 19 passed、2 warnings、19.33 秒及 KeyboardInterrupt；它不计作相关回归通过或产品超时。

## 真实模型与全量验收

真实验收使用隔离结果目录及遥测副本、Qwen3.5-9B、mock 关闭、MCP 关闭；服务 PID 23715，端口 8892，会话 `fix_oct19_31e7435b6851`。共 14 次 chat，包含 SSE、只读讨论、肯定修改及确定性取消分支，不等同于 14 次模型推理。未发布任务、未执行机器人。

初批第 12 轮后验收脚本退出 1，记录 446/447。该句明确重申“开始时间明确改为09:00”，开始时间值保持原样，但置信度从 1.0 变为 0.95、版本从 1 变为 2；脚本误将它视为完全禁止写入的字段。该失败属于验收假设问题，原脚本、原始输入、响应和失败汇总完整保留。独立离线复核先取得 27/27；随后只针对该明确重申字段允许元数据更新，其他禁止修改轮次仍严格要求字段和版本不变，并重新核验前 12 轮保存的响应。继续同一会话的原计划第 13、14 轮，没有重发前 12 次请求。

最终 **556/556 项检查通过，退出 0**，两段执行累计 261.372 秒（不含中间等待），无 HTTP 错误或超时。原 446/447 不被改写为成功，也不与 556 项相加。汇总见 `real/combined-summary.json`，完整最终检查见 `real/continuation/summary.json`。

- 初次时间禁止修改时，原09:00—12:00及版本保持不变；明确允许“半天两小时”时只将结束时间改为23:00；之后的时间禁止修改轮保留已有09:00—23:00。
- 全局暂停无论在前或在后都保持任务；工具字段暂停仍允许水深依次修改为320、330米；任务编号的局部否定不妨碍明确删除工具。
- 否定删除摄像机与新增声呐可以并存；“删除机械扫描声呐，可以吗？”保持全部字段、槽位版本和阶段；最终取消进入 `rejected` 且草稿清空。
- POST/SSE 与 GET 的字段、槽位及版本、阶段一致。三个结果根目录下 **157 个已有任务文件**及原始/隔离两份遥测文件 SHA-256 保持不变。
- 首次 SSE ping 为9.62毫秒，最终 result 为49.33秒。首事件提前到达不代表完整生成或吞吐已加速。

人工逐轮核验 14 条回复，12 条通过，2 条原因解释有偏差：第 2 轮将未修改归因于缺少明确结束时间，第 3 轮归因于未确认具体数值；实际原因分别是时间域禁止修改和整轮暂停。两条均正确表示未写入、列出原09:00—12:00，未观察到虚报已提交修改。原文及结论见 `real/reply-review-final.json/.md`，这两项仍是回复质量限制。

实际运行命令：

```bash
/root/miniconda3/envs/seagent/bin/python /tmp/seagent-fix-20261019/real/acceptance.py \
  --base-url http://127.0.0.1:8892 --service-pid 23715 \
  --result-dir /tmp/seagent-fix-20261019/service/result \
  --state-file /tmp/seagent-fix-20261019/service/state.yaml
/root/miniconda3/envs/seagent/bin/python /tmp/seagent-fix-20261019/real/continue_acceptance.py
```

脚本含本轮固定日期、会话及 PID，不能直接作为任意环境的通用压测脚本。验收结束后核验 PID、命令、cwd、隔离目录和进程组归属，停止本轮自建服务；已确认 PID 消失、8892 不再监听。没有重启或部署生产服务。

全量命令使用项目 `pyproject.toml` 中全部 testpaths（含两个 MCP 子项目）：

```bash
OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
/root/miniconda3/envs/seagent/bin/python -m pytest -q -ra --maxfail=1 \
  --junitxml=/tmp/seagent-fix-20261019/full.xml
```

全量结果为 **3969 passed、0 failed、18 skipped、23 warnings、595 subtests passed**，pytest 耗时 **925.05 秒（15分25秒）**，退出 0，无超时或挂起。外层进程记录为930.17秒，包含解释器启动与退出，见 `full-run.json`；完整输出为 `full.log/.xml`。

18 项既有跳过分别为17项外部 ROS 比较（需 `SEAGENT_RUN_EXTERNAL_COMPARISON=1` 与 `outside/` 依赖）及1项在线 Copernicus 测试（需 `RUN_COPERNICUS_LIVE_TEST=1`）。本轮没有新增跳过。23条 warning 包括 SWIG 弃用、音频标准库弃用、音频解码 fallback/librosa 弃用，以及既有任务 ID 多进程测试的14条多线程 fork 提示；未隐藏这些提示。

启动前已对 10 个变更 Python 源码及测试文件做 AST 检查并冻结 SHA-256，最终全量与真实验收结束后 **10/10 哈希一致**，见 `frozen-source.json`、`full-run.json`、`static-final.json`。39个文档本地链接存在，`git diff --check` 通过。独立源码和证据复核见 `global/independent-review.md`、`payload/independent-review.md` 和 `report-review.md`；后者审查时全量尚未结束，最终全量结论以上述实际退出记录为准。

## 保留边界

本轮不改变共享模型锁、JSON 总排队、即时取消/reset 或会话 TTL。自然语言保护不是任意语法的完整证明。初始解析器探针仍记录 `1小时或2小时 → 3600秒`、`1小时和半小时 → 3600秒`、`2小时-30分钟 → 9000秒`；这些备选、连接词和负分量边界未在本轮修复，也未做完整任务链验收，需后续明确歧义拒绝与算术语义，不能宣称所有时间边界已解决。原始证据为 `temporal/probe.log`。真实语音、浏览器实际交互、外部执行和部署容量不在本次草稿验收范围。

本轮范围验收达标，保留上述语言质量和未覆盖集成限制。最终工作区为6个生产 Python 文件、4个测试文件、3个文档的预期修改或新增；没有临时调试文件、依赖或配置改动，未提交、未推送。时间数值模块 `duration_parser.py` 本轮未改，本次时间缺陷位于整句限制与候选恢复流程。
