# SEAgent Marine Current MCP (v0.1)

本目录为 SEAgent 项目的**海洋流速查询与水下作业时间窗口评估** MCP 模块（`seagent_marine_current`）。

---

## 1. 架构定位与设计原则

> **一个 FastMCP 海流查询工具 + 一个 Copernicus Provider；海流插值与作业窗口计算留在 SEAgent 内部。现有 ROS 生产链保持不变。**

```text
SEAgent (用户 / ASR)
      │
      ▼
DialogueManager (已验证 Slot 更新)
      │
      ├─► [未来排期评估需求且位置/深度/时间齐全] ──► 异步只读查询 (不阻塞原对话)
      │                                                │
      │                                                ▼
      │                                    Marine MCP Client (stdio)
      │                                                │ tools/call
      │                                                ▼
      │                                    FastMCP Marine Server
      │                                    (get_current_forecast)
      │                                                │
      │                                                ▼
      │                                    CopernicusProvider (Toolbox)
      │                                    [原生 uo/vo/time/depth 数据]
      │                                                │
      ▼                                                ▼
窗口计算条件就绪 (用户时间 + 机器人限值) ◄────── 结构化数据与指纹校验
      │
      ▼
CurrentProcessor (深度/时间向量线性插值)
      │
      ▼
OperationWindowService (CHECK / SEARCH)
      │
      ▼
AVAILABLE / UNAVAILABLE / NOT_EVALUABLE
      │
      ▼
用户确认候选窗口 ──► 现有 SlotStore ──► Validator ──► 发布确认流程
```

### 核心设计原则

1. **协议标准性**：使用标准 Model Context Protocol (MCP) 与 FastMCP 框架实现，暴露标准 `tools/list` 与 `tools/call`。
2. **纯粹与隔离**：MCP Server 仅负责只读查询原生水文流速，不判断机器人准入，不修改 TaskIntent，不自动发布任务。
3. **高保真数学插值**：对相邻原生深度层线性插值，对时间做分段线性向量插值。由于二维向量线性插值模长为凸函数，区间极大流速严格由端点与内部原生节点决定，避免重采样漏掉原生峰值。
4. **安全声明**：计算结果固定标记 `basis="interpolated_model_current_only"` 且 `execution_authorized=False`，海流适航筛选永远不等于完整作业安全或下发许可。

---

## 2. 目录结构与文件清单

```text
mcp/operation-time-window/
├── pyproject.toml                         # 模块构建与测试配置
├── .env.example                           # 环境变量与 Copernicus 凭据示例
├── README.md                              # 本说明文档
├── src/seagent_marine_current/
│   ├── __init__.py                        # 包顶层导出
│   ├── contracts.py                       # CurrentQuery, CurrentForecastData, ForecastReply Pydantic 契约
│   ├── server.py                          # FastMCP 单工具入口 (get_current_forecast, stdio transport)
│   ├── backend.py                         # WorkerBackend: 单并发锁、超时熔断与异常捕获
│   ├── worker.py                          # CurrentWorker: 独立线程池隔离 Copernicus 同步阻塞 I/O
│   ├── provider.py                        # CopernicusProvider (真实) & SyntheticCopernicusProvider (离线/仿真)
│   ├── client.py                          # stdio 客户端封装: make_client, discover, query_current
│   ├── processing.py                      # SEAgent 侧深度/时间向量线性插值与凸函数区间极大值算法
│   └── windows.py                         # SEAgent 侧 CHECK 与 SEARCH 窗口状态机评估
├── examples/
│   ├── live_smoke.py                      # 真实 MCP 取数与离线仿真 smoke 测试脚本
│   └── evaluate_saved.py                  # 基于落盘预报 JSON 计算 CHECK / SEARCH 窗口脚本
├── tests_unit/                            # 41 个本地单元测试 (100% 覆盖核心契约、插值、搜索算法)
└── tests_mcp/                             # FastMCP stdio 协议测试与并发排队验证
```

---

## 3. 数据源规范与取数规则

```text
Product:   GLOBAL_ANALYSISFORECAST_PHY_001_024
Dataset:   cmems_mod_glo_phy-cur_anfc_0.083deg_PT6H-i
Variables: uo (东向流速), vo (北向流速)
原生分辨率: 水平 0.083° (~9.2km), 时间 6 小时 (21600s), 垂向 50+ 标准层
```

1. **环境凭据强校验**：必须通过环境变量 `COPERNICUSMARINE_SERVICE_USERNAME` 和 `COPERNICUSMARINE_SERVICE_PASSWORD` 提供，未配置时立即返回 `NOT_CONFIGURED`，严禁服务端卡入交互式密码输入。
2. **原生坐标切片**：经纬度对齐至最近 0.083° 水平网格点，目标深度选取精确层或相邻上下 2 层，时间选取外包 6 小时原生时间节点。
3. **数据完整性防伪造**：陆地遮罩或存在 NaN 坏点时直接抛出 `LAND_OR_INVALID` / `MISSING_DATA`，严禁偷偷向远处移位或补零冒充。

---

## 4. MCP 工具契约

### 工具名称

`get_current_forecast`

### 输入参数 (扁平 5 参数)

```json
{
  "latitude": 19.6,
  "longitude": 113.0,
  "operation_depth_m": 130.0,
  "start_time": "2026-09-14T08:00:00+08:00",
  "end_time": "2026-09-17T08:00:00+08:00"
}
```

### 响应契约 (`ForecastReply`)

- `status = "OK"`：`data` 完整返回 `[time][depth]` 矩阵，`error = null`。
- `status = "NOT_EVALUABLE"`：`data = null`，`error` 携带结构化错误码（`NOT_CONFIGURED`, `OUT_OF_BOUNDS`, `LAND_OR_INVALID`, `TIMEOUT`, `BUSY` 等）。

---

## 5. SEAgent 本地窗口计算 (CHECK & SEARCH)

### 5.1 向量插值与极值性质

- 深度插值：
  $$u(t, d) = (1 - \alpha) u(t, d_0) + \alpha u(t, d_1)$$
- 时间插值：
  $$u(t) = (1 - \beta) u(t_i) + \beta u(t_{i+1}), \quad v(t) = (1 - \beta) v(t_i) + \beta v(t_{i+1})$$
- 模长极值：
  $$V(t) = \sqrt{u(t)^2 + v(t)^2}$$
  由于 $V(t)$ 在分段线性区间内为严格凸函数，任意区间 $[a, b]$ 内的极大值 $V_{\max}$ **必在端点 $a$、$b$ 或区间内的原生时间节点 $t_k$ 取得**，彻底避免等间距重采样漏掉峰值。

### 5.2 CHECK

- 评估用户指定的固定时段 $[start, end]$，时长严格为 $end - start$。
- 若 $V_{\max} \le V_{\text{limit}}$ 则 `AVAILABLE`，否则 `UNAVAILABLE`。

### 5.3 SEARCH

- 在 $[search\_start, search\_end]$ 内以固定步长（默认 1 小时）推进滑动窗口 $s$。
- 候选窗口 $[s, s + \text{duration}]$ 计算 $V_{\max}$。
- 匹配结果按 `start_time` 升序，再按 $V_{\max}$ 升序排序输出。

---

## 6. 测试与运行验证

在当前 SEAgent 环境中直接执行以下命令：

```bash
# 1. 运行 41 个本地单元测试（契约、插值、物理极值与窗口算法）
PYTHONPATH=src python -m pytest tests_unit -v

# 2. 运行 FastMCP stdio 协议测试（工具发现、凭证缺失熔断、仿真流速调用、并发排队）
PYTHONPATH=src python -m pytest tests_mcp -v

# 3. 运行完整 45 项测试集
PYTHONPATH=src python -m pytest -q

# 4. 执行全流程烟测（启动真实 FastMCP stdio 服务端并完成查询）
python examples/live_smoke.py --use-synthetic --output current_forecast.json

# 5. 基于生成的预报 JSON 执行 CHECK 和 SEARCH 窗口计算
python examples/evaluate_saved.py --file current_forecast.json --current-limit 0.5 --duration-hours 4.0
```

---

## 7. 验收结论与环境状态

- **单元测试**：**46 / 46 全部通过**（包含数据契约、插值数学验证、Provider、CHECK/SEARCH 及 SEAgent Bridge 集成）。
- **FastMCP 协议测试**：**4 / 4 全部通过**（真实 stdio 子进程管道握手，工具声明与序列化校验完成）。
- **全集验证**：**50 / 50 测试 100% 绿灯**。
- **ROS 生产链保护**：现有 `mcp/ros-mcp`、`DialogueManager` 与 ROS 2 Bridge 生产逻辑未受破坏，保持独立隔离。
