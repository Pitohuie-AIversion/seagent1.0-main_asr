# 海流查询与作业窗口模块

核对日期：2026-09-30。`seagent_marine_current` 提供独立的海流 MCP 查询服务及本地窗口计算。主 `DialogueManager` 尚未自动调用该模块；仓库侧集成测试显式调用桥接辅助类，不代表生产接入完成。

## 实际调用链

```text
调用方 / examples/live_smoke.py
  → Marine MCP Client（stdio）
  → FastMCP Server：get_current_forecast
  → WorkerBackend → 单线程 CurrentWorker → CopernicusProvider
  → ForecastReply

调用方提供预报、深度、时间和流速限制
  → OperationWindowService（CHECK / SEARCH）→ CurrentProcessor
  → AVAILABLE / UNAVAILABLE / NOT_EVALUABLE
```

查询只读取 `uo/vo`，不发布任务。窗口结果固定包含 `basis="interpolated_model_current_only"` 和 `execution_authorized=False`；`AVAILABLE` 只表示满足给定模型海流限制。

## 代码入口

| 文件 | 当前职责 |
| --- | --- |
| [contracts.py](src/seagent_marine_current/contracts.py) | 查询参数、预报矩阵、来源标记和查询指纹 |
| [server.py](src/seagent_marine_current/server.py)、[client.py](src/seagent_marine_current/client.py) | stdio 单工具服务、发现与调用 |
| [backend.py](src/seagent_marine_current/backend.py)、[worker.py](src/seagent_marine_current/worker.py) | 异步锁、等待超时、同步 I/O 线程池 |
| [provider.py](src/seagent_marine_current/provider.py) | Copernicus 取数与合成测试数据 |
| [processing.py](src/seagent_marine_current/processing.py)、[windows.py](src/seagent_marine_current/windows.py) | 向量插值、固定窗口检查、候选窗口搜索 |
| [integration.py](src/seagent_marine_current/integration.py) | 任务字段转换、油田坐标回查和缓存辅助类 |

## 工具契约

`get_current_forecast` 接收五个扁平参数：`latitude`、`longitude`、`operation_depth_m`、`start_time`、`end_time`。时间必须携带时区，结束须晚于开始，深度单位为米且非负。

```json
{
  "latitude": 19.6,
  "longitude": 113.0,
  "operation_depth_m": 130.0,
  "start_time": "2026-10-01T08:00:00+08:00",
  "end_time": "2026-10-02T08:00:00+08:00"
}
```

日期仅示范格式，不保证届时有数据。成功返回 `status="OK"`、非空 `data`、`error=null`；不可评估返回 `status="NOT_EVALUABLE"`、`data=null` 和结构化错误。

Provider 配置的产品为 `GLOBAL_ANALYSISFORECAST_PHY_001_024`，数据集为 `cmems_mod_glo_phy-cur_anfc_0.083deg_PT6H-i`。真实取数需要 `COPERNICUSMARINE_SERVICE_USERNAME` 和 `COPERNICUSMARINE_SERVICE_PASSWORD`；缺失时返回 `NOT_CONFIGURED`。本轮未在线核验数据集可用性和预测范围。

## 窗口计算及限制

- CHECK 检查给定时段；SEARCH 按默认一小时步长枚举开始时间，并加入最后一个合法起点，最多允许 10,000 个候选。
- 对深度和时间分别做 `u/v` 线性插值。每个时间分段的向量模是凸函数，检查窗口端点与内部原生节点即可取得**插值曲线**的最大流速；这不是对真实海流峰值的保证。
- 合成数据评估要求同时设置 `SEAGENT_CURRENT_ENV=test` 和调用参数 `allow_synthetic_for_testing=True`。
- 等待超时不会终止已经运行的 Provider 线程；缓存也没有自动过期机制。网格、深度边界及时间连续性限制见[设计与实现状态](DESIGN_SPEC.md)。

## 安装与验证

以下命令从仓库根目录执行，使用已激活的 Python 3.10+ 环境。组合测试包含主仓库桥接用例，需先完成[主仓库开发环境安装](../../docs/development/testing.md)；子包 `[test]` 只覆盖本包依赖。依赖范围以本包 [pyproject.toml](pyproject.toml) 为准，真实下载另需安装 `copernicus` extra。

```bash
python -m pip install -e 'mcp/operation-time-window[test]'
RUN_COPERNICUS_LIVE_TEST=0 python -m pytest -q \
  mcp/operation-time-window/tests_unit \
  mcp/operation-time-window/tests_mcp \
  tests/test_marine_current_dialogue_integration.py

# 启动真实 stdio 协议进程，使用合成数据，不访问 Copernicus
python mcp/operation-time-window/examples/live_smoke.py \
  --use-synthetic --output /tmp/seagent-current-synthetic.json

# 此脚本不开放合成数据评估开关，因此预期显示 NOT_EVALUABLE
python mcp/operation-time-window/examples/evaluate_saved.py \
  --file /tmp/seagent-current-synthetic.json --current-limit 0.5 --duration-hours 4
```

真实下载示例（需要网络和账号；本轮未执行）：

```bash
python -m pip install -e 'mcp/operation-time-window[copernicus]'
python mcp/operation-time-window/examples/live_smoke.py \
  --prompt-credentials --offset-hours 24 --hours 72 \
  --output /tmp/seagent-current-live.json
```

## 验证记录

2026-09-30 上述三个测试目录/文件组合运行结果为 **90 passed, 1 skipped**：本包单元测试 80 项、MCP 测试 5 项通过及 1 项跳过、仓库桥接测试 5 项。该次显式设置 `RUN_COPERNICUS_LIVE_TEST=0`，跳过真实下载测试。运行记录见[文档审查测试记录](../../test_logs/documentation-review-2026-09-30.md)。

[baseline_status.json](evidence/baseline_status.json) 与 [current_status.json](evidence/current_status.json) 均为历史快照，文件名中的 `current` 不代表实时状态。以上结果不覆盖真实 Copernicus 下载、主对话流程自动接入、ROS 实机或现场精度验证。
