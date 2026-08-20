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
| **`rosbridge_client.py`** | **生产客户端** | **核心生产级 WebSocket 客户端**。实现完整内部协议（`UI接口协议.md`）：TaskType 枚举、`intent_to_syscmd` 转换、任务管理（TASK_MANAGE）、设备控制（CTRL_TASK）、AUV 任务、系统配置、遥测订阅，及后台监听线程。 |
| **`task_status_tracker.py`** | **状态追踪器** | **任务执行状态实时追踪**。订阅 `/task/system_status`，解析 `SysStatus.msg` 中的 `TaskStatus[]` 任务队列，提供 `wait_for_finish()` 阻塞等待与状态变化回调机制。 |
| **`seagent_mcp_adapter.py`** | stdio 适配器 | 通过 FastMCP stdio 协议与 Mock MCP 服务器交互（用于本地测试验证）。 |
| **`mock_rosbridge_server.py`** | 仿真服务端 | **Mock rosbridge WebSocket 服务端**（支持完整 `SysStatus.msg`、TASK_MANAGE 解析、任务状态生命周期自动推进）。 |
| **`mock_ros2_mcp_server.py`** | 仿真服务端 | **Mock FastMCP stdio 服务端**，用于本地无网络的 stdio 接口校验。 |
| **`test_rosbridge_client.py`** | 测试套件 | **完整内部协议测试**（35 个用例）。覆盖协议构造（K）、WebSocket 下发（L）、TASK_MANAGE 管理（M）、CTRL_TASK/AUV/sys_config（N）、遥测解析（O）、完整闭环（P）。 |
| **`test_architecture_validation.py`** | 测试套件 | **云-边-端分层架构测试**（20 个用例），验证 WebSocket 握手、公网任务下发、遥测回传及数据隔离（G, H, I, J）。 |
| **`test_public_libraries_comparison.py`** | 测试套件 | **公开库对比与模拟下发测试**（36 个用例），验证 3 大公开 ROS 2 MCP 库的契约与 SEAgent 兼容性（A~F）。 |
| **`test_real_llm_to_ros2_pipeline.py`** | E2E 脚本 | 真实端侧大模型全流程测试（需 GPU 与本地模型文件）。 |
| **`README.md`** | 文档 | 本模块的说明文档。 |

---

## 3. 使用方法与快速开始

### 3.1 运行全套 MCP 自动化测试（56 个用例）

在 SEAgent 项目根目录下执行：

```bash
/root/miniconda3/envs/seagent/bin/pytest mcp/test_architecture_validation.py mcp/test_public_libraries_comparison.py -v
```

预计结果：`56 passed in ~23s` (100% 通过)。

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
