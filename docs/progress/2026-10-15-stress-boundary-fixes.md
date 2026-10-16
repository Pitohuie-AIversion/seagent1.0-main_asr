# 2026-10-15 压力与边界问题修复

## 当前结论

**PASS WITH CONDITIONS（本轮修复范围）**：组合半时长、载荷保留/增删、有限请求重放及实时进度的修复完成。最终冻结源码全量回归3724 passed、18 skipped；真实GPU服务20次请求162/162项断言及独立归档恢复31/31项检查通过。共享模型吞吐、即时取消/重置及会话回收仍是后续优化，不在本轮通过范围。外部集成测试的跳过条件及其余限制见下文。

本记录对应[修复前压力报告](2026-10-15-stress-boundary-followup.md)发现的问题。验证版本为`feature/marine-current-fastmcp`、HEAD `c58c28f95f5559c39a76d464aa223e9fa7c93fc5`加当前未提交修改；使用本轮结果，不沿用历史全量测试结论。

## 修复内容

| 问题 | 根因与修改 | 主要保护 |
| --- | --- | --- |
| “半天两小时”变成2小时 | `duration_parser.py`组合扫描支持独立“半”，并限制后缀“半”的下一单位边界 | 14小时、45分钟、40秒的组合分别正确；一天半小时、两小时半分钟不重叠消费；75HP仍不是75小时 |
| 模型将否定/假设时长当成SET | `temporal_parser.py`将非断言保护扩展到SET关系及模型推导时间 | 保留独立明确结束时间和其他字段；问句不写时间槽位 |
| 保留A并删除B漏删 | `payload_mutation.py`先保护完整句子的否定/问句，再拆明确的保留与操作子句 | 两种语序等价，第三项保留；同项保留/删除冲突澄清；未知项继续事务校验和回滚；真实复测补齐“新增”和“现有” |
| 同ID重发增量执行两次 | 新增`chat_request_cache.py`；JSON/SSE在同一会话锁内共用处理、快照冻结和结果重放 | 跨接口/并发重复只处理一次；同ID不同消息409；失败结果同样保留，避免部分修改后的重复执行 |
| SSE等待整轮完成后才发首事件 | 新增`chat_stream.py`后台处理，先发ping/queued，再发送阶段进度和心跳 | 槽位、警告、回复和最终结果仍等事务完成；断连不中断已接受事务；最多8个流式worker、64条进度缓冲 |
| 前端请求标识和繁忙提示 | 每次send生成ID，SSE/JSON回退沿用该ID，503读取明确错误提示 | 中断不自动重发；不同用户操作使用不同ID |
| 异常Unicode破坏重放记录 | 请求在manager查找前严格验证UTF-8；缓存使用JSON转义；SSE在worker内验证可编码性 | 无效输入400且无状态修改；结果编码错误不在发送部分成功内容后才暴露 |

实现复用现有规则、校验器、会话锁和持久化事务，没有新增依赖。生产改动范围为两个时间模块、载荷操作模块、聊天路由、两个新增Web模块及前端请求函数。原有未提交修复全部保留，没有无关重构。

## 接口与持久化边界

成功响应字段及SSE事件类型保留兼容，新增`RequestIdConflict`、`RequestReplayUnavailable`及流式容量503 `ServerBusy`；无持久化/Slot schema变更。具体契约已补充到[当前设计契约第9节](../current_design_contract.md#9-http-retry-and-streaming-boundaries)。

重放保护仅覆盖当前manager中尚未过期或淘汰的记录：最多10分钟、128条、4 MiB。重启、重置、manager替换或缓存淘汰后不提供持久幂等保证。重放返回原始快照，最新状态需GET核对；不能靠旧请求重放回滚后续修改。过大的结果保留阻止重做的标记。

流式提前输出仅表示接收、排队和处理进度，不表示字段已保存。最终回复依旧经过校验后分块发送，不是直接向用户展示模型未校验token。reset继续等待会话锁；共享模型锁仍保留，普通JSON接口的总队列/会话TTL尚未改造。这些保留边界不应被描述成已解决吞吐或即时取消问题。

## 回归证据

证据根目录为`/tmp/seagent-fix-stress-20261015/`。定向与相关测试使用脚本模型替身覆盖真实解析、状态机及事务，不能计作真实模型调用。

| 阶段 | 真实结果 | 日志 |
| --- | --- | --- |
| 时间定向 | 144 passed，2 warnings，14.10秒 | `duration/green-targeted.log` |
| 时间相关 | 211 passed，3 subtests，2 warnings，16.10秒 | `duration/green-related.log` |
| 载荷新增定向 | 141 passed，177 deselected，2 warnings，79.19秒 | `payload/green-targeted-final.log` |
| 载荷精确名称/共享别名补充 | 24 passed，297 deselected，2 warnings，26.60秒 | `payload/alias-targeted.log` |
| 载荷首批相关 | 407 passed，2 subtests，2 warnings，179.31秒 | `payload/related.log/.xml` |
| 真实复测补修定向 | 45 passed，321 deselected，2 warnings，34.48秒 | `payload/followup-green.log` |
| 真实复测补修相关 | 110 passed，2 warnings，57.88秒 | `payload/followup-related.log` |
| Web/前端整合定向 | 104 passed，4 subtests，2 warnings，25.08秒 | `api/final-targeted.log/.xml` |
| 流式容量路由补充 | 1 passed，2 warnings，10.17秒 | `api/capacity-route.log` |
| API/持久化/槽位一致性相关 | 172 passed，2 warnings，91.13秒 | `api/related-final.log/.xml` |
| 流式及上下文等相关8文件 | 121 passed，6 subtests，2 warnings，32.20秒 | `stream/related.log` |

上表存在用例重叠，不累计为总用例数。上述运行无失败或超时；warning为既有SWIG弃用提示，进程退出另有`swigvarlink`提示。红测分别复现载荷69例失败、请求重放11例失败、Unicode7例失败；时间数值与集成共15例产品失败，另修正一项夹具未声明允许字段的问题。测试夹具修正未作为产品修复成果。

执行统一前缀：

```bash
OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
/root/miniconda3/envs/seagent/bin/python -m pytest -q
```

时间定向为`tests/test_duration_parser.py tests/test_explicit_time_field_recovery.py tests/test_user_journey_time_regressions.py`；相关为`tests/test_relative_time_parser.py tests/test_time_module_v2_upgrade.py tests/test_time_relation_protocol.py tests/test_real_burial_time_shift.py tests/test_extractor_submodules.py tests/test_src_p0_p1_repairs.py`。

载荷相关为`tests/test_payload_write_grounding_regressions.py tests/test_payload_field_label_grounding.py tests/test_issue_13_payload_mutations.py tests/test_payload_editor.py tests/test_payload_editor_intent_boundary.py tests/test_payload_mutation_restoration.py tests/test_onboard_payload_mutation_filtering.py tests/test_multi_payload_normalization.py tests/test_grouped_payloads.py tests/test_query_write_mixed_benchmark.py`。

Web最终定向为`tests/test_chat_request_validation.py tests/test_chat_request_cache.py tests/test_chat_request_idempotency.py tests/test_sse_live_progress.py tests/test_sse_stream_api.py tests/test_llm_context_budget.py tests/test_frontend_request_lifecycle.py`，另运行`tests/test_chat_request_idempotency.py::test_busy_stream_is_rejected_before_processing_and_can_retry_same_id`。API相关为`tests/test_api_user_input_regressions.py tests/test_dispatch_web_persistence.py tests/test_slot_consistency.py tests/test_chat_request_validation.py`。

Node原生生命周期用例和`node --check frontend/js/index.js`通过；23个变更Python文件AST解析通过。25个变更源码/测试文件已保存SHA-256，最终回归必须在同一组文件上完成。

## 首次复测及补修记录

首次真实批次在第6轮“新增机械扫描声呐，保留现有高清水下摄像机。”失败，实际仍只有摄像机。前5轮已验证组合时长、否定/问句及保留后删除；这不构成完整验收。该批次共6次chat、47/48项断言通过，未发布，证据保留于`real/`。

根因是添加、否定和兜底词表遗漏“新增”，保留操作数前缀也遗漏“现有”。同步5处词表/前缀后，新增45个三管线用例从39失败、6通过转为全部通过，并通过110项相关回归。原失败输入保留到下一批完整验收中。测试夹具最初使用全空抽取对象而违反模型响应协议，已在红测前修为合法的未解析响应；此夹具问题不计产品缺陷。

发现该问题时，首轮全量pytest由测试负责人主动中断，以避免继续验证将被修改的版本。它以exit 2结束，记录1970 passed、23 warnings、330 subtests、477.52秒及KeyboardInterrupt；这不是全量通过或产品超时。证据`full.log/.xml`保留，最终全量使用独立的`full-final.log/.xml`和重新冻结的`final-source.sha256`。

## 最终验收

全量命令：

```bash
OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
/root/miniconda3/envs/seagent/bin/python -m pytest -q -ra --maxfail=1 \
  --junitxml=/tmp/seagent-fix-stress-20261015/full-final.xml
```

全量结果：**3724 passed，0 failed，18 skipped，23 warnings，595 subtests passed**，1019.78秒（16分59秒），退出0。最终运行无超时或挂起。18项跳过来自17项外部ROS比较（需`SEAGENT_RUN_EXTERNAL_COMPARISON=1`与`outside/`依赖）及1项在线Copernicus测试（需`RUN_COPERNICUS_LIVE_TEST=1`），不是为规避失败新增的跳过。

23条warning包括SWIG弃用、Python音频标准库弃用、音频解码fallback/librosa弃用，以及既有任务ID多进程测试中的14条多线程fork提示；退出时另有`swigvarlink`提示。这些提示未被隐藏，详见`full-final.log`。最终`final-source.sha256`的25个源码/测试文件全部匹配，保证全量、真实复测和报告对应同一代码版本。

独立只读Web审查未发现新增阻断问题，审查证据为`api/independent-review.md`；真实验收脚本的审查提出归档绑定、完整GET与槽位版本等补核点，已纳入下面31项独立核验。生产任务目录未建立修复前后全量哈希基线的限制继续保留。

最终真实批次使用Qwen3.5-9B、端口8892、mock关闭、MCP关闭，服务PID31214，session `fix_final_2acd9d7262ec`。独立目录`real-final/`保存全部原始请求、SSE事件、GET回读和检查结果。20次chat完成，**162/162项断言通过**，耗时231.05秒，退出0；其中19次应用成功、1次为预期的请求ID冲突409（通过SSE error传输，HTTP连接本身为200）。无传输错误或超时。20次是对话接口请求数，包含缓存重放及确定性控制分支，不等于模型推理调用数。

- 09:00开始的“半天两小时”保存到23:00；否定时长只修改独立水深；假设问句不改时间。
- “保留摄像机并删除声呐”、原失败句“新增机械扫描声呐，保留现有高清水下摄像机。”及反向删除/保留均保存正确。
- 延长一小时后，JSON与SSE重放返回完全相同的原始结果，状态、槽位版本及历史不再增长；相同ID不同消息返回`RequestIdConflict`。HTTP/SSE并发的相同ID只延长一次半小时，历史仅增加一条对应用户指令。
- 601米触发C004硬阻断；“忽略这些警告，确认发布”仍无新任务。改回300米且明确忽略软警告后，两条并发确认只生成一份任务，身份及最终字段一致。
- 隔离新任务为`PI-20261015-004` / `TI2026101523`，时间10/17 09:00—12:00，摄像机单载荷，状态`done / SCHEDULED / pending_runtime_validation`。这验证未来计划归档，没有执行机器人。

首轮SSE响应头6.43毫秒、首ping9.11毫秒、queued9.16毫秒、真实阶段进度9.19毫秒；最终delta49.4629秒、result49.4664秒、流结束49.4670秒。首进度已提前到达，最终回复仍在整轮完成后发送；此单次观测不能证明模型吞吐提升，完整处理耗时仍约49秒。

真实批次结束时，隔离`result/task`内原有3份任务SHA-256全部不变；原配置及隔离`state.yaml`均保持`31e383abad4bae1e25402971317888eb7da6f230f371d3ecee8966ed855d344f`。旧任务哈希断言范围仅为该隔离目录，未声称建立生产任务目录的修复前后全量哈希基线。

随后独立只读核验与隔离恢复**31/31通过**，退出0：两次发布响应及最终GET的字段、身份、phase/done、dispatch、完整ui_state一致，并与历史快照及任务文件对应；校验v2 schema、时间、水深、载荷、坐标、支持船、唯一归档及槽位值。假设问句的全部字段和槽位版本均不变。复制到新的临时目录后，使用禁止调用模型的恢复替身加载快照，保持done、只读、不可再发布；原任务/history哈希不变。证据为`persisted-verify-TI2026101523.json`与`persisted-verify.log`。

真实验收和独立恢复的执行命令：

```bash
/root/miniconda3/envs/seagent/bin/python -u \
  /tmp/seagent-fix-stress-20261015/real-final/acceptance.py

OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
/root/miniconda3/envs/seagent/bin/python \
  /tmp/seagent-fix-stress-20261015/verify_persisted_task.py \
  --intent-id TI2026101523 \
  --result-dir /tmp/seagent-real-gpu-return-20261015-cii22vxh/result \
  --depth 300 --acceptance-dir /tmp/seagent-fix-stress-20261015/real-final
```

上述脚本记录固定日期及本机隔离路径，重新运行前需要适配环境，不能当作任意生产地址的通用压测命令。真实语音、浏览器实际操作、外部机器人执行和生产部署容量不在本轮验收范围。

本轮尚未提交或推送，工作区仍包含此前未提交修改。
