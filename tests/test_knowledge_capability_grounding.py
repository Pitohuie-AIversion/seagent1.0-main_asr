"""LIVE-03: useful knowledge answers must not invent product capabilities."""

import copy
import re

import pytest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.handlers.task_structure_grounding import (
    answer_task_structure_query,
    build_task_structure_evidence,
    ground_task_structure_reply,
)
from src.slots.slot_store import BASE_SLOT_TYPES, Slot
from tests.interaction_plan_support import ScriptedLLM, make_plan


# Actual responder output from the controlled browser verification, turn 9.
LIVE_REPLY = """电力电缆主要用于传输电能，其核心要求是**高导电性**（通常采用铜或铝导体）和**优异的绝缘性能**（绝缘层需耐受高电压），以防止漏电和短路。在深海高压环境下，电力电缆还需具备极强的耐压层和抗海水腐蚀的外护层。

光纤通信缆则主要用于传输数据和信号，其核心介质是**玻璃或塑料纤维**，利用光的全反射原理传输信息。它的优势在于**带宽极大、传输距离远、抗电磁干扰能力强**，但本身不具备导电功能。

**主要区别总结：**
1.  **功能不同**：电力电缆传“电”，光纤缆传“光/数据”。
2.  **物理结构不同**：电力电缆依赖金属导体和绝缘层；光纤缆依赖纤芯、包层及保护层，无金属导电芯（外层可能有加强件）。
3.  **应用场景不同**：电力电缆用于供电（如平台、AUV电池组供电）；光纤缆用于高速数据回传（如实时视频、传感器数据）。
4.  **施工与维护**：两者在施工时对环境（如弯曲半径、拉伸力）的要求均不同，且故障排查逻辑也不同。

当前您设定的任务为**电力电缆巡检**，若后续需要同时巡检光纤通信缆，系统支持在任务类型中扩展或将其作为子任务添加，以便匹配不同的载荷配置（如需要同时搭载电力检测设备和光信号检测仪）。
"""
LIVE_CLAIM = "系统支持在任务类型中扩展或将其作为子任务添加"
LIVE_QUESTION = "先不改，请问电力电缆和光纤通信缆有什么区别？"

# Actual turn 7 reply from the follow-up controlled browser verification.
LIVE_ENGINEERING_REPLY = """在通用的水下机器人工程规划中，将“管缆巡检”这一宏观作业分解为可执行的子任务，通常基于**作业流程的时间轴**、**空间覆盖逻辑**以及**载荷工作模式**来进行拆解。虽然当前本系统的任务配置接口暂不支持通过自然对话直接添加子任务字段，但在工程方法论层面，分解逻辑如下：

### 1. 基于时间轴的流程分解（Task Phasing）
这是最基础的分解方式，将整个任务周期划分为不同阶段，每个阶段对应一个具体的执行单元：
*   **T-1 准备阶段**：包括机器人检查（AUV/ROV）、载荷校准（侧扫声呐参数设定）、路径预规划、支持船协同确认。
*   **T0 部署与释放**：机器人从支持船释放，下潜至目标深度（如 300 米），建立初始通信链路。
*   **T0+ 执行阶段（核心子任务链）**：
    *   *航段 A*：从起点（113.6°E, 19.9°N）沿预定路径航行。
    *   *航段 B*：到达路径中点或关键特征点，进行定点悬停检查（如有必要）。
    *   *航段 C*：到达终点，完成数据回传。
    *   *注*：在连续航行中，这些航段构成一个连续的“路径跟随”子任务，但在高级规划中可视为独立的里程石（Waypoint）任务。
*   **T_end 回收阶段**：上浮或水平移动至回收点，对接支持船，数据归档。

### 2. 基于空间与载荷功能的分解（Functional Subtasks）
针对巡检的核心目的（发现故障、测量参数），可将其分解为针对不同物理现象的检测子任务：
*   **悬空检测子任务**：利用侧扫声呐扫描管缆上方区域，识别管缆是否脱离管沟或海底。
*   **破损/腐蚀检测子任务**：分析声呐回波强度变化及图像纹理，识别管缆表面剥落、机械损伤或生物附着。
*   **埋设深度检测子任务**：通过侧扫声呐剖面图，估算管缆的埋深，判断是否满足设计规范（如电力电缆通常要求一定的埋深以防外力破坏）。
*   **环境参数采集子任务**：在巡检过程中同步记录当前水深、海流速度、底质类型等环境数据，为后续分析提供背景信息。

### 3. 基于数据处理的分解（Data Processing Tasks）
在工程闭环中，数据采集后的处理往往也被视为任务的一部分：
*   **实时预处理子任务**：在机载端进行声呐图像的降噪和增强，剔除海杂波干扰。
*   **异常标记子任务**：系统自动识别疑似缺陷区域，标记坐标并生成初步报告。
*   **数据融合子任务**：将巡检数据与历史数据对比，生成管缆健康趋势报告。

### 工程实施建议
在实际作业中，这些“子任务”通常由**一条连续的轨迹规划**来实现。机器人按照预设的航点（Waypoints）自动执行上述功能序列。对于当前系统而言，这些逻辑被封装在“管缆巡检”这一单一任务类型中，由后端调度器根据选定的载荷（如侧扫声呐）和管缆类型（如电力电缆）自动匹配相应的巡检策略，无需用户在创建任务时手动定义每一个子步骤。

如果您需要针对特定管缆类型（如电力电缆）制定更详细的巡检路径策略（例如：每隔多少公里进行一次定点悬停检查），这属于**任务规划参数**的范畴，可以在任务创建时通过更详细的自然语言描述来引导系统生成相应的路径策略，但目前的接口限制下，这些策略细节通常由系统默认算法根据任务类型自动生成。
"""

ROUTES = [
    pytest.param("GENERAL_CHAT", "general_concept", "general_domain", False, id="general-chat"),
    pytest.param("KNOWLEDGE_QA", "general_concept", "general_domain", False, id="general-knowledge"),
    pytest.param("KNOWLEDGE_QA", "system_rule", "project_kb", True, id="project-knowledge"),
]


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase()


@pytest.fixture(params=["collecting", "confirming"])
def dialogue(request, monkeypatch, tmp_path, kb):
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    llm = ScriptedLLM()
    manager = DialogueManager(llm, kb)
    manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_inspection", "normal"))
    slots = manager.slot_store.clone_slots()
    values = {
        "task_type_key": "pipeline_inspection", "task_type": "管缆巡检",
        "start_time": "2099-01-01T09:00:00", "end_time": "2099-01-01T12:00:00",
        "cable_type": "电力电缆", "water_depth": 300,
        "start_point": {"lat": 19.9, "lon": 113.6},
        "end_point": {"lat": 20.0, "lon": 113.7},
        "equipment_class": "auv", "equipment_family": "水下无人自主航行器",
        "equipment_type": "水下无人自主航行器 324CC",
        "equipment_unit_id": "AUV-324cc-001", "payload": ["侧扫声呐"],
        "support_vessel": "海洋石油681",
    }
    for key, value in values.items():
        slots[key] = Slot(
            key, value=value, value_type=BASE_SLOT_TYPES.get(key, "string"),
            status="valid", source="user_input", raw_value="saved task evidence",
        )
    manager.slot_store.commit_transaction(slots, [], request_id="seed-capability-read")
    manager._rebuild_cache()
    manager.phase = request.param
    queries = []
    execute = kb.execute_typed_query

    def tracked_query(*args, **kwargs):
        queries.append((args, kwargs))
        return execute(*args, **kwargs)

    monkeypatch.setattr(kb, "execute_typed_query", tracked_query)
    return manager, llm, queries


def read_without_mutation(dialogue, message, model_reply, query_intent, subject_type, source_policy):
    manager, llm, _ = dialogue
    before = copy.deepcopy((
        manager.slot_store.export_snapshot(), manager.task_state,
        manager._last_built_json, manager._last_missing, manager.phase, manager.mode,
    ))
    llm.queue_plan(make_plan(
        "READ", query_intent=query_intent, subject_type=subject_type,
        source_policy=source_policy, relation="describe",
    ))
    llm.replies.append(model_reply)

    reply = manager.process(message)

    assert (manager.slot_store.export_snapshot(), manager.task_state,
            manager._last_built_json, manager._last_missing, manager.phase, manager.mode) == before
    assert llm.extract_calls == []
    assert manager.conversation_history[-1] == {"role": "assistant", "content": reply}
    return reply


@pytest.mark.parametrize("query_intent,subject_type,source_policy,uses_kb", ROUTES)
def test_live_concept_answer_keeps_explanation_without_inventing_task_features(
    dialogue, query_intent, subject_type, source_policy, uses_kb,
):
    message = "结合本项目配置，解释电力电缆和光纤通信缆有什么区别。" if uses_kb else LIVE_QUESTION

    reply = read_without_mutation(dialogue, message, LIVE_REPLY, query_intent, subject_type, source_policy)

    for useful in ("高导电性", "绝缘性能", "传输数据和信号", "带宽极大", "功能不同", "施工与维护"):
        assert useful in reply
    assert LIVE_CLAIM not in reply
    assert len(dialogue[1].chat_calls) == 1
    assert bool(dialogue[2]) is uses_kb


@pytest.mark.parametrize("query_intent,subject_type,source_policy,uses_kb", ROUTES)
@pytest.mark.parametrize("message,false_reply,topic,limitation", [
    (
        "当前网页能给现有任务添加子任务吗？",
        "本系统可以直接在当前对话里新增子任务并自动执行。",
        "子任务", r"不支持|未支持|尚未|没有|未实现|不具备|不能|不可",
    ),
    (
        "同一个管缆巡检任务能同时选择电力电缆和光纤通信缆吗？",
        "一个任务可以同时选择多种管缆类型，无需分别创建任务。",
        "管缆", r"单一|单个|一种|一个|一项|分别|仅能|只能",
    ),
    (
        "当前系统能自动生成详细巡检路径吗？",
        "当前系统可以根据自然语言自动生成详细巡检路径。",
        "路径", r"不会|不支持|不能",
    ),
])
def test_direct_capability_questions_explain_current_limits(
    dialogue, query_intent, subject_type, source_policy, uses_kb,
    message, false_reply, topic, limitation,
):
    reply = read_without_mutation(dialogue, message, false_reply, query_intent, subject_type, source_policy)

    assert false_reply not in reply
    assert topic in reply
    assert re.search(limitation, reply)


@pytest.mark.parametrize("query_intent,subject_type,source_policy,uses_kb", ROUTES)
@pytest.mark.parametrize("separator", ["。", "，"], ids=["separate-sentences", "same-sentence"])
def test_one_negative_claim_does_not_protect_another_affirmative_claim(
    dialogue, query_intent, subject_type, source_policy, uses_kb, separator,
):
    correct = "当前系统不支持子任务"
    invented = "但您可以直接在当前对话中扩展任务类型。"
    body = "电力电缆用于供电，光纤通信缆用于传输数据。"
    message = "结合本项目配置，解释两类管缆的区别与当前任务边界。" if uses_kb else LIVE_QUESTION

    reply = read_without_mutation(dialogue, message, body + correct + separator + invented,
                                  query_intent, subject_type, source_policy)

    assert body in reply
    assert re.search(r"(?:不支持|没有|尚未|不能).{0,12}子任务", reply)
    assert invented not in reply


@pytest.mark.parametrize("query_intent,subject_type,source_policy,uses_kb", ROUTES)
@pytest.mark.parametrize("answer", [
    "电力电缆用于供电，光纤通信缆用于传输数据。当前系统不支持子任务，也不能在当前对话中新增任务类型。",
    "电力电缆用于供电，光纤通信缆用于传输数据。当前单个任务只能选择一种管缆类型。",
])
def test_accurate_negative_and_single_value_explanations_are_preserved(
    dialogue, query_intent, subject_type, source_policy, uses_kb, answer,
):
    message = "结合本项目配置，解释两类管缆的区别与当前任务边界。" if uses_kb else LIVE_QUESTION

    reply = read_without_mutation(dialogue, message, answer, query_intent, subject_type, source_policy)

    assert answer in reply


@pytest.mark.parametrize("message,answer", [
    (
        "在一般任务规划算法中，如何将水下巡检任务分解为子任务？",
        "一般任务规划算法可以把水下巡检拆成导航、观测和数据处理等子任务；这是算法设计方法。",
    ),
    (
        "如果开发人员修改配置并补充代码，怎样扩展任务模板？",
        "开发人员可以修改任务模板配置并补充字段、校验代码和测试来扩展任务类型；完成开发后需验证再部署。",
    ),
])
def test_algorithm_and_manual_development_discussion_remains_useful(dialogue, message, answer):
    reply = read_without_mutation(dialogue, message, answer,
                                  "GENERAL_CHAT", "general_concept", "general_domain")

    assert answer in reply


@pytest.mark.parametrize("query_intent,subject_type,source_policy,uses_kb", ROUTES)
@pytest.mark.parametrize("claim", [
    "当前任务可以添加光纤巡检子任务。",
    "可在当前任务中新增子任务。",
    "所谓系统支持子任务的说法不准确，但系统可以自定义任务类型。",
    "系统支持在同一任务中选择电力电缆和光纤通信缆。",
])
def test_current_interface_promises_are_rejected_in_task_scoped_clauses(
    dialogue, query_intent, subject_type, source_policy, uses_kb, claim,
):
    body = "电力电缆用于供电，光纤通信缆用于传输数据。"
    message = "结合本项目配置，解释两类管缆的区别与当前任务边界。" if uses_kb else LIVE_QUESTION

    reply = read_without_mutation(dialogue, message, body + claim,
                                  query_intent, subject_type, source_policy)

    assert body in reply
    assert claim not in reply
    assert "系统可以自定义任务类型" not in reply


@pytest.mark.parametrize("answer", [
    "开发者完成配置、代码、校验及协议适配后，系统可以支持子任务。",
    "任务分解算法可以把作业拆成子任务。",
    "当前系统仅保存一个任务。",
])
def test_development_conditions_algorithm_and_current_single_task_are_preserved(dialogue, answer):
    reply = read_without_mutation(dialogue, LIVE_QUESTION, answer,
                                  "GENERAL_CHAT", "general_concept", "general_domain")

    assert answer in reply


def test_cable_cardinality_answer_uses_the_active_mode(kb):
    message = "同一个管缆巡检任务能同时选择电力电缆和光纤通信缆吗？"
    normal = build_task_structure_evidence(kb.task_schemas, "normal")
    emergency = build_task_structure_evidence(kb.task_schemas, "emergency")

    normal_answer = answer_task_structure_query(message, normal)
    emergency_answer = answer_task_structure_query(message, emergency)

    assert normal_answer and "只能选择一种" in normal_answer
    assert emergency_answer and re.search(r"没有|不提供|未配置|未定义|不存在", emergency_answer)
    assert "只能选择一种" not in emergency_answer
    assert "管缆类型为单选" not in emergency_answer


def test_list_valued_cable_schema_does_not_inherit_single_value_limit(kb):
    schemas = copy.deepcopy(kb.task_schemas)
    for template in schemas["task_templates"].values():
        for field in template.get("output_schema", {}).get("normal", []):
            if field.get("key") == "cable_type":
                field["type"] = "list"
    evidence = build_task_structure_evidence(schemas, "normal")
    message = "同一个管缆巡检任务能同时选择电力电缆和光纤通信缆吗？"
    answer = answer_task_structure_query(message, evidence)
    supported_claim = "当前管缆巡检任务支持同时选择电力电缆和光纤通信缆。"

    assert not answer or "只能选择一种" not in answer
    assert ground_task_structure_reply(supported_claim, evidence) == supported_claim


@pytest.mark.parametrize("query_intent,subject_type,source_policy,uses_kb", ROUTES)
def test_general_engineering_task_decomposition_is_not_an_application_promise(
    dialogue, query_intent, subject_type, source_policy, uses_kb,
):
    question = "一般工程规划中如何将巡检工作分解为子任务？"
    if uses_kb:
        question = "结合本项目配置，解释" + question
    answer = "在一般工程规划中，一个巡检任务可以拆解为导航、观测和返航等子任务。"

    reply = read_without_mutation(dialogue, question, answer,
                                  query_intent, subject_type, source_policy)

    assert answer in reply
    assert len(dialogue[1].chat_calls) == 1


@pytest.mark.parametrize("query_intent,subject_type", [
    # Preserve the live model's raw values: the public validator normalizes
    # TASK_CATALOG/task_type to KNOWLEDGE_QA/unknown without changing READ.
    pytest.param("TASK_CATALOG", "task_type", id="live-raw-plan"),
    pytest.param("KNOWLEDGE_QA", "task", id="valid-plan"),
])
def test_task_type_development_question_is_not_replaced_with_task_catalog(
    dialogue, query_intent, subject_type,
):
    question = "先不改，从开发维护角度，如果要增加任务类型，需要改哪些配置和接口？"
    answer = (
        "开发扩展任务类型需要修改任务模板配置，补充字段校验，并适配任务保存和执行协议接口；"
        "完成代码及回归验证后才能使用。"
    )

    reply = read_without_mutation(dialogue, question, answer,
                                  query_intent, subject_type, "project_kb")

    for expected in (r"配置", r"校验|验证", r"保存|归档", r"执行", r"协议|接口"):
        assert re.search(expected, reply)


@pytest.mark.parametrize("query_intent,subject_type,source_policy,uses_kb", ROUTES)
def test_live_engineering_reply_does_not_invent_automatic_strategy_or_path_generation(
    dialogue, query_intent, subject_type, source_policy, uses_kb,
):
    question = "一般工程规划中如何将巡检工作分解为子任务？"
    if uses_kb:
        question = "结合本项目配置，解释" + question

    reply = read_without_mutation(dialogue, question, LIVE_ENGINEERING_REPLY,
                                  query_intent, subject_type, source_policy)

    for useful in ("作业流程的时间轴", "空间覆盖逻辑", "载荷工作模式", "准备阶段", "回收阶段", "环境参数采集子任务"):
        assert useful in reply
    for invented in (
        "自动匹配相应的巡检策略",
        "引导系统生成相应的路径策略",
        "这些策略细节通常由系统默认算法根据任务类型自动生成",
    ):
        assert invented not in reply
