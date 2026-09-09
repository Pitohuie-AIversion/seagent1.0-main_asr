# SEAgent—ROS2 消息定义需求

版本：2026-09-04  
适用链路：SEAgent → rosbridge → ROS2 `sealien_ctrlpilot_msgmanagement`

## 1. 结论

当前 ROS 端实际消息包是 `sealien_ctrlpilot_msgmanagement`。其中 `SysTaskCmd.msg` 只支持旧版字段：`task/hole_id/x/y/z/roll/pitch/yaw`；而 SEAgent 协议需要 `task_type/task_id/priority/frame_id/pos_target/params/fail_stop`。两套定义不能直接按字段互换，必须由 ROS 组提供版本化消息，或在 MCP/ROS 适配层完成明确转换。

在本次测试阶段，建议保留现有消息不改 ABI，同时采用“新版消息优先、旧版兼容回退”的方式。

## 2. 必须保持兼容的现有消息

### `/task/sys_config`

类型：`sealien_ctrlpilot_msgmanagement/msg/SysConfig`

现有字段：

```text
uint8 task_type
uint8 task_src
float32 plan_threshold
uint8 ctr_mode
```

控制模式必须固定：`7=AUTOHOLD1`、`9=MISSION1`。建议在 `.msg` 注释中补充常量说明，代码中不要使用未命名的魔数。

### 任务命令旧版兼容接口

建议暂时保留现有旧接口（实际 topic 名称由当前 launch 配置确认）：

```text
uint8 task
uint8 hole_id
float32 x
float32 y
float32 z
float32 roll
float32 pitch
float32 yaw
```

旧接口只用于兼容测试，不应承载任务 ID、优先级、坐标系和失败策略。

## 3. 推荐新增的正式任务消息

新增：`seagent_msgs/msg/TaskCommand.msg`（或在现有包中按版本新增 `SysTaskCmdV2.msg`）：

```text
std_msgs/Header header

# 全链路唯一标识
string task_id
string intent_id
string robot_id

# 任务类型：0 TASK_MANAGE, 1 CLAMP_CABLE, 2 SEARCH_CABLE,
# 3 CLAMP_PIN, 4 INSERT_PLUG, 5 MOVE_TASK, 6 CTRL_TASK, 10 AUV_TASK
uint16 task_type
uint16 priority

# 坐标语义
string frame_id                 # odom / map / base_link
bool relative                   # true=相对当前 base_link，false=绝对 frame_id
geometry_msgs/Pose[] pos_target

# 任务参数，按 task_type 解释
float32[] params
bool fail_stop
```

### 任务类型约束

- `MOVE_TASK(5)`：必须 1 个目标点；`frame_id` 必填；`relative=true` 时目标点按 `base_link` 增量解释。
- `SEARCH_CABLE(2)`：必须 2 个点，顺序为 start、end。
- `AUV_TASK(10)`：一个或多个点；`params=[speed_mps,dive_angle_rad,ascent_angle_rad,auto_return,return_depth_m,return_speed_mps]`。
- `TASK_MANAGE(0)`：无目标点；`params[0]` 为动作码，`params[1]` 可选为目标任务 ID。
- 未使用的 `params` 必须为空，不能用 NaN 代替。

## 4. 推荐任务状态回传消息

新增：`seagent_msgs/msg/TaskStatus.msg`：

```text
std_msgs/Header header
string task_id
string intent_id
string robot_id
uint16 task_type
uint8 state          # 0 READY, 1 PLAN, 2 ENTER, 3 ONGOING,
                     # 4 EXIT, 5 FINISH, 6 PAUSE, 7 FAIL
float32 progress     # 0.0~1.0
geometry_msgs/PoseStamped current_pose
string frame_id
string error_code
string error_message
```

状态必须单调遵循 `READY → PLAN/ENTER → ONGOING → EXIT → FINISH`；失败统一进入 `FAIL`，并填写 `error_code`，不得只发自然语言日志。

## 5. 系统状态/遥测要求

现有 `SysStatus.msg` 可继续作为兼容状态，但至少要明确：

- `pose.header.frame_id` 当前到底是 `odom`、`map` 还是 `base_link`；
- `pose.header.stamp` 使用 ROS 时间还是设备时间；
- `alt` 的正方向和参考面；
- `ctr_mode` 与任务状态的优先级；
- `health=0` 的含义为无异常，非零必须能映射到错误码。

建议新增 `RobotTelemetry.msg`，包含 `PoseWithCovariance`、`TwistWithCovariance`、深度、DVL 状态、时间戳年龄和控制模式；但不应阻塞本次移动测试。

## 6. 坐标系与单位（必须写入接口协议）

1. 所有位置单位为米，角度单位为弧度；经纬度只用于导航输入，不直接写入 `Pose`。
2. `odom` 为局部笛卡尔坐标；`base_link` 为机体坐标；`map` 为全局坐标。三者的 TF 发布者和方向必须唯一。
3. `relative=true` 时，ROS 适配层应在收到命令瞬间读取当前 `base_link`，转换成当前仿真/控制器使用的 `odom` 目标；不能把相对量直接当绝对 odom。
4. 姿态使用四元数传输；仅在旧版 rpy 接口转换时使用 roll/pitch/yaw。
5. 经纬度到 odom 的原点、椭球模型、轴方向和高度基准必须由 ROS 组提供配置，不允许由 SEAgent 猜测。

## 7. Topic、类型和 QoS 约定

| 方向 | Topic | 类型 | QoS/要求 |
|---|---|---|---|
| SEAgent→ROS | `/task_cmd` | `TaskCommand`（兼容旧 `SysTaskCmd`） | reliable，深度 10 |
| SEAgent→ROS | `/task/sys_config` | `SysConfig` | reliable，深度 10 |
| ROS→SEAgent | `/task/system_status` | `SysStatus`/`TaskStatus[]` | reliable，周期 ≥2 Hz |
| ROS→SEAgent | `/task/task_status` | `TaskStatus` | reliable，状态变化立即发送 |
| ROS→SEAgent | `/tf`、`/tf_static` | TF 标准消息 | 必须可查询 `odom↔base_link` |

每次命令至少回传一次 `ACCEPTED/ENTER` 等价状态；任务结束必须回传 `FINISH` 或 `FAIL`。重复 `task_id` 必须幂等，不得重复执行。

## 8. ROS 组交付验收项

- 提供最终 `.msg` 文件、包名、topic 名、类型字符串和 `ros2 interface show` 输出；
- 提供 `odom/base_link/map` 的 TF 树和坐标转换示例；
- 用一个 `MOVE_TASK` 相对目标 `x=2.0` 完成：命令回传、状态回传、位姿变化三项可观测；
- 说明任务取消、暂停、超时、急停和重复任务 ID 的行为；
- 给出错误码表和最小 rosbag/日志字段；
- AUV 与测试 ROV 使用不同 ROS_DOMAIN_ID 或独立命名空间，测试不得影响 AUV。

## 9. 本次测试的最小落地方案

如果 ROS 组暂时不能新增消息，则由 MCP 适配层把新版 `TaskIntent` 转成旧 `SysTaskCmd`：仅允许 `MOVE_TASK` 映射到 `task=3`，把相对目标先转换成 `odom` 后填入 `x/y/z`，并在日志中保留 `task_id/intent_id/frame_id/relative`。该方案只用于联调，正式生产仍应切换到带 ID 和坐标语义的 V2 消息。
