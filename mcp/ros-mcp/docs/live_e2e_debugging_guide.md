# SEAgent 真实端到端 (Live E2E) 调试指南与准备清单

为了确保明日与支持船 Topside / 真实 ROV 水下机器人的实际端到端联调顺畅，本文档整理了**前置检查、参数配置、联调操作步骤与紧急避险预案**。

---

## 1. 联调前置检查清单 (Checklist)

| 序号 | 检查项 | 操作 / 确认内容 | 状态 |
| :---: | :--- | :--- | :---: |
| **1** | **网络连通性** | 确认云端 SEAgent 服务器与支持船 Topside 边缘服务器处于同一局域网或公网连通。<br>`ping <topside-ip>` | 待确认 |
| **2** | **9090 端口通畅** | 确认支持船网关 9090 WebSocket 端口已开启且防火墙未拦截。<br>`nc -zv <topside-ip> 9090` | 待确认 |
| **3** | **WGS-84 参考原点** | 确认明日海试作业区域的参考点经纬度（如流花油田 `22.80169°N, 113.52497°E`）。 | 已就绪 |
| **4** | **全量代码自检** | 执行全量回归测试确认无代码隐患。<br>`/root/miniconda3/envs/seagent/bin/pytest mcp/tests/ -q` (118 PASS) | ✅ 100% PASS |

---

## 2. 明日联调步骤 (Step-by-Step Action Plan)

### 步骤一：配置支持船 Topside 网关 IP

通过环境变量设置真实的网关地址（无需修改任何 Python 代码）：

```bash
export MCP_HOST="<支持船Topside实际IP>"   # 例如 192.168.1.100 或公网 IP
export MCP_PORT=9090                      # 默认 rosbridge 端口
```

### 步骤二：拉起可视化实时遥测控制台

在 SEAgent 项目根目录下运行：

```bash
/root/miniconda3/envs/seagent/bin/python mcp/mock/run_mcp_bridge.py --host $MCP_HOST --port $MCP_PORT
```

- **预期现象**：
  1. 终端显示 `[RosbridgeClient] 已连接: ws://<topside-ip>:9090`。
  2. 自动完成 `/task/system_status` 订阅。
  3. 控制台面板开始动态刷新真实 ROV 的水深、x/y/z 坐标、电池电量与控制模式。

### 步骤三：发起 4 大官方业务任务联调测试

在 SEAgent Web 端或 Python 对话系统中发起指令：
1. **管道/电缆巡检** (`pipeline_inspection` ➔ `SEARCH_CABLE = 2`)
2. **管道/电缆埋设** (`pipeline_burial` ➔ `CLAMP_CABLE = 1`)
3. **采油树阀门操作** (`tree_valve_operation` ➔ `INSERT_PLUG = 4`)
4. **常规阀门操作** (`valve_operation` ➔ `INSERT_PLUG = 4`)

在控制台面板观察任务状态从 `PLAN` ➔ `ONGOING` ➔ 推进至 `FINISH`。

---

## 3. 紧急避险与排险预案 (Emergency Response)

若明日真实联调过程中水下 ROV 出现物理异常或紧急避险需求，请立即在 CLI 控制台或代码中执行紧急避险指令：

| 紧急场景 | 指令调用 / CLI 输入 | 底层逻辑 |
| :--- | :--- | :--- |
| **单任务挂起** | `client.publish_task_manage(TaskManageAction.SUSPEND, task_id)` | 挂起指定异常任务 |
| **全任务急停** | `client.publish_task_manage(TaskManageAction.SUSPEND_ALL)` | **最高优先级 (priority=0)** 挂起所有任务 |
| **删除问题任务**| `client.publish_task_manage(TaskManageAction.DELETE, task_id)` | 从机器人队列中彻底清除 |
| **恢复阻塞** | `client.publish_task_manage(TaskManageAction.CLEAR_BLOCK)` | 清除安全阻断，恢复正常队列 |

---

## 4. 常见问题诊断与排查

1. **`ConnectionRefusedError: [Errno 111] Connection refused`**
   - **原因**：支持船侧 `rosbridge_server` 未启动或端口错误。
   - **解决**：请支持船工程师在终端运行 `ros2 launch rosbridge_server rosbridge_websocket_launch.xml`。

2. **`TaskMessageGuard: Duplicate payload detected`**
   - **原因**：由于重试导致相同的 TaskID 下发了完全相同的 Payload。
   - **解决**：属于 Guard 正常拦截行为，防止给机器人重复发送相同的作业指令。
