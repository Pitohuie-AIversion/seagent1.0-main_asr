# Debug Session: design-qa-write-failure

**Status**: [ARCHIVED — historical investigation; not the current defect tracker]
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
原始会话未保存可复核的日志、栈追踪或变量快照。本记录不再作为当前缺陷已复现或已修复的证据。

## 分析结论
本记录中的 H1-H5 是 2026-08-11 的待验证假设，不能直接映射到当前代码状态。当前路由与写入行为应以 [ADR-001](decisions/ADR-001-write-query-routing.md)、[ADR-005](decisions/ADR-005-llm-semantic-authority.md) 以及现行回归测试为准。

## 修复方案
该历史会话没有关联可核验的最小修复补丁。本记录仅保留问题背景和排查边界；新的复现应创建带测试、日志和提交引用的独立记录。

## 前后对比证据
未保存前后对比日志，不能据此宣称设计问答问题已闭环。
