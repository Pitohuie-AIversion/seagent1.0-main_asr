# 2026-09-30 文档审查验证记录

| 项目 | 本轮范围 |
| --- | --- |
| 仓库 | SEAgent，仓库根目录执行 |
| Python | 海流/ROS/当前时间模块：`/root/miniconda3/envs/seagent/bin/python`；治理矩阵/文档示例：`/root/miniconda3/bin/python`（3.12.3） |
| 变更类型 | 文档与报告；工作树已有的 Python 改动不属于本轮文档修订 |
| 结果 | 海流组合 90 passed / 1 skipped；ROS 协议与对话集成 11 passed；治理矩阵 121 passed / 2 subtests passed；后续时间模块 155 passed / 4 subtests passed；文档示例 7 项固定检查通过 |
| 未执行 | 全仓回归、真实 LLM/ASR、真实 Copernicus 下载、ROS 实机 |

## 海流组合验证

实际执行命令（当时输出至临时文件，现已复制原始日志与 JUnit）：

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 RUN_COPERNICUS_LIVE_TEST=0 \
/root/miniconda3/envs/seagent/bin/python -m pytest -q \
  mcp/operation-time-window/tests_unit \
  mcp/operation-time-window/tests_mcp \
  tests/test_marine_current_dialogue_integration.py \
  --junitxml=/tmp/seagent-docs-marine-20260930.xml \
  > /tmp/seagent-docs-marine-20260930.log 2>&1
```

退出码 0。输出末行：

```text
90 passed, 1 skipped in 8.14s
```

| 范围 | 通过 | 跳过 |
| --- | ---: | ---: |
| `tests_unit` | 80 | 0 |
| `tests_mcp` | 5 | 1 |
| `tests/test_marine_current_dialogue_integration.py` | 5 | 0 |

跳过的是受 `RUN_COPERNICUS_LIVE_TEST` 控制的真实下载测试；本次明确设置为 `0`，不能据此推断凭据是否存在。stdio 测试启动实际协议进程，但使用合成或缺少配置的测试分支。

原始文件：[stdout 日志](documentation-review-20260930/marine-pytest.stdout)、[JUnit XML](documentation-review-20260930/marine-pytest.xml)。

## ROS 协议与对话集成

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
/root/miniconda3/envs/seagent/bin/python -m pytest -q \
  mcp/ros-mcp/tests/test_ros_group_protocol_contract.py \
  mcp/ros-mcp/tests/test_dialogue_mcp_integration.py
```

退出码 0。以下是执行工具 stdout 的转录，不是另存的原始日志；当次未记录开始/结束的 UTC 秒值。

```text
...........                                                              [100%]
11 passed in 4.31s
```

该测试范围验证协议转换和 Mock 对话派发，不加载真实模型或连接实机。协议说明中的载荷示例另与 `intent_to_syscmd()` 输出比较一致，详见[协议核对报告](../mcp/ros-mcp/docs/adapter_specification_report.md)。

## 治理矩阵引用验证

执行时 `python` 解析为 `/root/miniconda3/bin/python`（Anaconda Python 3.12.3），与前两组解释器不同：

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 python -m pytest -q \
  tests/test_governance_invariants.py \
  tests/test_normalization_failure_contract.py \
  tests/test_issue_31_ui_state_contract.py \
  tests/test_model_profiles.py \
  tests/test_semantic_freedom_boundaries.py \
  tests/test_slot_consistency.py::SlotConsistencyTest::test_10_general_chat_leaves_slot_store_untouched
```

退出码 0。工具 stdout 末尾转录（未保存原始磁盘日志）：

```text
121 passed, 1 warning, 2 subtests passed in 15.45s
```

警告为 `PytestConfigWarning: Unknown config option: asyncio_mode`；该解释器未加载对应异步插件，本组同步测试仍正常完成。先前两次命令因测试节点拼写错误未完成收集，修正后才得到上述结果，不能将收集失败写成测试通过。覆盖范围与 24 个测试引用的核对见[治理验收矩阵](../docs/development/governance-acceptance-matrix.md)。

## 后续时间模块验证

为核对时间调研和历史升级报告，另行运行以下四份当前测试。此时工作树的 HEAD 为 `6ef8c92`；使用维护环境，并保留当次 stdout 和 JUnit：

```bash
TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 \
/root/miniconda3/envs/seagent/bin/python -m pytest -q \
  tests/test_time_module_v2_upgrade.py \
  tests/test_relative_time_parser.py \
  tests/test_duration_parser.py \
  tests/test_task_time_validation.py \
  --junitxml=/tmp/seagent-docs-temporal-current-20260930.xml \
  > /tmp/seagent-docs-temporal-current-20260930.stdout 2>&1
```

退出码 0。输出末行：

```text
155 passed, 4 subtests passed in 0.93s
```

原始文件：[stdout 日志](documentation-review-20260930/temporal-current.stdout)、[JUnit XML](documentation-review-20260930/temporal-current.xml)。JUnit 的 `tests=159` 包含 4 个子测试，与 pytest 的 155 个主测试通过数应分别阅读；失败、错误和跳过均为 0。

该结果仅对应当前选定测试，不能替代[时间模块历史报告](../artifacts/time_module_v2_upgrade_report.md)缺失的原 JUnit，不能据此追认其 145 项结果或自然语言识别准确率。

## 时间调研文档示例

从[历史调研草案](../artifacts/deep-research-report.md)原样提取 `resolve_local_datetime` 与 `require_unambiguous_time` 所在的两段 Python 代码，经静态检查只含标准库导入、类型和函数定义后，以隔离解释器执行：

```bash
/root/miniconda3/bin/python -I /tmp/seagent-temporal-example-check.py
```

7 项固定检查通过：上海普通时刻、洛杉矶 gap、洛杉矶 fold、拒绝 aware 输入，以及 require 函数对 gap/fold 的拒绝和普通时刻的返回。检查使用环境中已有的时区数据；未导入项目业务模块、调用厂商 API 或创建生产任务。

[示例执行记录](documentation-review-20260930/temporal-example-check.json)保存解释器、代码块哈希、输入和结果；其中行号是提取时位置，后续文档排版可能改变。临时驱动脚本未作为项目测试收录，这 7 项检查也不计入上述 pytest 用例数。该结果只支持这些固定样例，不证明完整 DST、历史时区规则或业务接入正确性。来源与实现对照见[独立核对说明](../artifacts/temporal-research-source-review.md)。

另行进行[文档保留性检查](documentation-review-20260930/temporal-research-preservation.json)：37 个引用块、32 个唯一 ID 的原始标记与顺序完整保留；两段 Python 示例与上述执行证据哈希一致；9 个 JSON 代码块均可解析。两个 `HH:MM:SS` pattern 使用 Node.js v18.20.8 的原生 ECMAScript 正则执行 48 项边界检查，全部通过。该检查不验证厂商 schema 支持或日期、时区语义，也不计入 pytest 数量。

## 结论边界

本轮测试支持上述模块和用例的结果，不能写成全仓通过。此前全量尝试中出现 `Killed`，没有完整通过结论，终止原因也未在本轮确认。历史性能、真实模型、现场实验数字均未因本次文档修订而重新验证。
