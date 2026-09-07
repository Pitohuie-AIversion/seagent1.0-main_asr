# 端到端场景演练与验收测试套件 (E2E Suites)

本目录归集了 SEAgent 1.0 系统在研发与迭代过程中用于验证全链路对话、工业级场景、对抗性压力及用户体验的集成测试脚本。

> **前置依赖**：本目录下的所有脚本均针对正在运行的后端服务（默认地址 `http://127.0.0.1:8890`）发起真实的 HTTP 请求。  
> 执行前请先在另一个终端启动后端：
> ```bash
> python run.py
> ```

---

## 脚本清单与功能索引

| 脚本文件 | 说明与验证场景 |
| :--- | :--- |
| `test_llm_full_product.py` | **大模型全流程产品测试套件**：覆盖功能正确性、输出准确性、安全性、合规性、稳定性及用户体验 6 大维度。 |
| `hardcore_industrial_suite.py` | **工业级严苛场景套件**：包含极端水流、盲区作业、超时重试、传感器异常等深水复杂工况的意图流转与约束校验。 |
| `stress_adversarial_test.py` | **对抗性压力测试**：时间悖论、陆地坐标越界、Prompt 注入与越狱、多冲突意图并发竞态压测。 |
| `ultra_hard_adversarial_test.py` | **超强对抗与容错测试**：针对边界槽位模糊输入、误导性指令的高强度防御测试。 |
| `test_all_frontend_features.py` | **前端接口与交互特性全量验证**：测试 `/api/chat`、ASR 上传、UI State 契约及状态同步。 |
| `full_user_comprehensive_test.py` | **全流程用户交互综合测试**：多轮人机交互、槽位补充与最终发布确认。 |
| `test_fleet_user_experience.py` | **机队级多智能体协同用户体验测试**：涉及跨平台、多型水下机器人的指派与状态反馈。 |
| `test_user_experience.py` | **单机用户体验与对话平滑度测试**。 |
| `verify_fixes.py` | **专项缺陷修复在线回归测试**。 |
| `verify_rectifications.py` | **系统整改项与状态机闭环验证脚本**。 |
| `_tmp_full_gpu_e2e.py` | **全 GPU 模式端到端连通性演练脚本**。 |

---

## 运行示例

```bash
# 激活环境
conda activate seagent

# 运行特定端到端场景
python scripts/e2e_suites/test_llm_full_product.py
python scripts/e2e_suites/hardcore_industrial_suite.py
```
