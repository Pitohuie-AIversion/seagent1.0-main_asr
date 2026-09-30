# ROS 2 MCP 历史测试沙箱

本目录使用本地 stdio Mock 模拟 ROS 消息，不需要真实机器人；它不是现行生产适配器的验收入口。生产协议与运行配置见 [ROS MCP 说明](../../mcp/ros-mcp/README.md)。

| 文件 | 行为 |
| --- | --- |
| `mock_ros2_mcp_server.py` | 用 FastMCP 模拟读取 `/task/system_status` 和发布 `/task_cmd` |
| `seagent_mcp_adapter.py` | 沙箱客户端，确实会对传入的 `RobotStateInfo` 调用 `set_status()`，并构建历史 SysTaskCmd 示例 |
| `test_e2e_ros2_mcp.py` | 显式运行的 Mock 测试，状态 fixture 使用 `tmp_path` 下的文件 |

根 pytest 配置默认排除 `scratch/`；需要时在仓库根目录显式执行：

```bash
python -m pytest scratch/ros2_mcp_test/test_e2e_ros2_mcp.py -s -v
```

测试会清理固定文件 `/tmp/mock_ros2_received_cmds.json`，不适合同时启动多个沙箱实例。单独调用适配器时，应传入隔离状态对象；不能因为测试 fixture 使用临时文件，就认为所有手动调用都不会写运行状态。

历史载荷示例不替代生产契约。本轮只核对文档和 fixture，未重新执行此沙箱，也未验证实机 ROS 链路。
