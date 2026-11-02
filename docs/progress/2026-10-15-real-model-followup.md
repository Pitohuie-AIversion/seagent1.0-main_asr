# 2026-10-15 GPU 恢复后的真实对话复测

本轮将工作方式切回“真实模型复现 → 根因修复 → 原句重放 → 回归与归档核验”。服务器已识别两张 NVIDIA vGPU-32GB；Qwen3.5-9B 在 GPU 0 推理，ASR 模型在 GPU 1 加载。实际对话通过 `/api/chat` 发出，每轮另外读取 `/api/session/state`，同时检查回复、字段、阶段和操作权限。

测试服务使用独立结果目录和 `config/state.yaml` 的原样副本；`OFFLINE_MOCK=0`、`SEAGENT_OFFLINE_MOCK=0`。MCP 关闭，未来计划保存成功只表示 `SCHEDULED`，不代表机器人已经执行。没有修改设备遥测或放宽约束。

## 修复内容

| 问题 | 根因与修复 | 验证重点 |
| --- | --- | --- |
| 型号 `75HP` 被识别为 75 小时，明确结束时间丢失 | 英文单位 `h` 缺少字母边界；时长解析补齐边界并保留 `1h30min` 等紧凑写法。 | 完整首轮 09:00—12:00、75HP、摄像机与前视声呐。 |
| “携带工具为…”被截成非法工具名称 | 添加命令只消费“携带”；现在消费完整字段前缀。 | 三种运行模式、完整首轮、未知工具拒绝。 |
| “携带工具和支持船我还没决定”误报添加非法“工具” | 把尚未决定的字段主语误当命令；待定分句阻止模型载荷提案，其他分句的明确编辑仍生效。 | 未决定字段保持缺失，既有载荷不被覆盖，无伪造载荷错误。 |
| “原有载荷保持不变”仍被模型提案改写 | 保留语义未进入显式操作保护；现在抑制冲突的候选与列表变更。 | 深度修改成功，载荷保留；独立明确添加仍有效。 |
| 否定、假设和疑问中的时间增量被执行 | 增量识别缺少语境边界；新增分句证据过滤，并抑制由错误增量推导的候选。 | “不要把开始时间推迟半小时”、独立肯定结束时间修改。 |
| 显式时间被模型漏抽后无法计算，或冲突被覆盖 | 原逻辑依赖模型候选；现在从明确字段标签及自身日期/时刻恢复证据，交由现有时间引擎检查。 | “时长加1小时、结束改15:00”拒绝冲突并保留原时间。 |
| “一个半天两小时”被截断 | 半单位分支提前返回，且模型可能只返回“两小时”；组合解析累计所有单位，肯定的完整持续时长分句优先。 | 38 小时，10/17 09:00 → 10/18 23:00。 |
| 支持船指导文案被删除，或未写入的状态被宣称已保存 | 原过滤依赖过宽动词和标题去重；按分句区分指导与事实，真实回执按完整内容生成。 | 保留“请选择”“尚未选择”；移除虚构保存、拒绝变更时显示真实保留值。 |
| 模型把高清摄像机说成机载标配 | 提示词和知识查询指导本身含错误示例；改为依据当前设备的机载/选配清单，回复层校正明确相反的分类。 | 高清摄像机属于选配，前视声呐属于机载；正确说明、建议和问句保留。 |
| Markdown 回执或自由正文的错误水深与真实值并存 | 旧回执清洗未覆盖加粗/标题/多行字段；增加块边界处理，并按已提交或已保存水深校正明确数值断言。 | 错误 5000 改为真实 300；保留后续指导、问题、示例、设备额定深度及前后值说明。 |

生产改动限于 `src/temporal/{duration_parser,temporal_parser}.py`、`src/handlers/{payload_mutation,write_reply_grounder,slot_filling}.py`、`src/extraction/prompts.py` 和 `src/knowledge/query_executor.py`。公共 HTTP API 与持久化 schema 没有变更。

75HP 的实际配置中，高清水下摄像机可选配，前视声呐是机载设备；当前额定最大水深为 **600 米**。完整混合载荷请求应保存摄像机、滤除额外挂载中的前视声呐，不能将二者都拒绝或允许任意工具。

## 真实模型证据

证据根目录：`/tmp/seagent-real-gpu-return-20261015-cii22vxh/`。

修复前的原始 HTTP 记录保留了摄像机分类错误、载荷待定误报、开始时间漏抽、组合时长截断和时间冲突覆盖。修复后已完成一轮核心流程及边界复测：

- 完整首轮保存结束时间、摄像机、支持船；无错误时长冲突。
- 改水深并要求载荷不变，载荷保留；否定时间推迟也没有改变时间。
- 矛盾时间输入被拒绝，原 09:00—12:00 保留；明确改为 09:00—13:00 后正常保存。
- 忽略软警告只进入确认阶段，另行确认发布才进入 `done`；重复确认不新增或改写任务文件。
- 缺载荷/支持船只提示待补充，没有虚构保存或非法“工具”错误。
- 5000 米请求触发 C004 硬阻断；“确认”和“忽略这些警告，继续”均不能绕过。
- 组合时长正确保存为 38 小时；边界 5 轮共 38 项检查通过，POST/GET 状态一致。

上述一轮的核心记录在 `core-final.log` 和逐轮 UUID JSON 中，边界记录在 `boundaries-final/`。核心脚本首次将已有 API 的 `ros2_dispatch.state` 错写为 `status`，在发布后产生辅助脚本 KeyError；已更正测试脚本，并单独补完重复确认及文件哈希断言。此错误没有修改产品 API，不计作产品故障或脚本完整通过。

最后的自由文本水深保护加入后，已再次重启服务。**最终源码的核心 8 轮和边界 5 轮全部通过，共 13 轮真实服务交互**。核心脚本退出码为0，记录在 `core-acceptance.log`、`core-final-summary.json`；边界 38 项断言见 `boundaries-acceptance/verification.json`。最终任务为 `PI-20261015-002`，归档编号 `TI2026101507`，时间 10/17 09:00—13:00，水深330米；重复确认前后所有任务文件的数量和 SHA-256 一致。

最终隔离归档 `TI2026101507` 已通过 19 项检查：v2 schema 使用 `kb.task_schemas`，任务/历史/槽位 ID 一致，09:00—13:00、330 米、摄像机和海洋石油681一致，状态为 `done / SCHEDULED / pending_runtime_validation`。复制到独立目录恢复后仍只读，原任务与历史 SHA-256 未改变。证据为 `persisted-verify-TI2026101507.json`（此前 `TI2026101504` 的同类报告也保留）；可复用脚本为 `verify_persisted_task.py`。

## 代码回归

下表是各阶段的真实 pytest 输出，重叠用例不重复累计为总数。pytest 使用脚本模型替身的用例不计为真实模型推理。

| 阶段 | 结果 | 证据 |
| --- | --- | --- |
| GPU 恢复时的 5 文件定向 | 396 passed，97.93s | `gpu-return-targeted.log/.xml` |
| 载荷待定修复相关 | 212 passed，106.36s | `pending-payload-green.log/.xml` |
| 载荷待定新增及 DM 集成 | 12 passed，19 deselected，14.55s | `pending-payload-integration.log/.xml` |
| 时间证据恢复与相关解析 | 117 passed，11.35s | `/tmp/seagent-real-gpu-return-20261015-cii22vxh/boundaries/time-recovery-green.log/.xml` |
| 机载分类及 Markdown 回执定向 | 138 passed，12.51s | `gpu-return-reply-targeted.log/.xml` |
| 回复与相邻模块 | 214 passed，101.31s | `gpu-return-reply-related.log/.xml` |
| 加入自由文本水深保护后的定向 | 167 passed，12.43s | `gpu-return-depth-reply-targeted.log/.xml` |
| 自由文本水深保护相关回归 | 214 passed，101.98s | `gpu-return-depth-reply-related.log/.xml` |
| 修复进行中的首轮全量 | 3377 passed，18 skipped，595 subtests，23 warnings，928.82s；不作为最终树验收 | `gpu-return-full.log/.xml` |
| 主动中止的验收尝试 | 为补完已确认的水深错述而中止，退出码2；不计完整通过 | `gpu-return-final-full-aborted.log/.xml` |
| 冻结源码后的最终全量 | **3461 passed，18 skipped，595 subtests passed，23 warnings，922.66s；退出码0** | `gpu-return-final-full.log/.xml` |

除单独标明外，pytest 日志在 `/tmp/seagent-oct15-verification/`。定向/相关运行出现的 2 条 warning 为 SWIG 弃用提示；进程退出时另打印 `swigvarlink` 弃用提示。首轮全量的跳过涉及可选外部 ROS 比较与 live Copernicus，warning 包含 SWIG、音频依赖弃用和 fork 提示。

最后一轮回复定向和相关回归使用相同的线程与离线环境变量：

```bash
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1
/root/miniconda3/envs/seagent/bin/python -m pytest -q \
  tests/test_reply_grounder_prose_preservation.py \
  tests/test_reply_grounder_payload_catalog.py \
  --junitxml=/tmp/seagent-oct15-verification/gpu-return-depth-reply-targeted.xml
/root/miniconda3/envs/seagent/bin/python -m pytest -q \
  tests/test_payload_knowledge_evidence.py tests/test_hsm_handlers.py \
  tests/test_payload_write_grounding_regressions.py tests/test_natural_language_formatting.py \
  tests/test_onboard_payload_mutation_filtering.py \
  tests/test_llm_semantic_authority.py::test_ground_write_reply_suppresses_resolved_end_time_parse_noise \
  tests/test_llm_semantic_authority.py::test_ground_write_reply_deduplicates_existing_recorded_summary \
  --junitxml=/tmp/seagent-oct15-verification/gpu-return-depth-reply-related.xml
```

最终全量使用正常 Python 环境，没有禁用 vLLM 导入或替换生产模型实现：

```bash
OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
/root/miniconda3/envs/seagent/bin/python -m pytest -q -ra --maxfail=1 \
  --junitxml=/tmp/seagent-oct15-verification/gpu-return-final-full.xml
```

在仓库根目录运行。最终运行前后核验 14 个变更 Python 文件的 SHA-256，全部一致；`gpu-return-final-source-check.log` 保存核验结果。最终 JUnit 的 failures/errors 均为0，无超时或挂起。`git diff --check` 通过，14 个变更 Python 文件语法解析通过。新增回归分别覆盖解析器、ParameterExtractor、DialogueManager 三种模式和回复事实校验。

## 范围与后续

- 个别模型回复仍会出现编号不连续、语句不完整或坐标方位符号混用的展示问题，例如缺载荷追问从第2项开始；本轮字段与状态断言通过，文案流畅度仍需后续打磨。
- 本轮检查文本任务交互、保存、约束与归档恢复；语音模型成功加载，不等于已经完成真实音频交互验收。
- 未来任务必须保留执行前校验；没有执行真实机器人下发，也没有用归档成功代替硬件执行证据。
- 此前 2 GiB/无 GPU 阶段的全量进程退出137、分批全量中断均不计通过；此前旧真实任务 `TI2026101501` 只作为历史证据。
- 代码和测试尚未提交或推送。工作区共涉及7个生产 Python 文件、7个测试文件及2份文档，未修改遥测、公共 API 或持久化 schema。

本轮验收为 **PASS WITH CONDITIONS**：上述已确认的状态与回复事实缺陷已修复，最终全量、13轮真实交互和19项归档恢复核验通过；条件是结论仅覆盖本轮列明的文本任务流程，展示文案、真实音频、外部集成及硬件执行仍按上述范围单独验证。隔离测试服务继续运行在8892端口。
