# SEAgent 真实模型多轮对话验收记录

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-09-26 |
| 项目 | SEAgent 水下机器人任务收集与准入系统 |
| 范围 | 管缆巡检、管缆埋设、采油树控制面板插入与拔出；知识问答、修改、软警告确认与未来任务发布 |
| 环境 | Python 3.12 / seagent；本地 Qwen3.5-9B；Qwen ASR 已加载；模型文件离线使用 |
| 隔离服务 | `http://127.0.0.1:8892`，`ENABLE_MCP=0`，无真实机器人桥接；结果、历史、状态副本及日志位于 `/tmp/seagent-real-dialogue-audit` |
| 当前状态 | 已完成主要流程复验；自然警告确认、知识否定句及后续载荷/支持船修复等待新实例复验；采油树动作归档与协议发现待修问题。本文为阶段验收记录，最终结果将按后续实测补充 |

## 1. 结论与问题记录

本轮按实际用户的自然表达操作真实本地模型，并逐轮比对回复、`/api/session/state`、约束状态和发布产物，不将 HTTP 200 单独作为通过依据。已发现“回复声称操作成功但后台状态未变化”、知识问答上下文超限，以及只读问题误进入编辑流程等问题。

已有全量执行记录为 **2907 passed、18 skipped、532 subtests passed**。该全量检查发生于本轮后续补丁之前，后续修复另有定向回归，不能将早先全量结果表述为覆盖全部最后变更。

| 复现问题 | 修复与当前验证情况 |
| --- | --- |
| 100 米管缆埋设任务询问机器人建议，回答列出全系统设备，包含无埋设能力的 AUV/观察级设备 | 依据当前任务能力和水深筛选，真实重放只列履带 1600HP、拖曳 1500HP、特种 600HP；没有替用户选择设备或写入槽位 |
| 采油树作业询问通用工作级设备适配性，被回答为未知设备 | 以完整设备身份和任务能力分别判断；修复后真实插入/拔出问答证据保留在 `tree.jsonl` |
| “把开工时间推迟半小时，作业时长仍保持两小时”被回复为已更新，但时间仍为 09:00—11:00 | 根据当前原句解析时间平移及持续时长，真实重放为 09:30—11:30；其他已填参数不变 |
| C032 未来排期提示被解释为设备异常，或无法读取当前警告 | 将活动警告提供给路由及只读状态回答；真实警告解释保留 `blocked_soft`，说明提示不代表机器人损坏 |
| 知识问答全量证据使输入超过 16384 token，接口返回误导性的槽位校验 400 | 使用实际分词器预留输出预算；先移除旧完整对话轮次，再无损压缩知识 JSON，必要时选择完整相关证据记录并声明证据不完整。真实服务已走缩减路径并返回 200 |
| 必需上下文仍无法容纳时可能被当作普通校验失败，或被路由吞成正常澄清 | 新增 `ContextBudgetError`，同步接口返回专用 503，SSE 返回专用 error；保留 request_id 与 retryable=false，并回滚本轮槽位、阶段和警告确认状态。定向测试通过 |
| 自然长句接受软警告时，回复称已确认，但后台仍为 `blocked_soft` | 路由协议要求显式生成 warning_action，避免该可选字段缺失；新补丁定向回归通过，真实原句待最终重启复验 |
| “只查询知识，不创建或修改任何任务”与载荷词共同出现，被快捷入口直接打开载荷配置卡片 | 快捷入口收窄为独立编辑指令；否定、问答和混合表达交给正常语义流程。定向回归通过，原句待最终重启复验 |
| 采油树插入/拔出动作归档丢失 | 历史状态区分插入/拔出，但 task_intent 均为 valve_operation，details 缺少 operation，ROS 映射固定 INSERT_PLUG。正在修复并准备归档及协议回归；本轮 MCP 禁用且仅 SCHEDULED，未实际派发 |
| 采油树载荷替换中混入设备本体自带工具或未真正携带的工具 | 依据原文携带/带上动词和否定条件约束列表修改；171 项回归及 2 个子测试通过，机载工具原句待最终真实复验 |

## 2. 处理边界与实现依据

任务发布仍由既有槽位完整性、软硬约束及最终确认控制。设备查询只提供配置支持的候选及适配依据；不因查询或建议改动当前任务。C032 属于未来排期的延后校验提示，不能据此推断设备故障、设备完全就绪或当前任务已发布。

上下文预算使用真实引擎的 `max_model_len` 和分词器计量，同时预留本次输出上限。系统指令、当前任务事实和最新完整用户输入保留；知识结果按完整记录重新序列化，保留 JSON 结构，并标记 `partial` 与省略条数。无法安全收敛时明确失败，不截断用户原句或结构化候选，不自动发布任务。

时间处理区分“开始时间平移”和“任务持续时长”。当前轮明示的持续时间优先于旧时长，真实非法的倒序结束时间仍由现有时间校验拒绝。本轮未变更系统时钟、实时遥测或网关配置。

## 3. 模块与接口对应

| 模块 | 相关职责或接口 |
| --- | --- |
| `src/handlers/grounded_catalog.py` | 当前任务设备选型、设备与任务适配问答 |
| `src/session/intent_router.py`、`src/llm_client.py` | 活动警告语义、结构化 warning_action、上下文预算与专用异常传播 |
| `src/handlers/telemetry_status.py`、`src/extraction/prompts.py` | 当前警告的只读解释及事实边界 |
| `src/knowledge/prompt_grounder.py`、`src/knowledge_retriever.py`、`src/handlers/slot_filling.py` | 未来任务背景证据排除旧实时遥测，保留静态设备资料 |
| `src/temporal/duration_parser.py`、`src/temporal/temporal_parser.py` | 相对时间平移与持续时长保留 |
| `src/handlers/payload_mutation.py` | 载荷编辑入口与列表替换语义 |
| `src/handlers/write_reply_grounder.py`、`src/handlers/constraint_decision.py` | 以实际写入和当前约束校正用户可见回复 |
| `src/dialogue_manager.py`、`src/exceptions.py` | 上下文预算异常的本轮事务回滚 |
| `src/web/routes_chat.py` | `/api/chat`、`/api/chat/stream` 专用失败响应及状态查询 |

## 4. 真实流程与原始证据

所有路径均以 `/tmp/seagent-real-dialogue-audit/` 为基准。JSONL 中同时保留失败复现和修复后重放，须依据 case/label/session_id 区分结果。

| 流程 | 已观测结果 | 证据 |
| --- | --- | --- |
| 埋设创建 | `pipeline_burial`，电力电缆、100 米、次日 09:00—11:00，继续询问缺失字段 | `burial.jsonl`：`fixed_create` |
| 埋设设备建议 | 仅列 3 款具备埋设能力且满足 100 米的型号；查询前后 collected 完全相同 | `fixed_recommend` |
| 埋设设备与坐标 | 选定 `CRAWLER-1600-001`；起点 19.8/113.5，终点 19.81/113.51 | `fixed_select_robot_and_route` |
| 自然时间修改 | 09:00—11:00 改为 09:30—11:30；设备、坐标、水深等保持不变 | `fixed_shift_time` |
| 埋设补全 | 机械切割开沟模块、海床地质探测设备、海洋石油681；必填字段完整，仅 C032 软警告 | `fixed_complete` |
| 埋设短语确认与发布 | “忽略软警告”进入 confirming；“确认发布”进入 done，派发状态 SCHEDULED | `fixed_explicit_acknowledge`、`fixed_publish` |
| 埋设发布产物 | `PB-20260926-002` / `TI2026092608`；计划次日 09:30，无即时机器人下发 | `result/task/task_intent_TI2026092608.json` 与 `result/history/history_TI2026092608.json` |
| 采油树插入 | 创建、设备适配问答、井口/结束时间修改、补全及发布；任务 `CT-20260926-001` / `TI2026092606`，SCHEDULED | `tree.jsonl`，具体后续修正以最终重放为准 |
| 采油树拔出 | 创建、设备能力询问、井口和结束时间修改、补全与发布；`CT-20260926-003` / `TI2026092609`，SCHEDULED；模型漏记支持船后，用户单独确认可恢复 | `tree.jsonl` |
| 巡检警告解释 | 已补全任务保持 blocked_soft；明确说明 C032 不代表机器人损坏，未忽略或发布 | `root.jsonl`、`root-verified.log`：`verified_warning_explanation` |
| Chrome 时间补全与刷新 | 当前已记录字段在刷新后保持一致，未记录未捕获浏览器异常 | `chrome-time-followup.jsonl`、`chrome-time-followup-resumed.log` 及 PNG |
| 真实知识预算缩减 | 一般规则解释请求返回 200；服务日志省略 56 条知识记录；任务类型仍空、collected={} | `burial.jsonl`：`context_budget_rules`；`server-final.log` 17:52:10 |

真实模型预算路径日志：

```text
2026-09-26 17:52:10,538 [WARNING] src.llm_client:
LLM context budget: omitted 0 old messages and 56 knowledge records
```

该真实请求用时约 55.41 秒。另以原始超限证据和同一本地分词器验证：原无历史提示词 27276 token，无损 JSON 压缩后仍为 18816 token；按完整记录收敛后为 14877 token，预留 1500 输出，总计 16377，不超过 16384。此数值属于独立分词器检查，不能与真实请求的省略条数混作同一次运行。

暂待最终复验的完整原句（另需采油树动作归档/协议、载荷替换和明确支持船原句复验）：

- “知道了，执行前复核实时海况和设备状态，我接受这项提醒，继续按这个方案发布。”
- “请解释 DVL 定位失锁、浑浊度、海流与水下作业安全的关系，并结合系统中的油田环境、禁入区、载荷和支持船规则说明。只查询知识，不创建或修改任何任务。”

## 5. 自动化验证

| 范围 | 已执行结果 | 日志或证据 |
| --- | --- | --- |
| 本轮全量检查 | 2907 passed、18 skipped、532 subtests passed；932.95 秒 | `full-tests.log`、`full-tests.xml` |
| 后续综合定向检查 | 471 passed、11 subtests passed | `final-targeted.log` |
| 上下文异常、路由透传等最后定向检查 | 85 passed | `context-final.log` |
| 补充证据与提示词回归 | 24 passed | `final-evidence-tests.log` |
| 上下文预算、模型 profile、HTTP/SSE 错误 | 99 passed | 执行 `tests/test_llm_context_budget.py tests/test_model_profiles.py tests/test_chat_request_validation.py` |
| 现有发布持久化及软警告事务 | 18 passed | 4 个发布/持久化定向文件 |
| 软警告结构化协议 | 116 passed | `warning-protocol-tests.log` |
| 载荷编辑入口语义 | 138 passed | `editor-intent-tests.log` |
| 载荷携带/替换与否定保护 | 171 passed、2 subtests passed | `payload-carry-tests.log` |
| KB、适配、推荐、模型权威与未来遥测证据 | 115 passed | 负责代理定向执行记录 |
| 补丁格式 | `git diff --check` 通过 | 执行时工作树检查 |

以上定向套件存在交集，不能累计为独立用例总数。18 个跳过项不算通过；测试日志仍包含已有库弃用和音频解码回退提示。最终小重启后的自然语言复验结果及载荷替换真实原句复验将在本报告补充。

复现自动化命令：

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 /root/miniconda3/envs/seagent/bin/python -m pytest -q
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 /root/miniconda3/envs/seagent/bin/python -m pytest -q tests/test_llm_context_budget.py tests/test_model_profiles.py tests/test_chat_request_validation.py
git diff --check
```

## 6. 验证限制

- 所有真实对话均运行在隔离本地服务，MCP 桥接禁用；本轮发布验证只到任务持久化和未来计划池，不代表 ROS 或真实机器人执行成功。
- 未测试真实海上设备、海况服务、机械执行和紧急硬件控制，不将模拟状态作为现场安全结论。
- ASR 模型虽已加载，本轮重点为自然语言文字对话；未进行人工麦克风采集、行业口音或语音识别准确率评测。
- 自然语言表达存在模型波动；已实测的原句及状态转移不代表全部同义表达和全部设备组合均已覆盖。
- 综合知识回答仍可能出现概括过度，例如将未来任务的延后校验泛化为所有任务；实际发布仍以后台约束及运行时检查为准。本项需结合最终提示词复验，不计为已消除。
- 旧服务实例仍存在长句警告确认、知识否定句快捷入口及载荷/支持船表达问题；其后续补丁需按原句真实复验，本文不提前标为通过。
- 采油树历史中的插入/拔出差异曾在任务归档中丢失，ROS 映射亦存在固定插入动作。该问题待修复及协议验证，本轮不声明两种动作已端到端传输通过。
