# SEAgent ROS 2 MCP 适配器 (SeagentROS2MCPAdapter) 技术说明文档

| 属性 | 内容 |
|:---|:---|
| **组件名称** | SEAgent ROS 2 MCP 适配器 (`SeagentROS2MCPAdapter`) |
| **源码路径** | `mcp/seagent_mcp_adapter.py` |
| **依赖协议库** | Anthropic 官方 `mcp` Python SDK (`mcp.client.stdio`) |
| **对接网关** | `ros-mcp-server` (RobotMCP / FastMCP 框架) |
| **底层 ROS 2 消息** | `sealien_ctrlpilot_llmbridge/msg/SysTaskCmd` (话题: `/task_cmd`) |
| **支持任务类型** | 管缆巡检 (`pipeline_inspection`)、管缆埋设 (`pipeline_burial`)、采油树控制面板插拔 (`tree_valve_operation`) |

---

## 1. 组件定位与架构职责

`SeagentROS2MCPAdapter` 作为 SEAgent 上层任务认知层与深海机器人 ROS 2 控制网关（`ros-mcp-server`）之间的核心桥梁，主要承担以下三项职责：

1. **JSON Schema 转换**：将 SEAgent 导出的 TaskIntent v2 单任务扁平 JSON，解析并装配为符合 ROS 2 工业控制协议的 `SysTaskCmd` 消息体。
2. **MCP 工具异步调用**：基于 Anthropic 官方 `mcp` Python SDK，建立与 `ros-mcp-server` 的 JSON-RPC 2.0 传输会话，调用 `publish_topic` 工具下发控制指令。
3. **遥测数据隔离获取**：通过 MCP 工具 `read_topic` 获取 ROS 2 侧 Topic `/task/system_status` 广播的物理姿态与状态推演数据，保持在内存数据结构（`TaskStatusTracker`）中，确保静态配置文件不被污染。

```text
[ SEAgent 任务认知层 ] ── TaskIntent v2 JSON
           │
           ▼
[ SeagentROS2MCPAdapter ] (mcp/seagent_mcp_adapter.py)
           │
           │  (基于 stdio / JSON-RPC 2.0 调用 MCP 工具)
           ▼
[ ros-mcp-server ] (@mcp.tool: publish_topic / read_topic)
           │
           │  (DDS 二进制数据帧 SysTaskCmd.msg / 话题: /task_cmd)
           ▼
[ ROV / AUV 水下机器人控制网关 ]
```

---

## 2. 三种核心任务类型的适配处理逻辑

适配器通过字典映射将上层语义化的 `task_type` 转换为 ROS 2 协议规定的整数枚举，并针对不同任务的数据特性装配目标位姿 `pos_target` 与控制参数 `params`。

### (1) 管缆巡检任务 (`pipeline_inspection`)

- **上层提取数据**：`start_point`（起点经纬度）、`end_point`（终点经纬度）、`water_depth_m`（水深）、`speed_ms`（巡航速度）。
- **底层枚举映射**：`task_type = 2` (`SEARCH_CABLE`, 巡缆)。
- **数据帧装配规则**：
  - `pos_target[0]`：起点位姿 $\text{Pose}(x = \text{lon}_1, y = \text{lat}_1, z = -h_{\text{water}})$
  - `pos_target[1]`：终点位姿 $\text{Pose}(x = \text{lon}_2, y = \text{lat}_2, z = -h_{\text{water}})$
  - `params`：$[h_{\text{water}}, v_{\text{speed}}]$
- **MCP Tool 下发请求 Payload 示例**：
  ```json
  {
    "topic": "/task_cmd",
    "message": {
      "task_type": 2,
      "task_id": 524289,
      "frame_id": "odom",
      "priority": 15,
      "pos_target": [
        { "position": { "x": 113.2, "y": 19.8, "z": -130.0 }, "orientation": { "x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0 } },
        { "position": { "x": 113.6, "y": 19.9, "z": -130.0 }, "orientation": { "x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0 } }
      ],
      "params": [ 130.0, 1.5 ],
      "fail_stop": true
    }
  }
  ```

---

### (2) 管缆埋设任务 (`pipeline_burial`)

- **上层提取数据**：`start_point`（埋设起始经纬度）、`water_depth_m`（水深）、`speed_ms`（埋设速度）。
- **底层枚举映射**：`task_type = 1` (`CLAMP_CABLE`, 夹缆/埋设)。
- **数据帧装配规则**：
  - `pos_target[0]`：埋设起点位姿 $\text{Pose}(x = \text{lon}, y = \text{lat}, z = -h_{\text{water}})$
  - `params`：$[h_{\text{water}}, v_{\text{speed}}]$
- **MCP Tool 下发请求 Payload 示例**：
  ```json
  {
    "topic": "/task_cmd",
    "message": {
      "task_type": 1,
      "task_id": 524290,
      "frame_id": "odom",
      "priority": 15,
      "pos_target": [
        { "position": { "x": 115.7, "y": 20.8, "z": -200.0 }, "orientation": { "x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0 } }
      ],
      "params": [ 200.0, 1.0 ],
      "fail_stop": true
    }
  }
  ```

---

### (3) 采油树控制面板插拔任务 (`tree_valve_operation`)

- **上层提取数据**：`oilfield_coordinates`（阀门/井口坐标）、`water_depth_m`（作业水深）、`wellhead_id`（井口编号）。
- **底层枚举映射**：`task_type = 4` (`INSERT_PLUG`, 采油树插拔/阀门操作)。
- **数据帧装配规则**：
  - `pos_target[0]`：阀门插孔目标位姿 $\text{Pose}(x = \text{lon}, y = \text{lat}, z = -h_{\text{water}})$
  - `params`：$[h_{\text{water}}, v_{\text{speed}}]$
- **MCP Tool 下发请求 Payload 示例**：
  ```json
  {
    "topic": "/task_cmd",
    "message": {
      "task_type": 4,
      "task_id": 524291,
      "frame_id": "odom",
      "priority": 15,
      "pos_target": [
        { "position": { "x": 115.735, "y": 20.815, "z": -300.0 }, "orientation": { "x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0 } }
      ],
      "params": [ 300.0, 1.5 ],
      "fail_stop": true
    }
  }
  ```

---

## 3. 其他控制与遥测通信支持

除了上述三种主要作业任务外，适配器同时提供如下通用控制接口支持：

1. **应急任务管理 (`task_type = 0`)**：
   通过 MCP 工具 `publish_topic` 向 `/task_cmd` 发送挂起 (SUSPEND)、恢复 (RESUME)、删除 (DELETE) 等控制指令。
2. **控制器模式配置 (`/task/sys_config`)**：
   发送 `ctr_mode` 配置控制模式（如定深模式 `AUTODEPTH=4` 或定高模式 `AUTODHEIGHT=5`）。
3. **遥测数据与推演状态读取**：
   通过 MCP 工具 `read_topic` 定期查询 `/task/system_status`，接收物理水深、距海底高度与任务状态推演（`READY` -> `PLAN` -> `ONGOING` -> `FINISH`）。

---

## 4. 代码测试与验证状态

在 `mcp/test_public_libraries_comparison.py` 测试套件中，已对 `SeagentROS2MCPAdapter` 在三种任务类型下的协议转换、MCP 工具调用以及异步响应进行了全量验证：

- **管缆巡检用例**：`test_pipeline_inspection_mcp_dispatch` (PASS)
- **管缆埋设用例**：`test_pipeline_burial_mcp_dispatch` (PASS)
- **采油树插拔用例**：`test_tree_valve_operation_mcp_dispatch` (PASS)
- **用例总数与结果**：36 / 36 项测试用例全部执行通过。
