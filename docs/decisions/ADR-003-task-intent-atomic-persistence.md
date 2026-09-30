# ADR-003：TaskIntent 文件安全排他锁与原子落盘机制

## 状态
Accepted；2026-09-30 对照实现补充提交后持久化失败与恢复语义。

## 背景
在任务规划确认阶段，需要将构建好的 `TaskIntent` JSON 文件持久化到磁盘目录。在多进程并发或重试场景下，如果直接以写模式打开目标文件写入，极易发生“半写入（Half-write/Partial file）”、“并发覆写（Race Condition）”或“符号链接替换攻击（Symlink Attack）”。此外，已生成的正式 `intent_id` 文件若被非法覆盖，会导致历史任务轨迹破坏与审计失效。

## 决策
在 [src/dispatch/task_intent_builder.py](../../src/dispatch/task_intent_builder.py) 中采用以下发布与恢复流程：
1. **纯内存构建**：`TaskIntentBuilder.prepare()` 仅在内存中生成 JSON 对象，不产生磁盘副作用。
2. **Staging 暂存区创建**：`create_staging()` 在任务目录下生成具有独占 PID、线程 ID 和随机 UUID 尾缀的临时文件（如 `task_intent_TI2026071801.staging_1234_5678_abcd1234`），使用 `O_CREAT | O_EXCL | O_NOFOLLOW` 模式写入并强制 `fsync`。
3. **安全原子发布与无覆盖锁定**：`publish_staging()` 获取跨进程排他锁 `TaskPublishLock`，校验并认领 staging 文件，再从受信任的内存 intent 写入私有临时文件，回读核对内容。使用 `_atomic_commit_noreplace()`（基于 `os.link`）将该私有文件提交为正式 `task_intent_TIxxxx.json`。若目标文件已存在，拒绝普通发布并抛出 `IntentIdConflict`，不覆盖或删除原文件。
4. **确认持久化或进入恢复**：正式文件可见后继续对文件及父目录执行 `fsync`。失败时抛出 `TaskCommitUncertainError`，保留文件与原 intent；`recover_committed()` 核对相同内容、普通文件与 inode 后重新确认持久化，不再分配编号或覆盖文件。

> [!IMPORTANT]
> **概念澄清：**
> 本 ADR 所定义的“原子提交”，指正式路径以完整 JSON 一次可见，并且不覆盖已存在文件。路径可见与掉电后持久化是不同保证：`os.link` 成功但后续 `fsync` 失败时，正式文件可能已经存在，此时必须报告待核对，不能宣称文件不存在或发布成功。
> **它绝不表示** Task Graph 任务分解理论中的“不可分割原子任务 (Atomic Sub-Task)”。

## 修改位置
- [src/dispatch/task_intent_builder.py](../../src/dispatch/task_intent_builder.py) (`TaskPublishLock`, `TaskIntentBuilder.prepare`, `create_staging`, `publish_staging`, `_atomic_commit_noreplace`, `TaskCommitUncertainError`, `recover_committed`)
- [src/handlers/task_commit.py](../../src/handlers/task_commit.py)（区分提交前回滚与提交后恢复）
- [src/exceptions.py](../../src/exceptions.py) (`IntentIdConflict`, `TaskPersistenceError`)

## 核心逻辑
```python
# 仅示意文件可见性提交；fsync 与恢复由 publish_staging/recover_committed 负责。
def _atomic_commit_noreplace(temp_file: Path, final_file: Path) -> None:
    if final_file.exists():
        raise FileExistsError(f"Final file already exists: {final_file}")

    try:
        # 使用 link 保证跨进程文件系统级的原子提交
        os.link(temp_file, final_file)
        try:
            temp_file.unlink()  # 清理私有临时链接；失败不撤销正式文件
        except OSError:
            pass  # 实际实现记录清理异常
    except FileExistsError:
        raise
```

## 正面影响
1. **避免发布过程暴露半写入文件**：先完整写入、同步并回读，再创建正式路径；该保证限于本发布流程，不代表能防止外部文件损坏。
2. **防止并发覆盖与冲突**：已发布的正式 `intent_id` 文件获得强物理保护，重复发布同一 ID 直接抛出 `IntentIdConflict` 阻断。
3. **防 Path Traversal 与 Symlink 攻击**：对暂存文件和目标文件的父路径、符号链接状态及 PID 拥有权进行严格的沙箱校验。

## 代价与限制
1. 依赖底层文件系统的硬链接 (`os.link`) 特性，要求 staging 文件与目标文件必须在同一文件系统中。
2. 增加了暂存文件创建与锁管理的物理 I/O 开销。
3. 不确定提交需要保留恢复状态；再次确认只核对原文件，修改草稿必须等待核对完成。恢复失败不能直接删除正式文件。

## 验证
- 单元测试：[tests/test_phase1_atomic_publish_final_closeout.py](../../tests/test_phase1_atomic_publish_final_closeout.py), [tests/test_p0_publish_race_and_router_closeout.py](../../tests/test_p0_publish_race_and_router_closeout.py), [tests/test_p0_security_final_closeout.py](../../tests/test_p0_security_final_closeout.py)
- 恢复边界：[tests/test_uncertain_task_commit_recovery.py](../../tests/test_uncertain_task_commit_recovery.py) 覆盖相同编号恢复、历史恢复、内容不匹配拒绝和普通重复发布拒绝。
- 上述测试为现有验证入口；本次文档核对没有重新执行全量测试。
