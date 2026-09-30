# 海流 MCP：设计与实现状态

核对日期：2026-09-30。本包已有独立服务、客户端、窗口算法和桥接辅助类；主 `DialogueManager` 尚未自动接入。本文区分现有行为与接入要求，安装及测试命令见 [README](README.md)。

## 1. 边界与数据契约

FastMCP stdio 服务只暴露 `get_current_forecast`。该工具只读查询海流，不获取风浪，不判断机器人完整准入条件，不回写 Slot 或发布 TaskIntent。CHECK/SEARCH 是调用方使用的 Python 服务，不是额外 MCP 工具。

`CurrentQuery` 接收经纬度、非负作业深度、带时区的开始和结束时间；结束须晚于开始。当前未实现“请求最多 10 天”的资源限制，也不承诺固定预测天数。可用范围须按本次返回数据检查。

查询指纹将经纬度格式化至四位小数、深度至一位小数，与 UTC 时间一同计算 SHA-256 并取前 16 位；不是原始浮点参数逐位相等。

`CurrentForecastData` 包含来源、`is_synthetic`、`snapshot_id`、查询指纹、`retrieved_at`、可选 `model_run`、网格坐标、时间/深度轴及 `[time][depth]` 的 `uo/vo` 矩阵。模型校验矩阵维度、一至两层深度及至少两个时间点，但不能据此推断已检查时间连续性或全部物理有效性。

`ForecastReply` 的 `OK` 携带数据，`NOT_EVALUABLE` 携带错误。客户端将 MCP tool error 映射为 `PROVIDER_ERROR`；空响应、无法解析的 JSON 等仍可能抛出客户端异常，调用方需要处理。

## 2. 取数实现及已知差距

| 项目 | 当前代码行为 | 尚不能据此保证的性质 |
| --- | --- | --- |
| 数据源 | 固定产品与数据集，使用 `copernicusmarine.open_dataset` | 本轮未联网确认数据集最新范围或物理适用性 |
| 凭据 | 接受 `COPERNICUSMARINE_SERVICE_USERNAME/PASSWORD`，兼容 `COPERNICUS_USERNAME/PASSWORD`；缺失返回 `NOT_CONFIGURED` | 未执行真实账号下载 |
| 水平位置 | 先按近似 `0.0833333` 度网格舍入，再以 `nearest` 选实际点 | `grid_distance_km` 按舍入点计算，未按最终实际坐标重算 |
| 深度 | 从代码内深度表选一至两层，再以 `nearest` 选实际层 | 不是先读取实际轴再选相邻层；越界深度被截到边界层 |
| 时间 | 构造 UTC 六小时外包节点并切片，返回实际时间轴 | 未验证所有相邻点严格相隔六小时，不能声称拒绝跨缺失间隔插值 |
| 缺失数据 | `uo/vo` 非有限值返回 `MISSING_DATA` | 不区分陆地遮罩与其他缺失原因，不能承诺统一返回 `LAND_OR_INVALID` |
| 起报时间 | 有 `forecast_reference_time` 坐标时读取，否则 `model_run=null` | 缺失时当前 `warnings` 仍可为空；取数时间不是起报时间 |

Provider 先切片再加载数据。深度处理使用单层边界值或截断插值权重，不能描述成“严格拒绝所有深度外推”。上述差距来自代码静态核对，未在本轮修复。

## 3. 并发、超时与合成数据

`WorkerBackend` 使用异步锁，锁被占用时返回 `BUSY`。`CurrentWorker` 以 `ThreadPoolExecutor(max_workers=1)` 执行同步 Provider；`asyncio.wait_for` 默认等待 30 秒，可通过 `SEAGENT_CURRENT_TIMEOUT_SECONDS` 调整。

**超时只结束异步等待，不能终止正在执行的线程或底层网络 I/O。** 锁随后释放，后续请求可能排在仍占用线程的任务后；`shutdown(wait=False)` 也不是强制终止。因此“取消后工作必然停止”及“整个生命周期只有一个未完成请求”仍是待实现要求。

后端在生产模式拒绝 `SEAGENT_CURRENT_USE_SYNTHETIC=1`；模式依次取 `SEAGENT_CURRENT_ENV`、`APP_ENV`、默认 `production`。数学处理层要求 `SEAGENT_CURRENT_ENV=test`；窗口服务还要求显式 `allow_synthetic_for_testing=True`。这些检查条件不完全相同，调用方应使用明确的测试配置。

客户端默认以当前解释器运行 `server.py`，只转发白名单变量及显式传入的 `env`；白名单不包含 `SEAGENT_CURRENT_ENV`。合成测试模式需像 `live_smoke.py` 一样显式传入。只读工具注解不等同于访问控制；目前没有远程 HTTP 部署入口。

## 4. 插值与窗口算法

先对 `u/v` 分量做深度线性插值，再沿时间做分段线性插值；流速为 `hypot(u,v)`，不含垂向速度。每个时间分段的向量模为凸函数，最大值可在分段端点取得。因此检查窗口端点及内部原生节点即可取得插值曲线的最大流速；此结论不约束真实未观测海流。

- CHECK：检查 `[start,end]` 是否在预报首尾覆盖范围内，以 `Vmax <= current_limit` 判断；不平移时间或写任务状态。
- SEARCH：校验可表示的正时长和步长，默认每小时推进起点，补入最后合法起点，候选上限 10,000。按开始时间、再按最大流速升序返回；不保证连续时间上的最优起点。
- 状态：`AVAILABLE`、`UNAVAILABLE`、`NOT_EVALUABLE`。结果均标记 `basis="interpolated_model_current_only"`、`execution_authorized=False`。

覆盖判断主要检查首尾范围；当前没有完整的时间间隔连续性、任意输入深度和阈值校验，也未将所有计算异常转换为 `NOT_EVALUABLE`。模型构造成功不等同于数据满足全部运行要求。

## 5. 桥接辅助类与主应用接入要求

`extract_current_query` 从任务字典、坐标或油田范围提取字段；没有时区的时间按 UTC 处理。作业深度优先用显式字段，部分海底任务从水深派生，并向传入字典写入 `operation_depth_source`；需要隔离状态的调用方应传副本。

`MarineCurrentBridge` 提供缓存写入和指纹匹配检查，没有自动过期、请求取消或在 `update_cache` 内核对最新任务的机制。`evaluate_task_window` 也不先检查缓存指纹。机器人流速限制辅助表和默认 `0.5 m/s` 在模块内定义，不是自动加载主仓库机队约束后的准入结论。

主应用接入仍需完成以下流程：

```text
已验证的 Slot 更新
  → 明确位置、作业深度、带时区的时间范围
  → 异步查询，记录查询指纹与会话取消状态
  → 返回后重新检查当前指纹、有效期与来源
  → 按当前任务和机器人约束重新计算窗口
  → 展示候选，用户确认后再走 SlotStore / Validator / 发布流程
```

位置、深度或时间范围改变后应重查；机器人限制或窗口时长改变时可在覆盖范围内重算。过时结果、取消会话和未知 freshness 不能因曾返回 `AVAILABLE` 而跳过主应用确认与门禁。这些是接入要求，不是现有辅助类已提供的完整保证。

## 6. 依赖与证据

[pyproject.toml](pyproject.toml) 声明 Python >=3.10，基础依赖包括 `pydantic>=2`、`fastmcp>=2,<4`、`mcp>=1.27,<2`、`numpy>=1.24`；`copernicus` extra 提供 `copernicusmarine>=2` 和 `xarray>=2023`。当前没有本模块专用部署锁文件，旧文档固定版本只是早期候选。

2026-09-30 离线组合运行为 **90 passed, 1 skipped**：80 项单元测试、5 项 MCP 测试及 5 项桥接测试通过，真实下载测试因 `RUN_COPERNICUS_LIVE_TEST=0` 跳过。命令见[运行记录](../../test_logs/documentation-review-2026-09-30.md)。`evidence/` 内的 41 项和 64 项通过记录是早期快照。

仍未验证：真实下载、真实海域精度、主对话流程自动集成、ROS 实机、部署依赖锁定及超时后底层 I/O 的可终止性。本轮只校正文档，没有修改这些实现。
