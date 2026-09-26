# SEAgent 真实模型多轮对话验收记录

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-09-26 至 2026-09-27 |
| 模型 | 本地 Qwen3.5-9B，经 vLLM 实际推理；没有使用 OFFLINE_MOCK 代替真实对话 |
| 覆盖 | 管缆巡检、管缆埋设、采油树控制面板插入与拔出；知识解释、设备建议、自然语言修改、软硬约束、确认、归档、重复发布、浏览器刷新 |
| 隔离环境 | 端口 8892，ENABLE_MCP=0；结果、日志及状态副本位于 /tmp/seagent-real-dialogue-audit |
| 结论 | 已完成下列实际流程核验及问题修复。拔出计划可归档，但当前 ROS 2 协议没有拔出指令，因此明确阻止执行，不能作为插入指令下发 |

## 1. 验收方法与结果

按用户自然表达发送真实 HTTP 对话，核对模型回复、会话字段、阶段、警告确认记录及最终任务文件。HTTP 200 或回复“已处理”不单独计为通过。真实失败记录与修复后记录均保留，通过 session_id 和 case 区分。

| 场景 | 最终核验结果 | 原始证据 |
| --- | --- | --- |
| 巡检完整流程 | 解释 C032 不改变任务；按提示全名确认进入 confirming；2500 米触发硬阻断，忽略指令不能绕过；改回 250 米后可确认归档，重复发布不重复创建 | root.jsonl，real-warning-acceptance-2；root-verified-summary.txt |
| 埋设时间修改 | “把开工时间推迟半小时，作业时长仍保持两小时”真正将 09:00—11:00 改为 09:30—11:30；其他字段保留 | burial.jsonl，fixed_shift_time |
| 埋设长句与自然确认 | 一次提交的时间、机器人、坐标、载荷及支持船均保存；“我接受这项提醒，继续按这个方案发布”进入确认阶段，另行确认后归档 | remaining-acceptance.jsonl，real-burial-acceptance-2 |
| 采油树插入 | 本体自带多功能液压机械臂不误加为选配电液机械臂；“再带上个腐蚀探头”增量添加规范名称，保留旧工具；自然确认后归档，operation=insert | tree-final.jsonl，real-tree-acceptance-3-insert |
| 采油树拔出 | 同轮修改机器人、两件工具、支持船 286、井口 WC16-B2 与结束时间 13:00 均保存；自然确认有效；归档 operation=withdraw，发送状态 BLOCKED 且不可重试 | tree-final.jsonl，real-tree-acceptance-5-withdraw |
| 综合知识问题 | 不误开载荷编辑卡、不误回任务菜单；检索实际项目规则，解释 DVL、浑浊度、海流、禁入区、载荷及支持船；查询后 collected 仍为空 | knowledge-final.log、evidence-final.jsonl、server-acceptance-3.log 与后续知识记录 |
| 动态规则目录 | 实际回复逐条匹配当前启用规则与 severity，C010 位于软警告组，不再出现旧菜单写死的阈值；collected 保持为空 | configured-rule-catalog-verified.log、configured-rule-catalog-summary.txt |
| 具体载荷要求与发布边界 | 声呐非强制选配；通用工作级 250HP 自带多功能液压机械臂，无需强制另带电液机械臂；最终显示完整参数、软硬约束、确认及协议边界；27.36s，collected 仍为空 | root.jsonl，real-knowledge-acceptance-10；final-payload-user-response.log、final-payload-user-response-summary.json |
| 知识上下文预算 | 实际触发完整知识记录裁减并成功回答；保留系统指令、最新用户输入与结构化记录，明确证据范围；最终知识检索日志曾省略 59 条记录 | server-acceptance-3.log，knowledge-final-summary.txt |
| 硬约束处理建议 | 缺少符合任务类型及水深的型号时明确说明，不再推荐不支持巡检的通用或特种工作级机器人 | evidence-final.jsonl，hard_evidence_alternatives |
| 浏览器交互 | 缺结束时间时保留编号追问；补全后显示实际已存字段；刷新会话恢复一致；本次 Chrome 记录无未捕获 JS 异常 | chrome-time-followup.jsonl、chrome-time-followup-resumed.log 及截图 |

## 2. 已修复问题

1. **软警告确认被吞掉**：路由接收当前活动警告，并显式输出 warning_action。抽取器回显的警告编号、自动油田解析及重复提取旧值均不再冒充任务参数修改。真实改变、非法值和未解决参数仍须先处理，不能随警告一并忽略。
2. **只读问题误入编辑或菜单**：载荷快捷入口只接受独立编辑指令；解释和具体规则问题走知识回答。规则目录改为读取实际启用的配置，移除旧菜单写死的流速、能见度和载荷配额说法。混合“通用原理＋项目规则”问题必须检索项目证据，不能仅用通用聊天作答。
3. **回复与实际状态不符**：清理未提交时间/载荷、虚假确认、直接绕过软警告发布及缺参数却称完整的文案，保留有用追问和实际提交回执。未来排期提示不被当成机器人故障或设备完全就绪的证据。
4. **时间与多字段遗漏**：支持开工、完工、收工别称和明确持续时长；补偿模型遗漏的唯一明确时间范围；明确支持船按任务允许值保存，同时防止条件、否定、撤销、问句与冲突选择被强制写入。
5. **载荷语义错误**：携带/带上表达保留用户指定工具，区分机载和选配；修复“带上个腐蚀探头”的量词别名处理，增量添加保留已有工具。
6. **设备与知识事实错误**：推荐按当前任务能力和水深筛选；硬约束回复收到相同筛选证据。知识回答区分规则 severity、一般建议、项目实际功能，以及可选载荷和强制要求；综合回答减少无关枚举，避免输出中途截断。进一步补全实际机型的 onboard_payloads、supported_payloads、任务能力和选择语义；该组结构化事实在上下文裁减时保留，避免只看到工具目录或笼统规则文案而推断“必须另带机械臂”。具体必选要求查询聚焦任务载荷字段、约束和匹配的工具，不再混入全量油田、船舶和型号介绍；综合问题保留原有检索范围。证据同时明确能力适配不代表已完成发布或执行校验。知识回复校正移除无完整校验依据的“即可发布/直接执行/满足全部准入”断言，保留否定句、问句及带完整校验条件的正确说明，并补充参数、软硬约束、确认和执行协议边界。
7. **上下文超限**：使用实际分词器和引擎最大长度预留输出预算；先去掉旧完整轮次、压缩 JSON，再按完整证据记录裁减。必需上下文无法容纳时返回专用 503 并回滚本轮状态，不误报槽位 400。
8. **插入/拔出动作丢失**：task.details.operation 保存动作；派发入口、真实桥接和 mock 适配器共用协议校验。拔出及未明确动作的旧泛化阀门任务均不得默认为 INSERT_PLUG。计划归档成功与实际下发成功分别说明。

## 3. 发布产物与协议核验

| 任务 | 归档编号 | 最终状态 |
| --- | --- | --- |
| 巡检 | PI-20260927-002 / TI2026092702 | SCHEDULED，未来计划，未实际执行 |
| 埋设 | PB-20260927-001 / TI2026092703 | SCHEDULED，未来计划，未实际执行 |
| 插入 | CT-20260927-003 / TI2026092705 | SCHEDULED；归档动作 insert |
| 拔出 | CT-20260927-004 / TI2026092708 | BLOCKED；归档动作 withdraw；协议不支持，未下发 |

任务文件位于 result/task/task_intent_编号.json，对应历史位于 result/history/history_编号.json。协议测试进一步核验插入编码为 INSERT_PLUG=4、params 为空；拔出不会调用 publish。此项传输验证使用测试替身，不能表述为真实机器人执行。

## 4. 自动化验证

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 本轮较早全量 | 2907 passed，18 skipped，532 subtests | full-tests.log |
| 继续验收后的全量 | 3052 passed，1 failed，18 skipped，23 warnings，532 subtests，918.52s；失败为腐蚀探头量词别名回归 | final-full-tests.log、final-full-tests.xml |
| 上述失败及载荷/油田/发布修复复验 | 218 passed，2 subtests；包含该失败用例 | acceptance-final-corrections.log |
| ROS 桥接与协议目录 | 188 passed，17 skipped | protocol-fixture-tests.log |
| 支持船与动作归档 | 89 passed | acceptance-targeted-tests.log |
| 真实失败的警告、知识、时间、回复回归 | 141 passed | real-replay-fix-tests.log |
| 混合知识证据、预算和状态保护 | 50 passed | knowledge-grounding-final-tests.log |
| 硬约束替代设备证据 | 25 passed，49 subtests | alternative-evidence-tests.log |
| 重复值确认与真实修改保护 | 50 passed | noop-warning-tests.log |
| 最后受影响模块集中回归 | 123 passed | final-affected-tests.log |
| 规则目录与具体问题分流 | 36 passed | catalog-final-tests.log |
| 最终知识/载荷/聚焦检索/预算集中回归 | 94 passed，2 warnings，29.96s | final-knowledge-regression.log |

全量发现的唯一失败已修复并定向复验；后续补丁由对应回归及真实对话覆盖，没有将较早全量结果冒称为最终全部代码一次全量通过。各定向套件有交集，不累计为独立用例数量。最后一组知识回归的 warning 为第三方 SWIG 类型缺少 __module__ 的弃用提示，未产生失败或超时。跳过项不计为通过。另已执行 Python 编译检查、前端 JavaScript 语法检查与 git diff --check。

复现命令：

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 /root/miniconda3/envs/seagent/bin/python -m pytest -q
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 /root/miniconda3/envs/seagent/bin/python -m pytest -q tests/test_warning_router_context.py tests/test_payload_mutation_restoration.py tests/test_valve_operation_dispatch.py
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 /root/miniconda3/envs/seagent/bin/python -m pytest -q tests/test_payload_knowledge_evidence.py tests/test_knowledge_reply_grounder.py tests/test_llm_context_budget.py tests/test_payload_source_contract.py tests/test_device_task_fit_followup.py tests/test_knowledge_grounded_enhancements.py tests/test_environment_knowledge_retriever.py tests/test_responder_validation_evidence.py tests/test_payload_recall.py tests/test_grouped_payloads.py tests/test_deferred_knowledge_context.py
git diff --check
```

## 5. 验证限制

- 当前 ROS 2 协议只定义采油树插入指令，没有拔出指令。已修复为明确阻止错误执行；真正执行拔出仍需先扩展并联调机器人协议。
- 全部模型交互运行于隔离目录，MCP 硬件桥接关闭；未执行真实机器人动作，未证明真实海况、硬件遥测或机械作业可用。
- ASR 模型已加载，但本轮重点是文字自然语言对话；未进行人工麦克风采集、行业口音或语音识别准确率评测。
- 真实模型输出具有波动；本报告证明列明原句和状态路径的观测结果，不宣称已覆盖所有同义表达、所有设备组合或所有知识事实。
