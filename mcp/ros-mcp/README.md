# SEAgent ROS 2 MCP 模块

本模块连接 SEAgent 已归档的任务与 ROS 2 控制系统。任务归档、发送和机器人执行是三个不同阶段；详细状态与重试规则见[执行下发契约](../../docs/execution_dispatch_contract.md)。

## 1. 运行链路

```text
DialogueManager 确认并归档 TaskIntent
    │
    ├─ Web：routes_chat 在首次进入 done 时尝试派发
    └─ Python：显式调用 dispatch_dialogue_result()
    │
    ▼
dispatch_completed_task()：时间门禁、执行前校验、发送记录核对
    │
    ▼
SEAgentMCPBridgeService → RosbridgeClient
    │ WebSocket
    ▼
支持船 rosbridge → ROS 2 控制节点
    │ /task/system_status
    ▼
TaskStatusTracker → 任务状态和遥测快照
```

`attach_mcp_bridge()` 只给 Manager 绑定引用，不安装自动派发回调。未来任务返回 `SCHEDULED`，到期后需要显式检查并下发，当前没有后台调度器。

`SeagentROS2MCPAdapter` 通过 FastMCP stdio 启动本地模拟服务器，用于协议实验。生产对话通过上述统一入口执行校验与派发。

## 2. 文件与导入路径

| 文件 | 职责 |
| --- | --- |
| [core/dialogue_mcp_integration.py](core/dialogue_mcp_integration.py) | 绑定 Manager 与桥接服务、显式派发、可选等待机器人终态 |
| [core/bridge_service.py](core/bridge_service.py) | 发送记录、遥测同步、连接与运行快照 |
| [core/rosbridge_client.py](core/rosbridge_client.py) | WebSocket 通信、任务消息转换、任务管理和设备控制 |
| [core/sealien_protocol.py](core/sealien_protocol.py) | 协议约束、可选地理投影、姿态计算与重复消息保护 |
| [core/task_status_tracker.py](core/task_status_tracker.py) | 解析任务队列、状态回调、等待 FINISH 或 FAIL |
| [core/runtime_config.py](core/runtime_config.py) | 运行配置校验与动态订阅规则 |
| [mock/run_mcp_bridge.py](mock/run_mcp_bridge.py) | 独立桥接 CLI、可选本地 Mock 服务器、遥测控制台 |
| [mock/mock_rosbridge_server.py](mock/mock_rosbridge_server.py) | WebSocket 模拟服务器和任务生命周期推进 |
| [mock/seagent_mcp_adapter.py](mock/seagent_mcp_adapter.py)、[mock/mock_ros2_mcp_server.py](mock/mock_ros2_mcp_server.py) | 本地 stdio 模拟链路 |
| [shim/](shim/) | 对上述实现的兼容导出 |
| [tests/](tests/) | 协议、桥接、配置、任务状态与模拟收发测试 |

物理目录是 `mcp/ros-mcp/{core,mock,shim}`。[mcp/__init__.py](../__init__.py) 扩展包搜索路径，保留 `mcp.core.*`、`mcp.mock.*`、`mcp.shim.*` 导入，并兼容上游 MCP SDK。业务调用可以使用：

```python
from mcp.shim.bridge_service import SEAgentMCPBridgeService
from mcp.shim.dialogue_mcp_integration import attach_mcp_bridge, dispatch_dialogue_result
```

## 3. 协议与运行配置

[config/ros2_protocol_spec.yaml](../../config/ros2_protocol_spec.yaml) 保存仓库内的静态协议定义：

| 用途 | Topic | ROS 2 消息类型 |
| --- | --- | --- |
| 任务发布与管理 | `/task_cmd` | `sealien_ctrlpilot_llmbridge/msg/SysTaskCmd` |
| 系统配置 | `/task/sys_config` | `sealien_ctrlpilot_llmbridge/msg/SysConfig` |
| 核心状态 | `/task/system_status` | `sealien_ctrlpilot_llmbridge/msg/SysStatus` |

当前业务模板为管缆巡检、管缆埋设和采油树阀门插拔。协议只支持阀门插入 `INSERT_PLUG=4`；拔出任务可归档，但派发会阻断。`valve_operation` 是协议兼容映射，不是独立的第四类业务模板。

默认坐标兼容映射为 longitude→x、latitude→y、负水深→z；地理投影工具需显式启用并指定参考原点。现场必须核对接收端 odom 约定，不能因工具存在就假定默认已使用 WGS-84 投影。

[config/ros2_runtime.yaml](../../config/ros2_runtime.yaml) 保存网关、动态订阅、消息解析和展示策略。主服务 `run.py --mcp` 加载此文件，合法配置变更通过新连接准备后替换旧连接；非法配置保留上一次有效运行态。独立 CLI 通过 `--host` / `--port` 指定网关，不加载这套热重载配置。

可选辅助话题使用 `sealien_ctrlpilot_msgmanagement/msg/*`，需要接收端构建相应接口包。辅助遥测不替代 `/task/system_status` 的任务状态判定。具体订阅和展示规则见[运行配置设计](../../docs/architecture/ros2_runtime_configuration.md)。

## 4. 启动与测试

以下命令在仓库根目录执行；项目维护环境使用 `/root/miniconda3/envs/seagent/bin/python`。其他环境按[开发测试指南](../../docs/development/testing.md)安装依赖后替换解释器路径。

运行 ROS MCP 自动化测试：

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
/root/miniconda3/envs/seagent/bin/python -m pytest -q mcp/ros-mcp/tests
```

默认使用本地模拟服务，不连接真实机器人。部分第三方库对比测试需显式启用；用例数量和结果以当次输出为准。全仓库测试使用 `python -m pytest -q`，也包含海流 MCP 子项目。

独立本地模拟桥接与遥测控制台：

```bash
/root/miniconda3/envs/seagent/bin/python -m mcp.mock.run_mcp_bridge \
  --host 127.0.0.1 --mock
```

默认 Mock 端口为 9091；显式指定其他 `--port` 时使用指定值。控制台展示遥测，使用 Ctrl+C 退出，不提供交互式任务控制命令。

连接真实网关、启用 Web 对话派发的步骤见[现场联调指南](docs/live_e2e_debugging_guide.md)。[真实模型实验脚本](tests/test_real_llm_to_ros2_pipeline.py)需要 GPU 和本地模型，但其 MCP 接收端仍是 stdio Mock；它不会作为普通 pytest 用例运行，也不能作为实机验收结论。

## 5. Python 对话集成

以下示例假定 `manager` 已通过对话完成确认归档，`bridge` 是已启动的 `SEAgentMCPBridgeService`：

```python
from mcp.shim.dialogue_mcp_integration import attach_mcp_bridge, dispatch_dialogue_result

attach_mcp_bridge(manager, bridge)
result = dispatch_dialogue_result(manager, wait_finish=True, timeout=60.0)
print(result["ros2_dispatch"])  # 包含详细门禁状态、原因和重试信息

if result["status"] == "success":
    terminal = result["final_status_item"]
    if terminal is None:
        print("等待超时，机器人执行结果尚未确认")
    else:
        print("机器人终态：", terminal.status_name)  # FINISH 或 FAIL
else:
    print(result["message"])
```

返回值中的 `status` 表示派发结果：

| ros2_dispatch.state | status | 含义 |
| --- | --- | --- |
| SENT | success | 已写入 ROS 传输，或已有可核验发送记录 |
| SCHEDULED、UNKNOWN | pending | 未到执行时间，或发送结果仍待核对 |
| BLOCKED、FAILED | error | 门禁阻断或派发失败，详情见 `ros2_dispatch` |

`success` 不表示机器人完成任务。只有 SENT 会进入可选等待；FINISH 与 FAIL 都会返回状态项，超时返回 `None`。未设置 `wait_finish` 时，`final_status_item` 也为 `None`。缺少桥接服务抛出 `RuntimeError`；未确认归档抛出 `ValueError`。

模拟对话集成测试使用预构造 intent 和 Mock Manager；真实抽取、发布落盘与现场机器人执行需分别验证。历史报告保留在 [docs/](docs/)，其中的旧路径、固定用例数和通过结论仅适用于报告当时的版本。
