# SEAgent 现场 ROS 2 联调指南

本文记录当前联调入口与结果判读方式。现场网关、坐标约定和机器人执行结果需在当次联调中核实；本地 Mock 测试的通过结果不能替代实机验收。

## 1. 联调准备

- 按[开发测试指南](../../../docs/development/testing.md)准备环境，并在仓库根目录运行：
  ```bash
  TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
  /root/miniconda3/envs/seagent/bin/python -m pytest -q mcp/ros-mcp/tests
  ```
  记录当次输出，不沿用历史报告中的固定用例数或通过结论。
- 确认支持船 rosbridge 网关地址、WebSocket 端口，以及[静态协议](../../../config/ros2_protocol_spec.yaml)中的消息包和话题。
- 核对接收端 odom 的坐标约定。默认转换直接使用经纬度作为 x/y；需要地理投影时应显式配置并核验参考原点，不沿用示例经纬度。
- 确认所选机器人遥测、任务时间和约束满足当前执行要求，详见[执行下发契约](../../../docs/execution_dispatch_contract.md)。

## 2. 建立连接

### 独立桥接与遥测控制台

该方式适合查看网关连接和核心遥测，不启动 Web 对话服务，也不接受交互式控制输入：

```bash
export MCP_HOST="<支持船 Topside 实际 IP>"
export MCP_PORT=9090

/root/miniconda3/envs/seagent/bin/python -m mcp.mock.run_mcp_bridge \
  --host "$MCP_HOST" --port "$MCP_PORT"
```

现场连接不要添加 `--mock`，并确保未设置 `MCP_MOCK`。建立连接后，控制台显示 `/task/system_status` 的水深、高度、控制模式和任务数量；使用 Ctrl+C 退出。

### Web 对话与统一派发

配置[ros2_runtime.yaml](../../../config/ros2_runtime.yaml)中的网关和订阅，准备本地模型后运行：

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
OFFLINE_MOCK=0 ENABLE_MCP=1 MCP_EMBEDDED_MOCK=0 \
LOCAL_MODEL_PATH=/path/to/Qwen3.5-9B \
/root/miniconda3/envs/seagent/bin/python run.py --mcp
```

`MCP_HOST` / `MCP_PORT` 可覆盖启动时的网关地址。Web 使用共享桥接实例；动态订阅和配置重载属于此主服务路径，详见[运行配置设计](../../../docs/architecture/ros2_runtime_configuration.md)。

## 3. 验证任务链路

| 业务模板 | ROS 2 映射 | 当前边界 |
| --- | --- | --- |
| 管缆巡检 `pipeline_inspection` | `SEARCH_CABLE=2` | 使用巡检起止坐标 |
| 管缆埋设 `pipeline_burial` | `CLAMP_CABLE=1` | 按模板收集并校验参数 |
| 采油树阀门 `tree_valve_operation` | 插入为 `INSERT_PLUG=4` | 拔出可归档，但协议不支持派发 |

`valve_operation` 只是协议兼容映射，不是第四类对话模板。

1. 在 Web 对话中完成参数收集和确认，核对 TaskIntent 归档结果。
2. 查看 `ros2_dispatch`：`SENT` 表示指令已写入传输或已有可核验发送证据；`BLOCKED` / `FAILED` 时先处理返回原因。
3. 未来任务返回 `SCHEDULED`。到期后显式调用 `POST /api/mcp/dispatch` 并提供会话 ID，重新检查实时执行条件。
4. 通过机器人遥测核对任务 ID 和执行状态；`FINISH` 才是执行完成，`FAIL` 是执行失败。`UNKNOWN` 需要核对发送记录，不能仅凭缺少遥测重新发送。

Python 集成可以调用 `dispatch_dialogue_result(manager, bridge, wait_finish=True)`。返回 `status="success"` 只表示派发成功；等待会在 FINISH 或 FAIL 时返回状态项，超时为 `None`。调用示例见[模块说明](../README.md)。

## 4. 任务控制接口

以下为已连接桥接对象 `bridge` 的 Python 调用，不是遥测控制台输入命令。返回的编号是控制请求编号，机器人是否执行仍以接收端和遥测为准。

| 操作 | 调用 | 结果判读 |
| --- | --- | --- |
| 挂起指定任务 | `bridge.suspend_task(task_id)` | 发送挂起请求，核对任务状态 |
| 恢复指定任务 | `bridge.resume_task(task_id)` | 发送恢复请求，核对任务状态 |
| 挂起所有任务 | `bridge.client.suspend_all()` | 协议中的挂起所有任务请求，不代表硬件急停确认 |
| 删除指定任务 | `bridge.delete_task(task_id)` | 记录 DELETE_REQUESTED；任务消失不能单独证明删除成功 |
| 清除阻塞 | `bridge.emergency_clear_block()` | 发送 CLEAR_BLOCK，核对接收端状态 |

当前任务状态协议没有 DELETED 状态。控制指令发送成功也不能直接宣称机器人队列已清空。

## 5. 排查方向

- 连接被拒绝：核对 rosbridge 是否启动、实际监听地址、端口和网络连通性。
- 连接正常但无核心遥测：核对 `/task/system_status` 的类型、订阅及机器人发布情况。
- 辅助订阅无数据：核对 `sealien_ctrlpilot_msgmanagement` 接口包及对应话题；辅助订阅不会替代核心任务状态。
- 重复消息被拦截或结果为 UNKNOWN：检查原任务 ID、持久发送记录和接收证据，按执行契约核对，避免创建新编号重复下发。
