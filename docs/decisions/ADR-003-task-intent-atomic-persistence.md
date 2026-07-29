# ADR-003：TaskIntent 文件安全排他锁与原子落盘机制

## 状态
Accepted

## 背景
在任务规划确认阶段，需要将构建好的 `TaskIntent` JSON 文件持久化到磁盘目录。在多进程并发或重试场景下，如果直接以写模式打开目标文件写入，极易发生“半写入（Half-write/Partial file）”、“并发覆写（Race Condition）”或“符号链接替换攻击（Symlink Attack）”。此外，已生成的正式 `intent_id` 文件若被非法覆盖，会导致历史任务轨迹破坏与审计失效。

## 决策
在 [src/task_intent_builder.py](file:///root/mzy/seagent1.0-main_asr/src/task_intent_builder.py) 中设计三阶段原子落盘机制：
1. **纯内存构建**：`TaskIntentBuilder.prepare()` 仅在内存中生成 JSON 对象，不产生磁盘副作用。
2. **Staging 暂存区创建**：`create_staging()` 在任务目录下生成具有独占 PID、线程 ID 和随机 UUID 尾缀的临时文件（如 `task_intent_TI2026071801.staging_1234_5678_abcd1234`），使用 `O_CREAT | O_EXCL | O_NOFOLLOW` 模式写入并强制 `fsync`。
3. **安全原子发布与无覆盖锁定**：`publish_staging()` 获取跨进程排他锁 `TaskPublishLock`。使用 `_atomic_commit_noreplace()`（基于 `os.link` 硬链接原子提交）将 staging 文件转存为正式 `task_intent_TIxxxx.json`。若目标文件已存在，无条件拒绝发布并抛出 `IntentIdConflict` 异常，禁用强制覆盖与删除。

> [!IMPORTANT]
> **概念澄清：**
> 本 ADR 所定义的“原子持久化（Atomic Persistence）”，特指 **TaskIntent JSON 文件在操作系统文件系统上的原子落盘与并发无覆盖安全保障**（即正式文件在磁盘上要么完整存在、要么完全不存在，杜绝半写入与覆写风险）。
> **它绝不表示** Task Graph 任务分解理论中的“不可分割原子任务 (Atomic Sub-Task)”。

## 修改位置
- [src/task_intent_builder.py](file:///root/mzy/seagent1.0-main_asr/src/task_intent_builder.py) (`TaskPublishLock`, `TaskIntentBuilder.prepare`, `create_staging`, `publish_staging`, `_atomic_commit_noreplace`)
- [src/exceptions.py](file:///root/mzy/seagent1.0-main_asr/src/exceptions.py) (`IntentIdConflict`, `TaskPersistenceError`)

## 核心逻辑
```python
# 原子提交与无覆盖拒绝伪代码
def _atomic_commit_noreplace(temp_file: Path, final_file: Path) -> None:
    if final_file.exists():
        raise FileExistsError(f"Final file already exists: {final_file}")

    try:
        # 使用 link 保证跨进程文件系统级的原子提交
        os.link(temp_file, final_file)
        temp_file.unlink() # 移除 staging
    except FileExistsError:
        raise
```

## 正面影响
1. **彻底消除半写入风险**：读取端永远不可能读取到只写了一半的损坏 JSON 文件。
2. **防止并发覆盖与冲突**：已发布的正式 `intent_id` 文件获得强物理保护，重复发布同一 ID 直接抛出 `IntentIdConflict` 阻断。
3. **防 Path Traversal 与 Symlink 攻击**：对暂存文件和目标文件的父路径、符号链接状态及 PID 拥有权进行严格的沙箱校验。

## 代价与限制
1. 依赖底层文件系统的硬链接 (`os.link`) 特性，要求 staging 文件与目标文件必须在同一文件系统中。
2. 增加了暂存文件创建与锁管理的物理 I/O 开销。

## 验证
- 单元测试：[tests/test_phase1_atomic_publish_final_closeout.py](file:///root/mzy/seagent1.0-main_asr/tests/test_phase1_atomic_publish_final_closeout.py), [tests/test_p0_publish_race_and_router_closeout.py](file:///root/mzy/seagent1.0-main_asr/tests/test_p0_publish_race_and_router_closeout.py), [tests/test_p0_security_final_closeout.py](file:///root/mzy/seagent1.0-main_asr/tests/test_p0_security_final_closeout.py)
- CI 门控： mandatory test suite 自动覆盖并发写锁与路径安全测试。
