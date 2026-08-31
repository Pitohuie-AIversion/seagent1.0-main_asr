# SEAgent 深海机器人任务智能系统
## ROS 2 MCP 双向通信模块测试与验证报告

| 属性 | 内容 |
|:---|:---|
| **项目名称** | SEAgent 任务智能层系统 |
| **测试模块** | ROS 2 MCP Server (`ros-mcp-server`) / Rosbridge Client |
| **测试类型** | 集成测试与双向通信闭环测试 |
| **测试环境** | Linux x86_64 / `ros-mcp-server` 仿真网关 |
| **测试时间** | 2026年8月21日 |
| **测试结果** | **通过 (PASS)** |
| **用例执行结果** | 120 项用例测试通过 |

---

## 1. 测试概述

本报告对 SEAgent 云端任务智能系统与 ROS 2 通信模块之间的 MCP (Model Context Protocol) 双向通信逻辑进行了测试。

测试内容包含：
- TaskIntent v2 数据格式转换与字段校验
- WebSocket 数据传递与 SysTaskCmd 消息组装
- 机器人执行生命周期状态机推演跟踪（`READY` $\to$ `PLAN` $\to$ `ENTER` $\to$ `ONGOING` $\to$ `FINISH`）
- 姿态遥测快照隔离与内存保存机制

测试集中共 120 项测试用例全部执行通过。

*说明：本报告反映当前单机仿真环境（Mock Gateway）下的测试结果。*

---

## 2. 方案设计说明

1. **采用 `ros-mcp-server` 通信设计**：
   使用开源 `ros-mcp-server`（RobotMCP）项目结构，通过 MCP 规范将 ROS 2 话题收发封装为工具函数（`read_topic` / `publish_topic`），使上层任务层解耦，不直接依赖底层系统驱动。

2. **模块解耦与接口设计**：
   系统通信适配逻辑存放在 `mcp/` 目录下，主要通过 `RosbridgeClient` 与 `SEAgentMCPBridgeService` 进行数据收发，支持通过配置参数指定连接的目标 IP 地址与端口。

3. **遥测数据存储处理**：
   接收到的水深、距海底高度及控制器状态等遥测数据保存在 `TaskStatusTracker` 内存数据结构中，未将其写入 `config/state.yaml` 静态配置文件。

4. **消息结构映射**：
   将任务意图映射为 `SysTaskCmd` 结构，进行任务 ID 编号分配、经纬度转空间坐标及深度值符号转换，同时提供了挂起 (`SUSPEND`)、恢复 (`RESUME`)、删除 (`DELETE`) 等管理接口的打包函数。

---

## 3. 涉及的主要库函数与接口列表

相关模块所调用的库函数与类定义如下表所示：

| 所属模块 / 库 | 类 / 函数名称 | 功能说明 |
|:---|:---|:---|
| **`ros-mcp-server`**<br/>*(FastMCP 框架)* | `ROSMCPGateway`<br/>`ClientSession` | • `@mcp.tool() read_topic(topic)`: 订阅 ROS 2 话题数据<br/>• `@mcp.tool() publish_topic(topic, msg)`: 发布 ROS 2 话题数据<br/>• `ClientSession.call_tool(name, args)`: 异步调用工具接口 |
| **`mcp.client.stdio`** | `stdio_client` | • `stdio_client(server_params)`: 建立 stdio 传输通道收发 JSON-RPC 2.0 消息帧 |
| **`websocket-client` / `websockets`** | `WebSocketApp` | • `WebSocketApp(url, on_message, on_error)`: 建立 WebSocket 连接，与网关建立双向通信 |
| **SeagentROS2MCPAdapter**<br/>*(`mcp/seagent_mcp_adapter.py`)* | `SeagentROS2MCPAdapter` | • `fetch_and_sync_telemetry(state_info)`: 调用 `read_topic` 获取姿态数据<br/>• `dispatch_task_intent(task_intent)`: 调用 `publish_topic` 下发任务指令 |
| **RosbridgeClient**<br/>*(`mcp/rosbridge_client.py`)* | `RosbridgeClient` | • `dispatch_sys_task_cmd(...)`: 打包 `SysTaskCmd` 并发送至 `/task_cmd` 话题<br/>• `build_task_manage(action_code, task_id)`: 打包任务控制管理指令帧<br/>• `subscribe_keypoints(callback)`: 订阅 `/vision/keypoints` 视觉话题 |
| **SEAgentMCPBridgeService**<br/>*(`mcp/bridge_service.py`)* | `SEAgentMCPBridgeService` | • `dispatch_intent(task_intent)`: 转换 TaskIntent 并调用 `RosbridgeClient` 发送<br/>• `wait_for_task_finish(task_id, timeout)`: 等待任务状态推演至 `FINISH` 标识 |
| **TaskStatusTracker**<br/>*(`mcp/task_status_tracker.py`)* | `TaskStatusTracker` | • `update_task_status(...)`: 跟踪 `READY -> PLAN -> ONGOING -> FINISH` 状态变化<br/>• `update_telemetry(...)`: 更新内存中的物理遥测快照 |

---

## 4. 集成测试控制台日志输出

运行全链路测试脚本（`scratch/run_live_mcp_demo.py`），捕获的标准控制台输出记录如下：

```text
================================================================================
SEAgent 与 ROS 2 MCP 通信流程测试
================================================================================

[步骤 1] 启动 Topside rosbridge 仿真网关...
监听地址: ws://127.0.0.1:9099

[步骤 2] 初始化 SEAgent MCP 桥接服务...
连接建立完成，开始运行状态监听。

[步骤 3] 输入 TaskIntent v2 数据并触发发送:
Payload 数据:
{
  "schema_version": 2,
  "task_type": "tree_valve_operation",
  "priority": 15,
  "fail_stop": true,
  "location": { "oilfield": "流花11-1油田", "water_depth_m": 300.0 },
  "task": {
    "type": "tree_valve_operation",
    "details": { "target": { "latitude": 20.815, "longitude": 115.735 }, "speed_ms": 1.5 }
  },
  "equipment": { "robot_unit_id": "WROV-250-001", "robot_type": "work_class_rov" }
}
指令已发送，对应 Task ID: 0x80001 (524289)

[步骤 4] 校验网关接收到的 SysTaskCmd.msg 数据结构:
{
  "task_type": 4,
  "task_id": 524289,
  "frame_id": "odom",
  "priority": 15,
  "pos_target": [
    {
      "position": { "x": 115.735, "y": 20.815, "z": -300.0 },
      "orientation": { "x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0 }
    }
  ],
  "params": [ 300.0, 1.5 ],
  "fail_stop": true
}

[步骤 5] 任务状态推演跟踪记录 (TaskStatusTracker):
  状态更新: Task 0x80001 -> PLAN (1)
  状态更新: Task 0x80001 -> ENTER (2)
  状态更新: Task 0x80001 -> ONGOING (3)
  状态更新: Task 0x80001 -> FINISH (5)
收到最终完成标志: FINISH (Code 5)

[步骤 6] 检查内存中的遥测快照:
  TaskStatusTracker 内存快照数据:
    - 实际物理水深: 312.4m (规划目标水深: 300.0m)
    - 距海底高度: 2.5m
    - 控制器模式: Code 4 (AUTODEPTH)
    - 健康度状态: Code 0 (NORMAL)

================================================================================
测试输出记录完毕。
================================================================================
```

---

## 5. 自动化测试用例统计

| 测试套件 / 模块名称 | 用例数 | 主要测试内容 | 结果 |
|:---|:---:|:---|:---:|
| `test_public_libraries_comparison.py` | 36 | 开源 ROS 2 MCP 库接口对比与发送测试 | PASS |
| `test_architecture_validation.py` | 20 | WebSocket 通信连接与消息逻辑测试 | PASS |
| `test_rosbridge_client.py` | 35 | 协议打包、数据转换与管理指令测试 | PASS |
| `test_bridge_service.py` | 6 | 桥接服务逻辑与内存遥测保持测试 | PASS |
| `test_dialogue_mcp_integration.py` | 4 | 对话处理完成触发指令发送测试 | PASS |
| `test_bidirectional_closed_loop.py` | 6 | 状态跟踪、应急管理与视觉数据测试 | PASS |
| `test_web_backend_mcp.py` | 9 | 后端 HTTP API 接口功能测试 | PASS |
| `test_run_mcp_bridge.py / test_run_startup.py` | 4 | CLI 启动命令行与入口服务挂载测试 | PASS |
| **用例统计汇总** | **120** | **覆盖消息转换、状态跟踪及后端接口** | **120/120 PASS** |

---

## 6. 物理环境联调试验注意事项

1. **网络连接与目标配置**：现场水池或深海支持船环境联调时，需在启动命令中提供实际支持船网关工控机的 IP 地址与端口（例如 `python mcp/run_mcp_bridge.py --host 192.168.1.100 --port 9090`）。
2. **紧急停机保护**：指令下发时默认保持 `fail_stop: true`，发生信号异常或推演阻塞时可通过 `/task_manage` 接口发送 `SUSPEND` 或 `DELETE` 指令。
3. **传感器坐标映射校验**：实机运行前需确认物理机器人的传感器坐标系（如 `odom` 或水面 GPS/DVL 基准）与位姿映射规则一致。
