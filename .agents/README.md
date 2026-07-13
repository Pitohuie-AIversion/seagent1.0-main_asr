# README_MAIN

SEAgent 是一个面向水下 ROV 作业任务规划的对话系统。用户用自然语言描述任务，系统负责收集任务参数、规范化字段、检查任务准入条件，最后生成可下发的 TaskIntent JSON。

当前主要支持两类业务：

**1、管缆巡检**

**2、采油树控制面板阀门操作**

## 一、功能

简单来说，系统主功能可以完成两件事：先把任务信息收集完整，再判断这个任务能不能准入执行。

### 1. 任务收集

#### 1.1 普通模式

普通模式会收集完整任务信息。

管缆巡检主要字段：

- 任务类型、开始时间、结束时间、管缆位置、管缆类型、起始点、结束点、水深、设备类型、设备全称、携带工具、支持船编号

采油树控制面板阀门操作主要字段：

- 任务类型、开始时间、结束时间、水深、油田名称、油田坐标、井口编号、采油树类型、设备类型、设备全称、携带工具、支持船编号

#### 1.2 紧急模式

用户表达“紧急 / 加急 / 急”等语义时，系统进入 emergency 模式，只收集更少的必要字段。

任务类型、开始时间、起始点、结束点、水深、设备类型

任务类型、开始时间、水深、油田坐标、设备类型

紧急模式字段同样配置在 `config/task_schemas.yaml` 下。

#### 1.3 任务收集脚本

| 主要涉及的脚本 | 实现的功能 |
| --- | --- |
| `seagent/seagent/src/normalizer.py` | 把用户说出来的“非标准字段值”映射成系统配置里允许的“标准字段值”。<br>例如用户可能说工作型机器人，`normalizer`将它映射为存放在`config`/`task_schemas`里的`allowed_values`中的某一候选。<br>如果用户输入和允许值完全相等，直接返回标准值。<br>如果字段是列表，就把字符串拆成多个项，逐个归一化。<br>如果精确匹配失败，就调用 LLM，让模型从合法选项里选一个最接近的。<br>如果模型返回的结果不在合法选项里，则丢弃，返回 `None`。 |
| `seagent/seagent/src/extractor.py` | 1. 调用 LLM 从自然语言中提取任务类型、紧急模式和任务参数。<br>2. 根据任务是否已确定，分别执行任务类型识别或按模板所需字段提取。<br>3. 每轮只返回新增或修改字段的 JSON diff，并限制字段和任务类型在系统支持范围内。<br>4. 以模拟当前时间为基准，将口语时间、水深和坐标按prompt要求归一化。<br>5. 结合对话上下文理解连续指令及用户对修改建议的确认。<br>6. 对模糊 ROV 描述提取 `rov_description`，避免直接填入不确定型号。<br>7. 根据任务类型和设备列表，通过 LLM 推荐最多 3 个 ROV 候选。 |
| `seagent/seagent/src/output_builder.py` | 1. 根据 `task_type` 和运行模式读取对应的 `output_schema`。<br>2. 生成待收集字段，并过滤无需用户填写的 `auto`、`fixed` 字段。<br>3. 根据任务状态构建标准 flat JSON，同时返回缺失字段供对话继续追问。<br>4. 自动生成按任务类型、模拟日期和当日序号组成的 `task_id`，并扫描历史文件避免重复。<br>5. 校验任务类型、坐标、数值、时间、原始值、字符串和列表等字段类型。<br>6. 解析并校验内联 `allowed_values` 及 ROV、支持船、工具等 `allowed_values_ref`。<br>7. 缓存合法值解析结果，并提供字段合法值查询接口供规范化流程使用。 |

### 2. 任务准入

任务准入负责判断任务参数是否满足作业要求。准入检查由 `src/validator.py` 执行，规则配置在 `config/constraints.yaml`。

准入结果分两类：

| 类型 | 含义 | 结果 |
| --- | --- | --- |
| 硬约束 hard | 违反后任务不能发布 | 必须修改字段，否则阻塞 |
| 软约束 soft | 存在风险但可继续 | 用户确认忽略后加入白名单 |

立即任务和未来任务的检查范围不同：

| 类型 | 判定 | 检查范围 |
| --- | --- | --- |
| 立即任务 | 开始时间距离当前时间不超过 10 分钟 | 参数 + 环境 + 状态全量检查 |
| 未来任务 | 开始时间超过当前时间 10 分钟 | 只检查静态参数和静态环境，不检查实时状态 |

#### 2.1 环境准入

环境准入围绕“几何、物理、语义、预测”的四层信息架构展开：

几何层：

| 检查项 | 说明 | 源 |
| --- | --- | --- |
| 禁入区 | 作业坐标落入禁入区时，任务禁止下发。判断坐标来源包括 `start_point`、`end_point`、`oilfield_coordinates`、`cable_position`。 | `config/environment.yaml`, `src/environment_info.py` |
| 已知油田区域匹配 | 坐标落入 `oil_fields` 范围时，可查询油田名称、底质和备注，并用于后续底质约束或 TaskIntent 补充。 | `config/environment.yaml`, `src/environment_info.py` |

后续向 任务目标区域 / 任务目标对象的空间位置、支援母船位置等外部基础地图对象 拓展迭代。

物理层：

| 检查项 | 说明 | 源 |
| --- | --- | --- |
| 海床底质 | 根据作业坐标匹配油田区域的 `seabed_type`；当设备禁止该底质时触发硬约束。当前底质来源是静态环境配置。 | `config/environment.yaml`, `src/environment_info.py` |
| 浑浊度 | 从机器人状态读取 `turbidity`；中等或高浑浊度触发软提示。该字段属于设备/作业状态，不属于 `environment.yaml` 的静态地图环境。 | `config/state.yaml`, `src/state_info.py` |
| 流速 | 从机器人状态读取 `current_velocity`；超过阈值时触发软提示或硬约束。该字段属于设备/作业状态，不属于 `environment.yaml` 的静态地图环境。 | `config/state.yaml`, `src/state_info.py` |

语义层：

| 检查项 | 说明 | 源 |
| --- | --- | --- |
| DVL 底锁失效风险高区域 | 根据作业坐标匹配 `dvl_bottom_lock_failure_areas`；命中时触发软提示。 | `config/environment.yaml`, `src/environment_info.py` |
| 障碍物密集区 | 从机器人状态读取 `obstacle_density`，为 `high` 时触发软提示。当前不是基于地图区域判断。 | `config/state.yaml`, `src/state_info.py` |
| 声学信号弱区 | 从机器人状态读取 `acoustic_comms_status`。当前不是基于地图区域判断。 | `config/state.yaml`, `src/state_info.py` |
| 外部支援情况 | 从机器人状态读取 `mothership_support`。当前没有母船坐标、距离或覆盖范围计算。 | `config/state.yaml`, `src/state_info.py` |

预测层：

当前暂无预测层。基于未来海况、流速变化、能见度变化、母船移动趋势或风险演化的预测判断是未来迭代方向。

环境元属性：

| 属性 | 说明 |
| --- | --- |
| 更新时间 | 用于判断环境信息的新鲜程度。超过1 小时，就认为环境/状态信息已过期，需要更新。在validator里修改。 |
| 置信度 | 用于表示当前环境信息可信程度。 |

环境相关脚本：

- `src/environment_info.py`：按坐标查询禁入区、DVL 风险区、底质（静态）。
- `web_backend.py` `/api/robot/set-state-info`：外部系统上报流速、浊度等信息（动态）。
- `src/validator.py`：执行禁入区、DVL 风险、海床底质等环境类约束；同时读取机器人状态执行浑浊度、流速、障碍物、母船支援等状态类约束。

#### 2.2 状态准入

状态准入判断当前机器人是否适合执行任务。

生存和可用状态：

| 检查项 | 说明 | 数据来源 |
| --- | --- | --- |
| 机器人总体状态 | `unavailable` 时禁止执行任务，不再继续进行其他模块判断 | `config/state.yaml` |
| 生存状态 | 异常时优先给出保守提示，必要时拒绝任务进入 | `config/state.yaml` |

运动能力状态：

| 检查项 | 说明 | 数据来源 |
| --- | --- | --- |
| 推进器状态 | 异常时机动能力受影响，不适合执行高动态机动、复杂区域穿行或高精度贴近任务 | `config/state.yaml` |
| 定深能力 | 异常时稳定控制能力受影响，不适合执行高精度悬浮作业 | `config/state.yaml` |

感知定位状态：

| 检查项 | 说明 | 数据来源 |
| --- | --- | --- |
| 声呐状态 | 异常时声学感知、声学探测能力下降，不建议执行强依赖声呐的任务 | `config/state.yaml` |
| 视觉状态 | 异常时视觉识别、视觉观测能力下降，不建议执行强依赖视觉识别的任务 | `config/state.yaml` |

执行机构状态：

| 检查项 | 说明 | 数据来源 |
| --- | --- | --- |
| 机械臂状态 | 异常时机器人不具备基本作业执行能力 | `config/state.yaml` |
| 末端执行器状态 | 异常时整体接触式作业能力下降，不适合执行抓取、接触、操作类任务 | `config/state.yaml` |

通信协同状态：

| 检查项 | 说明 | 数据来源 |
| --- | --- | --- |
| 水声无线通信状态 | AUV 通信能力与水声无线通信捆绑，异常则提醒失效 | `config/state.yaml` |
| 与母船连接状态 | 其他集群（ROV TRENCHER）通信能力与母船连接状态捆绑，异常则失效 | `config/state.yaml` |

状态相关脚本：

- `src/state_info.py`：读写机器人实时状态。
- `web_backend.py` `/api/robot/set-state-info`：外部系统上报机器人状态。
- `src/validator.py`：执行设备状态类约束判断。

状态上报示例见四。

#### 2.3 硬参数准入

硬参数准入判断用户任务是否超过设备自身的固定能力边界。

| 检查项 | 当前实际实现 | 主要配置/脚本 |
| --- | --- | --- |
| 设备类型匹配 | 管缆巡检要求观察级，采油树操作要求工作级。`validator.py` 会比较所选设备的 `category` 与约束要求，类型不符时产生硬性违规并阻止任务发布。 | `config/robot_fleet.yaml`, `config/constraints.yaml`, `config/task_schemas.yaml` |
| 设备最大工作水深 | 已实现任务 `water_depth` 与设备 `max_depth_m` 的比较，超限时产生硬性违规。 | `config/robot_fleet.yaml`, `config/constraints.yaml` |
| 海床/土质硬适配 | 当前根据任务坐标查询环境 `seabed_type`，并与设备的 `forbidden_seabed` 比较。 | `config/robot_fleet.yaml`, `config/environment.yaml`, `config/constraints.yaml` |

未来可迭代方向：载荷能力边界、最大埋设边界、行走速度边界、转弯半径边界、续航边界、尺寸重量边界、功率边界等。

#### 2.4 其他准入

其他准入检查不属于设备硬参数，但仍会影响任务收集、确认或发布。

| 检查项 | 说明 | 主要配置/脚本 |
| --- | --- | --- |
| 任务类型范围 | 只接受当前支持的任务类型 | `config/task_schemas.yaml` |
| 必填字段完整性 | 缺字段时不能进入最终确认 | `src/output_builder.py` |
| 枚举字段合法性 | 设备、工具、船舶等必须来自合法选项 | `config/assets.yaml`, `config/robot_fleet.yaml` |
| 时间类型判断 | 根据开始时间判断立即任务/未来任务 | `src/dialogue_manager.py`, `src/simulated_time.py` |

## 二、config和src

### 1. `src` 脚本说明

| 脚本 | 功能说明 |
| --- | --- |
| `src/__init__.py` | `src` 包的统一导出入口。 |
| `src/coord_parser.py` | 因prompt效果有限，用规则的形式把其他格式的坐标（例如：北纬xx度，东经xx度等类型）转化成（lat，lon）格式。 |
| `src/dialogue_manager.py` | 对话主控制器，串联任务类型识别、字段提取、字段规范化、缺失字段判断、约束检查、回复生成、最终确认和任务输出。 |
| `src/environment_info.py` | 环境信息查询模块，将任务中传入的作业坐标与 `config/environment.yaml` 配置的油田、禁入区和 DVL 风险区经纬度范围进行比较，返回是否禁入、海床底质和 DVL 风险信息。 |
| `src/extractor.py` | 字段提取器，调用 LLM 从用户输入中提取任务类型和任务字段，只返回本轮新增/更新字段的 JSON diff。 |
| `src/history_manager.py` | 对话历史快照管理，任务完成后把会话记录、任务状态、最终 JSON 等保存到 `/root/result/history`，并支持列表和读取。 |
| `src/id_sequence.py` | 日期递增编号工具，供builder调用，并会扫描已有结果文件避免重复。 |
| `src/knowledge_retriever.py` | 知识库加载与按需检索模块，统一读取任务 schema、设备库、资源库、约束、环境和状态信息，并按当前任务状态拼接相关知识。 |
| `src/llm_client.py` | 模型调用底座。 |
| `src/normalizer.py` | 字段值规范化器，把 LLM 提取出的原始字段映射到合法枚举值，优先精确/包含匹配，必要时再调用 LLM 辅助映射。 |
| `src/output_builder.py` | 标准 JSON 构建器，根据 `task_schemas.yaml` 把 `task_state` 转成最终 flat JSON，同时判断缺失字段、解析 allowed_values_ref、生成 task_id。 |
| `src/prompts.py` | 对话回复 prompt 构建模块，根据字段缺失、硬/软约束、任务阶段、知识上下文等生成给回复模型的 system/user messages。 |
| `src/simulated_time.py` | 模拟时间管理模块，提供当前模拟时间、日期、时间戳设置和读取能力，用于任务时间判断和状态时间戳。 |
| `src/state_info.py` | 机器人状态读写模块，将外部状态上报接口传入的状态更新覆盖到 `config/state.yaml` ，同时为 `validator.py` 提供最新状态数据用于约束判断。 |
| `src/task_intent_builder.py` | TaskIntent 生成模块，用户最终确认后把任务转换为执行系统需要的 TaskIntent JSON，并写入 `/root/result/task`。 |
| `src/validator.py` | 约束验证器，根据 `constraints.yaml` 和当前任务状态执行硬/软约束校验，支持按变化字段增量检查和违规信息格式化。 |

### 2.`config` 脚本说明

| 配置文件                   | 作用                     | 常改内容                                        |
| -------------------------- | ------------------------ | ----------------------------------------------- |
| `config/assets.yaml`       | 工具、载荷、支持船等资源 | 新增 payload、vessel                            |
| `config/constraints.yaml`  | 约束规则                 | 新增或调整软硬约束                              |
| `config/environment.yaml`  | 环境和作业区信息         | 新增区域、禁入区、底质                          |
| `config/robot_fleet.yaml`  | ROV/AUV 设备库           | 新增设备、设备类型、最大水深、别名              |
| `config/state.yaml`        | 机器人状态初值           | 状态初值，每次实时端口更新并覆盖                |
| `config/task_schemas.yaml` | 任务模板和输出字段       | 新增任务类型、调整必填字段、改普通/紧急模式字段 |

### 3. 项目入口与 Web 文件

| 文件 | 功能说明 |
| --- | --- |
| `run.py` | 启动入口。加载本地大模型、知识库、ASR 服务，并启动 Flask Web 服务。 |
| `web_backend.py` | Web 后端。提供聊天、ASR、状态上报、模拟时间、历史记录等 HTTP 接口，并管理多会话 `DialogueManager`。 |
| `session.py` | 兼容前端展示的会话状态容器。保存会话 ID、对话历史、已收集字段、缺失字段、最终 JSON 和确认状态等信息。 |
| `index.html` | Web 前端演示页面。提供文字输入、语音录制、状态展示、历史记录查看等交互。 |

## 三、系统流程（*）

用户发一句话后，系统大致按下面顺序处理：

1. `web_backend.py` 的 `/api/chat` 收到用户输入。
2. 每个 `session_id` 创建一个独立的 `DialogueManager`，模型和知识库共享。
3. `DialogueManager.process()` 调用 `Extractor` 提取任务类型和字段更新。
4. `Normalizer` 对字段做规范化，`OutputBuilder` 生成当前 flat JSON，并列出缺失字段。
5. `Validator` 根据当前字段、设备库、环境信息、状态信息执行约束检查。
6. `prompts.py` 组装回复 prompt，由 `LLMClient` 调用本地大模型生成下一轮回复。
7. 用户确认后，`TaskIntentBuilder` 生成任务文件，并由 `history_manager.py` 保存历史快照。

阶段状态机在 `src/dialogue_manager.py`：

| 阶段 | 含义 |
| --- | --- |
| `collecting` | 正在收集任务字段 |
| `blocked_hard` | 硬约束违规，任务不能继续，必须修改字段 |
| `blocked_soft` | 软约束警告，用户确认忽略后可继续 |
| `confirming` | 字段齐全且无硬阻塞，等待用户最终确认 |
| `done` | 任务完成并生成结果 |
| `rejected` | 任务被拒绝或取消 |

## 四、启动方式

激活环境，

进入项目目录：

```bash
cd /root/seagent/seagent
```

启动网页演示服务：

```bash
python run.py
```

启动后访问以交互：

```text
http://服务器IP:8890
```

在端口中粘入以下内容来模拟机器人状态输入：
```shell
curl -X POST http://localhost:8890/api/robot/set-state-info -H "Content-Type: application/json" -d '{"robot_name":"sealien_inspection","params":{"current_velocity":0.3,"turbidity":3,"obstacle_density":"low","mothership_support":"strong","update_timestamp":"2026-06-18T10:00:00+08:00","confidence":0.95,"overall_status":"available","survival_status":"normal","thruster_status":"normal","depth_keeping_status":"normal","sonar_status":"normal","vision_status":"normal","arm_status":"normal","end_effector_status":"normal","acoustic_comms_status":"normal","tether_connection_status":"normal"}}'
```

如果状态数据时间过期，动态约束会拦截任务，需要更新 `update_timestamp`。

## 五、模型与环境

### 1. 大语言模型

| 项 | 当前值 |
| --- | --- |
| 模型 | Qwen3.5-9B |
| 本地路径 | `.../model/Qwen3.5-9B` |
| 加载方式 | `vllm.LLM` |
| 关键参数 | `trust_remote_code=True`, `max_num_seqs=1`, `dtype=bfloat16/float16` |
| 调用封装 | `src/llm_client.py` |

### 2. 关键依赖

完整依赖见 `requirements.txt`。重点依赖：

- `torch==2.10.0`
- `torchaudio==2.10.0`
- `transformers==4.57.6`
- `vllm==0.18.1`

## 如何使用Conda将环境迁移到新服务器？

如果只需要迁移到 AutoDL 的新云服务器，通过已经建立的镜像迁移即可，或者直接克隆实例。注意镜像迁移不包括数据盘内文件。

如果需要手动迁移，推荐使用 `conda-pack` 打包现有环境。

### 1. 在旧服务器打包

先进入当前可运行环境：

```bash
conda activate 环境名
```

安装打包工具：

```bash
conda install -c conda-forge conda-pack
```

打包当前环境，当前环境名为 `seagent` 时示例：

```bash
conda pack -n seagent -o seagent_env.tar.gz
```

如果不知道环境名：

```bash
conda env list
```

### 2. 传到新服务器

```bash
scp seagent_env.tar.gz name@新服务器IP:/root/your_target_dir
```

同时要迁移这些目录：

| 内容 | 建议目标路径 |
| --- | --- |
| 项目代码 | `/root/seagent/seagent` |
| 大模型 | `.../model/Qwen3.5-9B` |
| 结果目录 | `.../result` |
| 文档目录 | `.../doc` |

### 3. 在新服务器解压环境

```bash
mkdir -p .../envs/seagent
cd .../envs/seagent
tar -xzf .../seagent_env.tar.gz
```

激活方式：

```bash
source .../envs/seagent/bin/activate
```

首次解压后执行修复脚本：

```bash
conda-unpack
```

注：本云服务器中模型文件、结果文件、文档均位于/root/autodl-tmp下。例如llm路径：/root/autodl-tmp/model/Qwen3.5-9B

### 4. 校验迁移是否成功

```bash
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
python -c "import vllm; print('vllm ok')"
```

### 5. 迁移后检查项

- `run.py` 中 `LOCAL_MODEL_PATH` 是否指向真实大模型路径。
- CUDA 驱动版本是否满足当前 PyTorch/vLLM。
- `8890` 端口是否开放或已做 SSH 转发。
- `.../result/task` 和 `.../result/history` 是否可写。

## 六、结果输出位置

| 输出 | 路径 | 脚本 |
| --- | --- | --- |
| TaskIntent JSON | `.../result/task/task_intent_{intent_id}.json` | `src/task_intent_builder.py` |
| 对话历史快照 | `.../result/history/history_{intent_id}.json` | `src/history_manager.py` |

## 七、能力总结

- 支持自然语言任务收集。
- 支持普通模式和紧急模式。
- 支持字段增量提取，不重复追问已知字段。
- 支持时间、水深、坐标、枚举字段规范化。
- 支持 ROV 型号推荐和设备类型约束。
- 支持硬约束阻塞、软约束确认忽略。
- 支持立即任务/未来任务不同约束范围。
- 支持机器人状态外部上报。
- 支持网页演示、语音输入(branch_asr）、历史记录查看。
- 支持任务完成后生成 TaskIntent JSON。
