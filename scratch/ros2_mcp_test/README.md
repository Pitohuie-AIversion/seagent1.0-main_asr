# ROS 2 MCP 测试沙箱 (ROS2 MCP Test Sandbox)

本目录为 SEAgent 与 ROS 2 MCP（Model Context Protocol）协同工作的独立测试与实验沙箱。

## 目录结构
* **`mock_ros2_mcp_server.py`**：基于 `FastMCP` 构建的 ROS 2 模拟服务端。提供 `/task/system_status` 遥测话题与 `/task_cmd` 任务指令话题。
* **`seagent_mcp_adapter.py`**：SEAgent 端的 MCP 客户端适配器。实现遥测数据向 `RobotStateInfo` 的原子同步，以及将 `TaskIntent` 转换为 `SysTaskCmd` ROS 2 消息下发。
* **`test_e2e_ros2_mcp.py`**：端到端集成测试脚本。

## 运行测试

在 `seagent` conda 环境下直接运行：

```bash
conda activate seagent
cd /root/mzy/seagent1.0-main_asr
pytest scratch/ros2_mcp_test/test_e2e_ros2_mcp.py -s -v
```
