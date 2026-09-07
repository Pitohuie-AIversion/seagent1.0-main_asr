# Debug Session: design-qa-write-failure

**Status**: [OPEN]
**Created**: 2026-08-11
**Session ID**: design-qa-write-failure

## 问题描述
- 用户无法完成设计问答（design QA）流程
- "总是无法写入" - 写操作意图被错误拦截
- 问答不按照项目意图执行 - 意图路由/槽位提取出现偏差

## 可证伪假设
1. **H1 (safety_gate 过度拦截)**: `safety_gate.py` L700 附近已知逻辑错误导致合法 WRITE 操作被误拦截，写入始终失败
2. **H2 (intent_router Fail-Smart 失效)**: `intent_router.py` 中 Fail-Smart 二次矫正机制未正确识别裸参数赋值场景，将本应为 WRITE/READ 的意图误判为 CLARIFY
3. **H3 (has_write_evidence 过于严格)**: `interaction_plan.py` 中 `has_write_evidence()` 安全底线过于保守，合法的设计问答写入被误判为假设性问句
4. **H4 (extractor SSOT 兜底缺失)**: `extractor.py` 中槽位提取规则层兜底逻辑未触发，LLM 空返回时未能正确补齐单位/数值，导致后续写操作参数不完整
5. **H5 (协议校验 Fail-Closed 误杀)**: `intent_router.py` 协议入口校验过于严格，合法的 confidence 值或字段被误判为非法拦截

## 证据收集计划
1. 静态审查核心文件：safety_gate.py (L700附近)、intent_router.py、interaction_plan.py、extractor.py
2. 运行现有测试套件观察失败案例
3. 添加插桩日志追踪路由决策链路
4. 针对性构造设计问答场景复现问题

## 运行时证据
_(待填充：日志、栈追踪、变量快照)_

## 分析结论
_(待填充：各假设确认/否决状态)_

## 修复方案
_(待填充：最小修复 Patch)_

## 前后对比证据
_(待填充：pre-fix vs post-fix 日志对比)_
