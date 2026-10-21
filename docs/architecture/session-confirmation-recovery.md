# 会话确认与恢复契约

HTTP 客户端确认的对象必须是用户刚核对的任务摘要。会话锁保证同一任务串行处理；它不能证明另一个标签页看到的是最新参数。

## 摘要确认

`/api/chat`、`/api/chat/stream`、`/api/session/state` 和历史恢复返回的 `ui_state.confirmation_token` 是不透明字符串。它绑定当前会话实例、任务槽位、约束证据、风险确认和执行控制状态。重复读取或重绘相同状态不改变标识。客户端不要自行计算或解析标识。

聊天请求携带用户当前所见状态的 `expected_confirmation_token`。明确发布命令、已归档任务的执行风险确认必须通过该校验，输入框文字与按钮使用同一规则。执行派发接口也使用该标识。缺失或过期时：

- JSON 接口返回 HTTP 409、`error: "ConfirmationStale"`。
- 已建立的 SSE 返回 `error` 事件，其 JSON 包含 `code: 409` 和同一错误类型。
- 响应包含最新 `ui_state`、`session_id` 和提示。前端先展示新摘要，由用户重新核对后发起新的确认，不自动重发。

`request_id` 的成功或不确定结果重放优先于摘要校验。原确认响应丢失后，携带同一请求标识的重试返回原结果，不再次发布。重置、历史恢复和配置迁移后的新会话实例不能复用旧确认标识，即使槽位版本号相同。

命令行及内部 `DialogueManager.process()` 调用没有浏览器摘要上下文，仍使用领域确认流程；HTTP 服务负责执行此客户端前置条件。

## 历史记录

`POST /api/history/load` 接收 `history_id`、当前 `session_id` 和 `restore_mode: "fork"`；省略模式时也按 `fork` 处理。

恢复成功后：

- `session_id` 是新会话，原会话对象继续保留。
- `previous_session_id` 指向原会话。
- 原会话有未完成草稿时，先保存普通历史快照；`preserved_draft_history_id` 返回可再次打开的记录。备份不会生成 TaskIntent，也不会覆盖已归档任务的固定历史文件。
- 快照非法或草稿备份失败时，不切换会话、不覆盖原草稿。

这不是每轮自动保存草稿的保证。浏览器刷新仍通过当前 `session_id` 恢复活跃进程中的会话。

## 配置刷新与控制结果

配置刷新须等待已接受的会话事务完成后再复制快照。锁顺序与聊天、重置保持一致：先获取会话锁，再短暂获取会话注册表锁；不能持有注册表锁等待会话事务。

迁移保留已处理 HTTP 请求的重放缓存，更新确认标识。单个会话迁移失败时保留原实例并报告失败，不以空会话替换。

任务归档后的控制及执行风险确认同样保存最新历史。若控制已处理而历史保存失败，响应通过 `persistence_warning` 和答复文字提示当前恢复点未保存；保留实际执行结果和请求重放记录，不能因此重新发送控制指令。

回归入口：`tests/test_session_confirmation_reliability.py`、`tests/test_web_context_lifecycle.py`、`tests/test_chat_request_idempotency.py`。
