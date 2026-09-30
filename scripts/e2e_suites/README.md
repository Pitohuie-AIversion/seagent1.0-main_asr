# 手动 HTTP 场景脚本

本目录保留研发期间的对话、接口和对抗场景脚本。它们直接请求运行中的后端（多处默认 `http://127.0.0.1:8890`），不属于根目录 `python -m pytest -q` 默认收集范围。脚本名称中的“全量”“工业级”等不能作为覆盖率或验收结论。

## 运行前提

这些脚本可能调用 `/api/reset`、修改机器人遥测、变更仿真时间和确认发布任务。请对独立测试服务运行，在启动服务前设置独立的 `SEAGENT_RESULT_DIR` 和指向状态副本的 `SEAGENT_STATE_FILE`。它们不会自动继承 pytest 的状态隔离；不要直接指向正在使用的会话服务。

部分场景使用固定日期、旧设备名称和回复文本断言。执行前核对所选脚本的 URL、时间基准、设备 ID 和输出路径；结果只反映该次服务配置。`OFFLINE_MOCK` 场景与真实模型测试应分别记录。现行自动化和真实模型入口见[开发测试指南](../../docs/development/testing.md)。

## 脚本索引

| 脚本 | 主要用途与范围 |
| --- | --- |
| `test_llm_full_product.py` | 多组产品场景的 HTTP 请求及结果汇总；不等于真实模型语义、ASR或安全性全面验收 |
| `hardcore_industrial_suite.py` | 工况与约束场景对话，使用脚本准备的环境和遥测 |
| `stress_adversarial_test.py`、`ultra_hard_adversarial_test.py` | 时间、坐标、指令冲突等对抗输入；不能仅凭名称认定完成并发压力测试 |
| `test_all_frontend_features.py` | 后端接口及 UI 状态字段检查，不是浏览器渲染/点击测试 |
| `full_user_comprehensive_test.py`、`test_user_experience.py` | 多轮收集、修改和确认交互 |
| `test_fleet_user_experience.py` | 机型/单机选择与状态交互，不证明多机器人协同调度已实现 |
| `verify_fixes.py`、`verify_rectifications.py` | 对应历史缺陷的定向在线检查 |
| `_tmp_full_gpu_e2e.py` | 历史 GPU 服务演练脚本；依赖与适用性需逐项核对 |

## 执行方式

在仓库根目录、已激活项目环境、独立测试服务已启动且配置核对完毕后，选择单个脚本运行：

```bash
python scripts/e2e_suites/test_user_experience.py
```

本次文档修订未启动这些在线脚本，也没有生成新的真实服务验收结果。历史输出见 `test_logs/`，应连同当时的模型模式、失败/警告数量和测试口径阅读。
