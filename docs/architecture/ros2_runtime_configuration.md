# ROS 2 动态订阅与展示配置设计

## 理解摘要

- `config/ros2_protocol_spec.yaml` 继续作为 UI 接口协议、核心话题、消息结构、枚举和任务映射的静态权威规范。
- 新增独立运行配置，控制当前 rosbridge 网关、启用的订阅、消息解析方式和 8088 展示字段。
- 修改运行配置后无需重启 6006；系统自动加载新增、删除或修改后的订阅。
- 6006 仍是 rosbridge 连接和实时状态的唯一所有者，8088 只代理并展示 6006 的快照。
- `/task/system_status` 仍是任务闭环的核心订阅，必须与静态协议规范交叉校验。
- 任意辅助消息默认按配置字段和安全裁剪后的原始 JSON 展示，不自动推断业务语义。
- 实时 ROS 消息只保存在内存，不持续覆盖任何 YAML 文件。

## 假设与非功能要求

- 单个运行实例连接一个 rosbridge 网关，配置规模为几十个订阅。
- 运行配置每秒检查一次，前端默认每秒刷新一次。
- 配置错误时保留上一份有效订阅和连接，并通过状态 API 暴露错误。
- 配置重载采用“新连接准备成功后再替换旧连接”，避免中途丢失核心订阅。
- 图像等大消息默认禁用；原始 JSON 必须受字节上限保护。
- 前端仅使用 `textContent` 创建动态内容，不把 ROS 消息作为 HTML 注入。
- 不提供8088任务下发接口，也不把辅助消息接入任务完成判定。

## 最终文件边界

### `config/ros2_protocol_spec.yaml`

保留：

- 协议来源和主消息包；
- rosbridge 传输协议与话题目录；
- TaskIntent 到 ROS 任务类型的映射；
- `SysTaskCmd`、`SysConfig`、`SysStatus` schema 与枚举。

移出：

- `active_host`、`active_port`、`active_mode` 等部署运行值。

### `config/ros2_runtime.yaml`

负责：

- 当前网关 host、port、mode；
- 自动重载间隔；
- 启用/禁用的订阅；
- 解析器选择；
- 前端标题、字段路径、标签、单位和原始消息开关；
- 前端刷新周期与单条原始消息字节上限。

## 数据流

```text
ros2_runtime.yaml
  -> 运行配置加载与校验
  -> RosbridgeClient 订阅注册
  -> TaskStatusTracker 或通用消息快照
  -> MCPBridgeService.status_payload()
  -> 6006 /api/mcp/status
  -> 8088 /api/bridge/status
  -> ros2_dashboard.html 动态卡片
```

核心 `system_status` 继续生成原有水深、姿态、任务列表等兼容字段；新增 `dynamic_subscriptions` 结构供前端按配置渲染。

## 错误处理与边界

- YAML 不可解析、字段非法、订阅 ID 重复或核心协议不一致：拒绝新配置，保留上一份有效运行态。
- 新网关或新订阅无法建立：不替换旧连接。
- 配置删除订阅：重载后断开旧连接，删除相应运行快照和前端卡片。
- 同一 topic 可配置多个展示视图，但消息类型必须一致。
- 大消息只提取配置字段；超过上限时不返回完整 raw JSON，并标记截断。
- `system_status` 核心订阅不可禁用，且只能使用 `system_status` 解析器。

## 测试策略

- 配置加载：合法配置、非法端口、重复 ID、核心协议漂移、大消息上限。
- 客户端：同 topic 多回调和退订行为。
- 桥接服务：配置驱动订阅、通用快照、热重载成功、非法配置保留旧运行态。
- API：`/api/mcp/status` 和 8088 代理包含动态订阅元数据。
- 前端：不包含聊天入口，动态容器、字段渲染、原始 JSON 安全输出和动态轮询周期存在。
- 实际链路：真实 rosbridge 9090、ROS 模拟发布、6006 内存快照和8088页面/API同步。

## 风险

- 自动重载会短暂建立第二条 rosbridge 连接；以原子替换降低中断风险。
- 任意消息结构不可自动获得业务含义；只支持显式字段路径和原始 JSON。
- 高频大消息可能消耗内存和带宽；默认禁用图像并限制 raw JSON。
- 静态协议和运行配置可能漂移；静态目录中已登记的订阅在加载时强制交叉校验。

## 决策记录

1. 采用“静态协议 YAML + 动态运行 YAML”，不把运行订阅和前端展示写入协议规范。
2. 不进一步拆分消息 schema，避免多个静态文件之间产生一致性负担。
3. 采用自动热加载；替代方案为手动按钮或重启生效，自动方式更符合动态跟随要求。
4. 8088保持只读代理；替代方案为8088直接连接 rosbridge，被否决以避免双状态源。
5. 核心状态使用专用解析器，辅助话题使用通用字段提取；不尝试自动理解任意 ROS 消息。
6. 配置错误保留最后有效状态；不因一次编辑错误中断正在执行的任务链。

## 传感器订阅扩展 v1

### 已确认范围

- 增加四个已有明确 ROS topic 的遥测订阅：
  - `/sensor/depth`：`sealien_ctrlpilot_msgmanagement/msg/DepthStatus`
  - `/sensor/imu_dvl`：`sealien_ctrlpilot_msgmanagement/msg/ImuDvlStatus`
  - `/sensor/thruster_status`：`sealien_ctrlpilot_msgmanagement/msg/ThrusterStatus`
  - `/system/heartbeat`：`sealien_ctrlpilot_msgmanagement/msg/HeartbeatStatus`
- `/task_cmd`、`/task/sys_config`、`/task/system_status` 等任务与系统核心消息继续使用
  `sealien_ctrlpilot_llmbridge/msg/*`。
- 视觉消息 `Keypoints` 和 `ConnectChristmasTreePlug` 在 llmbridge 包中没有对应定义，
  因此仍使用 `sealien_ctrlpilot_msgmanagement/msg/*`，不做错误替换。
- BME280、ImuNav、声呐高度计和进水检测等虽有消息定义，但当前仓库没有权威 topic
  对照，本轮不推断、不启用，等待 ROS 组提供 topic 表后扩展。

### 展示与运行规则

- 四项订阅均由 `config/ros2_runtime.yaml` 驱动并使用现有 `raw` 解析器。
- 四项 topic 和消息类型同时登记在静态协议目录中，运行配置加载时禁止漂移。
- 8088 根据 YAML 中的字段路径、标题和单位生成动态卡片，不增加固定前端组件。
- 默认关闭完整原始消息，只返回用于监控的关键字段，避免数组型高频消息扩大
  6006 到 8088 的流量。
- 尚未收到消息时显示 `WAITING`，超过新鲜度阈值显示 `STALE`，收到新消息后显示
  `LIVE`。
- 辅助传感器订阅不参与任务完成判定，不写入 `config/state.yaml`，也不改变
  `/task/system_status` 的任务生命周期语义。
- YAML 热加载失败时继续使用最后一次有效订阅集合，并通过状态接口报告配置错误。

### 非功能假设

- 单实例连接单个 rosbridge，传感器频率和数量处于现有几十路订阅能力范围内。
- 本地 YAML 为可信运维输入；8088 保持只读，不新增设备控制入口。
- 热加载检查和前端刷新仍为一秒，不为传感器单独建立轮询或持久化任务。

### 扩展决策记录

7. 第一阶段只启用已有明确 topic 的四项遥测；拒绝猜测其他消息的 topic。
8. 采用通用动态订阅与字段提取；暂不开发专用传感器解析器或聚合服务。
9. 消息包按领域分工：核心任务协议使用 llmbridge，传感器与该包未定义的视觉消息
   使用 msgmanagement。
10. 传感器状态只用于实时监控；替代方案“写入业务状态并参与任务判断”被排除，
    避免在没有数值语义、告警枚举和新鲜度合同的情况下影响任务安全。

### 实施验收

- 配置加载后四个订阅的 topic、消息类型和展示字段与上述定义完全一致。
- 保存有效 YAML 后无需重启即可出现四张8088动态卡片。
- 模拟 ROS2 发布后对应卡片从 `WAITING` 转为 `LIVE`，字段值正确；停止发布后转为
  `STALE`。
- `/task/system_status`、任务下发和8088只读边界的现有回归测试继续通过。
