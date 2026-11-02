# SEAgent ROS 2 通信适配与历史 Mock 协议勘误

| 属性 | 内容 |
| --- | --- |
| 项目 / 模块 | SEAgent / ROS 2 MCP |
| 修订日期 | 2026-09-30 |
| 核验环境 | 仓库源码、协议配置与本地自动化测试；未连接真实机器人 |
| 当前实现 | `core/bridge_service.py`、`core/rosbridge_client.py` |
| 历史实验组件 | `mock/seagent_mcp_adapter.py` 中的 `SeagentROS2MCPAdapter` |
| 证据边界 | 源码核对与本地测试不代表实机验收；历史测试统计单独保留 |
| 当前 PDF | [2026-09-30 协议核对版](SEAgent_ROS2_MCP_Protocol_Review_20260930.pdf) |
| 历史 PDF | [原集成测试报告](SEAgent_ROS2_MCP_Integration_Report.pdf)，保留历史内容，不作为当前接口说明 |

## 1. 核验结论与适用范围

生产对话链路通过 `SEAgentMCPBridgeService` 和 `RosbridgeClient` 直接使用 rosbridge WebSocket。`SeagentROS2MCPAdapter` 则通过 MCP SDK 的 stdio 传输启动本地 FastMCP 模拟进程。两条链路的传输、校验与载荷构造不同。

旧版本文将实验适配器写成生产桥梁，并将 `params=[水深, 速度]` 描述为三类任务的统一协议。当前生产协议要求管缆巡检、夹缆和插入任务的 `params` 为空。Mock 仍保留旧式载荷；模拟服务器返回成功仅说明它记录了输入，不证明输入符合生产协议。

当前调用与配置见 [MCP 模块说明](../README.md)、[执行下发契约](../../../docs/execution_dispatch_contract.md)及[静态协议定义](../../../config/ros2_protocol_spec.yaml)。

## 2. 两条运行链路

### 2.1 对话任务派发

```text
DialogueManager 完成任务归档
  -> dispatch_completed_task / dispatch_dialogue_result
  -> 时间门禁、执行前校验与发送记录核对
  -> SEAgentMCPBridgeService
  -> RosbridgeClient -> rosbridge WebSocket -> /task_cmd
  <- /task/system_status -> TaskStatusTracker
```

`attach_mcp_bridge()` 只绑定引用。Python 调用者需显式派发；Web 路由在首次进入 `done` 时尝试派发。未来任务返回 `SCHEDULED`，当前没有自动到期调度器。派发状态 `SENT` 不等于机器人执行完成；完成或失败由 `FINISH` / `FAIL` 状态确认。

### 2.2 stdio 模拟实验

```text
SeagentROS2MCPAdapter
  -> stdio_client / ClientSession.call_tool
  -> mock_ros2_mcp_server.py (本地 FastMCP 进程)
  -> publish_topic: 记录载荷；read_topic: 返回模拟数据
```

实验适配器每次调用单独打开 stdio 会话。`fetch_and_sync_telemetry(state_info)` 当前只返回遥测字典，未更新传入的 `state_info`，也不接入生产 `TaskStatusTracker`。本地 Mock 接收成功不代表 DDS 发布或真实机器人动作。

## 3. 当前生产协议与逐项迁移

### 3.1 三类业务模板的下发边界

| 业务任务 | 协议枚举 | `task.details` 输入 | `frame_id` / `pos_target` | `params` |
| --- | --- | --- | --- | --- |
| 管缆巡检 | `SEARCH_CABLE=2` | `start_point` 和 `end_point`；也支持至少两个 `waypoints` | 默认 `odom`；起点和终点两个位姿 | `[]` |
| 管缆埋设 | `CLAMP_CABLE=1` | `target` | 空字符串；一个位姿 | `[]` |
| 采油树插入 | `INSERT_PLUG=4` | `target`，且动作明确为 `insert` | 空字符串；一个位姿 | `[]` |
| 采油树拔出 | 未定义 | 动作 `withdraw` 可作为计划保存 | 转换 / 派发拒绝执行 | 不生成下发载荷 |

通用的采油树任务类型必须在 `task.details.operation` 中明确动作；具有明确插入名称的类型也可提供动作语义。含糊或相互冲突的动作会被拒绝。井口编号本身不能替代目标坐标。

水深由 `location.water_depth_m` 提供，也可由各坐标的 `depth` 覆盖；默认位置映射是 `x=longitude`、`y=latitude`、`z=-depth`。只有显式启用 `use_geodetic` 时才进行局部投影，参考原点需按现场配置核对。默认数值不能直接解释为已完成米制投影。上述三类协议载荷不传输航速。

### 3.2 旧说明与当前行为对照

| 旧说明或示例 | 当前事实与迁移要求 |
| --- | --- |
| 三类任务都发送 `params=[water_depth, speed_ms]` | 生产转换器要求三类任务均为 `params=[]`；水深体现在位姿 `z` |
| 巡检只需要一个 `target` | 生产巡检要求起终点两个位姿；当前 stdio Mock 仍只生成一个 `target`，不满足此约束 |
| 夹缆、插入使用 `frame_id="odom"` | 生产协议要求这两类操作 `frame_id=""`；旧 Mock 示例不能作为接口模板 |
| `INSERT_PLUG=4` 覆盖插入和拔出 | 仅支持明确插入；拔出和缺少动作的通用采油树任务会被拒绝 |
| 适配器自动分配唯一任务 ID | stdio Mock 固定使用 `0x80001`；生产由 `generate_task_id()` 在 AI 范围内分配 ID |
| 通过 `/task_manage` 发送管理动作 | 统一发送到 `/task_cmd`，使用 `task_type=0` 与管理动作参数 |
| 所有命令均 `fail_stop=true` | 普通任务默认 true；`build_task_manage_cmd()` 使用 `priority=0`、`fail_stop=false` |
| `fetch_and_sync_telemetry` 自动同步状态 | Mock 方法只返回字典；生产由桥接服务和追踪器维护运行快照 |
| 下发成功意味着任务完成 | `SENT` 表示传输发送状态；机器人 `FINISH` / `FAIL` 才是终态 |

### 3.3 协议示例

以下是夹缆任务经生产转换器生成的结构示例，不是实机日志。示例采用默认坐标兼容映射，未启用地理投影；显式任务 ID 仅用于展示。

```json
{
  "task_type": 1,
  "task_id": 524289,
  "frame_id": "",
  "priority": 15,
  "pos_target": [
    {
      "position": {"x": 115.7, "y": 20.8, "z": -200.0},
      "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
    }
  ],
  "params": [],
  "fail_stop": true
}
```

管理命令由 `build_task_manage_cmd(action, target_task_id)` 构造：`task_type=0`、`frame_id=""`、`pos_target=[]`，`params` 包含动作枚举及该动作需要的目标 ID。系统配置仍使用 `/task/sys_config`；任务状态仍来自 `/task/system_status`。

## 4. 模块与 API 对照

下列文件均相对 `mcp/ros-mcp/`。

| 文件 | 当前 API | 职责 |
| --- | --- | --- |
| `core/dialogue_mcp_integration.py` | `attach_mcp_bridge()`、`dispatch_dialogue_result()` | 绑定、显式派发和可选等待终态 |
| `core/bridge_service.py` | `SEAgentMCPBridgeService.dispatch_intent()` | 转换、发送记录与传输调度；业务调用需先通过统一派发门禁 |
| `core/rosbridge_client.py` | `intent_to_syscmd()`、`validate_sys_task_cmd()` | 任务转换和逐类协议校验 |
| `core/rosbridge_client.py` | `RosbridgeClient.publish_task_cmd()`、`task_manage()` | 使用 WebSocket 发布任务和管理指令 |
| `core/task_status_tracker.py` | `get_task_status()`、`wait_for_finish()` | 内存任务状态；等待 `FINISH` 或 `FAIL`，超时返回 `None` |
| `mock/seagent_mcp_adapter.py` | `dispatch_task_intent()`、`fetch_and_sync_telemetry()` | stdio 模拟调用；存在上表列明的协议与同步限制 |

`RosbridgeClient` 使用 `websocket.create_connection()`；旧报告中的 `WebSocketApp`、`dispatch_sys_task_cmd()`、`build_task_manage()`、`update_task_status()` 不是当前这条调用链的 API。

## 5. 验证记录与历史统计

本轮对照了协议转换器、动作校验、Mock 实现、桥接服务及现有协议测试。2026-09-30 使用维护环境 `/root/miniconda3/envs/seagent/bin/python` 执行以下命令，结果为 **11 passed in 4.31s**，无失败、跳过或测试警告。这一小范围测试不覆盖全部 MCP 功能。

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
/root/miniconda3/envs/seagent/bin/python -m pytest -q \
  mcp/ros-mcp/tests/test_ros_group_protocol_contract.py \
  mcp/ros-mcp/tests/test_dialogue_mcp_integration.py
```

| 验证范围 | 对应套件 | 本次结果 |
| --- | --- | --- |
| 协议布局、管理参数和 Topic 定义 | `test_ros_group_protocol_contract.py` | 通过 |
| 显式派发与对话集成 | `test_dialogue_mcp_integration.py` | 通过 |
| 合计 | 上述两份套件 | 11 通过；0 失败；0 跳过 |

历史说明曾记录 `test_public_libraries_comparison.py` 为 36 / 36 通过。这一数字保留为历史材料，本轮未复现该次执行。旧说明中的 `test_pipeline_inspection_mcp_dispatch`、`test_pipeline_burial_mcp_dispatch`、`test_tree_valve_operation_mcp_dispatch` 也不是当前测试方法名，不应继续作为可执行节点引用。

2026-08-21 的 120 项统计及旧控制台日志见[历史测试报告](mcp_execution_verification_report.md)。原 PDF 末页标记编写日期为 2026-08-26，与 Markdown 记录的测试日期不同，两个日期按原记录保留。原 PDF 不变；本次协议核对版 PDF 与本文配套，不替代历史实测记录。真实 ROS 2 网关、设备动作和现场坐标校准尚未在本轮验证。
