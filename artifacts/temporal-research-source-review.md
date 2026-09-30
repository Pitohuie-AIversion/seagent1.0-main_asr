# 时间调研来源核对与实现边界

| 项目 | 范围 |
| --- | --- |
| 复核日期 | 2026-09-30 |
| 对象 | 历史时间调研草案的关键外部主张、示例及本仓库对应实现 |
| 来源 | 实际访问的官方标准、厂商文档、维护者仓库及作者论文 |
| 本地基线 | 工作树；本轮开始时 HEAD 为 `6ef8c92` |
| 实际执行 | 当前时间模块 155 passed / 4 subtests passed；两段文档示例的 7 项固定检查通过 |
| 未执行 | 厂商 API 调用、跨工具中文基准、真实模型/ASR、实机或完整系统回归 |
| PDF | [本说明的 PDF 副本](temporal-research-source-review.pdf) |

## 1. 核对结论与来源边界

[原草案](deep-research-report.md)中的结构化参数、参考时间、时区和应用校验等基本概念有一手资料支持，但不能据公开接口断言厂商内部采用相同时间算法。本说明将标准事实、项目当前代码、方案建议和未验证能力分开记录。

原草案的 37 个引用块包含 32 个不同的 `turn…` ID，原 URL 映射仍未找到。已把原始标记及上下文保存到[引文存档](temporal-research-legacy-citations.json)，正文改成可读的“旧引文”链接。**下列链接是本次独立找到并阅读的资料，不是对这些旧 ID 的来源恢复。** 本次只复核下列关键主张，未逐句认证整篇历史草案。

## 2. 时间标准：事实与应用策略

| 主张 | 核对后的准确范围 | 一手依据 |
| --- | --- | --- |
| RFC 3339 表示时间 | 范围是具有 UTC 关系的时间点，不定义区间；不是自然语言解析或数据库设计标准 | [RFC 3339 §1](https://www.rfc-editor.org/rfc/rfc3339.html#section-1) |
| UTC offset 与区域时区 | 单个确定时间点可用 UTC/offset；保留未来地区本地钟点意图时，区域规则及更新政策才重要。后半句是工程推论 | [IANA 时区数据库](https://www.iana.org/time-zones) |
| `fold` 与 gap | `zoneinfo` 使用系统或 `tzdata` 数据；`fold` 可区分回拨时刻，构造器和 `replace()` 不自动拒绝不存在的本地时间 | [zoneinfo](https://docs.python.org/3/library/zoneinfo.html)、[PEP 495](https://peps.python.org/pep-0495/#the-fold-attribute) |
| 歧义处理 | Temporal 提供 earlier/later/compatible/reject；选择拒绝或澄清是应用政策，不能写成所有时间标准的统一要求 | [Temporal 时区与歧义](https://tc39.es/proposal-temporal/docs/timezone.html) |
| 日历日与时长 | 跨 offset 变化时，一个本地日不一定是 24 小时；中文“明天这个时候”怎样映射仍需业务解释 | [ZonedDateTime 的 hoursInDay 与 add](https://tc39.es/proposal-temporal/docs/zoneddatetime.html) |
| 区间与全天日期 | VEVENT 的 DTEND 不包含终点；DATE 是日期类型。这不规定 SEAgent 所有区间的端点规则 | [RFC 5545 §3.6.1](https://www.rfc-editor.org/rfc/rfc5545.html#section-3.6.1)、[§3.3.4](https://www.rfc-editor.org/rfc/rfc5545.html#section-3.3.4) |
| 重复事件 | 本地固定钟点适合带时区的 DTSTART；UTC/floating 表示也合法，TZID 不一定是 IANA 名称，两行 RRULE 示例不是完整日历文件 | [RFC 5545 §3.8.5.3](https://www.rfc-editor.org/rfc/rfc5545.html#section-3.8.5.3)、[§3.2.19](https://www.rfc-editor.org/rfc/rfc5545.html#section-3.2.19) |
| 重复规则的缺失时间 | 显式 DATE-TIME 与 RRULE 展开规则不同；后者生成的无效日期或缺失本地时间需跳过且不计数，不能默认把“每月31号”夹到月末 | [RFC 5545 §3.3.5](https://www.rfc-editor.org/rfc/rfc5545.html#section-3.3.5)、[§3.3.10](https://www.rfc-editor.org/rfc/rfc5545.html#section-3.3.10) |

原草案将无 offset 的 `local_time`、`end_local_time` 标为 `format: time`，现已更正为本方案的 `HH:MM:SS` pattern。JSON Schema 的 `time` 对应 RFC 3339 `full-time`；普通 JSON Schema 实现的 format 检查还取决于启用的词汇表/验证模式。该 pattern 不验证日期、时区、DST，也不代表所有厂商支持同一 schema。[JSON Schema 验证规范 §7](https://json-schema.org/draft/2020-12/json-schema-validation#section-7)

## 3. 厂商公开接口能证明什么

| 对象 | 可核实的公开行为 | 不能由此推出的结论 |
| --- | --- | --- |
| OpenAI | Structured Outputs 支持 JSON Schema 子集及所列日期时间格式，文档仍要求处理内容错误和未完成响应；微调模型的 format/pattern 等支持另有限制。[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) | 格式约束不证明时间语义正确，也不证明本仓库已接入该 API |
| OpenAI 工具调用 | strict 模式要求对象禁额外字段并将所有属性列为 required；函数实际执行由应用完成。首次 schema 处理/缓存的附加延迟说明须按具体模型范围阅读。[Function calling](https://developers.openai.com/api/docs/guides/function-calling) | 不能将某类模型的缓存说明推广到所有模型，或把本文完整时间编译流程称为厂商内部方案 |
| Google Gemini | 支持结构化输出子集，官方要求应用继续检查值与语义；Interactions 提供 previous_interaction_id 关联历史。[Structured outputs](https://ai.google.dev/gemini-api/docs/structured-output)、[Interactions](https://ai.google.dev/gemini-api/docs/interactions-overview) | 服务端对话历史不等于可信的用户时区、时间锚点或日历状态 |
| Anthropic | 工具 strict 模式约束 input_schema；细粒度流式工具输入可能暂时无效或被截断，应在完整性与解析检查后处理。[Strict tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use)、[Fine-grained streaming](https://platform.claude.com/docs/en/agents-and-tools/tool-use/fine-grained-tool-streaming) | schema 合规不代表调用已经获业务授权；不能执行未完整解析的日程参数 |
| Meta | Llama 3.3 70B Instruct 格式文档展示工具调用及 Today Date 上下文。[该型号的 prompt 格式](https://github.com/meta-llama/llama-models/blob/main/models/llama3_3/prompt_format.md) | 不能扩大成所有 Llama 版本具备相同协议，或模型自动获知当前日期 |
| Microsoft | Semantic Kernel TimePlugin 源码提供 Now/UtcNow 和本地时区信息。[官方源码](https://github.com/microsoft/semantic-kernel/blob/main/dotnet/src/Plugins/Plugins.Core/TimePlugin.cs) | 执行环境本地时区不自动等于用户时区；没有建立“五家组件最完整”的评价依据 |

本轮未发送 API 请求或安装厂商 SDK。上述能力是公开文档范围，SEAgent 当前模型路径仍使用本地 vLLM，见第 5 节代码对照。

## 4. 开源工具与论文评价

| 工具 | 已核实的用途/语言范围 | 本次修正与限制 |
| --- | --- | --- |
| HeidelTime | 文档时间表达抽取、TIMEX3 归一化，有手工中文资源。[官方项目](https://github.com/HeidelTime/heideltime) | 人工和自动生成语言资源质量不能等同；不据此推导本项目中文对话准确率 |
| SUTime | 参考日期与 TIMEX3 规则处理；官方说明的随附规则仅英语。[SUTime](https://nlp.stanford.edu/software/sutime.html) | CoreNLP 整体支持中文，不等于 SUTime 随附中文规则；SET 不等于可执行 RRULE |
| Duckling | 规则型结构化解析，存在中文 Time 规则，各语言维度不同。[官方项目](https://github.com/facebook/duckling) | 原文“快”改为待实测的解析路径候选，未作本项目延迟比较 |
| Recognizers-Text | 总体 README 列中文完整支持，同时将 Python 标为 alpha、Java 标为 in progress。[官方项目](https://github.com/microsoft/Recognizers-Text) | 须按具体语言端及版本核验；不能把总体声明当各端等价保证 |
| dateparser | 中文 locale、相对日期示例和可选时间跨度功能。[使用文档](https://dateparser.readthedocs.io/en/latest/)、[locale 清单](https://dateparser.readthedocs.io/en/latest/supported_locales.html) | 去掉“中文通常较好”的无基准评价；需检查默认补齐、时区和长文本搜索行为 |
| chrono-node | 默认国际英语；README 明确列 zh.hans/zh.hant 部分支持。[官方项目](https://github.com/wanasit/chrono) | 修正“中文必需外接 parser/LLM”；补充组件由目标语料缺口决定 |
| Chronyk | README 的时区参数采用秒偏移，未展示 IANA TZID 接口。[官方项目](https://github.com/KoffeinFlummi/Chronyk) | 旧 Python 分类器不能单独证明现代版本不兼容；不将其作为本方案时区核心是设计取舍 |

论文的语料与指标也应单列：SUTime 2012 论文使用 TempEval-2 英语任务；2014 年 HeidelTime 中文论文区分语料版本、片段抽取和归一化指标。它们都不能当成当前中文 ASR 对话或最终任务写入的成功率。本轮未跑跨工具基准，也未建立工具排名。[SUTime 作者论文](https://nlp.stanford.edu/pubs/lrec2012-sutime.pdf)、[HeidelTime 中文论文](https://aclanthology.org/E14-4026/)

## 5. SEAgent 当前实现对照

下表来自本轮代码阅读，不能由外部文档替代。

| 入口 | 当前行为 | 适用边界 |
| --- | --- | --- |
| [llm_client.py](../src/llm_client.py) | 本地 vLLM 路径按 response_schema 使用结构化参数，或请求 JSON 对象 | 不是上述厂商 SDK/API 的已完成接入 |
| [relative_time_parser.py](../src/temporal/relative_time_parser.py) | 已有 TemporalIR、base_dt 和 timezone_id 字段；内部按去掉 tzinfo 的墙钟部分计算，旧接口返回无 offset ISO 字符串 | timezone_id 的存在不代表执行了时区换算或 DST 合法性校验 |
| [time_range_engine.py](../src/temporal/time_range_engine.py) | 组装起止时间/时长，并将 DST 歧义码列入不可继续的错误集合 | 消费错误码不代表底层已能产生全部 gap/fold 错误 |
| [simulated_time.py](../src/temporal/simulated_time.py)、[time_context.py](../src/temporal/time_context.py) | 模拟时钟及展示使用 Asia/Shanghai；get_business_timezone 另读取 SEAGENT_TIMEZONE | 不能把该环境变量描述为所有解析入口已经统一切换时区 |
| [时间模块历史报告](time_module_v2_upgrade_report.md) | 已记录 IR 和 DST 架构预留 | 未发现该目录提供完整 RRULE 日历写入/展开引擎，草案 recurrence 方案不能登记为当前功能 |

因此，本轮对草案的改动是文档勘误与来源补充，未将新时间架构接入生产代码。naive 墙钟值在明确约定的内部层并非自动构成缺陷；真正需要核对的是跨模块转换与持久化边界是否保留相同时间语义。

## 6. 本地执行证据

当前代码的四个既有测试文件于本轮运行：`test_time_module_v2_upgrade.py`、`test_relative_time_parser.py`、`test_duration_parser.py`、`test_task_time_validation.py`，结果 **155 passed, 4 subtests passed in 0.93s**。详细命令与原始 stdout/JUnit 见[文档审查测试记录](../test_logs/documentation-review-2026-09-30.md)。这不是对历史 145 项结果的复现，也不是全仓回归。

原草案的两段标准库示例保持原样提取后，在隔离 Python 进程中检查了上海普通时刻、洛杉矶 gap/fold、aware 输入拒绝及 require 函数分支，**7 项固定检查通过**。源码块哈希、输入和结果见[示例执行记录](../test_logs/documentation-review-20260930/temporal-example-check.json)。该范围不证明所有历史时区、规则变更或厂商 API 行为正确。

## 7. 尚未恢复或验证的部分

- 32 个旧引用 ID 的原 URL 对照及历史网页版本仍缺失；本说明不填造这些映射。
- 历史性能报告没有本轮找到的逐次计时数据；时间历史报告的分类与可追溯源码数量存在差异，详见对应报告。
- 未做库的中文基准、厂商 schema 请求测试、真实模型/ASR、完整时间语义验收或全仓回归。
- 跨时区对话、DST、RRULE 及写入回读仍需具体业务设计与验证，不能仅通过补文档宣布实现。
