# SEAgent 深海机器人任务智能系统
## ROS 2 MCP 双向通信模块测试与验证报告

> 历史报告：测试时间为 2026-08-21。2026-09-30 修订保留原控制台日志和 120 项统计，补充协议与 API 勘误；未重新执行当年的完整测试，不能据此认定当前环境或实机验收通过。当前目录、入口和测试命令以 [MCP 模块说明](../README.md) 为准。

本报告配套的[原 PDF](SEAgent_ROS2_MCP_Integration_Report.pdf)保留历史内容；其末页编写日期为 2026-08-26，本文测试日期为 2026-08-21，两者按原记录保留。当前接口另见[协议勘误 Markdown](adapter_specification_report.md)与[2026-09-30 协议核对版 PDF](SEAgent_ROS2_MCP_Protocol_Review_20260930.pdf)。

## 0. 2026-09-30 迁移勘误

| 历史描述 | 当前行为 |
| --- | --- |
| MCP stdio 与 rosbridge WebSocket 作为同一条生产链路 | `SeagentROS2MCPAdapter` 是本地 stdio Mock；生产通过 `SEAgentMCPBridgeService` / `RosbridgeClient` 直连 rosbridge WebSocket |
| 三类任务 `params=[水深, 航速]` | 当前生产巡检、夹缆、插入均要求 `params=[]`；水深体现在位姿 `z` |
| 插入任务 `frame_id="odom"`，未明确动作 | 当前插入使用空 `frame_id`；通用采油树类型须明确 `operation="insert"`；拔出拒绝执行 |
| 通过 `/task_manage` 管理任务 | 使用 `/task_cmd`、`task_type=0` 及动作参数；不是独立话题 |
| 经纬度已转为米制坐标 | 默认仅 longitude→x、latitude→y；局部投影须显式启用并核对原点 |
| 对话完成自动下发并完成任务 | Python 绑定操作不派发；须显式调用。Web 路由在首次进入 `done` 时尝试派发，仍须通过门禁 |
| `success` / `SENT` 表示机器人完成 | 仅表示派发状态；机器人终态是 `FINISH` 或 `FAIL`，等待超时无终态 |

第 4 节中的旧 TaskIntent 未明确插入动作，输出含旧式 `params` 与 `frame_id`，不符合当前生产转换器；日志按历史原文保留，禁止直接作为当前下发模板。第 5 节计数同样只属于历史记录。

| 属性 | 内容 |
|:---|:---|
| **项目名称** | SEAgent 任务智能层系统 |
| **测试模块** | ROS 2 MCP Server (`ros-mcp-server`) / Rosbridge Client |
| **测试类型** | 集成测试与双向通信闭环测试 |
| **测试环境** | Linux x86_64 / `ros-mcp-server` 仿真网关 |
| **测试时间** | 2026年8月21日 |
| **历史测试结果** | 原报告记录为 **通过 (PASS)** |
| **历史用例执行结果** | 原报告记录为 120 项通过，本轮未复现该次运行 |

---

## 1. 历史测试概述

本报告对 SEAgent 云端任务智能系统与 ROS 2 通信模块之间的 MCP (Model Context Protocol) 双向通信逻辑进行了测试。

测试内容包含：
- TaskIntent v2 数据格式转换与字段校验
- WebSocket 数据传递与 SysTaskCmd 消息组装
- 机器人执行生命周期状态机推演跟踪（`READY` $\to$ `PLAN` $\to$ `ENTER` $\to$ `ONGOING` $\to$ `FINISH`）
- 姿态遥测快照隔离与内存保存机制

测试集中共 120 项测试用例全部执行通过。

*说明：本节反映当时单机仿真环境（Mock Gateway）的历史记录。*

---

## 2. 方案设计说明（按当前代码核对）

1. **区分协议实验与生产派发**：
   本地 FastMCP Mock 将模拟收发封装为 `read_topic` / `publish_topic` 工具。当前生产链路使用 rosbridge WebSocket，不经由这个 stdio Mock 工具会话。

2. **模块解耦与接口设计**：
   系统通信适配逻辑存放在 `mcp/` 目录下，主要通过 `RosbridgeClient` 与 `SEAgentMCPBridgeService` 进行数据收发，支持通过配置参数指定连接的目标 IP 地址与端口。

3. **遥测数据存储处理**：
   接收到的水深、距海底高度及控制器状态等遥测数据保存在 `TaskStatusTracker` 内存数据结构中，未将其写入 `config/state.yaml` 静态配置文件。

4. **消息结构映射**：
   生产转换器将任务意图映射为 `SysTaskCmd`，校验各任务类型的位姿、参数和动作。默认经纬度作兼容映射，地理投影须显式启用。挂起 (`SUSPEND`)、恢复 (`RESUME`)、删除 (`DELETE`) 通过 `/task_cmd` 的管理命令表达。

---

## 3. 涉及的主要库函数与接口列表

以下表格按当前源码替换旧报告中的过期 API 名称；不属于 2026-08-21 原始测试证据。

| 所属模块 / 库 | 类 / 函数名称 | 功能说明 |
|:---|:---|:---|
| MCP SDK / 本地 FastMCP | `ClientSession.call_tool()`、`stdio_client()` | 实验适配器启动本地进程并调用模拟工具；不属于生产 WebSocket 派发链路 |
| `websocket-client` | `websocket.create_connection()` | 当前 `RosbridgeClient.connect()` 使用的 WebSocket 连接入口 |
| `mock/seagent_mcp_adapter.py` | `SeagentROS2MCPAdapter` | `fetch_and_sync_telemetry(state_info)` 返回模拟遥测，未修改 `state_info`；`dispatch_task_intent()` 发送旧式实验载荷 |
| `core/rosbridge_client.py` | `intent_to_syscmd()`、`build_task_manage_cmd()` | 独立函数：生成并校验普通任务或管理命令 |
| `core/rosbridge_client.py` | `RosbridgeClient.publish_task_cmd()`、`task_manage()` | 发布 `/task_cmd`；视觉订阅仍可通过 `subscribe_keypoints()` 注册 |
| `core/bridge_service.py` | `SEAgentMCPBridgeService.dispatch_intent()`、`wait_for_task_finish()` | 发送记录与派发；等待 `FINISH` 或 `FAIL`，超时返回 `None` |
| `core/task_status_tracker.py` | `TaskStatusTracker.get_task_status()`、`latest_telemetry` | 从订阅更新的内存中读取任务状态和遥测 |

---

## 4. 历史集成测试控制台日志（原文保留）

原报告记载运行 `scratch/run_live_mcp_demo.py` 得到如下输出。本轮没有重跑或改写此日志；其中载荷已由第 0 节注明过期。

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

## 5. 历史自动化测试用例统计（2026-08-21）

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

## 6. 当前联调入口与验证边界

1. **网络连接与目标配置**：独立网关控制台入口为 `MCP_MOCK=0 python -m mcp.mock.run_mcp_bridge --host 192.168.1.100 --port 9090`，需替换为现场网关地址。该 CLI 不提供交互式任务控制；Web 对话集成另见[现场联调指南](live_e2e_debugging_guide.md)。
2. **任务管理与保护字段**：普通任务默认 `fail_stop=true`；`SUSPEND`、`RESUME`、`DELETE` 使用 `/task_cmd` 上的 `TASK_MANAGE=0`，管理命令由构造器设置 `priority=0`、`fail_stop=false`。字段值不替代对现场控制器行为的验证。
3. **传感器坐标映射校验**：实机运行前需确认物理机器人的传感器坐标系（如 `odom` 或水面 GPS/DVL 基准）与位姿映射规则一致。

本轮协议与对话集成验证的命令和结果见[适配说明](adapter_specification_report.md#5-验证记录与历史统计)。当前 MCP 子套件可用 `python -m pytest -q mcp/ros-mcp/tests` 重新运行；用例数量及通过、跳过、失败情况以当次输出为准。本文不将旧 120 项结果当作当前验收结果。
