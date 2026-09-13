# SEAgent Marine Current MCP — v0.1 代码方案与独立骨架

## 状态与范围

本包是新增的独立原型，不是已合并到 SEAgent 的功能。

- Copernicus Marine 单源，只读 `uo/vo` 海流，不取风、浪。
- FastMCP 是 MCP 的 Python 实现框架，非另一种“FastMCP 协议”。
- 初版 transport 只开放 stdio；不启动无鉴权 HTTP 服务。
- 仅暴露 `get_current_forecast`；机器人约束、CHECK/SEARCH 和任务发布留在 SEAgent。
- 时间直接使用用户输入，不自动增加准备、布放、回收或安全缓冲时长。
- 窗口结果只代表模型海流条件筛选，永远不等于完整作业安全或机器人执行许可。

**实际验证：41 个本地单元测试通过；FastMCP 协议测试在导入时因缺依赖而阻塞；本环境安装依赖遇到 DNS 失败；没有完成真实 Copernicus 下载、SEAgent 联调或 ROS 联调。详见 evidence/。**

## 1. 调用链

```text
SEAgent：用户/ASR → 已验证 Slot 更新
  → 已有未来/窗口需求，且位置、明确时间范围、operation_depth 齐备？
     否：继续对话，不调用
     是：异步只读查询，同时允许对话继续
       → MCP Client（initialize → tools/list → tools/call）
       → FastMCP Server：get_current_forecast
       → 有界 provider worker（隔离阻塞 I/O；超时/取消可终止）
       → CopernicusProvider → 官方 Toolbox → 原生 uo/vo/time/depth
       → 结构化结果与数据来源
  → 检查查询指纹仍对应当前位置/深度/时间
  → CurrentProcessor：深度线性插值、保留原生时间节点
  → 条件满足后由 OperationWindowService 执行 CHECK / SEARCH
  → AVAILABLE / UNAVAILABLE / NOT_EVALUABLE
  → 仅展示候选；用户确认后再走现有 SlotStore → Validator → 发布确认链
```

预测查询不等待 robot、task_type、duration 和全部任务字段齐全。窗口计算才等待相应约束及时间语义明确。
**原生6小时分辨率不是“只有6小时以后的任务才能触发”的定义。**近期但会持续到未来的任务，同样可能需要预测；超远期由真实时间轴覆盖检查拒绝，不硬编码“未来10天一定可取”。

## 2. 文件职责

| 文件 | 职责 |
|---|---|
| contracts.py | 输入/输出、坐标、时间、矩阵形状、查询指纹验证 |
| server.py | 单工具 FastMCP 注册；只读声明；stdio 服务入口 |
| backend.py / worker.py | 隔离官方同步 I/O、限制同时请求数、超时与取消清理 |
| provider.py | 固定产品、原生坐标选择、按点切片、数据检查 |
| client.py | 连接、工具发现、call_tool、结构化返回校验 |
| processing.py | SEAgent 侧数学处理，无网络、无写任务状态 |
| windows.py | SEAgent 侧 CHECK/SEARCH 参考实现，不暴露为 MCP tool |

内部 worker 不是第二个 MCP Server，也不是另一个 Provider。仅为避免同步 Toolbox 阻塞、stdout 污染及超时后工作仍无限继续；服务器一次最多运行一个查询，额外请求明确返回 BUSY。

## 3. 数据源和取数规则

```text
Product: GLOBAL_ANALYSISFORECAST_PHY_001_024
Dataset: cmems_mod_glo_phy-cur_anfc_0.083deg_PT6H-i
变量: uo, vo
预期原生间隔: 21600秒（6小时）
```

按官方稳定 Toolbox 2.4.1 `open_dataset` 接口编写；它返回 lazy xarray Dataset。读取坐标轴后先裁剪，再 `.load()`，不加载全球流场。

具体步骤：

1. 环境中必须提供账号与密码，否则立即 NOT_CONFIGURED，禁止服务进程进入交互登录。
2. 打开固定 dataset 的 uo/vo，读取 time/depth/latitude/longitude 坐标。
3. 在全球原生格网上选择最近水平点，返回实际网格坐标和距离；若该点/层为陆地或无效值则拒绝，不偷偷向远处找“好数据”。
4. 目标深度命中原生层则取1层，否则取上下直接相邻2层；不硬编码130m对应哪两个真实层。
5. 时间取用户区间的外包原生节点，保留首尾插值支撑；不改变用户任务时间。
6. 不允许外推、缺层跳跃、跨缺失时间间隔插值。实际6小时间隔变化时显式失败，不偷偷降级。
7. 返回矩阵形状固定 `[time][depth]`、速度单位 `m/s`、时间UTC、深度米且正向下。

请求最多10天是本包资源限制，不是安全标准，也不是预报期承诺。可用范围以此次实际 time 坐标为准。

`model_run` 仅在有明确 forecast_reference_time 时填写；缺失则 null 并警告。**retrieved_at 不能冒充模式起报时间，dataset_version 也不是 model_run。**

## 4. MCP 契约

客户端传5个扁平参数：

```json
{
  "latitude": 19.6,
  "longitude": 113.0,
  "operation_depth_m": 130,
  "start_time": "2026-09-12T08:00:00+08:00",
  "end_time": "2026-09-13T08:00:00+08:00"
}
```

上述日期仅为格式示例，真实测试脚本按运行时自动产生未来时间。

不接受调用方任意改 dataset、URL、文件路径、命令或安全阈值。账号密码不放在工具参数中。

返回 `ForecastReply`：

- `status=OK`：数据可结构化使用，`data` 非空，`error=null`。
- `status=NOT_EVALUABLE`：`data=null`，包含 `error.code/message/retryable`。
- 非法工具输入/未处理内部异常：遵循 MCP tool error，由调用层映射为不可评估；不伪造空数组成功。

数据包含 `request_fingerprint`、`snapshot_id`、provider/product/dataset、取数时间、可用的模型时间、实际网格坐标、原生时间步、原生深度层、uo/vo 矩阵及 warning。

只读注解只是给客户端的提示，不是访问控制。stdio 初版靠本机进程边界；远程化时再增加 Streamable HTTP + TLS + 身份/权限控制，不直接绑定公网无鉴权端口。

## 5. 本地流速与窗口规则

`operation_depth` 与 `water_depth` 分开。MCP 只接收前者；是否允许从海底水深派生由任务规则在上层决定，并保留 source，不由工具猜测。

对相邻原生深度层 u/v 分别线性插值，再在时间上做分段线性向量插值。流速为 `hypot(u,v)`，不是三维速度模（没有使用垂向w）。

### CHECK

给定 `[start,end]`，只检查该段。时长恰为 `end-start`；不搜索、不自动平移、不回写 Slot。

### SEARCH

给定搜索范围和 duration，候选开始时间默认每1小时推进，并纳入最后一个合法开始时间。每个候选结束时间严格为 `start+duration`，不是“5个整点=5小时”。

对每个候选求 `Vmax <= current_limit`。原生节点、窗口首尾全部参与取最大值，不因重采样丢失原生峰值。因为在一个线性u/v分段上，向量模为凸函数，所以该分段最大值位于端点。这是**插值曲线本身**的数学性质，不是对真实未观测海流的保证。

`native_time_step_seconds=21600`；`candidate_step_seconds=3600` 只是搜索起点网格；展示序列也可以每小时采样，但始终保留原生节点。SEARCH只穷举该网格，并不声称找到连续时间上的绝对最优起点。

排序固定为开始时间升序，再按Vmax升序；这是业务偏好，不称为置信度或准确率。

窗口业务状态始终只有3个：

- AVAILABLE：存在满足该模型海流限制的候选；不代表可执行。
- UNAVAILABLE：数据满足当前计算条件，但所有被检查候选均失败。
- NOT_EVALUABLE：参数/数据/调用条件不支持判断，保留 reason_code。

连续区间存在缺失时本版整体返回 NOT_EVALUABLE，不用缺失值当0，也不把“未知”叫“无窗口”。

## 6. 与现有仓库的接入

本轮读取了 ros-deployment 的 `mcp/__init__.py` 和 `mcp/core/dialogue_mcp_integration.py`。
前者扩展本地 mcp 包路径并转出部分 SDK 符号；后者仍在 done 后调用 ROS BridgeService。
本包命名为 `seagent_marine_current`，不新增另一个 `mcp` Python包，不修改上述文件。

建议作为 `services/marine_current/` 独立项目安装至专用虚拟环境，用绝对解释器路径和 `python -I -m seagent_marine_current.server` 启动。`-I` 防止从SEAgent工作目录误导入项目内的 `mcp`，但主应用内的MCP客户端仍须单独做import/协议回归测试。

最小主应用接缝（不是已实施的补丁）：

```text
已验证Slot更新
→ 由当前正确位置/时间/operation_depth 构造 CurrentQuery
→ 在应用已有异步任务机制中启动一次只读查询
→ 对话继续
→ 回来时检查 当前查询指纹 == 发起时指纹，且会话未取消
→ 匹配则缓存；不匹配则不写当前预测
→ task_type/robot/用户时间/批准流速限制齐备后，本地evaluate
```

无关备注或载荷字段更新，不应只因全局task_version变化就丢掉仍对应同一海流查询的数据。位置、深度、范围变化才重查；机器人限值或duration变化先判断是否仅需重算。首版无需事件总线。

会话缓存只复用同一查询、仍符合项目freshness规则的数据；本包不把未知model_run的数据自动认证为fresh。用户确认时还应重新核对任务、约束和snapshot绑定，不能把先前AVAILABLE当成永久执行授权。

MCP生命周期按应用/会话管理，不每一轮重新初始化。在同步DialogueManager里不要直接插入长时间阻塞下载，也不要在运行中的asyncio事件循环内调用 asyncio.run。

## 7. 安装与运行

参考依赖固定为 `fastmcp==2.14.5`、`copernicusmarine==2.4.1`，这是本方案按对应接口文档选定的候选基线，**不是声称FastMCP最新版本，也没有在本环境完成安装联调**。完整依赖锁文件应在干净环境通过MCP测试后生成，不升级现有SEAgent环境来赌兼容。

在解压目录执行：

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'

# 定向本地测试（合成数据，真实运行核心校验/插值/窗口代码）
python -m pytest tests_unit -v

# 模块协议测试（真实FastMCP in-memory/stdio，合成数据或无凭证分支）
python -m pytest tests_mcp -v

# 此独立包完整测试集；不是SEAgent全仓回归
python -m pytest -q

# 标准stdio Server由Client启动，无需手动占用另一个终端
python examples/live_smoke.py \
  --latitude 19.6 --longitude 113.0 --depth-m 130 \
  --offset-hours 24 --hours 72 --prompt-credentials \
  --output current_forecast.json
```

密码通过隐藏交互输入，仅进入进程环境，不进入LLM、命令行参数、输出JSON或Git。stdio不会自动继承所有环境变量，因此client.py仅转发所需白名单。

`live_smoke.py`返回0才表示此次真实tools/list、tools/call、Copernicus取数成功；返回2或异常都不算通过。应保存输出与运行环境，不用本地fixture替代真实验收。

## 8. 完成标准与未解决项

完成本轮海流MCP验收，需要：

1. 当前固定数据集真实取数，记录实际相邻层、时间轴、网格位置和uo/vo。
2. 真实MCP tools/list、tools/call的输入输出schema通过；stdio不会被日志污染。
3. 超时、取消、凭证失败、空数据、变更查询的旧结果不能写任务状态。
4. 在SEAgent分支接入后，普通聊天、ASR、原ROSBridge、Slot与发布回归通过。
5. 确定实际海域的空间/垂向代表性、潮汐等物理内容、起报时效和预测误差；单一6小时模型+插值不能据此承诺真实5小时连续安全。

尚未完成：真实网络取数、MCP协议运行、主仓库接入、现场精度验证、部署环境依赖锁定。
没有新增ROS MCP facade；没有远程HTTP部署；没有改TaskIntent或Slot schema；没有commit或push到用户仓库。
