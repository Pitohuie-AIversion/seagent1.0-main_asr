from tests.interaction_plan_support import ScriptedLLM, slot_candidate
from src.extractor import ParameterExtractor


def test_option_index_exact_mapping():
    # 上一条 assistant 消息展示了带有编号的合法选项列表
    history = [
        {"role": "user", "content": "我想巡检缆线，请问有哪些类型？"},
        {
            "role": "assistant",
            "content": (
                "请选择您要巡检的管缆类型：\n"
                "1. 海底油气管道\n"
                "2. 电力电缆\n"
                "3. 光纤通信缆"
            ),
        },
    ]

    llm = ScriptedLLM(
        extractions=[
            {
                "slot_candidates": [
                    slot_candidate(
                        "cable_type",
                        "2",
                        raw_key="管缆类型",
                        raw_value="2",
                    )
                ],
                "list_mutations": [],
                "time_relation": None,
                "unresolved": [],
            }
        ]
    )
    extractor = ParameterExtractor(llm)

    required = [
        {
            "key": "cable_type",
            "type": "string",
            "allowed_values": ["海底油气管道", "电力电缆", "光纤通信缆"],
        }
    ]

    result = extractor.extract_updates(
        "我选2",
        current_state={"task_type_key": "pipeline_inspection"},
        task_type_key="pipeline_inspection",
        required=required,
        conversation_history=history,
    )

    candidates = {
        item["canonical_key"]: item
        for item in result["slot_candidates"]
    }
    # 验证 Python 后端根据编号 2 确定性解析到了 "电力电缆"
    assert candidates["cable_type"]["normalized_value"] == "电力电缆"
    assert candidates["cable_type"]["resolution_method"] == "option_index_exact"


def test_relative_date_parsed_by_backend():
    llm = ScriptedLLM(
        extractions=[
            {
                "slot_candidates": [
                    slot_candidate(
                        "start_time",
                        "明天下午3点",
                        raw_key="开始时间",
                        raw_value="明天下午3点",
                    )
                ],
                "list_mutations": [],
                "time_relation": None,
                "unresolved": [],
            }
        ]
    )
    extractor = ParameterExtractor(llm)

    required = [
        {"key": "start_time", "type": "datetime"},
    ]

    result = extractor.extract_updates(
        "任务定在明天下午3点开始",
        current_state={"task_type_key": "pipeline_inspection"},
        task_type_key="pipeline_inspection",
        required=required,
    )

    candidates = {
        item["canonical_key"]: item
        for item in result["slot_candidates"]
    }
    assert "start_time" in candidates
    assert candidates["start_time"]["resolution_method"] == "relative_date_parsed"
