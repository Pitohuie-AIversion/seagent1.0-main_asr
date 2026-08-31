# SEAgent ROS 2 MCP 模块 (Model Context Protocol Integration)

本目录为 SEAgent 项目中**连接自然语言任务规划层与水下机器人 ROS 2 控制系统**的核心 MCP 集成模块。

---

## 1. 架构定位

采用确定好的**“云-边-端”公网协同架构**：

```text
[ 云端 SEAgent 服务器 ]
         │ (基于 TaskIntent v2 生成任务)
         ▼
[ SeagentROS2MCPAdapter ]
         │
         │  WebSocket 协议 (ws://topside-ip:9090) ─── 穿透公网/NAT
         ▼
[ 支持船 Topside 网关 (rosbridge_server / sealien_ctrlpilot_llmbridge) ]
         │
         │  rclpy / DDS / 局域网 / 脐带缆通信
         ▼
[ ROV / AUV 水下机器人 ROS 2 控制节点 ]
```

---

## 2. 核心文件清单

| 文件名 | 类型 | 职责说明 |
|:---|:---:|:---|
| **`run_mcp_bridge.py`** | **CLI 启动脚本** | **MCP 服务独立运行入口**。支持 `--host`, `--port`, `--mock` 参数，提供后台自动化桥接与控制台实时遥测面板。 |
| **`dialogue_mcp_integration.py`** | **对话闭环集成** | **DialogueManager ↔ MCP 桥接器**。提供 `attach_mcp_bridge` 与 `dispatch_dialogue_result`，将自然语言对话收集、落盘与 ROS 2 下发完全连通。 |
| **`bridge_service.py`** | **生产服务** | **SEAgent 云端 MCP 自动化桥接服务**。整合 WebSocket 客户端与 TaskStatusTracker，实现自动意图下发、遥测同步与任务生命周期追踪。 |
| **`rosbridge_client.py`** | **生产客户端** | **核心生产级 WebSocket 客户端**。实现完整内部协议（`UI接口协议.md`）：TaskType 枚举、`intent_to_syscmd` 转换、任务管理（TASK_MANAGE）、设备控制（CTRL_TASK）、AUV 任务、系统配置、遥测订阅，及无死锁后台监听线程。 |
| **`sealien_protocol.py`** | **高精度算法** | **水下协议与姿态算法组件**。实现 WGS-84 大地坐标系高精度投影 (`geodetic_to_odom_position`)、切线偏航角与四元数推算 (`yaw_between`/`pose`) 及 Payload 去重守护器 (`TaskMessageGuard`/`RequestIdGuard`)。 |
| **`task_status_tracker.py`** | **状态追踪器** | **任务执行状态实时追踪**。订阅 `/task/system_status`，解析 `SysStatus.msg` 中的 `TaskStatus[]` 任务队列，提供 `wait_for_finish()` 阻塞等待与状态变化回调机制。 |
| **`seagent_mcp_adapter.py`** | stdio 适配器 | 通过 FastMCP stdio 协议与 Mock MCP 服务器交互（用于本地测试验证）。 |
| **`mock_rosbridge_server.py`** | 仿真服务端 | **Mock rosbridge WebSocket 服务端**（支持完整 `SysStatus.msg`、TASK_MANAGE 解析、任务状态生命周期自动推进）。 |
| **`mock_ros2_mcp_server.py`** | 仿真服务端 | **Mock FastMCP stdio 服务端**，用于本地无网络的 stdio 接口校验。 |
| **`test_run_mcp_bridge.py`** | 测试套件 | **CLI 脚本测试**（3 个用例，T1~T3）。测试 CLI 参数解析、环境变量覆盖与 Mock 模式自拉起。 |
| **`test_sealien_protocol_integration.py`** | 测试套件 | **协议集成与算法测试**（8 个用例，T1~T8）。测试 WGS-84 高精度 ENU 投影、四元数航向计算、TaskMessageGuard 拦截与向后兼容性。 |
| **`test_bidirectional_closed_loop.py`** | 测试套件 | **双向收发闭环深度测试**（6 个用例，S1~S6）。涵盖动态任务跟踪、交互式中途挂起/恢复、应急清除阻断、连续姿态回传、视觉关键点双向接收、多机并发独立收发。 |
| **`test_dialogue_mcp_integration.py`** | 测试套件 | **对话流至 ROS 2 闭环测试**（4 个用例，R1~R4）。测试对话完成 (done 阶段) 触发 MCP 自动下发与等待机器人侧 FINISH 闭环。 |
| **`test_rosbridge_client.py`** | 测试套件 | **完整内部协议测试**（35 个用例，K~P）。覆盖协议构造、WebSocket 下发、TASK_MANAGE 管理、CTRL_TASK/AUV/sys_config、遥测解析、完整闭环。 |
| **`test_architecture_validation.py`** | 测试套件 | **云-边-端分层架构测试**（20 个用例，G~J），验证 WebSocket 握手、公网任务下发、遥测回传及数据隔离。 |
| **`test_public_libraries_comparison.py`** | 测试套件 | **公开库对比与模拟下发测试**（36 个用例，A~F），验证 3 大公开 ROS 2 MCP 库的契约与 SEAgent 兼容性。 |
| **`test_real_llm_to_ros2_pipeline.py`** | E2E 脚本 | 真实端侧大模型全流程测试（需 GPU 与本地模型文件）。 |
| **`README.md`** | 文档 | 本模块的说明文档。 |

---

## 3. 使用方法与快速开始

### 3.1 运行全套 MCP 自动化测试（118 个用例）

在 SEAgent 项目根目录下执行：

```bash
/root/miniconda3/envs/seagent/bin/pytest mcp/ -v
```

预计结果：`118 passed in ~40s` (100% 通过)。

### 3.2 运行真实端侧大模型 E2E 测试

确保环境具备 GPU（如 RTX 5090）且模型文件存放于指定路径后执行：

```bash
python mcp/test_real_llm_to_ros2_pipeline.py
```

### 3.3 代码调用示例

在 SEAgent 业务代码中使用适配器发送任务：

```python
from mcp.seagent_mcp_adapter import SeagentROS2MCPAdapter
from src.state_info import RobotStateInfo

# 初始化适配器
adapter = SeagentROS2MCPAdapter()

# 1. 任务下发
task_intent_v2 = {
    "schema_version": 2,
    "task_type": "tree_valve_operation",
    "location": {"oilfield": "流花11-1油田", "water_depth_m": 300.0},
    "task": {
        "details": {"target": {"latitude": 20.815, "longitude": 115.735}}
    }
}
result = await adapter.dispatch_task_intent(task_intent_v2)
print("下发结果:", result)

# 2. 遥测同步
state_info = RobotStateInfo()
telemetry = await adapter.fetch_and_sync_telemetry(state_info)
print("遥测更新成功:", telemetry)
```
