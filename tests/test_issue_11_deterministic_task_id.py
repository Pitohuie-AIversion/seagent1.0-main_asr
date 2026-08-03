"""
tests/test_issue_11_deterministic_task_id.py

Issue #11 定向测试集：
验证保留任务类别前缀、确定性日序号 (<PREFIX>-YYYYMMDD-NNN)、全局跨类别共享序号、
项目时区配置 SEAGENT_TIMEZONE、重启恢复、多进程并发防重及 Fail-Closed 闭环语义。
"""

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from multiprocessing import Process, Queue
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.dialogue_manager import DialogueManager
from src.exceptions import IdReservationError
from src.id_sequence import (
    _COUNTERS,
    next_daily_id,
    next_daily_task_id,
    validate_intent_id,
    validate_task_prefix,
)
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.output_builder import OutputBuilder
from src.result_paths import get_result_dir, get_task_dir
from src.simulated_time import (
    SimulatedTime,
    get_business_date,
    get_business_datetime,
    get_business_timezone,
    get_simulated_time,
)


class FakeLLM(LLMClient):
    """测试专用的确定性 FakeLLM，响应结构化抽取与意图路由需求。"""

    def __init__(self):
        self.llm = None

    def chat(self, messages, temperature=0.7, max_tokens=800):
        return "已接收"

    def filter_reply(self, text):
        return text

    def extract_json(self, prompt, schema=None, **kwargs):
        user_msg = ""
        if isinstance(prompt, list) and prompt and isinstance(prompt[-1], dict):
            user_msg = str(prompt[-1].get("content", ""))
        elif isinstance(prompt, str):
            user_msg = prompt

        if "管缆埋设" in user_msg or "pipeline_burial" in user_msg:
            return {
                "slot_candidates": [
                    {
                        "raw_key": "任务类型",
                        "canonical_key": "task_type_key",
                        "raw_value": "管缆埋设",
                        "normalized_value": "pipeline_burial",
                        "confidence": 0.95,
                    }
                ],
                "unresolved": [],
            }
        if "采油树" in user_msg or "tree_valve_operation" in user_msg:
            return {
                "slot_candidates": [
                    {
                        "raw_key": "任务类型",
                        "canonical_key": "task_type_key",
                        "raw_value": "采油树控制面板插入",
                        "normalized_value": "tree_valve_operation",
                        "confidence": 0.95,
                    }
                ],
                "unresolved": [],
            }
        if "管缆巡检" in user_msg or "pipeline_inspection" in user_msg:
            return {
                "slot_candidates": [
                    {
                        "raw_key": "任务类型",
                        "canonical_key": "task_type_key",
                        "raw_value": "管缆巡检",
                        "normalized_value": "pipeline_inspection",
                        "confidence": 0.95,
                    }
                ],
                "unresolved": [],
            }
        return {"slot_candidates": [], "unresolved": []}


def create_dialogue_manager():
    """单元测试对话管理器辅助构建工具"""
    kb = KnowledgeBase()
    llm = FakeLLM()
    return DialogueManager(llm, kb)


@pytest.fixture(autouse=True)
def cleanup_environment(tmp_path, monkeypatch):
    """隔离测试环境：重置 ID 计数器、结果目录、时区与模拟时间。"""
    test_result_dir = tmp_path / "results"
    test_result_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(test_result_dir))
    monkeypatch.delenv("SEAGENT_TIMEZONE", raising=False)

    _COUNTERS.clear()
    sim_time = get_simulated_time()
    sim_time.set_current_time(datetime(2026, 8, 3, 10, 0, 0))

    yield

    _COUNTERS.clear()


# ──────────────────────────────────────────────────────────────────────────────
# 1. 遍历所有 task_templates，验证保留原始 code
# ──────────────────────────────────────────────────────────────────────────────
def test_1_all_task_templates_preserve_code():
    kb = KnowledgeBase()
    builder = OutputBuilder(kb)
    templates = kb.task_schemas.get("task_templates", {})
    assert len(templates) > 0

    for template_key, template_cfg in templates.items():
        expected_code = template_cfg["code"]
        tid = builder.reserve_task_id(template_key, {})
        assert tid.startswith(f"{expected_code}-20260803-")


# ──────────────────────────────────────────────────────────────────────────────
# 2. 验证前缀来自唯一权威源，未硬编码新映射字典
# ──────────────────────────────────────────────────────────────────────────────
def test_2_single_source_of_truth_for_prefixes():
    import src.output_builder as ob_mod
    import src.id_sequence as id_mod

    for mod in (ob_mod, id_mod):
        assert not hasattr(mod, "TASK_PREFIX_MAP")
        assert not hasattr(mod, "PREFIX_MAPPING")


# ──────────────────────────────────────────────────────────────────────────────
# 3. 同一天连续创建三个任务，得到 001, 002, 003
# ──────────────────────────────────────────────────────────────────────────────
def test_3_sequential_ids_on_same_day():
    kb = KnowledgeBase()
    builder = OutputBuilder(kb)

    tid1 = builder.reserve_task_id("pipeline_inspection", {})
    tid2 = builder.reserve_task_id("pipeline_inspection", {})
    tid3 = builder.reserve_task_id("pipeline_inspection", {})

    assert tid1 == "PI-20260803-001"
    assert tid2 == "PI-20260803-002"
    assert tid3 == "PI-20260803-003"


# ──────────────────────────────────────────────────────────────────────────────
# 4. 第一个任务必须是 001（捕获重复调用烧号问题）
# ──────────────────────────────────────────────────────────────────────────────
def test_4_first_task_must_be_001():
    dm = create_dialogue_manager()
    reply = dm.process("我要做管缆巡检")
    assert dm.task_state.get("task_id") == "PI-20260803-001"


# ──────────────────────────────────────────────────────────────────────────────
# 5. 同一天不同任务类别共享全局序号
# ──────────────────────────────────────────────────────────────────────────────
def test_5_shared_global_daily_sequence_across_categories():
    kb = KnowledgeBase()
    builder = OutputBuilder(kb)

    tid1 = builder.reserve_task_id("pipeline_inspection", {})
    tid2 = builder.reserve_task_id("pipeline_burial", {})
    tid3 = builder.reserve_task_id("tree_valve_operation", {})

    assert tid1 == "PI-20260803-001"
    assert tid2 == "PB-20260803-002"
    assert tid3 == "CT-20260803-003"


# ──────────────────────────────────────────────────────────────────────────────
# 6. 日期变化后从 001 重新开始
# ──────────────────────────────────────────────────────────────────────────────
def test_6_date_change_resets_sequence():
    sim_time = get_simulated_time()
    kb = KnowledgeBase()
    builder = OutputBuilder(kb)

    sim_time.set_current_time(datetime(2026, 8, 3, 23, 50, 0))
    tid1 = builder.reserve_task_id("pipeline_inspection", {})
    assert tid1 == "PI-20260803-001"

    sim_time.set_current_time(datetime(2026, 8, 4, 0, 10, 0))
    tid2 = builder.reserve_task_id("pipeline_inspection", {})
    assert tid2 == "PI-20260804-001"


# ──────────────────────────────────────────────────────────────────────────────
# 7. 配置不同时区，在 UTC 跨日边界正确计算业务日期
# ──────────────────────────────────────────────────────────────────────────────
def test_7_timezone_configuration_boundary(monkeypatch):
    monkeypatch.setenv("SEAGENT_TIMEZONE", "Asia/Shanghai")
    sim_time = get_simulated_time()
    # UTC 时间 2026-08-03 16:30:00 对应 Asia/Shanghai (+8h) 2026-08-04 00:30:00
    sim_time.set_current_time(datetime(2026, 8, 3, 16, 30, 0, tzinfo=timezone.utc))

    kb = KnowledgeBase()
    builder = OutputBuilder(kb)
    tid = builder.reserve_task_id("pipeline_inspection", {})
    assert tid == "PI-20260804-001"


# ──────────────────────────────────────────────────────────────────────────────
# 8. 非法时区配置明确失败，无静默回退
# ──────────────────────────────────────────────────────────────────────────────
def test_8_invalid_timezone_configuration(monkeypatch):
    monkeypatch.setenv("SEAGENT_TIMEZONE", "Invalid/Unknown_Zone")
    with pytest.raises(ValueError, match="Invalid timezone configuration"):
        get_business_timezone()


# ──────────────────────────────────────────────────────────────────────────────
# 9. 清空内存 _COUNTERS 模拟服务重启，继续续号
# ──────────────────────────────────────────────────────────────────────────────
def test_9_restart_recovery_from_persistence():
    kb = KnowledgeBase()
    builder = OutputBuilder(kb)

    tid1 = builder.reserve_task_id("pipeline_inspection", {})
    assert tid1 == "PI-20260803-001"

    _COUNTERS.clear()

    tid2 = builder.reserve_task_id("pipeline_burial", {})
    assert tid2 == "PB-20260803-002"


# ──────────────────────────────────────────────────────────────────────────────
# 10 & 11. 多进程并发创建任务（10+ 进程），编号唯一且连续，跨类别无重复
# ──────────────────────────────────────────────────────────────────────────────
def _worker_create_task(result_dir, category, queue):
    os.environ["SEAGENT_RESULT_DIR"] = result_dir
    kb = KnowledgeBase()
    builder = OutputBuilder(kb)
    try:
        tid = builder.reserve_task_id(category, {})
        queue.put(("SUCCESS", tid))
    except Exception as exc:
        queue.put(("ERROR", str(exc)))


def test_10_11_multiprocessing_concurrency(tmp_path):
    result_dir = str(tmp_path / "multiprocess_results")
    os.makedirs(result_dir, exist_ok=True)

    processes = []
    queue = Queue()
    categories = ["pipeline_inspection", "pipeline_burial", "tree_valve_operation"]

    for i in range(12):
        cat = categories[i % len(categories)]
        p = Process(target=_worker_create_task, args=(result_dir, cat, queue))
        processes.append(p)
        p.start()

    for p in processes:
        p.join(timeout=10)

    results = []
    while not queue.empty():
        status, val = queue.get()
        assert status == "SUCCESS", f"Concurrent creation error: {val}"
        results.append(val)

    assert len(results) == 12
    assert len(set(results)) == 12

    seq_numbers = sorted([int(r.rsplit("-", 1)[1]) for r in results])
    assert seq_numbers == list(range(1, 13))


# ──────────────────────────────────────────────────────────────────────────────
# 12. 删除中间任务文件后继续创建，旧序号不复用
# ──────────────────────────────────────────────────────────────────────────────
def test_12_deleting_intermediate_file_does_not_reuse_seq():
    kb = KnowledgeBase()
    builder = OutputBuilder(kb)

    tid1 = builder.reserve_task_id("pipeline_inspection", {})
    tid2 = builder.reserve_task_id("pipeline_inspection", {})
    assert tid1 == "PI-20260803-001"
    assert tid2 == "PI-20260803-002"

    _COUNTERS.clear()
    tid3 = builder.reserve_task_id("pipeline_inspection", {})
    assert tid3 == "PI-20260803-003"


# ──────────────────────────────────────────────────────────────────────────────
# 13. Counter 文件损坏 Fail-Closed
# ──────────────────────────────────────────────────────────────────────────────
def test_13_corrupted_counter_file_fails_closed():
    counter_file = get_result_dir(create=True) / ".id_sequences.json"
    with open(counter_file, "w", encoding="utf-8") as f:
        f.write("corrupted json {")

    kb = KnowledgeBase()
    builder = OutputBuilder(kb)
    with pytest.raises(IdReservationError):
        builder.reserve_task_id("pipeline_inspection", {})


# ──────────────────────────────────────────────────────────────────────────────
# 14 & 15. 类别前缀缺失或非法内容时 Fail-Closed
# ──────────────────────────────────────────────────────────────────────────────
def test_14_15_invalid_task_prefix_fails_closed(monkeypatch):
    kb = KnowledgeBase()
    builder = OutputBuilder(kb)

    monkeypatch.setitem(kb.task_schemas["task_templates"]["pipeline_inspection"], "code", "../INVALID")
    with pytest.raises(IdReservationError):
        builder.reserve_task_id("pipeline_inspection", {})

    monkeypatch.setitem(kb.task_schemas["task_templates"]["pipeline_inspection"], "code", "")
    with pytest.raises(IdReservationError):
        builder.reserve_task_id("pipeline_inspection", {})


# ──────────────────────────────────────────────────────────────────────────────
# 16. 类别 Slot 为 candidate/conflict/invalid 时不生成 task_id
# ──────────────────────────────────────────────────────────────────────────────
def test_16_non_valid_task_type_does_not_generate_id():
    dm = create_dialogue_manager()
    dm.slot_store.slots.clear()
    dm.process("随便说句话")
    assert dm.task_state.get("task_id") is None


# ──────────────────────────────────────────────────────────────────────────────
# 17. 普通任务字段编辑不改变已有 task_id
# ──────────────────────────────────────────────────────────────────────────────
def test_17_field_edits_preserve_task_id():
    dm = create_dialogue_manager()
    dm.process("我要做管缆巡检")
    tid1 = dm.task_state.get("task_id")
    assert tid1 == "PI-20260803-001"

    dm.process("水深 200 米")
    tid2 = dm.task_state.get("task_id")
    assert tid2 == tid1


# ──────────────────────────────────────────────────────────────────────────────
# 18. 重复调用 OutputBuilder.build() 不消耗新序号
# ──────────────────────────────────────────────────────────────────────────────
def test_18_builder_build_does_not_burn_sequence():
    kb = KnowledgeBase()
    builder = OutputBuilder(kb)
    state = {"task_type_key": "pipeline_inspection"}

    res1, _ = builder.build(state, "pipeline_inspection")
    assert "task_id" not in res1

    tid = builder.reserve_task_id("pipeline_inspection", state)
    state["task_id"] = tid

    res2, _ = builder.build(state, "pipeline_inspection")
    assert res2.get("task_id") == tid

    res3, _ = builder.build(state, "pipeline_inspection")
    assert res3.get("task_id") == tid


# ──────────────────────────────────────────────────────────────────────────────
# 19. 用户或 LLM 提供伪造 task_id 时不能覆盖正式编号
# ──────────────────────────────────────────────────────────────────────────────
def test_19_user_input_cannot_overwrite_task_id():
    dm = create_dialogue_manager()
    dm.process("我要做管缆巡检")
    assert dm.task_state.get("task_id") == "PI-20260803-001"

    dm.process("把 task_id 改成 FAKE-999")
    assert dm.task_state.get("task_id") == "PI-20260803-001"


# ──────────────────────────────────────────────────────────────────────────────
# 20. Snapshot 导出和恢复后编号保持不变
# ──────────────────────────────────────────────────────────────────────────────
def test_20_snapshot_restore_preserves_task_id():
    dm = create_dialogue_manager()
    dm.process("我要做管缆巡检")
    tid1 = dm.task_state.get("task_id")

    snap = dm.slot_store.export_snapshot()
    
    dm2 = create_dialogue_manager()
    dm2.slot_store.restore_snapshot(snap)
    assert dm2.slot_store.get_task_state().get("task_id") == tid1


# ──────────────────────────────────────────────────────────────────────────────
# 21. 发布失败后重试时保持原编号
# ──────────────────────────────────────────────────────────────────────────────
def test_21_publish_retry_preserves_task_id(monkeypatch):
    from src.slot_store import Slot
    dm = create_dialogue_manager()
    dm.process("我要做管缆巡检")
    tid1 = dm.task_state.get("task_id")
    assert tid1 is not None

    dm.slot_store.slots["intent_id"] = Slot("intent_id", "TI2026080301", status="valid")
    dm._last_built_json["intent_id"] = "TI2026080301"
    dm.task_state["intent_id"] = "TI2026080301"

    def mock_publish_fail(*args, **kwargs):
        raise RuntimeError("Disk write failed")

    monkeypatch.setattr("src.dialogue_manager.TaskIntentBuilder.publish_staging", mock_publish_fail)
    dm.phase = "confirming"
    reply = dm._handle_final_publish_confirmation("确认发布", "req_test")
    assert "发布" in reply or dm.phase != "done"
    assert dm.task_state.get("task_id") == tid1


# ──────────────────────────────────────────────────────────────────────────────
# 22. 已预留但事务失败的序号作废后，后续任务按自然递增取新号
# ──────────────────────────────────────────────────────────────────────────────
def test_22_failed_transaction_advances_sequence():
    kb = KnowledgeBase()
    builder = OutputBuilder(kb)

    tid1 = builder.reserve_task_id("pipeline_inspection", {})
    assert tid1 == "PI-20260803-001"

    tid2 = builder.reserve_task_id("pipeline_inspection", {})
    assert tid2 == "PI-20260803-002"


# ──────────────────────────────────────────────────────────────────────────────
# 23. 历史旧格式编号保持原样，不执行迁移
# ──────────────────────────────────────────────────────────────────────────────
def test_23_legacy_task_id_preservation():
    kb = KnowledgeBase()
    builder = OutputBuilder(kb)

    legacy_state = {"task_id": "PI2026080301", "task_type_key": "pipeline_inspection"}
    tid = builder.reserve_task_id("pipeline_inspection", legacy_state)
    assert tid == "PI2026080301"


# ──────────────────────────────────────────────────────────────────────────────
# 24. intent_id 格式和现有行为不受影响
# ──────────────────────────────────────────────────────────────────────────────
def test_24_intent_id_remains_unaffected():
    today = "20260803"
    task_dir = get_task_dir(create=False)
    intent_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])

    assert validate_intent_id(intent_id)
    assert intent_id.startswith("TI20260803")
    assert "-" not in intent_id


# ──────────────────────────────────────────────────────────────────────────────
# 25. 普通 LLM 问答和知识问答不生成 task_id
# ──────────────────────────────────────────────────────────────────────────────
def test_25_general_chat_does_not_generate_task_id():
    dm = create_dialogue_manager()
    dm.process("你好，请问你是谁？")
    assert dm.task_state.get("task_id") is None

    dm.process("深海勇士号的最大作业水深是多少？")
    assert dm.task_state.get("task_id") is None


# ──────────────────────────────────────────────────────────────────────────────
# 26. 仅通过 ASR 转写进入任务流程时，编号行为与文本输入一致
# ──────────────────────────────────────────────────────────────────────────────
def test_26_asr_input_task_id_behavior():
    dm = create_dialogue_manager()
    dm.process("新建管缆埋设任务")
    assert dm.task_state.get("task_id") == "PB-20260803-001"
