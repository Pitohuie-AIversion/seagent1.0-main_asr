# ADR-008：任务约束驱动的机器人候选自动收敛

## 状态

已确认，2026-08-13。

## 背景与问题

当前机器人选择已经能够根据任务模板中的 `allowed_robot_classes` 和
`required_capabilities` 构造 `Class -> Family -> Variant -> Unit` 静态候选树，
并按每层候选数执行“0 个失败关闭、1 个自动绑定、多个等待消歧”。

但是候选树尚未使用已经收集的任务条件，也没有在即时任务中使用 Unit 的
在线、空闲和状态时效信息。因此系统可能在已有条件足以唯一确定机器人时仍要求
用户选择，也可能把当前不可用的 Unit 暴露为候选。

此外，正常对话录入路径会校验完整四级关系，但 Validator 与 Snapshot restore
边界尚未复用同一校验，历史或外部状态可能携带彼此不一致的四级选择。

## 已确认范围

本轮实现以下行为：

- 任务类型仍先通过 Class 与 Family capability 门禁；
- 已确认 `water_depth` 时，用 `Variant.hard_params.max_depth_m` 过滤型号；
- 已确认 `payload` 时，要求每项都属于 `onboard_payloads` 与
  `supported_payloads` 的并集；
- 对开始时间已确定且位于当前十分钟窗口内的任务，用在线、空闲及状态时效过滤
  Unit；未来任务不使用当前忙闲状态，仍在执行或发布边界重检；
- 过滤结果向上裁剪空的 Variant、Family 和 Class，然后执行既有 0/1/多候选规则；
- Validator 和 Snapshot restore 复用 KnowledgeBase 的四级静态关系校验。

本轮不增加 `burial_depth`。虽然机器人配置存在 `max_burial_depth_m`，当前任务
Schema 没有对应任务字段，不能从 `water_depth`、任务类型或载荷名称推断目标埋深。
也不从任务时间窗推断续航、不从坐标推断航程、不从载荷名称推断重量。

## 决策

### 一个权威候选域

`KnowledgeBase.get_feasible_robot_selection_domain()` 是机器人候选计算的唯一权威
入口。它按以下顺序计算：

1. `allowed_robot_classes` 与 `required_capabilities`；
2. Variant 静态硬参数与用户已确认任务条件；
3. 即时任务的 Unit 运行可用性；
4. 删除不再包含可行子节点的父节点。

DialogueManager 和 OutputBuilder 均消费同一候选域，避免自动绑定结果与 UI
候选列表不一致。

### 只使用可证实映射

水深判断沿用 Validator 的包含边界语义：`water_depth <= max_depth_m`。非法、非有限
或非正的任务水深，以及需要判断时缺失或非法的 `max_depth_m`，均失败关闭。

Payload 只读取用户实际确认的 `task_state.payload`。`assets.yaml` 中的 common
列表是建议知识，不作为隐式任务要求。可选搭载的 payload 仍视为静态可行，具体安装
准备不改变机器人层级关系。

### 动态状态与未来任务

只有 `start_time` 已确认且处于当前十分钟窗口内时，当前遥测才参与 Unit 预选。
没有开始时间或属于未来的任务保留全部静态可行 Unit，防止用当前忙闲状态错误决定
未来调度。无论是否参与预选，发布前的精确 Unit 可用性检查和状态版本 TOCTOU 防护
保持不变。

### 自动值可撤销

由候选唯一性产生的 `source="auto"` 不是用户偏好。当任务条件变化导致候选重新变为
多个，或原自动值不再可行时，系统清除该自动值及其下游，再按新候选域收敛。用户
显式选择属于已确认任务事实：即使它与水深或载荷组合不兼容也保留，由 Validator
形成硬约束阻断，避免把“组合不可执行”错误呈现为“设备信息缺失”。

级联同时保留一个不受水深、载荷和实时状态影响的静态 admission domain。
任务类型或父级切换后，不再属于该静态域的旧选择必须清理，不区分来源。
若深层 Variant 或 Unit 是用户明确选择，其由注册表反推的自动父层也不得
因动态可行性变化被擦除。
任务类型真正变化时，旧任务语境中尚未生效的 candidate、conflict、invalid 或
unresolved 机器人选择也必须失效；同一任务内的冲突审计状态仍完整保留。

### 边界复用

KnowledgeBase 提供从 task state 校验完整机器人选择的入口：Unit 存在时从注册表反推
缺失父级，但所有显式父级必须与 Unit 的真实 Class、Family、Variant 一致。交互期没有
Unit 的部分状态继续允许收集；Preview/Publish 必须具有明确 Unit。

Validator 在读取遥测前执行该静态校验。SlotStore Snapshot restore 在提交候选状态前
执行相同校验，失败时保持现有状态不变。快照只提供 Family、Variant 或
Unit 时，恢复过程从 Registry 补齐该选择唯一确定的父级，但不向下代替
用户选择任何子级。

当用户只明确选择 Class 或 Family，而水深、载荷或即时运行状态已使该
分支没有可行子节点时，Validator 返回 `NO_FEASIBLE_ROBOT_CANDIDATE`
失败关闭，要求修改任务条件或机器人选择。已选定具体 Variant/Unit 时不用
该通用错误抢占诊断，仍由既有 C004/C020 等具体约束说明水深或运行状态问题。

## 备选方案

### 在 DialogueManager 中逐个增加条件分支

这会让自动绑定、UI 候选、Validator 和发布边界形成多套规则，容易再次漂移，因此不采用。

### 先让用户选择，再由 Validator 拒绝

这不能满足“系统先自动收敛，无法唯一时才询问”，还会产生本可避免的交互回退，
因此不采用。

### 所有未来任务也按当前运行状态过滤

当前忙闲不能代表未来执行窗口，可能错误排除届时可用设备，因此不采用。

## 测试与验收

必须先在旧实现上得到 RED，并覆盖：

- 800 米管缆巡检排除两个 600 米 ROV 型号并自动锁定 AUV；
- 用户指定载荷能排除不支持该载荷的型号；
- 同型号两台 Unit 一忙一闲时，即时任务自动锁定空闲 Unit；
- 两台均可用时仍等待消歧，未来任务不使用当前忙闲过滤；
- 零可行候选失败关闭；
- Validator 拒绝混合 Class/Family/Variant/Unit；
- Snapshot restore 原子拒绝同类混合状态，同时允许没有 Unit 的部分状态恢复；
- Variant-only/Unit-only 快照反推并补齐唯一父级，但不自动选择新子级；
- 显式 Class/Family 分支在当前条件下局部零候选时明确硬阻断，条件放宽后可恢复；
- 普通对话、既有静态级联、运行状态发布门禁和完整离线测试集无新增回归。
