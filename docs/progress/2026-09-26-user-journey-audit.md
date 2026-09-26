# SEAgent 用户流程排查与修复记录

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-09-26 |
| 环境 | Python 3.12.3（seagent）、Node.js 18.20.8、无头 Chrome、双 NVIDIA vGPU-32GB |
| 模型 | 本地 Qwen3.5-9B、Qwen ASR 0.6B；模型加载保持离线 |
| 验收服务 | 独立端口 8892；日志、任务、历史、遥测副本位于 `/tmp/seagent-user-audit` |
| 结果 | 已修复本轮可复现问题；全量回归、补充定向测试及实际用户流程结果见下文 |

## 1. 结论与修复范围

以浏览器和真实本地模型操作系统，覆盖知识查询、三种任务模板（四种具体作业）、参数修改、软硬约束、确认发布、历史恢复、重置、语音上传、翻译和监控页面。修复包含以下行为：

| 问题 | 修复后行为 |
| --- | --- |
| 中文或 emoji 录音名被误判为不支持格式 | 根据原文件扩展名校验，使用随机安全文件名在临时目录处理 |
| 错误 JSON 或字段类型触发接口 500 | 对话、流式、重置、时间、历史、翻译和控制相关入口在修改状态前拒绝非法输入 |
| 单条损坏历史让整个列表不可用 | 跳过非法时间元数据、损坏 JSON 和非 UTF-8 文件，正常历史仍可访问 |
| 重置/恢复慢响应覆盖用户新选择 | 校验请求代际；失效响应不覆盖当前会话，恢复失败保留原会话 |
| 软警告按钮、网关保存失败后不能重试 | 恢复按钮与表单，并抑制重复提交 |
| 监控旧响应覆盖新状态、慢网络刷新饥饿 | 按已渲染响应序号丢弃过期结果，持续慢网络仍可更新 |
| 窄屏输入区按钮裁切 | 调整输入框收缩、按钮换行和手机上下布局 |
| 油田地点被挤入支持船字段 | 修正抽取提示与允许候选字段的矛盾，独立保留油田候选 |
| 用户指定的已知机器人被模型替换为另一型号 | 用完整设备目录核实显式单机，再由原任务兼容性规则拒绝；明确提示“不支持当前任务” |
| 时间区间已修正但回复仍带过期错误 | 同轮上下文参与时间预检，保留真正非法时间的拒绝行为 |
| 小数/布尔编号及超大控制值进入控制接口 | 拒绝无效编号、网关参数及超出 float32 表示范围的控制值 |
| README 引导直接打开本地 HTML | 改为通过后端服务地址访问页面 |

## 2. 实现边界

保留既有任务模板、槽位事务、任务兼容性规则、软硬约束和原子发布机制。设备证据校验位于 WRITE 候选处理路径，没有增加关键词意图路由。识别已登记设备与判断设备能否执行当前任务分别处理，防止任务过滤抹去用户明确选择的设备。

语音上传仍采用扩展名白名单和内容解码校验，客户端文件名不参与实际存储路径。前端请求修复以当前会话和已显示状态为依据；网络失败不会自动重放发布或控制操作。

## 3. 关键模块与接口

| 模块 | 相关接口/函数 |
| --- | --- |
| `src/web/routes_chat.py` | `/api/chat`、`/api/chat/stream`、`/api/reset` |
| `src/web/routes_time_history.py` | `/api/time/set`、`/api/history/load` |
| `src/web/routes_asr.py` | `/api/asr` |
| `src/web/routes_translate.py` | `/api/translate` |
| `src/web/routes_mcp.py` | 网关、任务管理、设备控制与派发入口 |
| `src/session/history_manager.py` | `list_history` |
| `src/extraction/extractor.py` | `EXTRACTION_SYSTEM` |
| `src/handlers/explicit_value_grounding.py` | `ground_explicit_values` |
| `src/handlers/equipment_cascade.py` | 单机兼容性失败提示 |
| `src/temporal/temporal_parser.py` | 同轮时间区间预检 |
| `frontend/js/index.js` | 会话恢复、重置、软警告操作 |
| `frontend/ros2_dashboard.html` | 监控轮询、网关保存 |
| `frontend/css/index.css` | 输入区和窄屏布局 |

## 4. 实际用户流程证据

| 场景 | 观测结果 |
| --- | --- |
| 帮助和机器人知识查询 | HTTP 200，返回说明和知识库设备信息 |
| 巡检收集中插入查询 | 查询前后已填任务参数保持一致 |
| 修改水深 | 300 米改为 250 米，其他任务参数保留 |
| 轻型设备请求 2500 米作业 | 进入 `blocked_hard`，显示 C004 与设备 600 米能力限制 |
| 合法载荷补全、接受软警告、确认发布 | 先 `blocked_soft`，再 `confirming`，最终 `done/SCHEDULED` |
| 发布产物 | 隔离目录生成 `task_intent_TI2026092601.json` 与对应历史 |
| 服务重启后恢复并再次确认发布 | 恢复 `done`，提示无需重复发布；任务文件数量和内容均不变 |
| 重置恢复会话 | 返回 `reset=true`，随后状态查询 `exists=false` |
| 管缆埋设、采油树插入/拔出 | 正确进入相应任务模板，继续询问缺失参数；取消埋设任务进入 `rejected` |
| 中文文件名真实 ASR、中文翻译英文 | 上传 `录音.wav` 返回 200；翻译返回“The robot is performing a cable inspection task.” |
| SSE | 返回 `ping/step/delta/result/end`，以 `[DONE]` 结束 |
| 浏览器请求恢复 | 刷新保留任务字段；断网后软警告按钮可重试 |
| 监控交互 | 旧响应不覆盖新状态；重复点击只提交一次；失败后表单可重试 |
| 响应式布局 | Chrome 的 320/390/780/1440 像素、中英文共 8 种组合未出现横向溢出 |

最后真实模型复测：真实重放3条输入全部通过：不兼容机器人保持拒绝且没有被换成其他型号；油田不再误作支持船；采油树两种作业的09–11点与10–12点区间正确保存且无过期错误提示。最终版本SSE事件链路通过。

主要原始证据保存在 `/tmp/seagent-user-audit/`：`real-dialogue-*.json*`、`fixed-live-acceptance.jsonl`、`real-task-breadth.jsonl`、`final-real-replay.jsonl`、`fixed-live-stream.txt`，以及浏览器日志和截图。过程中发现的失败重放也保留在这些记录中，最终结果以本节及最后复测文件为准。

## 5. 自动化验证

全量回归结果：

```text
2873 passed, 18 skipped, 23 warnings, 532 subtests passed in 885.87s (0:14:45)
```

补充定向验证（覆盖全量执行期间最后补齐的设备候选与时间预检修复）：

```text
373 passed, 2 warnings, 43 subtests passed in 51.06s
```

| 验证范围 | 结果与证据 |
| --- | --- |
| 修复前全量基线 | 2716 passed、18 skipped、530 subtests passed |
| 中文录音、历史、时间、翻译 | 定向 123 passed；包含 52 个新增参数化场景 |
| MCP 参数与既有网关流程 | 60 passed |
| 前端请求、历史、载荷、监控与渲染 | 40 passed / 12 subtests；慢网补充后相关 14 passed / 4 subtests |
| 设备证据与既有设备流程 | 174 passed / 39 subtests；辅助候选补充后相关 57 passed / 8 subtests |
| Python/JavaScript 语法、补丁格式 | compileall、node --check、git diff --check 通过 |

上述定向套件存在交集，不累计为总用例数。完整日志和 JUnit 位于 `pytest-verified.log` 与 `pytest-verified.xml`。

复现全量检查：

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 /root/miniconda3/envs/seagent/bin/python -m pytest -q
/root/miniconda3/envs/seagent/bin/python -m compileall -q src tests mcp/ros-mcp mcp/operation-time-window
node --check frontend/js/index.js
git diff --check
```

## 6. 验证限制

- 真实模型服务使用隔离的任务、历史和遥测副本，并设置 `ENABLE_MCP=0`。机器人下发和网关变更以模拟桥接及受控网络验证，未向实际机器人发送指令。
- 真实海流服务和可选第三方 ROS 对比测试按配置跳过；本轮结果不代表真实海上作业验证。
- ASR 使用本地短音频验证上传和推理链路；未进行实际麦克风采集或中文行业语音准确率评测。
- 语义生成仍有模型波动。本轮长句软警告确认曾需要改用界面明确提供的“忽略软警告”，随后流程正常。自然语言覆盖不等于全部表达均已验证。
- 不同任务模板已验证创建和取消，完整参数收集与最终发布以巡检任务为代表；其余模板的设备兼容性等由现有自动化测试验证，未逐一进行全部设备组合的真实模型发布。
