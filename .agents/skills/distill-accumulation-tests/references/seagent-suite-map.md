# SEAgent 累计测试集蒸馏图谱

## 目录

1. 基线与权威来源
2. 39 个场景的能力分组
3. Runner 架构
4. 已形成的高价值模式
5. 跨代迁移债务
6. 下一代补齐清单

## 1. 基线与权威来源

本图谱依据以下实际资产形成：

- `tests/run_accumulation_integration_tests.py`：可执行行为的唯一权威来源；包含 TS-01 至 TS-39。
- `tests/test_accumulation/测试集05.md`：人工复现与评审文本；场景 ID 与 runner 对齐。
- `tests/test_accumulation_fixtures.py`：验证坐标、设备、factory、verifier、文件恢复和文档基线不漂移。
- `config/environment.yaml`、`config/robot_fleet.yaml`：区域、海床、设备清单、`unit_id` 与 `status_ref` 的权威来源。
- `tests/test_accumulation/测试机.md`：物理状态刷新闭环和真实执行命令。

当前 runner 固定访问 `http://localhost:8890`，状态请求超时 10 秒，对话请求超时 180 秒；通过 `ACCUMULATION_TEST_IDS` 选择用例；未知 ID 直接失败。测试前保存 `config/state.yaml` 原始字节，退出时恢复。

## 2. 39 个场景的能力分组

场景可能同时属于多个组。机器可读详情见 `seagent-case-catalog.json`。

### A. 正常准入、约束与遥测

- TS-01 正常设备与环境进入确认。
- TS-02 禁入区硬阻断并在修改位置后解除。
- TS-03 流速超上限硬阻断。
- TS-04 高浑浊度软警告。
- TS-05 设备总体不可用硬阻断。
- TS-06 定深异常限制高精度作业。
- TS-07 视觉异常与高浑浊度组合限制。
- TS-08 机械臂异常限制接触式作业。
- TS-09 通信异常提示协同风险。
- TS-10 遥测过期及警告确认流程。
- TS-11 AUV 母船支援弱与水声通信异常组合警告。
- TS-12 高障碍密度软警告。
- TS-13 海床质地与埋设设备不匹配。
- TS-23 修正硬水深违规后必须继续报告浑浊度软警告。
- TS-29 设备能力与任务类型不匹配。
- TS-30 硬阻断不能被“忽略警告/确认发布”绕过。
- TS-31 软警告可确认后进入发布，但确认警告本身不发布。
- TS-34 更新 telemetry 后“重新检查”必须重读快照。
- TS-36 结束时间不晚于开始时间触发 C031 硬阻断。
- TS-37 过去开始时间触发 C030 软警告。
- TS-38 不可用支持船触发 C007 硬阻断。

### B. 意图、抽取、规范化与 Slot

- TS-14 多轮稀疏补齐。
- TS-15 1.2 千米规范化为 1200 米。
- TS-16 已收集水深被用户显式修改。
- TS-17 口语表达映射到标准任务和井口。
- TS-21 显式紧急意图。
- TS-22 隐式紧急意图。
- TS-24 拖曳式重载设备解析。
- TS-25 特种工作级设备解析。
- TS-26 轻型工作级设备解析。
- TS-28 完整首轮消息的全部稳定字段精确提取。
- TS-32 插入/拔出动作语义区分。

### C. 边界、路由与安全响应

- TS-18 域内与域外复合请求确定性阻断。
- TS-19 同轮多个域内任务要求用户选择单一任务。
- TS-20 域外 PPT 请求与模型/系统提示泄露防范。
- TS-35 设备知识问答不创建任务槽位。

### D. 生命周期、发布与持久化

- TS-27 确认发布返回 final JSON，并可验证 task/history 产物。
- TS-31 软警告确认到正式发布的完整链路。
- TS-33 收集中取消任务后重置会话与槽位。
- TS-34 外部状态变化后的重新检查。
- TS-39 已发布会话重复确认复用同一 intent，且不新增或改写产物。

## 3. Runner 架构

当前执行模型由以下部分组成：

- factory：正常 telemetry、管缆巡检、采油树操作和管缆埋设消息；
- API adapter：`set_robot_state`、`reset_session`、`chat`；
- action adapter：允许在对话轮次之间调用状态更新；
- verifier：结构化字段、约束 code、发布产物和幂等性；
- case：ID、名称、状态注入、文本/动作步骤、逐步 verifier、expected failure；
- harness：模拟时间派生、唯一 session、setup、执行、cleanup、汇总和退出码；
- fixture meta-tests：保证测试数据仍与环境和设备配置一致。

特别语义：callable action 不增加对话 `step_idx`；verifier 索引只对应对话响应。迁移时应显式区分 `action_index` 与 `observation_index`，降低错位风险。

## 4. 已形成的高价值模式

- 正常基线避开禁区和 DVL 风险区，单例只注入目标异常。
- 运行时配置而非文档决定合法实体和值域。
- fresh/stale telemetry 从模拟时间计算，避免真实时间漂移。
- 调度 `unit_id` 与状态注入 `status_ref` 分离。
- 首轮完整抽取单独严格验收，不用第二轮补齐掩盖模型漏提。
- 约束回归同时检查 `done=false` 与 `final_json=null`，防止“回复说阻断但实际已发布”。
- 发布验证可读取 TaskIntent/history 并做 schema、ID、phase 与内容一致性检查。
- 重复确认记录首次文件集合、字节和 mtime，验证无新增和无改写。
- mutable state 文件在所有退出路径按原始字节恢复。
- XFAIL 失败可接受，但 XPASS 被视为异常，推动删除过期豁免。

## 5. 跨代迁移债务

- 多个旧场景仍依赖中文关键词或宽泛 `or` 断言；目标 API 应返回结构化 `violation_codes`、severity 和 phase。
- TS-01、TS-11、TS-14、TS-18、TS-19、TS-23、TS-30 等含无条件通过或弱首轮断言；迁移时需逐轮定义状态变化。
- BASE_URL、超时、sleep、状态文件和 artifact 环境变量属于 runner 配置，不应写死。
- 当前目录以 Python lambda 嵌入 case，难以独立审阅；下一代应由数据目录描述输入和不变量，由 verifier registry 执行复杂检查。
- 当前人工文档与 runner 名称存在轻微措辞差异；ID 集合一致比标题逐字相同更可靠。
- 当前发布校验默认关闭真实产物检查，只有设置 `SEAGENT_VERIFY_ARTIFACTS=1` 才执行；高风险流水线应默认启用隔离产物验证。

## 6. 下一代补齐清单

现有 39 例不能单独证明以下要求，迁移时应新增：

- 普通 LLM 自由对话有正向内容断言，而不仅是不创建槽位；
- 真实 ASR 标注音频、关键术语准确率和 ASR 后任务/普通对话分流；
- 用户拒绝发布与修改后重新确认；
- 无效 candidate 不覆盖已确认 Slot，且 version/raw_value/source 保持正确；
- snapshot schema 不合法、旧 schema 兼容和恢复；
- staging 存在但 final 不存在、final 已存在、quarantine 和不完整事务恢复；
- 跨进程锁、并发发布、并发读写与 TOCTOU 防护；
- API/前端契约和前端普通对话回归；
- setup 中断、chat 超时、持久化失败时 fail closed；
- 对每个关键 verifier 的负样本单元测试。
