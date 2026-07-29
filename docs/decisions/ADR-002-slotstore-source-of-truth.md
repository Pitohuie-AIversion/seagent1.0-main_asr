# ADR-002：SlotStore 作为系统统一状态中心 (Single Source of Truth)

## 状态
Accepted

## 背景
在早期实现中，系统任务状态同时散落在 `DialogueManager` 的临时字典 `task_state`、`last_built_json`、对话历史以及各个逻辑模块的局部变量中。这种多源状态设计极易导致“槽位状态不一致”问题：例如，提取器更新了临时字典，但构建器导出的 JSON 缺失字段，或者验证器校验的状态与下游生成的 TaskIntent 内容不一致。

## 决策
采用 [src/slot_store.py](file:///root/mzy/seagent1.0-main_asr/src/slot_store.py) 中的 `SlotStore` 作为全系统唯一的任务状态真理源 (Single Source of Truth)：
1. 统一管理所有基本槽位、任务 Schema 槽位与内部状态槽位。
2. 规定仅状态为 `valid` 且值非 `None` 的 Slot 才能作为当前确认生效的任务状态（通过 `SlotStore.get_task_state()` 导出）。
3. 引入全局 `version` 版本号自增与快照导出/恢复 (`export_snapshot` / `import_snapshot`) 机制，支持严格的状态回滚与不变性校验。
4. 下游 JSON 导出与物理校验必须统一从 `SlotStore` 派生，禁止使用绕过 `SlotStore` 的临时变量。

## 修改位置
- [src/slot_store.py](file:///root/mzy/seagent1.0-main_asr/src/slot_store.py) (`SlotStore`, `Slot`, `SlotVersionConflict`, `SnapshotValidationError`)
- [src/dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py) (`DialogueManager.slot_store`)
- [src/output_builder.py](file:///root/mzy/seagent1.0-main_asr/src/output_builder.py) (`OutputBuilder.build_flat_json`)

## 核心逻辑
```python
# 状态派生与锁定流程
class SlotStore:
    def get_task_state(self) -> Dict[str, Any]:
        """仅导出 status == 'valid' 且 value is not None 的槽位事实"""
        with self._lock:
            return {
                key: copy.deepcopy(slot.value)
                for key, slot in self.slots.items()
                if slot.status == "valid" and slot.value is not None
            }
```

## 正面影响
1. **彻底消除多源状态分歧**：状态更新、物理校验与 JSON 导出全部基于同一份 `SlotStore` 数据。
2. **状态透明可追溯**：每个槽位记录类型、状态（missing, valid, candidate, conflict 等）、来源、置信度与更新版本。
3. **安全事务支持**：借助快照与版本号，可轻松支持复杂的多轮对话回滚与只读状态断言。

## 代价与限制
1. 所有状态变更必须经过 `SlotStore` 封装方法，增加了对象封装开销。
2. 在 Schema 切换（如 Standard 切换至 Emergency 紧急模式）时，需正确执行 `init_task_slots` 同步槽位定义。

## 验证
- 单元测试：[tests/test_slot_consistency.py](file:///root/mzy/seagent1.0-main_asr/tests/test_slot_consistency.py), [tests/test_p0_final_consistency.py](file:///root/mzy/seagent1.0-main_asr/tests/test_p0_final_consistency.py)
- CI 门控：通过 `python -m unittest discover tests` 运行全量测试防范状态回归。
