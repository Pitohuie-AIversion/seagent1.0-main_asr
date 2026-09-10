# Scratch Directory (`scratch/`)

本目录主要用于开发者本地临时演练、临时调试脚本与一次性排查工具。

---

## 目录使用规则

1. **禁止正式模块与测试直接依赖 `scratch/`**：
   - 所有正式测试用例、服务模块和 CI 流程均严禁直接 `import scratch.*`；
   - 正式运维脚本、测试报表生成器与 ROS2 遥测仿真器已正规化迁移至 `scripts/`（如 `scripts/generate_report.py`、`scripts/run_ros2_telemetry_echo_node.py`）；
   - 本目录下保留了同名 facade 脚本仅用于兼容历史本地调用。

2. **目录内容分类**：
   - `ros2_mcp_test/`：独立 ROS2 MCP 集成联调沙箱；
   - `test_state.yaml`：MCP 测试环境状态夹具；
   - 其他历史排查与资产更新脚本：为开发阶段历史沉淀，不作为生产交付产物。
